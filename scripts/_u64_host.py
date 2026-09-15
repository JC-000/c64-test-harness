"""One definition of "which Ultimate device is this script allowed to talk to".

Issue #243. Nine scripts under ``scripts/`` each picked a device host their
own way, and most fell back to a hard-coded address when the caller named
none. That address belonged to no device on this bench, so those scripts did
not fail cleanly when run without arguments — they talked to *whatever
answers at that address on whatever network the machine is on*, which may be
someone else's hardware. Two of them (``stress_u64_queue.py``,
``run_u64_parallel_locked.py``) exist to put sustained parallel load on a
device. Device addresses live in CLAUDE.md and the operator's environment,
never in source: pinning one here is how the last default got written.

The rule, applied identically everywhere:

* a host named on the command line is the caller's consent — use it;
* otherwise a ``U64_HOST`` already in the environment is used **verbatim**
  and never overwritten;
* otherwise refuse, loudly, on stderr, with exit status 2. Never invent one.

``U64_HOST`` is a live gate: setting it is what puts a real device in play,
so a script only exports it (``export=True``) when it has to hand the gate to
a child — the pytest runners. See CLAUDE.md § "Standing hardware-safety
clause" before pointing any of this at the C64 Ultimate.

Issue #244 adds the second half: naming a device is consent to drive it,
not permission to drive it *while someone else is*. :func:`hold_device_lock`
is the one way a script takes the cross-process ``DeviceLock`` — after the
host is resolved, before the first request, released in ``finally`` — so
that ``run_prg``/``sid_play``/``reset`` in a script cannot replace a
neighbouring lane's program (docs/device_locking.md, #194). Scripts that go
through ``create_manager``/``UnifiedManager`` are already locked by the
manager and do not need it; the pytest-launching wrappers are locked per
test by ``tests/conftest.py``. ``tests/test_u64_runner_script_gates.py``
pins all three shapes structurally.

Imported by sibling scripts as::

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _u64_host import hold_device_lock, require_u64_host
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Iterator, TextIO

if TYPE_CHECKING:  # pragma: no cover
    from c64_test_harness.backends.device_lock import DeviceLock


__all__ = [
    "resolve_u64_host",
    "require_u64_host",
    "no_host_message",
    "hold_device_lock",
    "NO_HOST_EXIT",
    "NO_LOCK_EXIT",
]

#: Exit status used when no device was named. Distinct from 1 (the scripts'
#: own "ran and failed") so a refusal is not read as a device fault.
NO_HOST_EXIT = 2

#: Exit status when the ``DeviceLock`` could not be taken — the harness would
#: not import, or the acquire budget ran out behind another holder. Distinct
#: from 2 (nothing named) and 1 (ran and failed): the script never touched
#: the device, and a queue is not a device fault.
NO_LOCK_EXIT = 3

_NO_HOST = """\
refusing to run: no Ultimate device named.

U64_HOST is the live gate for this script, and it will not pick a device for
you — a default address reaches whatever answers at it on this network.
Name one explicitly:

    {usage}
    U64_HOST=<device> {argv0}
"""


def _argv0(argv0: str | None) -> str:
    return argv0 if argv0 is not None else f"python3 {sys.argv[0]}"


def no_host_message(argv0: str | None = None, *, usage: str | None = None) -> str:
    """The refusal text, so callers can render it without duplicating it."""
    shown = _argv0(argv0)
    return _NO_HOST.format(argv0=shown, usage=usage or f"{shown} <HOST>")


def resolve_u64_host(
    arg: str | None = None,
    *,
    stderr: TextIO | None = None,
) -> str | None:
    """Return the device host to drive, or ``None`` if none was named.

    An explicit *arg* wins over ``U64_HOST`` — it is the more deliberate
    signal — but a disagreement between the two is reported on stderr
    rather than applied silently, because "I exported U64_HOST and the
    script used something else" is otherwise invisible.

    Empty strings count as not-set: ``U64_HOST=`` is not a device.
    """
    env_host = os.environ.get("U64_HOST") or None
    arg_host = arg or None

    if arg_host and env_host and arg_host != env_host:
        print(
            f"warning: U64_HOST={env_host} in the environment, "
            f"but {arg_host} named on the command line; using {arg_host}",
            file=stderr if stderr is not None else sys.stderr,
        )
    return arg_host or env_host


def require_u64_host(
    arg: str | None = None,
    *,
    argv0: str | None = None,
    usage: str | None = None,
    export: bool = False,
    stderr: TextIO | None = None,
) -> str:
    """Like :func:`resolve_u64_host`, but refuse rather than return ``None``.

    :param export: also set ``U64_HOST`` in this process's environment, so
        child processes inherit the gate. Only the pytest-launching
        wrappers need this; for everything else the gate stays exactly as
        the caller left it.
    :raises SystemExit: status :data:`NO_HOST_EXIT` when no host was named.
        The message goes to stderr first.
    """
    host = resolve_u64_host(arg, stderr=stderr)
    if host is None:
        print(
            no_host_message(argv0, usage=usage),
            file=stderr if stderr is not None else sys.stderr,
        )
        raise SystemExit(NO_HOST_EXIT)
    if export:
        # A no-op when the caller already set it to this value.
        os.environ["U64_HOST"] = host
    return host


@contextmanager
def hold_device_lock(
    host: str,
    *,
    default_timeout: float | None = None,
    lock_dir: Path | None = None,
    stderr: TextIO | None = None,
) -> Iterator["DeviceLock"]:
    """Hold *host*'s ``DeviceLock`` for the body of a ``with`` block.

    Use it around **everything** that drives the device, from constructing
    the client to the last restore — the unit of exclusion is the program on
    the machine, not the HTTP request (docs/device_locking.md, rule 1)::

        host = require_u64_host(args.host, ...)
        with hold_device_lock(host):
            client = Ultimate64Client(host=host)
            ...

    The budget is resolved through the harness's own
    ``resolve_lock_timeout``: ``U64_DEVICE_LOCK_TIMEOUT`` when set, else
    *default_timeout*, else the manager path's
    ``unified_manager.DEFAULT_LOCK_TIMEOUT`` — so a script queues exactly as
    long as ``create_manager`` would, and a malformed variable fails here
    (``DeviceLockTimeoutConfigError``) before any lock or device is touched.

    ``allow_nested=True``: a script that re-enters the library while holding
    the device (e.g. ``create_manager`` inside this block) joins the hold
    instead of waiting on its own flock, which could never end (#273).

    Fails closed, never falls back to an unlocked run: if the harness will
    not import, or the budget runs out behind another holder, the refusal
    goes to stderr and the script exits :data:`NO_LOCK_EXIT` without having
    sent a request. A timeout's diagnostics (holder PID, liveness, lockfile
    age) are printed; do **not** reboot a device because a lock timed out.
    """
    out = stderr if stderr is not None else sys.stderr
    try:
        from c64_test_harness.backends.device_lock import (
            DeviceLock,
            DeviceLockTimeout,
            resolve_lock_timeout,
        )
        from c64_test_harness.backends.unified_manager import DEFAULT_LOCK_TIMEOUT
    except ImportError as exc:
        print(
            f"refusing to run: cannot take the DeviceLock for {host} because "
            f"c64_test_harness will not import ({exc}). Driving the device "
            f"unlocked is the failure the lock exists to prevent.",
            file=out,
        )
        raise SystemExit(NO_LOCK_EXIT) from exc

    timeout = resolve_lock_timeout(
        None,
        default=DEFAULT_LOCK_TIMEOUT if default_timeout is None else default_timeout,
    )
    lock = DeviceLock(host, lock_dir=lock_dir, allow_nested=True)
    try:
        lock.acquire_or_raise(timeout=timeout)
    except DeviceLockTimeout as exc:
        print(f"refusing to run: {exc}", file=out)
        raise SystemExit(NO_LOCK_EXIT) from exc
    try:
        yield lock
    finally:
        lock.release()

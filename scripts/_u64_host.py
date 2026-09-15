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

Imported by sibling scripts as::

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _u64_host import require_u64_host
"""
from __future__ import annotations

import os
import sys
from typing import TextIO


__all__ = ["resolve_u64_host", "require_u64_host", "no_host_message", "NO_HOST_EXIT"]

#: Exit status used when no device was named. Distinct from 1 (the scripts'
#: own "ran and failed") so a refusal is not read as a device fault.
NO_HOST_EXIT = 2

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

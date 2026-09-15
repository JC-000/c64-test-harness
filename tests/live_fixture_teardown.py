"""Teardown for live device fixtures: every step runs, the lock goes last (#334).

A bare post-``yield`` teardown -- ``client.reset()``, ``client.close()``,
``lock.release()`` one after another -- stops at the first step that
raises, so a raising ``close()`` orphans the ``DeviceLock`` and a failed
restore skips everything after it.  #276 repaired that shape in
``test_ultimate64_transport_live.py``; this module is the same repair for
the other live fixtures, shared so the shape is written once:

* :func:`teardown_then_release` attempts every step, each on its own, and
  calls the lock release **last**, in a ``finally``, whatever the steps did;
* :func:`raise_teardown_failures` raises what failed, so a restore that did
  not happen is reported rather than swallowed.  Fixtures call it *after*
  their ``finally``, so it never masks an exception already propagating
  from the ``yield`` (the failures are still logged at WARNING);
* :func:`read_restore_defaults` / :func:`restore_default_steps` restore the
  config items a module owns to the ``default`` the device reports for each,
  one bodyless ``set_config_item`` PUT per item (never the POST
  ``set_config_items_batch``, and not ``set_config_items``, which stops at
  the first failure).

Why ``default`` and not the value read at entry: the bench baseline is
``current == default`` per item (owner decision 2026-09-05), and #364 chose
defaults for the CPU-speed restore for the same reason -- an entry value can
be a SIGKILLed predecessor's residue, and restoring it would re-install the
drift.  The device reports its own ``default``, so the value is right for
its generation without anything hard-coded here.

Nothing here runs on SIGKILL; entry reconciliation is the remedy for that.
"""
from __future__ import annotations

import functools
import logging
from typing import Any, Callable, Iterable, Mapping, Sequence

_log = logging.getLogger(__name__)

Step = tuple[str, Callable[[], Any]]


def attempt_steps(steps: Iterable[Step]) -> list[tuple[str, BaseException]]:
    """Attempt every step; return ``(label, exception)`` for each that raised."""
    failures: list[tuple[str, BaseException]] = []
    for label, step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 -- collected, raised by the caller
            _log.warning("teardown step %s failed: %r", label, exc)
            failures.append((label, exc))
    return failures


def teardown_then_release(
    steps: Iterable[Step], release: Callable[[], Any]
) -> list[tuple[str, BaseException]]:
    """Attempt every step, then *release* -- last, always."""
    try:
        return attempt_steps(steps)
    finally:
        release()


def raise_teardown_failures(
    what: str, failures: Sequence[tuple[str, BaseException]]
) -> None:
    """A teardown step that did not happen says so."""
    if failures:
        detail = "; ".join(f"{label} failed: {exc!r}" for label, exc in failures)
        raise RuntimeError(f"{what}: {detail}") from failures[0][1]


def read_restore_defaults(
    client: Any, items: Mapping[str, Sequence[str]]
) -> list[tuple[str, str, Any]]:
    """``(category, item, default)`` for each owned item, read before any write.

    Refuses to start (raises) when an item reports no usable ``default``:
    the exit restore could not put it back.  An item already off its
    default at entry is logged at WARNING, saying what exit will write.
    """
    plan: list[tuple[str, str, Any]] = []
    for category, names in items.items():
        for item in names:
            entry = client.get_config_item(category, item)
            default = entry.get("default") if isinstance(entry, dict) else None
            if default is None or default == "":
                raise RuntimeError(
                    f"{category} / {item} reports no default ({entry!r}); refusing "
                    "to start, because exit could not restore it"
                )
            current = entry.get("current")
            if current != default:
                _log.warning(
                    "%s / %s drifted at entry: current %r, default %r; it will be "
                    "written to its default at exit",
                    category, item, current, default,
                )
            plan.append((category, item, default))
    return plan


def restore_default_steps(
    client: Any, plan: Iterable[tuple[str, str, Any]]
) -> list[Step]:
    """One bodyless ``set_config_item`` PUT per planned item, as teardown steps."""
    return [
        (
            f"restore {category} / {item} = {default!r}",
            functools.partial(client.set_config_item, category, item, default),
        )
        for category, item, default in plan
    ]

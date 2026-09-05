"""Where the U64_HOST-gated capture tests write their WAV/JSON output.

Issue #220: ``test_chromatic_capture_live.py`` and
``test_multi_sid_parallel_live.py`` used to write into the tracked
``tests/wav_captures/`` tree on every live run, dirtying the working
tree with binary diffs and silently re-basing the committed reference
(``duration_seconds`` 14.972 -> 14.9, ``packets_received`` 3743 -> 3725,
and the ``.wav`` bytes, with no test having failed).

The rule now:

* By default a capture goes under the scratch directory the caller
  hands in (pytest's ``tmp_path`` / ``tmp_path_factory``), mirroring the
  tracked layout as ``<scratch>/wav_captures/<suite>/``.
* Only with ``WAV_CAPTURES_REFRESH=1`` (``true``/``yes``/``on`` also
  count) does the capture land in :data:`TRACKED_ROOT` -- a deliberate
  refresh of the committed reference, to be reviewed and committed on
  purpose.

The tracked fixtures under :data:`TRACKED_ROOT` remain the reference
for anything that compares a fresh capture against the committed one.
This module is plain (no pytest import) so it is importable from any
test file the way ``bridge_platform`` is.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

#: Environment knob that redirects captures into the tracked tree.
REFRESH_ENV = "WAV_CAPTURES_REFRESH"

#: The committed reference captures: ``tests/wav_captures/``.
TRACKED_ROOT = Path(__file__).resolve().parent / "wav_captures"

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def refresh_requested(environ: Mapping[str, str] | None = None) -> bool:
    """Whether the caller asked to refresh the committed reference.

    :param environ: Mapping to consult instead of :data:`os.environ`
        (unit tests pass their own).
    """
    env = os.environ if environ is None else environ
    return env.get(REFRESH_ENV, "").strip().lower() in _TRUTHY


def capture_dir(
    suite: str,
    scratch: str | os.PathLike[str],
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Directory a capture *suite* should write into.

    :param suite: One path component naming the suite (``"chromatic"``,
        ``"multi_sid"``) -- the same name as its folder under
        :data:`TRACKED_ROOT`.
    :param scratch: Per-run scratch root (``tmp_path`` or
        ``tmp_path_factory.mktemp(...)``); used unless a refresh was
        requested.
    :param environ: See :func:`refresh_requested`.
    :returns: ``TRACKED_ROOT / suite`` when :data:`REFRESH_ENV` is set,
        else ``<scratch>/wav_captures/<suite>``.  The directory is not
        created; callers ``mkdir(parents=True, exist_ok=True)`` as before.
    :raises ValueError: if *suite* is empty or not a single plain
        path component.
    """
    if not suite or Path(suite).name != suite or suite in (".", ".."):
        raise ValueError(f"suite must be a single path component, got {suite!r}")
    if refresh_requested(environ):
        return TRACKED_ROOT / suite
    return Path(scratch) / "wav_captures" / suite

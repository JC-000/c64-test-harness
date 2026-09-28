"""An FTP stand-in for offline tests that reach the ``/Temp`` sweep (#511).

Since #511 a leak-prone device's first attachment-creating request of a
process (or after each ``DeviceLock`` acquire) sweeps ``/Temp`` first, so an
offline test that sends one to a fake host reaches ``gc_temp_folder``. Without
this it would dial the fake host's port 21 for real, and fail slowly.

``EmptyTempFTP`` answers like a device with FTP File Service on and nothing
in ``/Temp``. It records each session's host in ``EmptyTempFTP.sessions``.
Modules that test the sweep itself use their own recording fakes.
"""
from __future__ import annotations


class EmptyTempFTP:
    sessions: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def __enter__(self) -> "EmptyTempFTP":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def connect(self, host, port=21, timeout=None):
        EmptyTempFTP.sessions.append(host)

    def login(self, user, password):
        return None

    def cwd(self, path):
        return None

    def nlst(self):
        return []

    def delete(self, name):  # pragma: no cover - nothing is ever listed
        raise AssertionError(f"EmptyTempFTP lists nothing, yet {name!r} was deleted")


def install(monkeypatch) -> type[EmptyTempFTP]:
    """Route every sweep in this test to an empty ``/Temp`` and forget ledgers."""
    from c64_test_harness.backends import ultimate64_temp_gc as gc_mod

    EmptyTempFTP.sessions = []
    monkeypatch.setattr(gc_mod, "FTP", EmptyTempFTP)
    gc_mod._reset_temp_ledgers()
    return EmptyTempFTP

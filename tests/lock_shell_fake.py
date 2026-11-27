"""A lock shell for tests: the environment, the lockfile's holder and this process's parents, all faked.

``lock_shell_holds`` (backends/device_lock.py) reads ``C64U_LOCK_PID`` / ``C64U_LOCK_HOST``, asks
``device_lock_holder`` who holds the lockfile, and walks this process's parents with ``ps``. Nothing here takes a
lock, runs ``ps`` or reaches a device.
"""
import os

import c64_test_harness.backends.device_lock as dl

SHELL_PID = 4242


def fake_parents(monkeypatch, *ancestors):
    """Make this process's parents, nearest first, *ancestors*; then pid 1."""
    me = os.getpid()
    links = dict(zip((me,) + ancestors, ancestors + (1,)))
    monkeypatch.setattr(dl, "_parent_pid", lambda pid: links.get(pid))


def unreadable_parents(monkeypatch):
    """Make every parent lookup fail, as when ``ps`` cannot be run: the walk learns nothing on its first hop."""
    monkeypatch.setattr(dl, "_parent_pid", lambda pid: None)


def fake_lock_shell(monkeypatch, host, *, pid=SHELL_PID, holder=SHELL_PID, ancestors=(SHELL_PID,)):
    """Run the test as the child of a lock shell *pid* bound to *host* (None: no ``C64U_LOCK_HOST``).

    *holder* is the PID the lockfile names for **every** device (None: nobody holds it), so only the
    ``C64U_LOCK_HOST`` comparison can tell a shell for another device apart. For the same reason no test built
    on this can tell which device's lockfile was asked about (PR #587 review, finding 4): do not rely on these
    tests for the per-device holder lookup. *ancestors* are this process's parents (None: no parent can be
    read). The defaults are a genuine shell; change one to forge the claim.
    """
    monkeypatch.setenv("C64U_LOCK_PID", str(pid))
    if host is None:
        monkeypatch.delenv("C64U_LOCK_HOST", raising=False)
    else:
        monkeypatch.setenv("C64U_LOCK_HOST", host)
    monkeypatch.setattr(dl, "device_lock_holder",
                        lambda h, lock_dir=None: None if holder is None else {"pid": holder})
    if ancestors is None:
        unreadable_parents(monkeypatch)
    else:
        fake_parents(monkeypatch, *ancestors)


#: Claims that must be refused: (keyword overrides for fake_lock_shell, what is wrong).
FORGED = [
    ({"holder": 777}, "another process holds the lock"),
    ({"holder": None}, "nobody holds the lock"),
    ({"ancestors": (100, 200)}, "the holder is not an ancestor"),
    ({"ancestors": ()}, "the chain ends at init"),
    ({"ancestors": None}, "the parent cannot be read"),
    ({"pid": "self"}, "this process names itself without holding the lock"),
]
FORGED_KW = [kw for kw, _ in FORGED]
FORGED_IDS = [why for _, why in FORGED]


def forge(monkeypatch, host, kw):
    """fake_lock_shell with one of FORGED's overrides."""
    kw = dict(kw)
    if kw.get("pid") == "self":
        kw.update(pid=os.getpid(), holder=os.getpid())
    fake_lock_shell(monkeypatch, host, **kw)

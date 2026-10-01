"""Parent-death watchdog for a launched x64sc (#517).

An x64sc outlives a launcher that dies from a signal (SIGKILL, or an
unhandled SIGTERM/SIGHUP): the ``atexit`` hook in ``vice_lifecycle`` never
runs, and macOS has no ``PR_SET_PDEATHSIG``.  This module is the sidecar
that closes that gap.  ``ViceProcess.start()`` runs it as a separate
process *next to* x64sc -- not between the harness and x64sc, so the
emulator stays the direct ``Popen`` child and ``ViceProcess.pid`` /
``resolve_vice_pid`` are unchanged.

Protocol, on the watchdog's stdin (the read end of a pipe whose write end
only the launcher holds)::

    T <pid> <start>\\n
                arm: watch this process, whose start-time token (see
                ``start_time``) the launcher read while the PID was its
                own unreaped child and so could not have been reused
    D\\n         disarm: exit without touching anything (stop/detach)
    EOF         the launcher is gone -- by exit, crash or any signal,
                because the kernel closes its descriptors -- so terminate
                the armed target: SIGTERM, then SIGKILL after a grace
                period, the shape of ``scripts/cleanup_vice_ports.py``.

The watchdog never matches a process by name; other projects run x64sc
on the same host.  It signals only the PID it was armed with, and only
while that PID still has the start time the launcher sent: the arm is
refused if they already differ, and every signal is preceded by the same
check, so a PID that was reaped and reused is left alone.  The launcher
reads the start time itself, straight after ``Popen`` returns, because
the watchdog cannot vouch for the PID on its own: it may read the arm
only after the launcher has died (a ``python -I`` start takes tens of
milliseconds), when the target is already re-parented.

Start time resolution: clock ticks on Linux (``/proc``), microseconds on
macOS (``sysctl kern.proc.pid``), whole seconds where neither is
available (``ps -o lstart``).  The command
name is deliberately not part of the identity: a launcher that is a
script ``exec``-ing x64sc changes it after the PID is armed.

Stdlib only, and no imports from the package: it is run by path with
``python -I`` so that it starts fast and depends on nothing the consumer's
environment might shadow.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from typing import BinaryIO, Callable

#: Seconds between SIGTERM and SIGKILL, as in ``ViceProcess.stop()``.
GRACE_SECONDS = 5.0
POLL_SECONDS = 0.1

def _ps(pid: int, fields: str) -> str:
    try:
        return subprocess.run(
            ["ps", "-o", fields, "-p", str(pid)],
            capture_output=True, text=True, check=False,
            env=dict(os.environ, LC_ALL="C"),
        ).stdout.strip()
    except OSError:
        return ""


def _darwin_start_time(pid: int) -> str | None:
    """``p_starttime`` of *pid* in microseconds, via ``sysctl``; "" for a
    zombie or a missing process; None if sysctl itself is unavailable.

    ``kinfo_proc.kp_proc`` is ``struct extern_proc``: a union whose
    ``__p_starttime`` member (``struct timeval``) sits at offset 0, then
    two pointers and ``p_flag``, so ``p_stat`` is the char at offset 36
    (``SZOMB`` = 5; checked against ``ps -o stat`` on macOS 27).
    """
    import ctypes
    import ctypes.util

    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        mib = (ctypes.c_int * 4)(1, 14, 1, pid)  # CTL_KERN, KERN_PROC, KERN_PROC_PID
        buf = ctypes.create_string_buffer(1024)
        size = ctypes.c_size_t(len(buf))
        if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) != 0:
            return None
    except (OSError, AttributeError):
        return None
    if size.value < 40 or buf.raw[36] == 5:
        return ""  # no such process, or a zombie
    sec = int.from_bytes(buf.raw[0:8], sys.byteorder, signed=True)
    usec = int.from_bytes(buf.raw[8:12], sys.byteorder, signed=True)
    return f"usec:{sec}.{usec:06d}"


def start_time(pid: int) -> str | None:
    """A token for *pid*'s start time, or None if it is gone or a zombie.

    Two processes that ever held the same PID have different tokens
    (to within the resolution noted in the module docstring).
    """
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            stat = f.read().decode("ascii", "replace")
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return None  # Linux, and the process is gone
        stat = ""
    except OSError:
        stat = ""
    if stat:
        # comm is parenthesised and may contain spaces: split after it.
        rest = stat[stat.rfind(")") + 2:].split()
        if len(rest) < 20 or rest[0] == "Z":
            return None
        return f"ticks:{rest[19]}"
    if sys.platform == "darwin":
        token = _darwin_start_time(pid)
        if token is not None:
            return token or None
    out = _ps(pid, "stat=,lstart=").split()
    # stat, then lstart's five fields: Tue Sep 29 09:44:55 2026
    if len(out) < 6 or out[0].startswith("Z"):
        return None
    return "lstart:" + "_".join(out[1:6])  # one token on the wire


class Watchdog:
    """The decision logic, with every effect injectable for tests."""

    def __init__(
        self,
        *,
        start_of: Callable[[int], str | None] = start_time,
        kill: Callable[[int, int], None] = os.kill,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        grace: float = GRACE_SECONDS,
    ) -> None:
        self._start_of = start_of
        self._kill = kill
        self._sleep = sleep
        self._clock = clock
        self._grace = grace

    def run(self, stream: BinaryIO) -> str:
        """Serve the protocol until disarm or EOF; return the outcome."""
        target: int | None = None
        start: str | None = None
        while True:
            line = stream.readline()
            if not line:
                break  # EOF: the launcher is gone
            cmd = line.split()
            if not cmd:
                continue
            if cmd[0] == b"D":
                return "disarmed"
            if cmd[0] == b"T" and len(cmd) == 3 and target is None:
                pid, claimed = int(cmd[1]), cmd[2].decode("ascii", "replace")
                if self._start_of(pid) != claimed:
                    # Already gone, or the PID is not the process the
                    # launcher named: never ours to signal.
                    return "refused"
                target, start = pid, claimed
        if target is None:
            return "unarmed"
        return self.terminate(target, start)

    def _same(self, pid: int, start: str | None) -> bool:
        now = self._start_of(pid)
        return now is not None and now == start

    def _signal(self, pid: int, sig: int) -> str | None:
        try:
            self._kill(pid, sig)
        except ProcessLookupError:
            return "gone"
        except PermissionError:
            return "denied"
        return None

    def terminate(self, pid: int, start: str | None) -> str:
        if not self._same(pid, start):
            return "gone"
        failed = self._signal(pid, signal.SIGTERM)
        if failed:
            return failed
        deadline = self._clock() + self._grace
        while self._clock() < deadline:
            if not self._same(pid, start):
                return "terminated"
            self._sleep(POLL_SECONDS)
        if not self._same(pid, start):
            return "terminated"
        return self._signal(pid, signal.SIGKILL) or "killed"


def _close_inherited_fds() -> None:
    for fd_dir in ("/proc/self/fd", "/dev/fd"):
        try:
            fds = [int(name) for name in os.listdir(fd_dir)]
        except (OSError, ValueError):
            continue
        for fd in fds:
            if fd > 2:
                try:
                    os.close(fd)
                except OSError:
                    pass  # the listing's own directory fd, already closed
        return
    os.closerange(3, 4096)


def main(argv: list[str]) -> int:
    # Keep only stdin (the pipe), stdout and stderr: anything else this
    # process inherited belongs to someone else, and holding a pipe's
    # write end open would delay its reader's EOF.
    _close_inherited_fds()
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    # argv[1] is the launcher's PID: informational only, for ``ps``.
    Watchdog().run(sys.stdin.buffer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

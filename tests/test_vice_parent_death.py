"""An x64sc does not outlive a launcher killed by a signal (#517).

The functional tests run a child Python *launcher* that starts an
emulator through ``ViceProcess``, then kill that launcher with a signal
whose default action skips ``atexit`` (PR #515's hook), and look at the
process table.  Most use the ``x64sc`` stand-in from
``test_vice_orphan_teardown`` (``/bin/sleep`` renamed, re-signed on
macOS); one launches the real emulator.  Cleanup only ever kills a PID
that this test launched and verified -- never anything merely named
``x64sc``: other projects run VICE on this host.

The ``Watchdog`` unit tests drive the decision logic with fakes: what it
signals, and above all what it refuses to signal.
"""

from __future__ import annotations

import io
import logging
import os
import select
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from c64_test_harness.backends import vice_lifecycle, vice_watchdog
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_watchdog import Watchdog, start_time

from test_vice_orphan_teardown import (  # noqa: F401 - fixtures
    _alive,
    _is_stub,
    _kill_if_ours,
    _wait_dead,
    stub_x64sc,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process semantics"
)

SRC = str(Path(vice_lifecycle.__file__).resolve().parents[2])

#: The watchdog needs: EOF delivery + a ps/sysctl check + SIGTERM.  The
#: stand-in dies on SIGTERM, so this is generous.
DEATH_BOUND = 10.0


# ---------------------------------------------------------------------------
# A launcher process we can kill
# ---------------------------------------------------------------------------


class Launcher:
    """A child interpreter that launches one emulator and then waits.

    It prints ``<x64sc pid> [<extra>...]`` and blocks on stdin, so the
    test decides how it dies.
    """

    def __init__(self, body: str, env: dict[str, str]) -> None:
        script = textwrap.dedent(
            """
            import os, sys
            from c64_test_harness.backends.vice_lifecycle import (
                ViceConfig, ViceProcess)
            """
        ) + textwrap.dedent(body) + "\nsys.stdin.read()\n"
        self.proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, PYTHONPATH=SRC,
                     PYTHONDONTWRITEBYTECODE="1", **env),
        )
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read()
            self.proc.kill()
            self.proc.wait()
            raise AssertionError(f"launcher failed to start: {err}")
        self.tokens = line.split()

    def kill(self, sig: int) -> None:
        os.kill(self.proc.pid, sig)
        self.proc.wait(timeout=10)
        assert self.proc.returncode == -sig, (
            f"launcher exited {self.proc.returncode}, not by signal {sig}: "
            "the test would not be exercising a signal death"
        )

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            stream.close()


STUB_LAUNCH = """
p = ViceProcess(ViceConfig(executable=os.environ["STUB"], sound=False))
p.start()
"""


@pytest.mark.parametrize("sig", [signal.SIGKILL, signal.SIGTERM, signal.SIGHUP])
def test_launcher_killed_by_signal_takes_its_x64sc_with_it(stub_x64sc, sig):
    launcher, sleeper = stub_x64sc
    run = Launcher(STUB_LAUNCH + "print(p.pid, flush=True)\n",
                   {"STUB": str(launcher)})
    pid = int(run.tokens[0])
    try:
        deadline = time.monotonic() + 5
        while not _is_stub(pid, sleeper) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert _is_stub(pid, sleeper), "the stand-in never started"
        run.kill(sig)
        assert _wait_dead(pid, DEATH_BOUND), (
            f"x64sc stand-in {pid} outlived a launcher killed by "
            f"{signal.Signals(sig).name}"
        )
    finally:
        run.close()
        _kill_if_ours(pid, sleeper)


def test_launcher_killed_straight_after_start_takes_its_x64sc_with_it(
    stub_x64sc,
):
    """No settle: the launcher dies while the watchdog may still be
    starting up, before it has read the arm."""
    launcher, sleeper = stub_x64sc
    run = Launcher(STUB_LAUNCH + "print(p.pid, flush=True)\n"
                   "os.kill(os.getpid(), 9)\n",
                   {"STUB": str(launcher)})
    pid = int(run.tokens[0])
    try:
        run.proc.wait(timeout=10)
        assert run.proc.returncode == -signal.SIGKILL
        assert _wait_dead(pid, DEATH_BOUND), (
            f"x64sc stand-in {pid} outlived a launcher SIGKILLed straight "
            "after start()"
        )
    finally:
        run.close()
        _kill_if_ours(pid, sleeper)


def test_detached_x64sc_survives_a_sigkilled_launcher(stub_x64sc):
    launcher, sleeper = stub_x64sc
    run = Launcher(STUB_LAUNCH + "p.detach()\nprint(p.pid, flush=True)\n",
                   {"STUB": str(launcher)})
    pid = int(run.tokens[0])
    try:
        run.kill(signal.SIGKILL)
        # Long enough for an armed watchdog to have acted (it reacts in
        # well under a second; see the test above).
        time.sleep(2.0)
        assert _alive(pid) and _is_stub(pid, sleeper), (
            "a detached x64sc was killed when its launcher died"
        )
    finally:
        run.close()
        _kill_if_ours(pid, sleeper)


def test_forked_child_does_not_keep_the_parents_x64sc_alive(stub_x64sc):
    """A fork copies the pipe's write end; EOF needs every copy closed."""
    launcher, sleeper = stub_x64sc
    run = Launcher(
        STUB_LAUNCH + """
import time
child = os.fork()
if child == 0:
    time.sleep(60)          # outlives the launcher, holding nothing
    os._exit(0)
print(p.pid, child, flush=True)
""",
        {"STUB": str(launcher)},
    )
    pid, child = int(run.tokens[0]), int(run.tokens[1])
    try:
        run.kill(signal.SIGKILL)
        assert _alive(child), "test setup: the forked child is already gone"
        assert _wait_dead(pid, DEATH_BOUND), (
            "a forked child's inherited pipe end kept x64sc alive after "
            "its launcher was SIGKILLed"
        )
    finally:
        run.close()
        _kill_if_ours(pid, sleeper)
        # The forked child is the launcher's (we started the launcher);
        # confirm it is still a python running our sleep before killing.
        args = subprocess.run(["ps", "-o", "args=", "-p", str(child)],
                              capture_output=True, text=True).stdout
        if "c64_test_harness" in args and "sys.stdin.read()" in args:
            os.kill(child, signal.SIGKILL)


def test_watchdog_is_retired_by_stop_wait_and_detach(stub_x64sc):
    """No watchdog outlives the process it guarded, in this interpreter."""
    launcher, sleeper = stub_x64sc

    def started(**env) -> tuple[ViceProcess, int]:
        cfg = ViceConfig(executable=str(launcher), sound=False,
                         env=dict(os.environ, **env) if env else None)
        p = ViceProcess(cfg)
        p.start()
        assert p._watchdog is not None, "no watchdog was started"
        return p, p._watchdog.pid

    def reaped(wd_pid: int) -> bool:
        try:
            os.waitpid(wd_pid, os.WNOHANG)
        except ChildProcessError:
            return True  # already reaped by retire()
        return False

    p, wd = started()
    p.stop()
    assert reaped(wd) and p._watchdog is None

    p, wd = started(STUB_SECS="1")
    assert p.wait_for_exit(timeout=10) == 0
    assert reaped(wd) and p._watchdog is None

    p, wd = started()
    vpid = p.pid
    try:
        p.detach()
        assert reaped(wd) and p._watchdog is None
    finally:
        p.stop()
    assert not _alive(vpid)
    assert not vice_lifecycle._WATCHDOGS


def test_failed_launch_retires_the_watchdog(tmp_path):
    p = ViceProcess(ViceConfig(executable=str(tmp_path / "no-such-x64sc"),
                               sound=False))
    with pytest.raises(OSError):
        p.start()
    assert not vice_lifecycle._WATCHDOGS


def test_consumer_pipes_are_not_held_open_by_the_watchdog(stub_x64sc):
    """posix_spawn hands the watchdog every *inheritable* fd above stderr.
    A consumer's inheritable pipe must still reach EOF when the consumer
    closes its write end, not only when the emulator is stopped."""
    launcher, sleeper = stub_x64sc
    rp, wp = os.pipe()
    os.set_inheritable(wp, True)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    try:
        p.start()
        assert p._watchdog is not None, "no watchdog was started"
        # Let the watchdog get past its start-up fd sweep.
        time.sleep(0.5)
        os.close(wp)
        wp = None
        readable, _, _ = select.select([rp], [], [], 2.0)
        assert readable and os.read(rp, 1) == b"", (
            "the consumer's pipe saw no EOF: the watchdog holds its write end"
        )
    finally:
        if wp is not None:
            os.close(wp)
        os.close(rp)
        p.stop()


def test_launcher_with_stdin_closed_still_takes_its_x64sc_with_it(stub_x64sc):
    """With fd 0 closed, os.pipe() returns the read end as fd 0; dup2'ing
    it onto the watchdog's stdin would then leave it close-on-exec."""
    launcher, sleeper = stub_x64sc
    run = Launcher("os.close(0)\n" + STUB_LAUNCH
                   + "print(p.pid, flush=True)\nos.kill(os.getpid(), 9)\n",
                   {"STUB": str(launcher)})
    pid = int(run.tokens[0])
    try:
        run.proc.wait(timeout=10)
        assert run.proc.returncode == -signal.SIGKILL
        assert _wait_dead(pid, DEATH_BOUND), (
            f"x64sc stand-in {pid} outlived a SIGKILLed launcher that had "
            "closed its stdin"
        )
    finally:
        run.close()
        _kill_if_ours(pid, sleeper)


def test_an_interpreter_that_cannot_run_the_watchdog_is_reported(
    stub_x64sc, monkeypatch, caplog,
):
    """sys.executable starts but does not run the script (an embedded host,
    a frozen app): the launch is unguarded, and it must say so."""
    launcher, sleeper = stub_x64sc
    monkeypatch.setattr(sys, "executable", "/usr/bin/true")
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    vpid = None
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            vpid = p.pid
            deadline = time.monotonic() + DEATH_BOUND
            while time.monotonic() < deadline and not any(
                "not guarded" in r.getMessage() for r in caplog.records
            ):
                time.sleep(0.05)
        finally:
            p.stop()
    assert any(
        "not guarded" in r.getMessage() and str(vpid) in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_a_watchdog_that_never_confirms_the_arm_is_reported(
    stub_x64sc, tmp_path, monkeypatch, caplog,
):
    """The interpreter starts and keeps reading -- the arm is delivered --
    but never runs the watchdog, so no confirmation comes back."""
    launcher, sleeper = stub_x64sc
    mute = tmp_path / "mute-python"
    mute.write_text("#!/bin/sh\nexec cat > /dev/null\n")
    mute.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(mute))
    monkeypatch.setattr(vice_lifecycle, "_WATCHDOG_ACK_SECONDS", 1.0)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    vpid = None
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            vpid = p.pid
            assert p._watchdog is not None
            deadline = time.monotonic() + DEATH_BOUND
            while time.monotonic() < deadline and not any(
                "never confirmed" in r.getMessage() for r in caplog.records
            ):
                time.sleep(0.05)
        finally:
            p.stop()
    assert any(
        "never confirmed" in r.getMessage() and str(vpid) in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_an_arm_that_cannot_be_sent_is_reported(
    stub_x64sc, monkeypatch, caplog,
):
    """The watchdog's pipe refuses the write (it died first)."""
    launcher, sleeper = stub_x64sc
    monkeypatch.setattr(vice_lifecycle._ParentDeathWatchdog, "_send",
                        lambda self, message: False)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            vpid = p.pid
        finally:
            p.stop()
    assert any(
        "could not be sent" in r.getMessage() and str(vpid) in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_a_frozen_application_skips_the_watchdog_with_a_warning(
    stub_x64sc, monkeypatch, caplog,
):
    launcher, sleeper = stub_x64sc
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(vice_lifecycle, "_warned_watchdog_unavailable", False)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            assert p._watchdog is None, "a frozen app ran sys.executable"
        finally:
            p.stop()
    assert any("watchdog unavailable" in r.getMessage()
               for r in caplog.records)


def test_a_fork_during_pipe_creation_does_not_keep_the_write_end(stub_x64sc):
    """Another thread forks between os.pipe() and the watchdog's
    registration: the child must still not hold the write end."""
    launcher, _ = stub_x64sc
    res = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(
            """
            import os, sys, threading, time
            from c64_test_harness.backends import vice_lifecycle as vl
            made, ends = threading.Event(), []
            real_pipe = os.pipe
            def slow_pipe():
                r, w = real_pipe()
                ends.append(w)
                made.set()
                time.sleep(0.3)      # preempted right after the pipe
                return r, w
            vl.os.pipe = slow_pipe
            results = []
            def forker():
                made.wait()
                vl.os.pipe = real_pipe
                child = os.fork()
                if child == 0:
                    try:
                        os.fstat(ends[0])
                        os._exit(1)      # write end still open
                    except OSError:
                        os._exit(0)
                results.append(os.waitpid(child, 0)[1])
            t = threading.Thread(target=forker)
            t.start()
            wd = vl._ParentDeathWatchdog.spawn()
            t.join()
            wd.retire()
            print("held" if results[0] else "closed", flush=True)
            """)],
        capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1",
                 STUB=str(launcher)),
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.split()[-1] == "closed", (
        "a child forked mid-spawn kept the watchdog pipe's write end"
    )


def _run_script(body: str, timeout: float = 30.0) -> str:
    """Run *body* in a child interpreter; a hang is reported, not waited on."""
    try:
        res = subprocess.run(
            [sys.executable, "-c", textwrap.dedent(body)],
            capture_output=True, text=True, timeout=timeout,
            env=dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1"),
        )
    except subprocess.TimeoutExpired:
        return "hung"
    assert res.returncode == 0, res.stderr
    return res.stdout.split()[-1]


def test_a_signal_handler_that_forks_inside_the_fork_guard_does_not_hang():
    """A handler runs on the thread that holds the guard; its fork must not
    wait for that thread (a non-reentrant lock deadlocks here)."""
    out = _run_script(
        """
        import os, signal
        from c64_test_harness.backends import vice_lifecycle as vl
        children = []
        def handler(signum, frame):
            pid = os.fork()
            if pid == 0:
                os._exit(0)
            children.append(pid)
        signal.signal(signal.SIGUSR1, handler)
        real_pipe = os.pipe
        def pipe():
            ends = real_pipe()
            vl.os.pipe = real_pipe          # only the first pipe
            signal.raise_signal(signal.SIGUSR1)   # inside the guard
            return ends
        vl.os.pipe = pipe
        wd = vl._ParentDeathWatchdog.spawn()
        wd.retire()
        os.waitpid(children[0], 0)
        print("returned", flush=True)
        """,
        timeout=20,
    )
    assert out == "returned", "a fork from a signal handler deadlocked"


def test_the_fork_guard_is_still_reentrant_in_a_forked_child():
    """The child re-creates the guard after fork; a signal-handler fork
    inside the guard must not deadlock there either."""
    out = _run_script(
        """
        import os, signal, time
        from c64_test_harness.backends import vice_lifecycle as vl
        child = os.fork()
        if child == 0:
            grandchildren = []
            def handler(signum, frame):
                pid = os.fork()
                if pid == 0:
                    os._exit(0)
                grandchildren.append(pid)
            signal.signal(signal.SIGUSR1, handler)
            real_pipe = os.pipe
            def pipe():
                ends = real_pipe()
                vl.os.pipe = real_pipe
                signal.raise_signal(signal.SIGUSR1)
                return ends
            vl.os.pipe = pipe
            wd = vl._ParentDeathWatchdog.spawn()
            wd.retire()
            os.waitpid(grandchildren[0], 0)
            os._exit(0)
        deadline = time.monotonic() + 15
        status = "hung"
        while time.monotonic() < deadline:
            pid, code = os.waitpid(child, os.WNOHANG)
            if pid:
                status = "returned" if code == 0 else f"exit-{code}"
                break
            time.sleep(0.05)
        if status == "hung":
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        print(status, flush=True)
        """,
        timeout=30,
    )
    assert out == "returned", f"forked child: {out}"


def test_a_signal_handler_that_stops_vice_inside_the_registry_lock_does_not_hang(
    stub_x64sc,
):
    """A SIGTERM-style handler calling stop() while the interrupted code
    holds the at-exit registry lock (#515's lock)."""
    launcher, sleeper = stub_x64sc
    out = _run_script(
        f"""
        import os, signal
        from c64_test_harness.backends import vice_lifecycle as vl
        p = vl.ViceProcess(vl.ViceConfig(executable={str(launcher)!r},
                                          sound=False))
        p.start()
        pid = p.pid
        signal.signal(signal.SIGUSR1, lambda s, f: p.stop())
        with vl._LIVE_LOCK:
            signal.raise_signal(signal.SIGUSR1)
        print(pid, flush=True)
        print("returned", flush=True)
        """,
        timeout=30,
    )
    assert out == "returned", "stop() from a signal handler deadlocked"


def _ack_thread(pid: int):
    for t in threading.enumerate():
        if t.name == f"x64sc-watchdog-ack-{pid}":
            return t
    return None


def _unguarded(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if "not guarded" in r.getMessage()]


def test_a_healthy_launch_is_confirmed_without_a_warning(
    stub_x64sc, monkeypatch, caplog,
):
    """Negative control for the arm confirmation: the real watchdog acks."""
    launcher, sleeper = stub_x64sc
    monkeypatch.setattr(vice_lifecycle, "_WATCHDOG_ACK_SECONDS", 2.0)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            t = _ack_thread(p.pid)
            assert t is not None, "no confirmation thread was started"
            t.join(timeout=DEATH_BOUND)
            assert not t.is_alive()
        finally:
            p.stop()
    assert _unguarded(caplog) == []


def _silent_interpreter(tmp_path: Path) -> Path:
    """Starts, holds its pipes open, never runs the watchdog: no ack."""
    silent = tmp_path / "silent-python"
    silent.write_text("#!/bin/sh\nexec /bin/sleep 30\n")
    silent.chmod(0o755)
    return silent


def test_no_warning_when_x64sc_exited_before_the_confirmation_window(
    stub_x64sc, tmp_path, monkeypatch, caplog,
):
    launcher, sleeper = stub_x64sc
    monkeypatch.setattr(sys, "executable", str(_silent_interpreter(tmp_path)))
    monkeypatch.setattr(vice_lifecycle, "_WATCHDOG_ACK_SECONDS", 1.5)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False,
                               env=dict(os.environ, STUB_SECS="0.2")))
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            t = _ack_thread(p.pid)
            assert t is not None
            t.join(timeout=DEATH_BOUND)   # x64sc exits during the window
            assert p._proc.poll() is not None, "test setup: x64sc still runs"
        finally:
            p.stop()
    assert _unguarded(caplog) == []


def test_detach_before_the_watchdog_answers_does_not_warn(
    stub_x64sc, tmp_path, monkeypatch, caplog,
):
    """detach() retires a watchdog that never answered; the confirmation
    thread then sees EOF, which is not a reason to warn."""
    launcher, sleeper = stub_x64sc
    monkeypatch.setattr(sys, "executable", str(_silent_interpreter(tmp_path)))
    monkeypatch.setattr(vice_lifecycle, "_WATCHDOG_ACK_SECONDS", 5.0)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            t = _ack_thread(p.pid)
            assert t is not None
            p.detach()                    # reaps the silent watchdog: EOF
            t.join(timeout=DEATH_BOUND)
            assert not t.is_alive()
        finally:
            p.stop()
    assert _unguarded(caplog) == []


def test_a_confirmation_thread_that_cannot_start_does_not_fail_the_launch(
    stub_x64sc, monkeypatch, caplog,
):
    launcher, sleeper = stub_x64sc

    class NoThreads(threading.Thread):
        def start(self):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(vice_lifecycle.threading, "Thread", NoThreads)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    with caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__):
        try:
            p.start()
            assert p in vice_lifecycle._LIVE_PROCESSES
            assert p._watchdog is not None
        finally:
            p.stop()
    assert any("confirmation" in r.getMessage() for r in caplog.records)


def test_a_python_without_ctypes_still_gets_a_watchdog(
    stub_x64sc, monkeypatch,
):
    """start_time() falls back to ps; nothing about ctypes may fail start()."""
    from c64_test_harness.backends import vice_watchdog as vw

    launcher, sleeper = stub_x64sc
    monkeypatch.setattr(vw, "_LIBC", None)
    monkeypatch.setitem(sys.modules, "ctypes", None)   # import -> ImportError
    monkeypatch.setitem(sys.modules, "ctypes.util", None)
    p = ViceProcess(ViceConfig(executable=str(launcher), sound=False))
    try:
        p.start()
        assert p._watchdog is not None
        assert start_time(p.pid) is not None
    finally:
        p.stop()


# ---------------------------------------------------------------------------
# The real emulator
# ---------------------------------------------------------------------------


@pytest.mark.vice_live
def test_real_x64sc_dies_with_a_sigkilled_launcher():
    from conftest import require_vice_or_skip

    require_vice_or_skip()
    run = Launcher(
        """
from c64_test_harness.backends.vice_manager import PortAllocator
allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
port = allocator.allocate()
reservation = allocator.take_socket(port)
if reservation is not None:
    reservation.close()
p = ViceProcess(ViceConfig(port=port, warp=True, sound=False,
                           minimize=True))
p.start()
print(p.pid, p.config.executable or "", flush=True)
""",
        {},
    )
    pid = int(run.tokens[0])
    # Identity of the emulator we launched, taken while the launcher
    # (its parent) is alive, so the PID cannot have been reused.
    ident = start_time(pid)
    try:
        ppid = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
        assert ident is not None and ppid == str(run.proc.pid), (
            "test setup: x64sc is not the launcher's child"
        )
        comm = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)],
                              capture_output=True, text=True).stdout.strip()
        assert os.path.basename(comm) == "x64sc", comm
        run.kill(signal.SIGKILL)
        deadline = time.monotonic() + DEATH_BOUND
        while time.monotonic() < deadline and start_time(pid) == ident:
            time.sleep(0.05)
        assert start_time(pid) != ident, (
            f"real x64sc {pid} outlived its SIGKILLed launcher"
        )
    finally:
        run.close()
        if ident is not None and start_time(pid) == ident:
            os.kill(pid, signal.SIGKILL)


@pytest.mark.vice_live
@pytest.mark.elevation("vice_root")
def test_root_x64sc_under_sudo_dies_with_a_sigkilled_launcher():
    """The sudo-wrapped launch: the watchdog can signal only the sudo
    wrapper (this process's child), and relies on sudo relaying SIGTERM
    to the root x64sc.  ``run_as_root=True`` takes that path without the
    ethernet cart, so no BPF node or interface is touched.

    Safety net for a red run: ``limit_cycles`` makes the root emulator
    exit on its own (about 60 s unwarped), since this process cannot
    signal it and the bench grants no NOPASSWD ``kill``.
    """
    from conftest import require_vice_or_skip

    require_vice_or_skip()
    run = Launcher(
        """
from c64_test_harness.backends.vice_manager import PortAllocator
allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
port = allocator.allocate()
reservation = allocator.take_socket(port)
if reservation is not None:
    reservation.close()
p = ViceProcess(ViceConfig(port=port, warp=False, sound=False,
                           minimize=True, run_as_root=True,
                           limit_cycles=60_000_000))
p.start()
assert p.is_sudo_child, "launch was not sudo-wrapped"
import time
deadline = time.monotonic() + 10
vice = None
while vice is None and time.monotonic() < deadline:
    vice = p.resolve_vice_pid()
    time.sleep(0.05)
print(p.pid, vice, flush=True)
""",
        {},
    )
    sudo_pid = int(run.tokens[0])
    assert run.tokens[1] != "None", "root x64sc never appeared under sudo"
    vice_pid = int(run.tokens[1])
    ident = start_time(vice_pid)
    try:
        user = subprocess.run(["ps", "-o", "user=", "-p", str(vice_pid)],
                              capture_output=True, text=True).stdout.strip()
        assert ident is not None and user == "root", (
            f"test setup: pid {vice_pid} is not a root x64sc ({user!r})"
        )
        run.kill(signal.SIGKILL)
        deadline = time.monotonic() + DEATH_BOUND
        while time.monotonic() < deadline and start_time(vice_pid) == ident:
            time.sleep(0.05)
        assert start_time(vice_pid) != ident, (
            f"root x64sc {vice_pid} outlived its SIGKILLed launcher "
            f"(sudo wrapper {sudo_pid})"
        )
    finally:
        run.close()


# ---------------------------------------------------------------------------
# Watchdog decision logic, with fakes
# ---------------------------------------------------------------------------


TARGET = 5151
ARM = b"T %d t0\n" % TARGET


class FakeProcs:
    """A process table: ``{pid: start}``; signals are recorded."""

    def __init__(self, table, *, dies_on=(signal.SIGTERM, signal.SIGKILL)):
        self.table = dict(table)
        self.dies_on = dies_on
        self.sent: list[tuple[int, int]] = []
        self.on_sleep = None
        self.now = 0.0

    def start_of(self, pid):
        return self.table.get(pid)

    def kill(self, pid, sig):
        if pid not in self.table:
            raise ProcessLookupError(pid)
        self.sent.append((pid, sig))
        if sig in self.dies_on:
            del self.table[pid]

    def sleep(self, secs):
        self.now += secs
        if self.on_sleep:
            self.on_sleep()

    def clock(self):
        return self.now


def _dog(procs: FakeProcs, kill=None) -> Watchdog:
    return Watchdog(start_of=procs.start_of, kill=kill or procs.kill,
                    sleep=procs.sleep, clock=procs.clock, grace=1.0)


def _stream(*lines: bytes) -> io.BytesIO:
    return io.BytesIO(b"".join(lines))


def test_eof_after_arm_terminates_the_target():
    procs = FakeProcs({TARGET: "t0"})
    assert _dog(procs).run(_stream(ARM)) == "terminated"
    assert procs.sent == [(TARGET, signal.SIGTERM)]


def test_a_target_that_ignores_sigterm_is_killed_after_the_grace():
    procs = FakeProcs({TARGET: "t0"}, dies_on=(signal.SIGKILL,))
    assert _dog(procs).run(_stream(ARM)) == "killed"
    assert procs.sent == [(TARGET, signal.SIGTERM), (TARGET, signal.SIGKILL)]
    assert procs.now >= 1.0, "SIGKILL came before the grace period ran out"


def test_disarm_never_signals():
    procs = FakeProcs({TARGET: "t0"})
    assert _dog(procs).run(_stream(ARM, b"D\n")) == "disarmed"
    assert procs.sent == []


def test_eof_without_a_target_signals_nothing():
    procs = FakeProcs({TARGET: "t0"})
    assert _dog(procs).run(_stream()) == "unarmed"
    assert procs.sent == []


def test_reused_pid_is_not_signalled():
    """At EOF the armed PID belongs to a process with another start time."""
    procs = FakeProcs({TARGET: "t0"})

    class Reuse(io.BytesIO):
        def readline(self, *a):
            line = super().readline(*a)
            if not line:  # the old x64sc is gone and its PID reused
                procs.table[TARGET] = "t1"
            return line

    assert _dog(procs).run(Reuse(ARM)) == "gone"
    assert procs.sent == []


def test_pid_reused_during_the_grace_is_not_sigkilled():
    procs = FakeProcs({TARGET: "t0"}, dies_on=())

    def reuse():
        procs.table[TARGET] = "t1"

    procs.on_sleep = reuse
    assert _dog(procs).run(_stream(ARM)) == "terminated"
    assert procs.sent == [(TARGET, signal.SIGTERM)]


def test_an_arm_that_does_not_match_the_process_is_refused():
    """The named PID already has another start time when the arm is read
    (reused before the watchdog got to it): never armed, never signalled,
    even after EOF."""
    procs = FakeProcs({TARGET: "t1"})
    assert _dog(procs).run(_stream(ARM)) == "refused"
    assert procs.sent == []


def test_permission_denied_is_reported_not_retried():
    procs = FakeProcs({TARGET: "t0"})

    def deny(pid, sig):
        raise PermissionError(pid)

    assert _dog(procs, kill=deny).run(_stream(ARM)) == "denied"


def test_start_time_reads_the_real_table():
    me = start_time(os.getpid())
    assert me is not None
    assert start_time(os.getpid()) == me, "start time is not stable"
    child = subprocess.Popen(["/bin/sleep", "30"])
    try:
        token = start_time(child.pid)
        assert token is not None and token != me
        child.kill()
        deadline = time.monotonic() + 5
        while start_time(child.pid) is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        # Dead but not yet reaped by us: a zombie counts as gone.
        assert start_time(child.pid) is None
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


@pytest.mark.parametrize("disarm", [False, True])
def test_watchdog_script_end_to_end(disarm):
    """The script as launched -- ``python -I`` by path, no package on
    sys.path -- kills an armed child of its launcher on EOF, and leaves it
    alone when disarmed first."""
    target = subprocess.Popen(["/bin/sleep", "30"])
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    dog = subprocess.Popen(
        [sys.executable, "-I", vice_watchdog.__file__, str(os.getpid())],
        stdin=subprocess.PIPE, env=env,
    )
    try:
        dog.stdin.write(b"T %d %s\n"
                        % (target.pid, start_time(target.pid).encode()))
        if disarm:
            dog.stdin.write(b"D\n")
        dog.stdin.close()  # EOF: what a dead launcher looks like
        assert dog.wait(timeout=DEATH_BOUND) == 0
        if disarm:
            assert target.poll() is None, "a disarmed watchdog killed"
        else:
            assert target.wait(timeout=DEATH_BOUND) == -signal.SIGTERM
    finally:
        if target.poll() is None:
            target.kill()
        target.wait()
        if dog.poll() is None:
            dog.kill()
            dog.wait()

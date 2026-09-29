"""No x64sc survives a failed or abandoned launch (issue #514's orphan).

Every test here launches a *real* short-lived process in place of x64sc
(``/bin/sleep`` copied to a file named ``x64sc``, so ``ps`` sees the same
comm name the harness matches on) and injects the failure that used to
leak it.  The assertion is always about the process table, never about a
mock having been called.  No VICE is spawned and no port is listened on.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import vice_lifecycle
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import ViceInstanceManager

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX process semantics"
)

SRC = str(Path(vice_lifecycle.__file__).resolve().parents[2])


def _alive(pid: int) -> bool:
    """True while *pid* exists and is not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    stat = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _is_stub(pid: int, stub: Path) -> bool:
    comm = subprocess.run(
        ["ps", "-o", "comm=", "-p", str(pid)],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    return bool(comm) and os.path.basename(comm) == stub.name


def _kill_if_ours(pid: int, stub: Path) -> None:
    """Kill *pid* only if it is still running the stub we created."""
    if _is_stub(pid, stub):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture
def stub_x64sc(tmp_path):
    """A stand-in x64sc: /bin/sleep under the name ``x64sc``.

    ``sleep`` would reject VICE's flags, so the harness is pointed at a
    launcher that drops argv and execs the renamed sleep (same PID, comm
    ``x64sc``).
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    sleeper = bindir / "x64sc"
    shutil.copy("/bin/sleep", sleeper)
    if sys.platform == "darwin":
        # A copied platform binary is SIGKILLed at exec until re-signed.
        if shutil.which("codesign") is None:
            pytest.skip("codesign unavailable to re-sign the stand-in")
        subprocess.run(["codesign", "-f", "-s", "-", str(sleeper)],
                       check=True, capture_output=True)
    launcher = bindir / "x64sc-launch"
    launcher.write_text(f'#!/bin/sh\nexec "{sleeper}" 120\n')
    launcher.chmod(0o755)
    return launcher, sleeper


@pytest.fixture
def spawned(stub_x64sc):
    """Record every Popen the harness makes of the stub; kill leftovers."""
    launcher, sleeper = stub_x64sc
    real_popen = subprocess.Popen
    procs: list[subprocess.Popen] = []

    def spy(args, *a, **kw):
        p = real_popen(args, *a, **kw)
        if args and args[0] == str(launcher):
            procs.append(p)
            # Prove the stand-in is really running before the harness
            # continues, or a "no survivor" assertion would be vacuous.
            deadline = time.monotonic() + 5
            while not _is_stub(p.pid, sleeper):
                assert p.poll() is None and time.monotonic() < deadline, (
                    "stand-in x64sc did not start"
                )
                time.sleep(0.02)
        return p

    with patch.object(vice_lifecycle.subprocess, "Popen", spy):
        yield procs
    for p in procs:
        if p.poll() is None:
            _kill_if_ours(p.pid, sleeper)
            p.wait(timeout=5)


def _cfg(launcher: Path) -> ViceConfig:
    return ViceConfig(executable=str(launcher), port=6599, warp=True,
                      sound=False, minimize=True)


# ---------------------------------------------------------------------------
# Candidate 1: _start_instance cleanup on every exit path
# ---------------------------------------------------------------------------


class TestStartInstanceNeverOrphans:
    def _assert_all_dead(self, procs):
        assert procs, "the stub was never launched; the test proves nothing"
        for p in procs:
            assert _wait_dead(p.pid), (
                f"stub x64sc pid {p.pid} survived a failed acquire()"
            )

    def test_interrupt_during_connect_stops_the_process(
        self, spawned, stub_x64sc,
    ):
        launcher, _ = stub_x64sc
        mgr = ViceInstanceManager(_cfg(launcher), port_range_start=6590,
                                  port_range_end=6599, max_retries=1)
        with patch(
            "c64_test_harness.backends.vice_manager.BinaryViceTransport",
            side_effect=KeyboardInterrupt,
        ):
            with pytest.raises(KeyboardInterrupt):
                mgr.acquire()
        self._assert_all_dead(spawned)
        # The port the failed attempt held is free again.
        assert mgr._allocator.allocated_ports == frozenset()

    def test_error_after_connect_stops_process_and_closes_transport(
        self, spawned, stub_x64sc,
    ):
        launcher, _ = stub_x64sc
        transport = MagicMock()
        mgr = ViceInstanceManager(_cfg(launcher), port_range_start=6590,
                                  port_range_end=6599, max_retries=1)
        with (
            patch(
                "c64_test_harness.backends.vice_manager.BinaryViceTransport",
                return_value=transport,
            ),
            patch.object(ViceProcess, "get_listener_pid",
                         side_effect=OSError("lsof exploded")),
        ):
            with pytest.raises(OSError, match="lsof exploded"):
                mgr.acquire()
        self._assert_all_dead(spawned)
        transport.close.assert_called()
        assert mgr.active_count == 0

    def test_retried_failures_leave_no_process_behind(
        self, spawned, stub_x64sc,
    ):
        launcher, _ = stub_x64sc
        mgr = ViceInstanceManager(_cfg(launcher), port_range_start=6590,
                                  port_range_end=6599, max_retries=3)
        with (
            patch(
                "c64_test_harness.backends.vice_manager.BinaryViceTransport",
                return_value=MagicMock(),
            ),
            patch("c64_test_harness.backends.vice_manager.time.sleep"),
            patch("c64_test_harness.backends.port_lock.PortLock.update_vice_pid",
                  side_effect=RuntimeError("lockfile write failed")),
        ):
            with pytest.raises(RuntimeError, match="lockfile write failed"):
                mgr.acquire()
        assert len(spawned) == 3
        self._assert_all_dead(spawned)


# ---------------------------------------------------------------------------
# Candidate 4 (minor): shutdown() must not strand later instances
# ---------------------------------------------------------------------------


def test_shutdown_stops_every_instance_even_if_one_raises(
    spawned, stub_x64sc,
):
    launcher, _ = stub_x64sc
    mgr = ViceInstanceManager(_cfg(launcher), port_range_start=6590,
                              port_range_end=6599, max_retries=1)
    with patch(
        "c64_test_harness.backends.vice_manager.BinaryViceTransport",
        return_value=MagicMock(),
    ):
        first = mgr.acquire()
        second = mgr.acquire()
    assert len(spawned) == 2
    real_stop = first.process.stop

    def exploding_stop():
        real_stop()
        raise RuntimeError("teardown step failed")

    first.process.stop = exploding_stop
    with pytest.raises(RuntimeError, match="teardown step failed"):
        mgr.shutdown()
    for p in spawned:
        assert _wait_dead(p.pid), f"pid {p.pid} stranded by shutdown()"
    assert mgr._allocator.allocated_ports == frozenset()
    # ...and the cross-process port locks were released too.
    from c64_test_harness.backends.port_lock import PortLock
    for port in (first.port, second.port):
        lock = PortLock(port)
        assert lock.acquire(), f"port lock {port} still held after shutdown"
        lock.release()


# ---------------------------------------------------------------------------
# wait_for_exit: an interrupt must not drop the handle of a live process
# ---------------------------------------------------------------------------


def test_interrupted_wait_for_exit_stops_the_process(spawned, stub_x64sc):
    launcher, _ = stub_x64sc
    proc = ViceProcess(_cfg(launcher))
    proc.start()
    popen = proc._proc
    real_wait = popen.wait
    calls = {"n": 0}

    def wait(timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt
        return real_wait(timeout=timeout)

    popen.wait = wait
    with pytest.raises(KeyboardInterrupt):
        proc.wait_for_exit(timeout=30)
    assert _wait_dead(popen.pid), "wait_for_exit dropped a live x64sc"


# ---------------------------------------------------------------------------
# Candidate 3: a consumer that exits without stop() leaves nothing behind
# ---------------------------------------------------------------------------


def _run_child(script: str, launcher: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1",
               STUB=str(launcher))
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True, text=True, env=env, timeout=60,
    )


def test_process_abandoned_at_interpreter_exit_is_stopped(stub_x64sc):
    launcher, sleeper = stub_x64sc
    res = _run_child(
        """
        import os
        from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
        p = ViceProcess(ViceConfig(executable=os.environ["STUB"], sound=False))
        p.start()
        print(p.pid, flush=True)
        # no stop(): the consumer just exits
        """,
        launcher,
    )
    assert res.returncode == 0, res.stderr
    pid = int(res.stdout.split()[0])
    try:
        assert _wait_dead(pid), (
            f"x64sc stand-in {pid} outlived the interpreter that launched it"
        )
    finally:
        _kill_if_ours(pid, sleeper)


def test_manager_abandoned_at_interpreter_exit_is_stopped(stub_x64sc):
    launcher, sleeper = stub_x64sc
    res = _run_child(
        """
        import os
        from unittest.mock import MagicMock, patch
        from c64_test_harness.backends.vice_lifecycle import ViceConfig
        from c64_test_harness.backends.vice_manager import ViceInstanceManager
        mgr = ViceInstanceManager(
            ViceConfig(executable=os.environ["STUB"], sound=False),
            port_range_start=6590, port_range_end=6599, max_retries=1)
        with patch("c64_test_harness.backends.vice_manager.BinaryViceTransport",
                   return_value=MagicMock()):
            inst = mgr.acquire()
        print(inst.pid, flush=True)
        del mgr, inst          # dropped, never shut down
        """,
        launcher,
    )
    assert res.returncode == 0, res.stderr
    pid = int(res.stdout.split()[0])
    try:
        assert _wait_dead(pid), f"x64sc stand-in {pid} outlived its manager"
    finally:
        _kill_if_ours(pid, sleeper)


def test_forked_child_exit_does_not_stop_the_parents_process(stub_x64sc):
    launcher, sleeper = stub_x64sc
    res = _run_child(
        """
        import os, sys
        from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
        p = ViceProcess(ViceConfig(executable=os.environ["STUB"], sound=False))
        p.start()
        child = os.fork()
        if child == 0:
            sys.exit(0)        # normal exit: runs atexit handlers in the child
        os.waitpid(child, 0)
        alive = p._proc.poll() is None
        print(p.pid, alive, flush=True)
        p.stop()
        """,
        launcher,
    )
    assert res.returncode == 0, res.stderr
    pid_s, alive = res.stdout.split()[:2]
    try:
        assert alive == "True", (
            "a forked child's interpreter exit stopped the parent's x64sc"
        )
    finally:
        _kill_if_ours(int(pid_s), sleeper)


# ---------------------------------------------------------------------------
# Candidate 2: sudo-wrapped launch
# ---------------------------------------------------------------------------


def _wrapper_tree(sleeper: Path, *, ignore_term: str = "none") -> subprocess.Popen:
    """wrapper(sh) -> monitor(sh) -> x64sc: the use_pty shape of sudo.

    *ignore_term*: ``"none"``; ``"all"`` (the wrapper, the monitor and the
    stand-in all ignore SIGTERM); or ``"below"`` (the wrapper dies on
    SIGTERM, the monitor and stand-in ignore it -- x64sc is re-parented
    the moment the wrapper exits).  An ignored signal stays ignored across
    fork and exec.
    """
    inner = f'"{sleeper}" 120 & wait'
    if ignore_term == "all":
        outer = 'trap "" TERM; /bin/sh -c "$0" & wait'
    elif ignore_term == "below":
        outer = '(trap "" TERM; exec /bin/sh -c "$0") & wait'
    else:
        outer = '/bin/sh -c "$0" & wait'
    return subprocess.Popen(
        ["/bin/sh", "-c", outer, inner],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _find_stub_under(root: int, sleeper: Path, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="],
                             capture_output=True, text=True).stdout
        rows = [line.split(None, 2) for line in out.splitlines()]
        kids = {int(r[0]): (int(r[1]), r[2]) for r in rows if len(r) == 3}
        for pid, (ppid, comm) in kids.items():
            if os.path.basename(comm) != sleeper.name:
                continue
            anc = ppid
            while anc > 1 and anc in kids:
                if anc == root:
                    return pid
                anc = kids[anc][0]
        time.sleep(0.05)
    return None


def test_sudo_child_found_when_it_is_a_grandchild(stub_x64sc):
    _, sleeper = stub_x64sc
    wrapper = _wrapper_tree(sleeper)
    stub_pid = _find_stub_under(wrapper.pid, sleeper)
    try:
        assert stub_pid is not None, "test setup: stub never appeared"
        proc = ViceProcess(ViceConfig(sound=False))
        proc._proc = wrapper
        proc._is_sudo_child = True
        assert proc.resolve_vice_pid() == stub_pid
    finally:
        if stub_pid is not None:
            _kill_if_ours(stub_pid, sleeper)
        wrapper.kill()
        wrapper.wait(timeout=5)


@pytest.mark.parametrize("ignore_term", ["all", "below"])
def test_sudo_stop_reports_an_x64sc_it_could_not_kill(
    stub_x64sc, caplog, ignore_term,
):
    """x64sc ignores SIGTERM and `sudo -n kill` is refused (it is not in
    the bench's NOPASSWD allowlist), so the stand-in survives stop():
    stop() must say so, by PID -- including when the wrapper itself exits
    at once and x64sc has already been re-parented."""
    _, sleeper = stub_x64sc
    wrapper = _wrapper_tree(sleeper, ignore_term=ignore_term)
    stub_pid = _find_stub_under(wrapper.pid, sleeper)
    assert stub_pid is not None, "test setup: stub never appeared"
    proc = ViceProcess(ViceConfig(sound=False))
    proc._proc = wrapper
    proc._is_sudo_child = True
    # Simulate a root x64sc the unprivileged parent cannot signal: the
    # harness's only privileged route is `sudo -n kill`, make it fail.
    real_run = subprocess.run

    def run(args, *a, **kw):
        if args[:3] == ["sudo", "-n", "kill"]:
            return subprocess.CompletedProcess(args, 1)
        return real_run(args, *a, **kw)

    try:
        with (
            patch.object(vice_lifecycle.subprocess, "run", run),
            caplog.at_level(logging.WARNING, logger=vice_lifecycle.__name__),
        ):
            proc.stop()
        assert _alive(stub_pid)
        assert any(str(stub_pid) in r.getMessage() for r in caplog.records
                   if r.levelno >= logging.WARNING), caplog.text
    finally:
        _kill_if_ours(stub_pid, sleeper)
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=5)


# ---------------------------------------------------------------------------
# Candidate 4 (minor): callers between launch and hand-over
# ---------------------------------------------------------------------------


class _PolicyRefusingTransport:
    """A transport whose policy setter raises, as a validating one may."""

    def close(self):
        pass

    @property
    def memory_policy(self):
        return None

    @memory_policy.setter
    def memory_policy(self, value):
        raise ValueError("policy refused")


@pytest.mark.parametrize("entry", ["acquire", "instance"])
def test_unified_manager_releases_when_handover_fails(
    spawned, stub_x64sc, entry,
):
    from c64_test_harness.backends.unified_manager import UnifiedManager

    launcher, _ = stub_x64sc
    mgr = UnifiedManager(
        backend="vice", vice_config=_cfg(launcher),
        vice_kwargs=dict(port_range_start=6590, port_range_end=6599,
                         max_retries=1),
        memory_policy=object(),
    )
    with patch(
        "c64_test_harness.backends.vice_manager.BinaryViceTransport",
        return_value=_PolicyRefusingTransport(),
    ):
        with pytest.raises(ValueError, match="policy refused"):
            if entry == "acquire":
                mgr.acquire()
            else:
                with mgr.instance():
                    pass
    assert len(spawned) == 1
    assert _wait_dead(spawned[0].pid), "UnifiedManager kept a launched x64sc"
    assert mgr._manager.active_count == 0


def test_render_wav_interrupted_before_wait_stops_the_process(
    spawned, stub_x64sc, tmp_path,
):
    from c64_test_harness.backends import render_wav as rw

    launcher, _ = stub_x64sc
    prg = tmp_path / "t.prg"
    prg.write_bytes(b"\x01\x08\x00\x00")
    with patch.object(rw.logger, "info", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            rw.render_wav(prg, tmp_path / "o.wav", 0.1,
                          config=ViceConfig(executable=str(launcher)))
    assert len(spawned) == 1
    assert _wait_dead(spawned[0].pid), "render_wav left x64sc running"

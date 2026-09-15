"""#323: a lock held by the parallel runner deadlocked its own pytest children.

``scripts/run_u64_parallel_locked.py`` took ``DeviceLock(host)`` in a pool
worker and then ran ``python -m pytest <live file>`` as a child.  That child's
autouse ``device_lock_guard`` acquires ``DeviceLock(host, allow_nested=True)``,
and ``allow_nested`` joins a hold **in the same process only**.  So the child
queued on the flock its own parent held, and because the parent was alive and
its lockfile fresh, the child's deadline kept extending until the parent's
subprocess timeout killed it.

Everything here is offline: a private lock directory (``XDG_RUNTIME_DIR`` in
``tmp_path``), a TEST-NET-1 host, the non-probing ``acquire`` API, and no
device, VICE or network.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import select
import textwrap
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_HOST = "192.0.2.1"  # RFC 5737: reaches nothing

_CHILD = textwrap.dedent(
    """
    import sys
    from c64_test_harness.backends.device_lock import DeviceLock

    host, timeout, window = sys.argv[1], float(sys.argv[2]), sys.argv[3]
    lock = DeviceLock(host, allow_nested=True)
    kwargs = {} if window == "default" else {"progress_window": None}
    print("WAITING", flush=True)
    got = lock.acquire(timeout=timeout, **kwargs)
    print("ACQUIRED" if got else "NOT-ACQUIRED", flush=True)
    if got:
        lock.release()
    """
)


@pytest.fixture
def child_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """This process and its children share one private lock directory."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("U64_DEVICE_LOCK_TIMEOUT", raising=False)
    env = {k: v for k, v in os.environ.items() if k != "U64_DEVICE_LOCK_TIMEOUT"}
    env.update(
        XDG_RUNTIME_DIR=str(tmp_path),
        PYTHONPATH=str(_REPO / "src"),
        PYTHONDONTWRITEBYTECODE="1",
    )
    return env


def _child(env: dict[str, str], timeout: float, window: str, wall: float):
    return subprocess.run(
        [sys.executable, "-c", _CHILD, _HOST, str(timeout), window],
        env=env, capture_output=True, text=True, timeout=wall,
    )


def _await_line(proc: subprocess.Popen, want: str, deadline: float) -> bool:
    """True once *proc* prints the line *want*, False at EOF or *deadline* s."""
    end = time.monotonic() + deadline
    while (left := end - time.monotonic()) > 0:
        ready, _, _ = select.select([proc.stdout], [], [], left)
        if not ready:
            return False
        line = proc.stdout.readline()
        if not line:
            return False
        if line.strip() == want:
            return True
    return False


def test_a_child_process_cannot_join_the_lock_its_parent_holds(child_env) -> None:
    """The #323 shape, reproduced without a device.

    Three observations, all while this process holds the lock:

    1. **Control:** a second ``DeviceLock(allow_nested=True)`` *in this
       process* joins the hold at once, so ``allow_nested`` works as documented.
    2. A child process with the same flag and a hard deadline
       (``progress_window=None``) does **not** acquire: nesting does not cross
       a process boundary.
    3. A child with the guard's default ``progress_window`` never returns at
       all: the holder is alive and its lockfile fresh, so the child keeps
       extending its own 0.5 s deadline.  That is the runner's hang.  It is
       timed from the child's ``WAITING`` line, printed just before
       ``acquire``, so a child that is merely slow to start cannot pass
       (#381 review round 1, mutant D2).

    Then, with the hold released, the same child acquires at once (control).
    """
    from c64_test_harness.backends.device_lock import DeviceLock

    parent = DeviceLock(_HOST)
    assert parent.acquire(timeout=5.0)
    try:
        nested = DeviceLock(_HOST, allow_nested=True)
        assert nested.acquire(timeout=0.5) is True
        nested.release()

        hard = _child(child_env, 0.5, "hard", wall=20.0)
        assert hard.stdout.split() == ["WAITING", "NOT-ACQUIRED"], hard.stderr[-800:]

        # ``with`` closes both pipes on exit, after the kill (#381 round 2).
        with subprocess.Popen(
            [sys.executable, "-c", _CHILD, _HOST, "0.5", "default"],
            env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) as proc:
            try:
                assert _await_line(proc, "WAITING", deadline=20.0), (
                    "the default-window child never reached acquire"
                )
                time.sleep(0.5 + 2.5)  # its own timeout, plus a margin
                assert proc.poll() is None, (
                    "the default-window child returned instead of extending its "
                    f"wait (exit {proc.returncode})"
                )
            finally:
                proc.kill()
                proc.wait()
    finally:
        parent.release()

    free = _child(child_env, 5.0, "default", wall=20.0)
    assert free.stdout.split() == ["WAITING", "ACQUIRED"], free.stderr[-800:]


def _load_runner():
    path = _REPO / "scripts" / "run_u64_parallel_locked.py"
    spec = importlib.util.spec_from_file_location("_parallel_runner_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_runner_launches_its_child_without_taking_the_lock(
    child_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Behavioural half of #323: the worker function takes no ``DeviceLock``.

    ``DeviceLock`` is replaced with something that fails the test if
    constructed, and ``subprocess.run`` with a recorder, so nothing is launched.
    """
    from c64_test_harness.backends import device_lock

    runner = _load_runner()

    def refuse(*args, **kwargs):
        raise AssertionError("the runner constructed a DeviceLock around its child (#323)")

    monkeypatch.setattr(device_lock, "DeviceLock", refuse)
    calls: list[tuple[list[str], dict]] = []

    def record(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout="3 passed in 0.10s\n", stderr="")

    monkeypatch.setattr(runner.subprocess, "run", record)
    worker = getattr(runner, "_run_file", None) or getattr(runner, "_run_locked")
    result = worker("tests/test_ultimate64_client_live.py", _HOST, None)

    assert len(calls) == 1, calls
    cmd, kwargs = calls[0]
    assert cmd[1:4] == ["-m", "pytest", "tests/test_ultimate64_client_live.py"]
    assert kwargs["env"]["U64_HOST"] == _HOST
    assert result["returncode"] == 0 and "passed" in result["summary"]

    # #380: by default the child has no wall-clock cap.  Its run time
    # includes per-test lock waits behind other workers and lanes, which the
    # guard bounds itself, so a fixed cap (it was 90 s) killed fast files
    # under contention.  "timeout" absent and timeout=None both mean no cap.
    assert kwargs.get("timeout") is None, (
        f"the runner capped a child at {kwargs.get('timeout')!r} s by default; "
        "that cap includes per-test lock waits (#380)"
    )

    # An explicit cap passes through unchanged.
    worker("tests/test_ultimate64_client_live.py", _HOST, None, file_timeout=1234.5)
    assert calls[-1][1].get("timeout") == 1234.5, calls[-1]

    # The timeout branch (#381 review round 1, mutant R5): a child that
    # outlives an explicit cap is a failed file, and the summary says why.
    def time_out(cmd, **kwargs):
        calls.append((cmd, kwargs))
        raise subprocess.TimeoutExpired(cmd, 90)

    monkeypatch.setattr(runner.subprocess, "run", time_out)
    timed = worker("tests/test_ultimate64_client_live.py", _HOST, None, file_timeout=90.0)
    assert len(calls) == 3, calls
    assert timed["returncode"] == 1, timed
    assert "TIMEOUT" in timed["summary"] and "lock waits" in timed["summary"], timed


def test_the_runner_file_timeout_flag_reaches_every_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#380: ``--file-timeout`` is opt-in, validated, and reaches ``_run_file``.

    The pool is replaced by a synchronous stand-in and ``_run_file`` by a
    recorder, so nothing is launched and no device is named but TEST-NET-1.
    """
    runner = _load_runner()
    seen: list[float | None] = []

    def fake_run_file(test_file, host, password, file_timeout=None):
        seen.append(file_timeout)
        return {"file": test_file, "pid": 1, "returncode": 0,
                "summary": "1 passed", "elapsed": 0.0}

    class _Done:
        def __init__(self, value):
            self._value = value

        def result(self):
            return self._value

    class _SyncPool:
        def __init__(self, max_workers=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def submit(self, fn, *args, **kwargs):
            return _Done(fn(*args, **kwargs))

    monkeypatch.setattr(runner, "_run_file", fake_run_file)
    monkeypatch.setattr(runner, "ProcessPoolExecutor", _SyncPool)
    monkeypatch.setattr(runner, "as_completed", lambda futures: list(futures))
    monkeypatch.delenv("U64_HOST", raising=False)

    monkeypatch.setattr(sys, "argv", ["run_u64_parallel_locked.py", _HOST])
    assert runner.main() == 0
    assert seen == [None] * len(runner.TEST_FILES), seen

    seen.clear()
    monkeypatch.setattr(
        sys, "argv", ["run_u64_parallel_locked.py", _HOST, "--file-timeout", "600"]
    )
    assert runner.main() == 0
    assert seen == [600.0] * len(runner.TEST_FILES), seen

    for bad in ("0", "-5", "nan", "inf", "soon"):
        monkeypatch.setattr(
            sys, "argv", ["run_u64_parallel_locked.py", _HOST, "--file-timeout", bad]
        )
        with pytest.raises(SystemExit) as exc:
            runner.main()
        assert exc.value.code == 2, bad

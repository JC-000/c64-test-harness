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
import textwrap
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
       extending its own 0.5 s deadline.  That is the runner's hang.

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
        assert hard.stdout.split() == ["NOT-ACQUIRED"], hard.stderr[-800:]

        with pytest.raises(subprocess.TimeoutExpired):
            _child(child_env, 0.5, "default", wall=4.0)
    finally:
        parent.release()

    free = _child(child_env, 5.0, "default", wall=20.0)
    assert free.stdout.split() == ["ACQUIRED"], free.stderr[-800:]


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

"""Unit tests for scripts/cleanup_vice_ports.py.

All tests are fully mocked -- no real processes, no network, no /proc reads
beyond what monkeypatch redirects. Fast and safe to run anywhere.
"""
from __future__ import annotations

import importlib.util
import os
import signal
import sys
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "cleanup_vice_ports.py"
)
_spec = importlib.util.spec_from_file_location("cleanup_vice_ports", _MODULE_PATH)
assert _spec and _spec.loader
cleanup_vice_ports = importlib.util.module_from_spec(_spec)
sys.modules["cleanup_vice_ports"] = cleanup_vice_ports
_spec.loader.exec_module(cleanup_vice_ports)


# ---------------------------------------------------------------------------
# parse_ranges
# ---------------------------------------------------------------------------


def test_parse_ranges_single():
    assert cleanup_vice_ports.parse_ranges("6511:6531") == [(6511, 6531)]


def test_parse_ranges_multi():
    assert cleanup_vice_ports.parse_ranges("6511:6531,6560:6580") == [
        (6511, 6531),
        (6560, 6580),
    ]


def test_parse_ranges_whitespace_tolerated():
    assert cleanup_vice_ports.parse_ranges(" 6511:6531 , 6560:6580 ") == [
        (6511, 6531),
        (6560, 6580),
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "not-a-range",
        "6511",
        "abc:def",
        "6511:6531,",
        "6531:6511",  # reversed
        "0:100",  # zero port
        "70000:70010",  # out of range
    ],
)
def test_parse_ranges_invalid(bad):
    with pytest.raises(ValueError):
        cleanup_vice_ports.parse_ranges(bad)


# ---------------------------------------------------------------------------
# Helpers for the kill_vice_ports fake environment.
# ---------------------------------------------------------------------------


class _FakeWorld:
    """Tiny simulator for listeners, comms, and kill semantics."""

    def __init__(self):
        # port -> pid
        self.listeners: dict[int, int] = {}
        # pid -> comm
        self.comms: dict[int, str] = {}
        # pid -> alive
        self.alive: set[int] = set()
        # pid -> dies on SIGTERM?
        self.dies_on_term: dict[int, bool] = {}
        # pid -> dies on SIGKILL?
        self.dies_on_kill: dict[int, bool] = {}
        # Signal log
        self.signals_sent: list[tuple[int, int]] = []  # (pid, signo)

    def add(
        self,
        *,
        port: int,
        pid: int,
        comm: str = "x64sc",
        dies_on_term: bool = True,
        dies_on_kill: bool = True,
    ):
        self.listeners[port] = pid
        self.comms[pid] = comm
        self.alive.add(pid)
        self.dies_on_term[pid] = dies_on_term
        self.dies_on_kill[pid] = dies_on_kill

    def get_listener_pid(self, port: int) -> int | None:
        return self.listeners.get(port)

    def comm_of(self, pid: int) -> str | None:
        if pid not in self.alive and pid not in self.comms:
            return None
        return self.comms.get(pid)

    def os_kill(self, pid: int, signo: int) -> None:
        self.signals_sent.append((pid, signo))
        if pid not in self.alive:
            raise ProcessLookupError(pid)
        if signo == 0:
            return
        if signo == signal.SIGTERM and self.dies_on_term.get(pid, True):
            self.alive.discard(pid)
        elif signo == signal.SIGKILL and self.dies_on_kill.get(pid, True):
            self.alive.discard(pid)

    def pid_alive(self, pid: int) -> bool:
        return pid in self.alive


@pytest.fixture
def world(monkeypatch):
    w = _FakeWorld()
    monkeypatch.setattr(cleanup_vice_ports, "get_listener_pid", w.get_listener_pid)
    monkeypatch.setattr(cleanup_vice_ports, "comm_of", w.comm_of)
    monkeypatch.setattr(cleanup_vice_ports, "_pid_alive", w.pid_alive)
    monkeypatch.setattr(cleanup_vice_ports.os, "kill", w.os_kill)
    return w


# ---------------------------------------------------------------------------
# kill_vice_ports
# ---------------------------------------------------------------------------


def test_kill_vice_ports_no_listeners(world, capsys):
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.01)
    assert rc == 0
    assert world.signals_sent == []
    out = capsys.readouterr().out
    assert "no harness-bound x64sc processes found" in out


def test_kill_vice_ports_comm_mismatch(world, capsys):
    world.add(port=6512, pid=9999, comm="nginx")
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.01)
    assert rc == 0
    assert world.signals_sent == []
    assert "no harness-bound" in capsys.readouterr().out


def test_kill_vice_ports_sigterm_succeeds(world, capsys):
    world.add(port=6512, pid=12345, dies_on_term=True)
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.5)
    assert rc == 0
    # One SIGTERM, no SIGKILL.
    term_count = sum(1 for (_, s) in world.signals_sent if s == signal.SIGTERM)
    kill_count = sum(1 for (_, s) in world.signals_sent if s == signal.SIGKILL)
    assert term_count == 1
    assert kill_count == 0
    out = capsys.readouterr().out
    assert "-> SIGTERM" in out
    assert "all gone" in out


def test_kill_vice_ports_sigkill_required(world, capsys):
    world.add(port=6512, pid=12345, dies_on_term=False, dies_on_kill=True)
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.2)
    assert rc == 0
    term_count = sum(1 for (_, s) in world.signals_sent if s == signal.SIGTERM)
    kill_count = sum(1 for (_, s) in world.signals_sent if s == signal.SIGKILL)
    assert term_count == 1
    assert kill_count == 1
    out = capsys.readouterr().out
    assert "SIGKILL (still alive)" in out
    assert "all gone" in out


def test_kill_vice_ports_sigkill_fails(world, capsys):
    world.add(port=6512, pid=12345, dies_on_term=False, dies_on_kill=False)
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.2)
    assert rc == 1
    out = capsys.readouterr().out
    assert "still alive" in out


def test_kill_vice_ports_dedupes_pids(world):
    # Two ports bound to the same PID.
    world.add(port=6512, pid=77777)
    world.listeners[6513] = 77777  # second port, same pid
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6514)], grace=0.1)
    assert rc == 0
    term_count = sum(1 for (pid, s) in world.signals_sent if s == signal.SIGTERM)
    assert term_count == 1


def test_kill_vice_ports_dry_run(world, capsys):
    world.add(port=6512, pid=12345)
    rc = cleanup_vice_ports.kill_vice_ports(
        [(6511, 6513)], grace=0.1, dry_run=True
    )
    assert rc == 0
    # No real signals sent (only os.kill signal log receives real calls).
    assert all(s == 0 for (_, s) in world.signals_sent) or world.signals_sent == []
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "-> SIGTERM (dry-run)" in out
    # Process must still be alive after dry-run.
    assert 12345 in world.alive


def test_kill_vice_ports_scoped(world, capsys):
    # In-range: kill. Out-of-range: leave alone.
    world.add(port=6512, pid=11111)  # in range
    world.add(port=6600, pid=22222)  # out of range
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6531)], grace=0.1)
    assert rc == 0
    killed_pids = {pid for (pid, s) in world.signals_sent if s == signal.SIGTERM}
    assert killed_pids == {11111}
    assert 22222 in world.alive


def test_kill_vice_ports_multi_range(world):
    world.add(port=6512, pid=11111)
    world.add(port=6570, pid=22222)
    world.add(port=6600, pid=33333)  # outside both
    rc = cleanup_vice_ports.kill_vice_ports(
        [(6511, 6531), (6560, 6580)], grace=0.1
    )
    assert rc == 0
    killed_pids = {pid for (pid, s) in world.signals_sent if s == signal.SIGTERM}
    assert killed_pids == {11111, 22222}
    assert 33333 in world.alive


def test_kill_vice_ports_quiet(world, capsys):
    rc = cleanup_vice_ports.kill_vice_ports(
        [(6511, 6513)], grace=0.01, quiet=True
    )
    assert rc == 0
    out = capsys.readouterr().out
    # Quiet suppresses the "no processes found" line.
    assert out == ""


# ---------------------------------------------------------------------------
# Unprivileged silent-failure detection (x64sc file capabilities)
# ---------------------------------------------------------------------------


def test_silent_failure_detected_when_all_comm_reads_fail(monkeypatch, capsys):
    """Listeners present but comm unreadable -> warn + return EXIT_UNVERIFIABLE."""
    listeners = {6512: 11111, 6513: 22222}

    def fake_get_listener_pid(port):
        return listeners.get(port)

    def fake_comm_of(pid):
        return None  # EACCES for every PID (non-dumpable due to file caps)

    monkeypatch.setattr(
        cleanup_vice_ports, "get_listener_pid", fake_get_listener_pid
    )
    monkeypatch.setattr(cleanup_vice_ports, "comm_of", fake_comm_of)

    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.01)
    assert rc == cleanup_vice_ports.EXIT_UNVERIFIABLE
    captured = capsys.readouterr()
    # Warning must be on stderr, not stdout.
    assert "WARNING" in captured.err
    assert "found 2 listener(s)" in captured.err
    assert "sudo" in captured.err
    # And the happy-path "no harness-bound" message must NOT appear.
    assert "no harness-bound" not in captured.out


def test_no_warning_when_some_comm_reads_succeed(world, capsys):
    """If even one comm is readable, we are clearly privileged enough -- no warning."""
    world.add(port=6512, pid=11111)  # comm readable, matches x64sc
    # Second listener: pid exists but comm unreadable. Simulate by
    # registering it as a listener without adding it to world.alive/comms
    # via .add(); instead inject directly so comm_of returns None.
    world.listeners[6513] = 22222
    # (pid 22222 not in world.comms/alive -> comm_of returns None)

    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.1)
    assert rc == 0
    captured = capsys.readouterr()
    assert "WARNING" not in captured.err
    # The readable+matching PID got signaled.
    killed = {pid for (pid, s) in world.signals_sent if s == signal.SIGTERM}
    assert killed == {11111}


def test_no_warning_when_no_listeners_at_all(world, capsys):
    """Truly clean system -- should print the normal 'no listeners' line, not the EACCES warning."""
    rc = cleanup_vice_ports.kill_vice_ports([(6511, 6513)], grace=0.01)
    assert rc == 0
    captured = capsys.readouterr()
    assert "no harness-bound x64sc processes found" in captured.out
    assert "WARNING" not in captured.err


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_argparse_dry_run(world, capsys):
    rc = cleanup_vice_ports.main(["--range", "6511:6531", "--dry-run"])
    assert rc == 0


def test_cli_bad_range(capsys):
    rc = cleanup_vice_ports.main(["--range", "not-a-range"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "error" in err

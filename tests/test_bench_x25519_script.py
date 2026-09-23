"""``scripts/bench_x25519_u64_turbo.py`` must park-check the build it derived (#462).

The script read ``main_loop`` out of ``labels.txt`` and then compared the
boot poll against the literal ``4C 2A 08`` (``JMP $082A``). The x25519 build
parks at ``$082D`` (``4C 2D 08``), so every speed reported "Program did not
start" after its ``reboot()`` and ``run_prg`` upload had been spent. Its
``--prg``/``--labels`` defaults also named a Linux home directory (#245).

No device traffic: the script is imported, its device calls are doubles,
and ``hold_device_lock`` raises before any client could be built.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPT = (Path(__file__).resolve().parent.parent
           / "scripts" / "bench_x25519_u64_turbo.py")

_LOAD = 0x0801
_IMAGE_LEN = 0x1800
_LABELS = {
    "x25519_base": 0x1741,
    "x25_scalar": 0x19A0,
    "x25_result": 0x19E0,
    "bench_ticks": 0x177A,
    "vic_blank": 0x17E1,
    "vic_unblank": 0x17EA,
}


class _ReachedLock(BaseException):
    """Raised by the fake lock: the script got as far as the device."""


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_x25519_under_test", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jmp(addr: int) -> bytes:
    return bytes([0x4C, addr & 0xFF, addr >> 8])


def _write_build(directory: Path, main_loop: int = 0x082D,
                 park: bytes | None = None) -> Path:
    """Write ``x25519.prg`` + ``labels.txt``; the image parks at *main_loop*."""
    image = bytearray(_IMAGE_LEN)
    offset = main_loop - _LOAD
    image[offset:offset + 3] = _jmp(main_loop) if park is None else park
    prg = directory / "x25519.prg"
    prg.write_bytes(bytes([_LOAD & 0xFF, _LOAD >> 8]) + bytes(image))
    lines = [f"al C:{addr:06X} .{name}" for name, addr in _LABELS.items()]
    lines.append(f"al C:{main_loop:06X} .main_loop")
    (directory / "labels.txt").write_text("\n".join(lines) + "\n")
    return prg


# ---------------------------------------------------------------------------
# The boot poll
# ---------------------------------------------------------------------------

def _drive_run_one_speed(monkeypatch, module, tmp_path, main_loop, parked_reads):
    prg = _write_build(tmp_path, main_loop=main_loop)
    labels = module.Labels.from_file(prg.parent / "labels.txt")
    writes: list[tuple[int, bytes]] = []

    class _FakeTransport:
        def read_memory(self, addr: int, n: int) -> bytes:
            if addr == main_loop:
                return parked_reads
            if addr == module.SENTINEL_ADDR:
                return bytes([module.SENTINEL_VALUE])
            if addr == labels["x25_result"]:
                return module.RFC7748_EXPECTED
            return bytes(n)

        def write_memory(self, addr: int, data: bytes) -> None:
            writes.append((addr, bytes(data)))

    class _FakeClient:
        def reboot(self) -> None:
            pass

        def run_prg(self, data: bytes) -> None:
            pass

    clock = iter(float(i) for i in range(10_000))
    monkeypatch.setattr(module, "set_reu", lambda *a, **k: None)
    monkeypatch.setattr(module, "set_turbo_mhz", lambda *a, **k: None)
    monkeypatch.setattr(module, "time", types.SimpleNamespace(
        sleep=lambda *a: None, monotonic=lambda: next(clock)))

    result = module.run_one_speed(
        _FakeClient(), _FakeTransport(), prg.read_bytes(), labels, 48, 60.0)
    return result, writes


@pytest.mark.parametrize("main_loop", [0x082D, 0x0900])
def test_run_one_speed_accepts_the_build_park_bytes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, main_loop: int
) -> None:
    """The poll must match ``JMP main_loop`` for whatever the build says.

    ``$0900`` is an address no literal here has held, so hard-coding the
    current build's ``4C 2D 08`` passes the first arm and fails this one.
    """
    module = _load()

    result, writes = _drive_run_one_speed(
        monkeypatch, module, tmp_path, main_loop, _jmp(main_loop))

    assert result is not None, "the boot poll never matched the parked JMP"
    assert result["correct"] is True
    assert [d for a, d in writes if a == main_loop] == [_jmp(module.BENCH_SUB)]


def test_run_one_speed_rejects_the_stale_literal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``4C 2A 08`` read at ``$082D`` is not the build parked there."""
    module = _load()

    result, writes = _drive_run_one_speed(
        monkeypatch, module, tmp_path, 0x082D, bytes([0x4C, 0x2A, 0x08]))

    assert result is None
    assert [a for a, _ in writes if a == 0x082D] == []


# ---------------------------------------------------------------------------
# The pre-upload derivation check
# ---------------------------------------------------------------------------

def test_check_main_loop_accepts_the_build(tmp_path: Path) -> None:
    module = _load()
    prg = _write_build(tmp_path)

    assert module._check_main_loop(prg.read_bytes(), 0x082D) is None


def test_check_main_loop_rejects_a_non_park_instruction(tmp_path: Path) -> None:
    """``$082A`` in the real build holds ``JSR $0844``: a listing from a
    different build than the PRG looks exactly like this."""
    module = _load()
    prg = _write_build(tmp_path, park=bytes([0x20, 0x44, 0x08]))

    error = module._check_main_loop(prg.read_bytes(), 0x082D)

    assert error is not None and "$082D" in error, error


@pytest.mark.parametrize("main_loop", [0x0700, _LOAD + _IMAGE_LEN - 1])
def test_check_main_loop_rejects_an_address_outside_the_image(
    tmp_path: Path, main_loop: int
) -> None:
    module = _load()
    prg = _write_build(tmp_path)

    error = module._check_main_loop(prg.read_bytes(), main_loop)

    # Told apart from "holds the wrong instruction": there is nothing there.
    assert error is not None and "outside the PRG image" in error, error


# ---------------------------------------------------------------------------
# main(): refuse before the lock, default to the PRG's sibling listing
# ---------------------------------------------------------------------------

def _arm_main(monkeypatch: pytest.MonkeyPatch, argv: list[str]):
    module = _load()
    locks: list[str] = []

    def _lock(host: str):
        locks.append(host)
        raise _ReachedLock

    def _no_client(*a, **k):
        raise AssertionError("a device client was built")

    monkeypatch.setattr(module, "require_u64_host", lambda **kw: "192.0.2.1")
    monkeypatch.setattr(module, "hold_device_lock", _lock)
    monkeypatch.setattr(module, "Ultimate64Client", _no_client)
    monkeypatch.setattr(sys, "argv", ["bench", *argv])
    return module, locks


def test_main_refuses_a_mismatched_build_before_the_lock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prg = _write_build(tmp_path, park=bytes([0x20, 0x44, 0x08]))
    module, locks = _arm_main(monkeypatch, ["--prg", str(prg), "--speeds", "1"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    assert locks == [], "the check ran after the lock, i.e. after device contact"


def test_main_reads_labels_beside_the_prg_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prg = _write_build(tmp_path)
    module, locks = _arm_main(monkeypatch, ["--prg", str(prg), "--speeds", "1"])

    with pytest.raises(_ReachedLock):
        module.main()

    assert locks == ["192.0.2.1"]


def test_main_takes_the_prg_from_x25519_prg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    prg = _write_build(tmp_path)
    monkeypatch.setenv("X25519_PRG", str(prg))
    module, locks = _arm_main(monkeypatch, ["--speeds", "1"])

    with pytest.raises(_ReachedLock):
        module.main()

    assert locks == ["192.0.2.1"]


def test_main_with_no_prg_named_refuses_before_the_lock(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("X25519_PRG", raising=False)
    module, locks = _arm_main(monkeypatch, ["--speeds", "1"])

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "X25519_PRG" in out.out + out.err
    assert locks == []

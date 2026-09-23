"""The turbo-bench module must take its addresses from the build (#439).

``tests/test_u64_turbo_bench_live.py`` hard-coded

    X25519_CLAMP = 0x1509
    X25_SCALAR   = 0x19A0
    MAIN_LOOP    = 0x082A

under the comment "Addresses (from labels.txt)". The x25519 link layout then
moved and the literals did not. The build on this bench says ``main_loop`` is
``$082D`` and ``x25519_clamp`` is ``$148E``; ``$082A`` holds ``20 44 08``
(``JSR $0844``), which is exactly what all four live failures read back.

The cost of that staleness is what makes it worth a guard rather than a
corrected literal: the mismatch was only observable from the *polling loop*,
so each speed spent its ``reboot()`` + ``run_prg`` upload first. A full
session is 12 uploads — on leak-prone firmware, 12 uncollectable ``/Temp``
attachments bought for no measurement. Deriving the addresses at import means
the gate is a collection-time skip, before any fixture and so before the
first upload.

No device traffic: only the module's label resolver (parsing by
``c64_test_harness.Labels``, #463) and code builders are exercised,
against listings written into ``tmp_path``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


_LIVE_MODULE = Path(__file__).resolve().parent / "test_u64_turbo_bench_live.py"

#: The real build's values on this bench, read from
#: ``/Users/someone/Documents/c64-x25519/build/labels.txt`` (2026-09-10) and
#: corroborated against the PRG image: ``$082D`` holds ``4C 2D 08`` (the
#: self-``JMP``) and ``$148E`` holds ``AD A0 19 29 F8 8D A0 19``
#: (``LDA x25_scalar; AND #$F8; STA x25_scalar`` — the clamp itself).
_REAL_MAIN_LOOP = 0x082D
_REAL_CLAMP = 0x148E
_REAL_SCALAR = 0x19A0

#: The stale literals the module used to carry.
_STALE_MAIN_LOOP = 0x082A
_STALE_CLAMP = 0x1509


def _write_labels(
    directory: Path,
    *,
    main_loop: int | None = _REAL_MAIN_LOOP,
    clamp: int | None = _REAL_CLAMP,
    scalar: int | None = _REAL_SCALAR,
    extra: str = "",
) -> Path:
    """Write a ca65 label listing in the exact shape the build emits."""
    lines = []
    if clamp is not None:
        lines.append(f"al C:{clamp:06X} .x25519_clamp")
    if scalar is not None:
        lines.append(f"al C:{scalar:06X} .x25_scalar")
    if main_loop is not None:
        lines.append(f"al C:{main_loop:06X} .main_loop")
    # A real listing carries ~160 unrelated symbols; keep one so the parser
    # is never exercised on a file containing only what it looks for.
    lines.append("al C:000879 .input_buffer")
    text = "\n".join(lines) + "\n" + extra
    path = directory / "labels.txt"
    path.write_text(text)
    return path


def _arm(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs) -> ModuleType:
    """Point the module at a PRG whose sibling listing we control."""
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    prg = build / "x25519.prg"
    prg.write_bytes(b"\x01\x08")
    if kwargs.pop("labels", True):
        _write_labels(build, **kwargs)
    monkeypatch.setenv("X25519_PRG", str(prg))
    return _load()


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_turbo_bench_labels_under_test", _LIVE_MODULE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Parsing, through the resolver (#463: the module no longer has its own)
# ---------------------------------------------------------------------------

def _resolve_text(module: ModuleType, tmp_path: Path, text: str):
    """Run the module's resolver on a listing whose text we choose."""
    build = tmp_path / "resolve"
    build.mkdir(exist_ok=True)
    prg = build / "x25519.prg"
    prg.write_bytes(b"\x01\x08")
    (build / "labels.txt").write_text(text)
    return module._resolve_labels(prg)


def test_parser_reads_the_segment_prefixed_ca65_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``al C:00082D .main_loop`` -> ``{"main_loop": 0x082D}``."""
    module = _arm(monkeypatch, tmp_path)

    labels, reason = _resolve_text(
        module, tmp_path,
        "al C:00082D .main_loop\n"
        "al C:0019A0 .x25_scalar\n"
        "al C:00148E .x25519_clamp\n",
    )

    assert reason == ""
    assert labels == {
        "main_loop": 0x082D, "x25_scalar": 0x19A0, "x25519_clamp": 0x148E,
    }


def test_parser_accepts_the_prefix_less_form_the_repo_parser_reads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``al 00082D .main_loop`` (no ``C:``) is a listing too (#463).

    The module's own parser required the ``C:`` prefix and so called this
    "not a listing"; ``Labels`` reads both forms, and so does the module.
    """
    module = _arm(monkeypatch, tmp_path)

    labels, reason = _resolve_text(
        module, tmp_path,
        "al 00082D .main_loop\n"
        "al 0019A0 .x25_scalar\n"
        "al 00148E .x25519_clamp\n",
    )

    assert reason == "", reason
    assert labels["main_loop"] == 0x082D


def test_resolver_quotes_a_non_label_line_when_nothing_parses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _arm(monkeypatch, tmp_path)

    labels, reason = _resolve_text(
        module, tmp_path,
        "\n"                      # blank lines are not a defect
        "this is not a label\n",
    )

    assert labels is None
    assert "1 unparseable" in reason and "'this is not a label'" in reason, (
        "when nothing parses, the skip reason must quote the first "
        f"non-blank line and not count the blank one: {reason!r}"
    )


def test_listing_with_none_of_the_symbols_quotes_what_it_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An HTML 404 page can carry one line ``Labels`` accepts.

    It then parses to one unrelated symbol, so the "not a listing" branch
    does not fire and the reason is "missing symbols". With none of the
    required symbols present, the reason must still show what the file is.
    """
    module = _arm(monkeypatch, tmp_path)

    labels, reason = _resolve_text(
        module, tmp_path,
        "<html><title>404 Not Found</title>\n"
        "al 404 .notfound\n"
        "</html>\n",
    )

    assert labels is None
    assert "main_loop" in reason, reason
    assert "404 Not Found" in reason, (
        f"the reason should quote the file's first line: {reason!r}"
    )


def test_listing_missing_one_symbol_is_not_given_a_sample(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real listing short one symbol is a wrong build, not a strange file."""
    module = _arm(monkeypatch, tmp_path, main_loop=None)

    assert "first line" not in module._LABELS_SKIP_REASON


# ---------------------------------------------------------------------------
# The build decides
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("main_loop", [_REAL_MAIN_LOOP, 0x0900])
def test_main_loop_follows_the_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, main_loop: int
) -> None:
    """Two different listings, two different addresses.

    One arm is the real build's ``$082D`` — the value the stale ``$082A``
    got wrong. The other is an address no literal in this repository has
    ever held, so a module that "fixed" #439 by hard-coding ``$082D``
    passes the first arm and fails this one.
    """
    module = _arm(monkeypatch, tmp_path, main_loop=main_loop)

    assert module.MAIN_LOOP == main_loop
    assert module._LABELS_SKIP_REASON == ""


@pytest.mark.parametrize("clamp", [_REAL_CLAMP, 0x1600])
def test_clamp_entry_point_follows_the_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, clamp: int
) -> None:
    """``x25519_clamp`` was stale too — ``$1509`` is mid-instruction."""
    module = _arm(monkeypatch, tmp_path, clamp=clamp)

    assert module.X25519_CLAMP == clamp


def test_scalar_address_follows_the_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``x25_scalar`` matched the build by luck; it must now match by rule."""
    module = _arm(monkeypatch, tmp_path, scalar=0x1A00)

    assert module.X25_SCALAR == 0x1A00


def test_harness_chosen_addresses_are_not_taken_from_the_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``SENTINEL``/``TRAMPOLINE`` are free RAM this test picks, not build
    labels. A listing that happens to define them must not move them."""
    module = _arm(
        monkeypatch,
        tmp_path,
        extra="al C:00C000 .sentinel\nal C:00C100 .trampoline\n",
    )

    assert module.SENTINEL == 0x0350
    assert module.TRAMPOLINE == 0x0360


# ---------------------------------------------------------------------------
# The derived code
# ---------------------------------------------------------------------------

def test_boot_poll_bytes_are_assembled_at_the_build_main_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The self-``JMP`` the boot check waits for must target main_loop.

    This is the defect proper: the poll compared against a literal
    ``4C 2A 08`` while the program parked at ``$082D`` as ``4C 2D 08``.
    """
    module = _arm(monkeypatch, tmp_path)

    assert module._jmp_bytes(module.MAIN_LOOP) == bytes([0x4C, 0x2D, 0x08])
    assert module._jmp_bytes(0x0900) == bytes([0x4C, 0x00, 0x09])
    # And never the bytes the stale constant produced.
    assert module._jmp_bytes(module.MAIN_LOOP) != bytes(
        [0x4C, _STALE_MAIN_LOOP & 0xFF, _STALE_MAIN_LOOP >> 8]
    )


def test_trampoline_jsrs_the_build_clamp_address(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _arm(monkeypatch, tmp_path)

    code = module._trampoline_code(module.X25519_CLAMP)

    assert code[0] == 0x20, "first instruction must be JSR"
    assert code[1:3] == bytes([_REAL_CLAMP & 0xFF, _REAL_CLAMP >> 8])
    assert code[1:3] != bytes([_STALE_CLAMP & 0xFF, _STALE_CLAMP >> 8])
    # The rest of the trampoline still stores $42 at the sentinel and parks.
    assert code[3:5] == bytes([0xA9, 0x42])
    assert code[5:8] == bytes([0x8D, module.SENTINEL & 0xFF, module.SENTINEL >> 8])
    park = module.TRAMPOLINE + 8
    assert code[8:11] == bytes([0x4C, park & 0xFF, park >> 8])


def test_hijack_jumps_to_the_trampoline_wherever_it_is(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The hijack written over ``main_loop`` must follow ``TRAMPOLINE``.

    This was the last literal left in the module: ``4C 60 03`` spelled out.
    Move the trampoline and the jump stayed behind, landing the 6510 in
    whatever sits at ``$0360`` — on hardware, after all 12 uploads.
    """
    module = _arm(monkeypatch, tmp_path)
    assert module._hijack_code() == bytes([0x4C, 0x60, 0x03])

    monkeypatch.setattr(module, "TRAMPOLINE", 0x0370)

    assert module._hijack_code() == bytes([0x4C, 0x70, 0x03]), (
        "the hijack is a literal again: it does not track TRAMPOLINE"
    )


def test_run_clamp_fresh_hijacks_main_loop_to_the_trampoline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Drive the real helper against fakes and inspect what it writes.

    The unit test above pins ``_hijack_code``; this one pins the *call
    site*, so a literal reintroduced at the ``write_bytes`` call cannot
    pass by leaving the helper correct and unused. No device: the client,
    the transport, the memory helpers and ``sleep`` are all doubles.
    """
    module = _arm(monkeypatch, tmp_path)
    monkeypatch.setattr(module, "TRAMPOLINE", 0x0370)

    writes: list[tuple[int, bytes]] = []

    class _FakeTransport:
        def read_memory(self, address: int, count: int) -> bytes:
            if address == module.MAIN_LOOP:
                return module._jmp_bytes(module.MAIN_LOOP)  # booted, parked
            return bytes([0x42])  # sentinel: clamp already finished

    class _FakeClient:
        def reboot(self) -> None:
            pass

        def run_prg(self, data: bytes) -> None:
            pass

    monkeypatch.setattr(module, "write_bytes",
                        lambda t, addr, data: writes.append((addr, bytes(data))))
    monkeypatch.setattr(module, "read_bytes", lambda t, addr, n: bytes(n))
    monkeypatch.setattr(module, "set_reu", lambda *a, **k: None)
    monkeypatch.setattr(module, "set_turbo_mhz", lambda *a, **k: None)
    monkeypatch.setattr(module.time, "sleep", lambda *a, **k: None)

    module._run_clamp_fresh(_FakeClient(), _FakeTransport(), b"", bytes(32), 1)

    hijacks = [data for addr, data in writes if addr == module.MAIN_LOOP]
    assert hijacks == [bytes([0x4C, 0x70, 0x03])], (
        f"the write over main_loop must be JMP $0370, got {hijacks!r}"
    )
    # And the trampoline itself went to the relocated address.
    assert any(addr == 0x0370 for addr, _ in writes), (
        f"trampoline not written at the relocated address: {writes!r}"
    )


# ---------------------------------------------------------------------------
# The three failure modes, told apart
# ---------------------------------------------------------------------------

def test_absent_listing_names_the_file_and_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _arm(monkeypatch, tmp_path, labels=False)

    assert module._LABELS is None
    reason = module._LABELS_SKIP_REASON
    assert "labels.txt" in reason
    # Told apart from an unreadable file: the parse's own FileNotFoundError
    # also names the path, so without the existence check this would pass.
    assert "not found" in reason and "could not be read" not in reason, reason
    # A fallback to the old literals would re-create #439 exactly.
    assert module.MAIN_LOOP is None
    assert module.X25519_CLAMP is None
    assert module.X25_SCALAR is None


def test_file_that_is_not_a_listing_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = tmp_path / "build"
    build.mkdir()
    prg = build / "x25519.prg"
    prg.write_bytes(b"\x01\x08")
    (build / "labels.txt").write_text("<html>404 Not Found</html>\n")
    monkeypatch.setenv("X25519_PRG", str(prg))

    module = _load()

    assert module._LABELS is None
    reason = module._LABELS_SKIP_REASON
    assert "no ca65 label lines" in reason, reason
    assert "404" in reason, "the reason should quote what it actually found"


def test_listing_missing_a_symbol_names_that_symbol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A listing from some *other* PRG parses fine and is still unusable."""
    module = _arm(monkeypatch, tmp_path, main_loop=None)

    assert module._LABELS is None
    reason = module._LABELS_SKIP_REASON
    assert "main_loop" in reason, reason
    # Told apart from the "not a listing" case.
    assert "no ca65 label lines" not in reason


def test_unset_prg_reports_that_rather_than_a_missing_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("X25519_PRG", raising=False)
    module = _load()

    assert module._LABELS is None
    assert "X25519_PRG" in module._LABELS_SKIP_REASON


# ---------------------------------------------------------------------------
# The gate fires before any upload
# ---------------------------------------------------------------------------

def test_label_failure_is_a_collection_time_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point of #439's cost note.

    A mismatch must stop the session at collection — before the ``client``
    fixture, before ``reboot()``, before the first of 12 ``run_prg``
    uploads. That means the labels reason has to reach ``pytestmark``,
    where pytest evaluates it without instantiating a fixture.
    """
    module = _arm(monkeypatch, tmp_path, main_loop=None)

    skipifs = [m for m in module.pytestmark if m.name == "skipif"]
    firing = [
        m for m in skipifs
        if m.args and m.args[0] and "main_loop" in m.kwargs.get("reason", "")
    ]
    assert firing, (
        "no module-level skipif carries the label resolver's reason, so a "
        f"stale build would only fail mid-upload: {skipifs!r}"
    )


def test_good_listing_leaves_the_label_gate_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The gate must not skip the module whenever the build *is* fine."""
    module = _arm(monkeypatch, tmp_path)

    # Select the gate by position, not by its reason. On the happy path the
    # labels reason is "" and so is the PRG gate's, so a reason-based filter
    # is satisfied by the wrong mark: it stayed green under a mutant that
    # deleted the labels gate from pytestmark outright, while its own
    # message claimed the gate was present (reviewer finding 2).
    assert len(module.pytestmark) == 4, (
        f"expected four collection-time gates, got {module.pytestmark!r}"
    )
    labels_gate = module.pytestmark[3]
    assert labels_gate.name == "skipif"
    assert labels_gate.kwargs.get("reason") == module._LABELS_SKIP_REASON == ""
    assert labels_gate.args[0] is False, (
        "the labels gate fires even though every required symbol resolved"
    )


# ---------------------------------------------------------------------------
# Real ld65 output, not only listings this file wrote
# ---------------------------------------------------------------------------

def test_parses_the_checked_in_real_ld65_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, labels_path: Path
) -> None:
    """Parse genuine ``ld65 -Ln`` output rather than text this test wrote.

    ``tests/fixtures/labels.txt`` is a real listing from another C64
    project, already checked in and used by other tests — and it is a
    *different shape* from x25519's: four lowercase hex digits
    (``al C:df0a``) rather than six uppercase (``al C:00082D``). Both are
    real ld65 output, so the parser has to take both.
    """
    module = _arm(monkeypatch, tmp_path)

    # That project has no x25519 symbols; add two so the resolver accepts it
    # and every other line is still that project's real output.
    labels, reason = _resolve_text(
        module, tmp_path,
        labels_path.read_text()
        + "al C:00148E .x25519_clamp\nal C:0019A0 .x25_scalar\n",
    )

    assert reason == "", f"real ld65 output did not resolve: {reason}"
    # Every non-blank line of the fixture (names are unique) plus the two
    # appended: a parser that silently drops some real lines fails here.
    real = [line for line in labels_path.read_text().splitlines() if line.strip()]
    assert len(real) == 761
    assert len(labels) == len(real) + 2
    assert labels["main_loop"] == 0x0883      # that project's, not x25519's
    assert labels["reu_addr_ctrl"] == 0xDF0A  # four lowercase digits


def test_resolves_addresses_from_a_verbatim_x25519_listing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end on bytes the x25519 build itself emitted.

    ``tests/fixtures/x25519_labels_excerpt.txt`` holds three lines copied
    verbatim from that build's ``labels.txt`` (2026-09-10), including the
    ``C:`` prefix its Makefile's ``sed`` adds to ``ld65 -Ln`` output. Put
    where the module looks for it, the filename, the regex and the
    resolver all run against real output instead of a fixture's idea of it.
    """
    excerpt = (Path(__file__).resolve().parent
               / "fixtures" / "x25519_labels_excerpt.txt")
    build = tmp_path / "build"
    build.mkdir()
    prg = build / "x25519.prg"
    prg.write_bytes(b"\x01\x08")
    (build / "labels.txt").write_text(excerpt.read_text())
    monkeypatch.setenv("X25519_PRG", str(prg))

    module = _load()

    assert module._LABELS_SKIP_REASON == ""
    assert (module.MAIN_LOOP, module.X25519_CLAMP, module.X25_SCALAR) == (
        0x082D, 0x148E, 0x19A0
    )
    assert module._jmp_bytes(module.MAIN_LOOP) == bytes([0x4C, 0x2D, 0x08])

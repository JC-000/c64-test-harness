"""scripts/dis6502.py is a thin CLI shim over c64_test_harness.disasm.

Two 6510 disassemblers landed from two PRs with one opcode table each
(issue #170's script, and the library module built for jsr-timeout failure
messages).  Consolidated to ONE table, in the package -- this file now only
proves the shim delegates instead of duplicating that table, and pins the
opcode-length coverage and illegal/legal spot checks that used to live here
in tests/test_disasm.py, against the real table.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))

import dis6502  # noqa: E402
from dis6502 import dis  # noqa: E402
import c64_test_harness.disasm as disasm_module  # noqa: E402
from c64_test_harness.disasm import disassemble  # noqa: E402


def one(code: bytes, pc: int = 0x1000) -> str:
    return dis(code, pc, pc, pc + 1)


def test_dis_delegates_to_the_package_rendering():
    """``dis()`` on a single instruction must equal the package's own
    lower-case rendering of that instruction -- no independent table."""
    for code, pc in [
        (bytes([0x22]), 0x1000),
        (bytes([0x87, 0x87]), 0x1000),
        (bytes([0x81, 0xEB]), 0x1000),
        (bytes([0xEA]), 0x1000),
        (bytes([0xAD, 0x00, 0x02]), 0x1000),
        (bytes([0x6C, 0x02, 0xA0]), 0x1000),
    ]:
        expected = disassemble(code, pc, upper=False)[0]
        assert one(code, pc) == expected


def test_dis_delegates_truncated_tail_too():
    expected = disassemble(bytes([0xAD, 0x01]), 0x2000, upper=False)[0]
    assert dis(bytes([0xAD, 0x01]), 0x2000, 0x2000, 0x2001) == expected


def test_script_has_no_opcode_table_of_its_own():
    """The whole point of the consolidation: one opcode table, living in
    c64_test_harness.disasm -- not a second one duplicated here, and not
    a smaller look-alike either.  A strict ``len == 256`` check only
    catches a full duplicate table; a partial one (say, a 128-entry
    table covering just the illegal opcodes) would slip right past it.
    Flag any module-level dict/list/tuple large enough to plausibly be
    an opcode-keyed table -- and, the stronger guarantee, pin that the
    shim's `dis()` is built on the actual shared function object, not a
    re-implementation that merely happens to produce the same strings."""
    MAX_BENIGN_SIZE = 20  # generous headroom over any real config/const the script needs
    suspects = {
        name: len(value)
        for name, value in vars(dis6502).items()
        if not name.startswith("__")  # not module machinery (e.g. __builtins__)
        and isinstance(value, (dict, list, tuple))
        and len(value) > MAX_BENIGN_SIZE
    }
    assert suspects == {}, f"script still carries large module-level collection(s): {suspects}"
    assert dis6502.disassemble is disasm_module.disassemble


def test_script_imports_the_shared_disassemble_function():
    assert dis6502.disassemble is disassemble


def test_dis_end_bound_excludes_an_instruction_that_has_not_started_yet():
    """Every other test in this file calls ``dis()`` with a single-
    instruction window (``end = start + 1``), which never exercises the
    ``< end`` filter on a second instruction -- ``<= end`` passes them
    all too, while changing real output for any multi-instruction range.

    mem = NOP NOP LDA $1234 NOP, base $4000: NOP at $4000, NOP at $4001,
    LDA $1234 at $4002-$4004, NOP at $4005. ``end = $4002`` (the LDA's
    own address) must exclude the LDA -- only the two NOPs decode,
    matching the original script (verified against the pre-consolidation
    scripts/dis6502.py, byte-for-byte)."""
    mem = bytes([0xEA, 0xEA, 0xAD, 0x34, 0x12, 0xEA])
    base = 0x4000
    assert dis(mem, base, base, 0x4002) == "4000  ea        NOP\n4001  ea        NOP"


def test_dis_end_bound_still_includes_an_instruction_already_begun():
    """The other half of the same boundary: ``end`` landing *inside* the
    LDA (one byte past its opcode) does not truncate it -- the original
    script decodes a full instruction once started, regardless of where
    ``end`` falls inside it, and only checks ``pc < end`` before
    *starting* the next one.  So the LDA is included whole and the
    trailing NOP at $4005 is excluded, matching the original script."""
    mem = bytes([0xEA, 0xEA, 0xAD, 0x34, 0x12, 0xEA])
    base = 0x4000
    assert dis(mem, base, base, 0x4003) == (
        "4000  ea        NOP\n4001  ea        NOP\n4002  ad 34 12  LDA $1234"
    )

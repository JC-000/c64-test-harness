"""TAP files and the cassette pulse stimulus (issue #584), offline.

Functional: the TAP encoder is run through an independent decoder; the
stimulus routine is executed on a small cycle-counting 6502 model and the
edges it writes to ``$01`` are timed; ``run_tape_stimulus`` and the
cassette-buffer guard are run against a recording transport.

Safety: the guard makes ``MemoryPolicy`` refuse host writes into the
KERNAL's cassette buffer for the duration of a tape operation, and the
stimulus runner reads nothing until the pulse train is over (#573).

Nothing here spawns VICE or talks to a device.
"""

from __future__ import annotations

import pytest

from c64_test_harness import tape as tp
from c64_test_harness.memory_policy import MemoryPolicy, MemoryPolicyError, MemoryRegion
from c64_test_harness.verify import PrgFile


# ---------------------------------------------------------------------------
# TAP container
# ---------------------------------------------------------------------------

def test_parse_tap_reads_version_length_and_data():
    raw = b"C64-TAPE-RAW" + bytes([1, 0, 0, 0]) + (5).to_bytes(4, "little") + bytes([0x30] * 5)
    tap = tp.parse_tap(raw)
    assert (tap.version, tap.declared_length, tap.data) == (1, 5, bytes([0x30] * 5))
    assert tap.length_matches


def test_a_length_field_that_disagrees_with_the_data_is_reported():
    raw = b"C64-TAPE-RAW" + bytes([1, 0, 0, 0]) + (0).to_bytes(4, "little") + bytes([0x30] * 7)
    tap = tp.parse_tap(raw)
    assert tap.declared_length == 0 and len(tap.data) == 7
    assert not tap.length_matches


@pytest.mark.parametrize("raw", [b"", b"C64-TAPE-RAW\x01", b"C64-TAPE-RAX" + bytes(8)])
def test_parse_tap_refuses_what_is_not_a_tap(raw):
    with pytest.raises(ValueError):
        tp.parse_tap(raw)


def test_pulses_decode_overflows_by_version():
    v1 = tp.parse_tap(tp.build_tap(bytes([0x30, 0x00, 0x10, 0x27, 0x00, 0x42]), version=1))
    assert v1.pulses() == [0x30 * 8, 10000, 0x42 * 8]
    v0 = tp.parse_tap(tp.build_tap(bytes([0x30, 0x00, 0x42]), version=0))
    assert v0.pulses() == [0x30 * 8, 2048, 0x42 * 8]
    assert v1.duration_seconds(1000) == pytest.approx((0x30 * 8 + 10000 + 0x42 * 8) / 1000)


def test_a_truncated_overflow_is_dropped_not_misread():
    tap = tp.parse_tap(tp.build_tap(bytes([0x30, 0x00, 0x10]), version=1))
    assert tap.pulses() == [0x30 * 8]


# ---------------------------------------------------------------------------
# The KERNAL encoding, through an independent decoder
# ---------------------------------------------------------------------------

def _kernal_decode(pulse_bytes: bytes) -> list[list[int]]:
    """Written from the format description, not from the encoder: a byte is
    L,M then nine pairs (S,M = 0, M,S = 1), data LSB first then parity;
    L,S ends a block. Returns each block's bytes, parity checked."""
    def kind(b):
        return "S" if b < 0x39 else ("M" if b < 0x4C else "L")
    k = [kind(b) for b in pulse_bytes]
    blocks, cur, i = [], [], 0
    while i < len(k) - 1:
        if k[i] == "L" and k[i + 1] == "M":
            bits = []
            for j in range(9):
                a, b = k[i + 2 + 2 * j], k[i + 3 + 2 * j]
                assert (a, b) in (("S", "M"), ("M", "S")), f"bad bit pair at pulse {i}"
                bits.append(1 if a == "M" else 0)
            assert sum(bits) % 2 == 1, "odd parity"
            cur.append(sum(bit << n for n, bit in enumerate(bits[:8])))
            i += 20
        elif k[i] == "L" and k[i + 1] == "S":
            blocks.append(cur)
            cur = []
            i += 2
        else:
            assert k[i] == "S", f"stray {k[i]} at pulse {i}"
            i += 1
    return blocks


def test_a_prg_is_encoded_as_header_and_data_each_twice():
    prg = PrgFile(0x0801, bytes(range(1, 60)))
    tap = tp.parse_tap(tp.build_tap_from_prg(prg, name="KNOWN"))
    assert tap.version == 1 and tap.length_matches
    blocks = _kernal_decode(tap.data)
    assert len(blocks) == 4
    h1, h2, d1, d2 = blocks
    assert h1[:9] == [0x89, 0x88, 0x87, 0x86, 0x85, 0x84, 0x83, 0x82, 0x81]
    assert h2[:9] == [9, 8, 7, 6, 5, 4, 3, 2, 1]
    header = bytes(h1[9:-1])
    assert len(header) == 192 and h1[9:] == h2[9:]
    assert header[0] == 1                                  # relocatable BASIC program
    assert header[1:5] == bytes([0x01, 0x08, 0x3C, 0x08])  # $0801, end $083C (exclusive)
    assert header[5:21] == b"KNOWN" + b" " * 11
    assert set(header[21:]) == {0x20}
    x = 0
    for b in header:
        x ^= b
    assert h1[-1] == x
    assert bytes(d1[9:-1]) == prg.data and d1[9:] == d2[9:]
    assert d1[:9] == h1[:9] and d2[:9] == h2[:9]


def test_pilot_lengths_precede_each_block():
    tap = tp.parse_tap(tp.build_tap_from_prg(PrgFile(0xC000, b"\x60")))
    data = tap.data
    assert data[:0x6A00] == bytes([tp.TAP_SHORT]) * 0x6A00
    assert data[0x6A00] == tp.TAP_LONG
    # The data block's pilot: the longest run of shorts after the header's.
    runs, run = [], 0
    for b in data[0x6A00:]:
        if b == tp.TAP_SHORT:
            run += 1
        else:
            runs.append(run)
            run = 0
    # The end marker's short, the header's trailer, then the data pilot.
    assert max(runs) == 1 + 0x4E + 0x1500


def test_file_type_follows_the_load_address_unless_given():
    def htype(prg, **kw):
        return _kernal_decode(tp.parse_tap(tp.build_tap_from_prg(prg, **kw)).data)[0][9]
    assert htype(PrgFile(0x0801, b"\x00\x00")) == 1
    assert htype(PrgFile(0xC000, b"\x60")) == 3
    assert htype(PrgFile(0x0801, b"\x00\x00"), file_type=3) == 3


@pytest.mark.parametrize("kw", [
    {"name": "lower"}, {"name": "X" * 17}, {"file_type": 2}, {"file_type": 0},
])
def test_build_tap_from_prg_refuses_bad_arguments(kw):
    with pytest.raises(ValueError):
        tp.build_tap_from_prg(PrgFile(0x0801, b"\x00\x00"), **kw)


def test_empty_program_is_refused():
    with pytest.raises(ValueError):
        tp.build_tap_from_prg(PrgFile(0x0801, b""))


def test_decode_tap_blocks_recovers_the_program_and_flags_damage():
    prg = tp.tape_test_program()
    tap = tp.parse_tap(tp.build_tap_from_prg(prg))
    blocks = tp.decode_tap_blocks(tap)
    assert [b.first_copy for b in blocks] == [True, False, True, False]
    assert all(b.checksum_ok and b.parity_errors == 0 for b in blocks)
    assert blocks[2].payload == prg.data and blocks[3].payload == prg.data
    assert blocks[0].payload[0] == 1 and len(blocks[0].payload) == 192

    # Flip one data bit of the first data copy: swap one bit pair.
    data = bytearray(tap.data)
    start = 0x6A00 + 2 * (202 * 20 + 2) + 0x4F + 0x4E + 0x1500   # first data block's first byte
    at = start + 9 * 20 + 2                                       # past the countdown, first bit pair
    assert data[start] == tp.TAP_LONG and data[start + 1] == tp.TAP_MEDIUM
    data[at], data[at + 1] = data[at + 1], data[at]
    damaged = tp.decode_tap_blocks(tp.parse_tap(tp.build_tap(bytes(data))))
    assert damaged[2].parity_errors == 1 and not damaged[2].checksum_ok
    assert damaged[2].payload != prg.data
    assert damaged[3].checksum_ok and damaged[3].payload == prg.data


def test_tape_test_program_is_a_linked_basic_program():
    prg = tp.tape_test_program(lines=3)
    assert prg.load_address == 0x0801
    d = prg.data
    addr, off, numbers = 0x0801, 0, []
    while True:
        link = d[off] | (d[off + 1] << 8)
        if link == 0:
            break
        numbers.append(d[off + 2] | (d[off + 3] << 8))
        assert d[off + 4] == 0x8F                        # REM
        end = d.index(0, off + 4)                        # the line's terminator
        assert link == addr + (end + 1 - off)            # what the KERNAL relink computes
        off, addr = end + 1, link
    assert numbers == [10, 20, 30]
    assert off + 2 == len(d)


# ---------------------------------------------------------------------------
# The stimulus routine, executed
# ---------------------------------------------------------------------------

class _Cpu:
    """The opcodes the stimulus uses, with their cycle counts.

    Records ``(cycle, value)`` at the end of every write to ``$01``.
    ``$D011`` bit 7 alternates every 9000 cycles (the raster's high bit).
    """

    def __init__(self, image: bytes, load: int, port: int = 0x37) -> None:
        self.mem = bytearray(0x10000)
        self.mem[load:load + len(image)] = image
        self.mem[0x01] = port
        self.port_at_entry = port
        self.mem[0xD011] = 0x1B
        self.mem[0xD015] = 0x81
        self.a = self.x = 0
        self.z = self.n = False
        self.i = False
        self.cycles = 0
        self.port_writes: list[tuple[int, int]] = []
        self.d011_writes: list[int] = []
        self.d011_reads: list[tuple[int, int]] = []
        self.d015_writes: list[tuple[int, int]] = []

    def rd(self, addr):
        if addr == 0xD011:
            v = (self.mem[addr] & 0x7F) | (0x80 if (self.cycles // 9000) & 1 else 0)
            self.d011_reads.append((self.cycles, v))
            return v
        return self.mem[addr]

    def wr(self, addr, v):
        self.mem[addr] = v
        if addr == 0x01:
            self.port_writes.append((self.cycles, v))
        if addr == 0xD011:
            self.d011_writes.append(v)
        if addr == 0xD015:
            self.d015_writes.append((self.cycles, v))

    def nz(self, v):
        self.z, self.n = v == 0, bool(v & 0x80)

    def run(self, pc: int, limit: int = 400_000_000) -> None:
        m = self.mem
        while self.cycles < limit:
            op = m[pc]
            lo, hi = m[pc + 1], m[pc + 2]
            ab = lo | (hi << 8)
            if op == 0x78:
                self.i = True; pc += 1; self.cycles += 2
            elif op == 0x58:
                self.i = False; pc += 1; self.cycles += 2
            elif op == 0xEA:
                pc += 1; self.cycles += 2
            elif op == 0xA9:
                self.a = lo; self.nz(lo); pc += 2; self.cycles += 2
            elif op == 0xA2:
                self.x = lo; self.nz(lo); pc += 2; self.cycles += 2
            elif op == 0x09:
                self.a |= lo; self.nz(self.a); pc += 2; self.cycles += 2
            elif op == 0x29:
                self.a &= lo; self.nz(self.a); pc += 2; self.cycles += 2
            elif op == 0xA5:
                self.cycles += 3; self.a = self.rd(lo); self.nz(self.a); pc += 2
            elif op == 0x85:
                self.cycles += 3; self.wr(lo, self.a); pc += 2
            elif op == 0xAD:
                self.cycles += 4; self.a = self.rd(ab); self.nz(self.a); pc += 3
            elif op == 0x8D:
                self.cycles += 4; self.wr(ab, self.a); pc += 3
            elif op == 0x0D:
                self.cycles += 4; self.a |= self.rd(ab); self.nz(self.a); pc += 3
            elif op == 0x2C:
                self.cycles += 4; v = self.rd(ab); self.n = bool(v & 0x80)
                self.z = (v & self.a) == 0; pc += 3
            elif op == 0xCE:
                self.cycles += 6; v = (self.rd(ab) - 1) & 0xFF; self.wr(ab, v); self.nz(v); pc += 3
            elif op == 0xCA:
                self.x = (self.x - 1) & 0xFF; self.nz(self.x); pc += 1; self.cycles += 2
            elif op == 0x4C:
                pc = ab; self.cycles += 3
            elif op in (0xD0, 0x10, 0x30):
                taken = {0xD0: not self.z, 0x10: not self.n, 0x30: self.n}[op]
                nxt = pc + 2
                self.cycles += 2
                if taken:
                    dest = (nxt + (lo - 256 if lo & 0x80 else lo)) & 0xFFFF
                    self.cycles += 1 + (1 if (dest >> 8) != (nxt >> 8) else 0)
                    nxt = dest
                pc = nxt
            elif op == 0x60:
                self.cycles += 6
                return
            else:
                raise AssertionError(f"opcode ${op:02X} at ${pc:04X} is not one the stimulus uses")
        raise AssertionError("the routine did not return")


def _run(stim: tp.TapeStimulus) -> _Cpu:
    cpu = _Cpu(stim.prg[2:], stim.load_address)
    cpu.run(stim.sys_address)
    return cpu


def _rising(cpu: _Cpu) -> list[int]:
    out, prev = [], cpu.port_at_entry & 0x08
    for cyc, v in cpu.port_writes:
        if v & 0x08 and not prev:
            out.append(cyc)
        prev = v & 0x08
    return out


@pytest.mark.parametrize("tap_value", [8, 9, 10, 11, 12, 0x30, 0x42, 0x56, 0x57, 254, 255])
def test_every_period_is_exactly_tap_value_times_eight_cycles(tap_value):
    stim = tp.build_tape_pulse_stimulus(300, tap_value=tap_value)
    edges = _rising(_run(stim))
    assert len(edges) == 300
    gaps = {b - a for a, b in zip(edges, edges[1:])}
    assert gaps == {tap_value * 8}
    assert stim.period_cycles == tap_value * 8


@pytest.mark.parametrize("pulses", [1, 2, 255, 256, 257, 513, 65535])
def test_the_pulse_count_is_exact_across_the_16_bit_borrow(pulses):
    stim = tp.build_tape_pulse_stimulus(pulses, tap_value=8)
    edges = _rising(_run(stim))
    assert len(edges) == pulses
    assert {b - a for a, b in zip(edges, edges[1:])} <= {64}


@pytest.mark.parametrize("port", [0x37, 0x3F, 0x17])
def test_the_routine_restores_what_it_changed_and_reports_done(port):
    stim = tp.build_tape_pulse_stimulus(5)
    cpu = _Cpu(stim.prg[2:], stim.load_address, port=port)
    cpu.run(stim.sys_address)
    assert cpu.mem[0x01] == port and cpu.mem[0xD011] == 0x1B and cpu.mem[0xD015] == 0x81
    assert cpu.mem[stim.done_address] == tp.STIMULUS_DONE
    assert cpu.i is False
    # Only bit 3 of $01 ever differed from the value it started with.
    assert {v & 0xF7 for _, v in cpu.port_writes} == {port & 0xF7}
    # Five pulses whatever the line's level on entry; putting a high line
    # back afterwards is one more edge.
    edges = _rising(cpu)
    assert {b - a for a, b in zip(edges[:5], edges[1:5])} == {stim.period_cycles}
    assert len(edges) == (6 if port & 0x08 else 5)
    # The screen was blanked (bit 4 clear) before the first pulse.
    assert cpu.d011_writes[0] == 0x0B and cpu.d011_writes[-1] == 0x1B


def test_sprites_are_off_and_a_new_frame_has_begun_before_the_first_pulse():
    """What keeps the VIC from stealing cycles: no sprites, and the screen
    blanked before a frame starts (the blanking takes effect per frame)."""
    stim = tp.build_tape_pulse_stimulus(4)
    cpu = _run(stim)
    first = _rising(cpu)[0]
    assert [(c < first, v) for c, v in cpu.d015_writes] == [(True, 0x00), (False, 0x81)]
    blanked_at = None
    for c, v in cpu.d011_reads:
        if v & 0x10 == 0:
            blanked_at = c
            break
    assert blanked_at is not None and blanked_at < first
    seen = [bool(v & 0x80) for c, v in cpu.d011_reads if blanked_at <= c < first]
    # The raster's high bit was seen set and then clear: a frame boundary
    # passed between blanking and the first pulse.
    assert True in seen and seen[-1] is False and seen.index(True) < len(seen) - 1


@pytest.mark.parametrize("kw", [
    {"margin": -1.0}, {"margin": float("nan")}, {"clock_hz": 0}, {"clock_hz": float("inf")},
    {"clock_hz": -1},
])
def test_run_tape_stimulus_refuses_a_wait_that_could_end_early(kw):
    t = _Transport()
    stim = tp.build_tape_pulse_stimulus(10)
    with pytest.raises(ValueError):
        tp.run_tape_stimulus(t, stim, sleep=lambda s: None, **kw)
    assert t.log == []


def test_done_is_cleared_at_entry_so_a_stale_marker_cannot_pass():
    stim = tp.build_tape_pulse_stimulus(1)
    image = bytearray(stim.prg[2:])
    image[stim.done_address - stim.load_address] = tp.STIMULUS_DONE
    cpu = _Cpu(bytes(image), stim.load_address)
    # Stop before the loop ends: run only the setup by limiting cycles.
    with pytest.raises(AssertionError, match="did not return"):
        cpu.run(stim.sys_address, limit=60)
    assert cpu.mem[stim.done_address] == 0


def test_default_prg_starts_with_a_sys_stub_and_other_addresses_are_bare():
    from c64_test_harness.execute import parse_basic_sys_address
    stim = tp.build_tape_pulse_stimulus(10)
    assert stim.prg[:2] == b"\x01\x08"
    assert parse_basic_sys_address(stim.prg) == stim.sys_address == 2061
    bare = tp.build_tape_pulse_stimulus(10, load_address=0xC000)
    assert bare.sys_address == 0xC000 and bare.prg[:2] == b"\x00\xC0"
    assert bare.prg[2] == 0x78
    assert len(_rising(_run(bare))) == 10


def test_the_routine_stays_out_of_the_cassette_buffer_and_zero_page():
    stim = tp.build_tape_pulse_stimulus(3)
    cpu = _Cpu(stim.prg[2:], stim.load_address)
    before = bytes(cpu.mem)
    cpu.run(stim.sys_address)
    changed = {a for a in range(0x10000) if cpu.mem[a] != before[a]}
    end = stim.load_address + len(stim.prg) - 2
    assert changed <= set(range(stim.load_address, end))
    assert not changed & set(range(0x033C, 0x03FC))


@pytest.mark.parametrize("kw", [
    {"pulses": 0}, {"pulses": 65536}, {"tap_value": 7}, {"tap_value": 256},
    {"pulses": True}, {"load_address": 0x08F0},
])
def test_stimulus_refuses_what_it_cannot_time_exactly(kw):
    with pytest.raises(ValueError):
        tp.build_tape_pulse_stimulus(**kw)


def test_duration_is_pulses_times_period_over_the_clock():
    stim = tp.build_tape_pulse_stimulus(1000, tap_value=0x30)
    assert stim.duration_seconds(1000) == pytest.approx(1000 * 384 / 1000)


# ---------------------------------------------------------------------------
# run_tape_stimulus and the cassette-buffer guard, against a recording transport
# ---------------------------------------------------------------------------

class _Transport:
    """Records every call; RAM is a bytearray. ``sleep`` marks the timeline."""

    def __init__(self) -> None:
        self.mem = bytearray(0x10000)
        self.log: list[tuple] = []
        self.memory_policy = MemoryPolicy()

    def write_memory(self, addr, data, **kw):
        self.memory_policy.check_write(addr, len(data))
        self.log.append(("write", addr, len(data)))
        self.mem[addr:addr + len(data)] = bytes(data)

    def read_memory(self, addr, length):
        self.log.append(("read", addr, length))
        return bytes(self.mem[addr:addr + length])

    def inject_keys(self, codes):
        self.log.append(("keys", bytes(codes)))

    def resume(self):
        self.log.append(("resume",))


def test_run_tape_stimulus_reads_nothing_until_the_pulses_are_over():
    t = _Transport()
    stim = tp.build_tape_pulse_stimulus(2000, tap_value=0x30)

    def sleep(seconds):
        t.log.append(("sleep", seconds))
        t.mem[stim.done_address] = tp.STIMULUS_DONE   # the routine finishes meanwhile

    assert tp.run_tape_stimulus(t, stim, clock_hz=985248, margin=0.5, sleep=sleep) is True
    kinds = [e[0] for e in t.log]
    typed = max(i for i, e in enumerate(t.log) if e[0] == "keys")
    slept = kinds.index("sleep")
    assert typed < slept
    assert "read" not in kinds[typed:slept]          # #573: none between the SYS and the wait
    assert kinds[slept + 1:] == ["read"]
    assert t.log[-1] == ("read", stim.done_address, 1)
    assert t.log[slept][1] >= 2000 * 384 / 985248 + 0.5
    assert b"SYS2061\r" in b"".join(e[1] for e in t.log if e[0] == "keys")
    # Started from the keyboard: nothing was written into the cassette buffer.
    assert not any(e[0] == "write" and e[1] < 0x03FC and e[1] + e[2] > 0x033C for e in t.log)


def test_run_tape_stimulus_reports_a_routine_that_did_not_finish():
    t = _Transport()
    stim = tp.build_tape_pulse_stimulus(10)
    assert tp.run_tape_stimulus(t, stim, sleep=lambda s: None) is False


def test_cassette_buffer_is_refused_inside_the_guard_and_free_again_after():
    t = _Transport()
    t.memory_policy = MemoryPolicy(reserved_regions=(MemoryRegion(0x1000, 0x1100, "mine"),))
    original = t.memory_policy
    with tp.cassette_buffer_reserved(t):
        for addr in (0x0360, 0x03C0, 0x033C, 0x03FB):
            with pytest.raises(MemoryPolicyError):
                t.write_memory(addr, b"\x00")
        with pytest.raises(MemoryPolicyError):
            t.write_memory(0x0334, bytes(128))       # the liveness span reaches into it
        t.write_memory(0x033B, b"\x00")              # one below
        t.write_memory(0x03FC, b"\x00")              # one above
        with pytest.raises(MemoryPolicyError):
            t.write_memory(0x1000, b"\x00")          # the caller's own reservation still holds
    assert t.memory_policy is original
    t.write_memory(0x0360, b"\x00")


def test_guard_restores_the_policy_when_the_block_raises():
    t = _Transport()
    original = t.memory_policy
    with pytest.raises(RuntimeError):
        with tp.cassette_buffer_reserved(t):
            raise RuntimeError("boom")
    assert t.memory_policy is original


def test_guard_leaves_a_transport_without_a_policy_alone():
    class Bare:
        pass
    with tp.cassette_buffer_reserved(Bare()):
        pass

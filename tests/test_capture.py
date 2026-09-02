"""Host-side packet capture abstraction (issue #158).

Two things are pinned here without an emulator or a network interface:

1. The pure-Python parser for the buffer a ``read(2)`` on ``/dev/bpf*``
   returns.  macOS hands back *several* records per read, each a
   ``struct bpf_hdr`` followed by the captured bytes, with every record
   start rounded up to ``BPF_WORDALIGN`` (4 bytes).  The header bytes
   below are constructed by hand from the SDK's ``<net/bpf.h>``::

       struct bpf_hdr {
           struct timeval32 bh_tstamp;   /* u32 sec, u32 usec       */
           bpf_u_int32      bh_caplen;   /* bytes actually captured */
           bpf_u_int32      bh_datalen;  /* original frame length   */
           u_short          bh_hdrlen;   /* 18 rounded up -> 20     */
       };

   Every capture length in this file is chosen NOT to be a multiple of
   four, so a parser that advances by ``hdrlen + caplen`` without the
   alignment step reads the second record from the wrong offset and
   fails.  One test packs records *without* the alignment padding and
   requires the parser to reject the buffer rather than emit garbage.

2. The remedy text ``open_capture`` attaches when no capture path is
   available.  The point of the abstraction is that the two ethernet
   TX/RX tests skip *with the fix in the reason* when capture is
   genuinely absent, and never when it is present -- so the reason has
   to be the literal command an operator runs.
"""

from __future__ import annotations

import errno
import struct

import pytest

from c64_test_harness.capture import (
    BPF_HDR_SIZE,
    BpfDescriptor,
    BpfParseError,
    CaptureUnavailable,
    bpf_descriptor_summary,
    bpf_descriptors,
    bpf_wordalign,
    open_capture,
    parse_bpf_records,
)
import c64_test_harness.capture as capture_mod


# ---------------------------------------------------------------------------
# Hand-built records
# ---------------------------------------------------------------------------


def _record(frame: bytes, *, datalen: int | None = None, hdrlen: int = 20,
            pad_to_alignment: bool = True) -> bytes:
    """One ``bpf_hdr`` + payload, laid out the way the kernel emits it.

    ``hdrlen`` is what the kernel *reports* and is the offset of the
    frame from the start of the record; the struct itself is 18 bytes,
    so the default 20 carries two bytes of alignment padding inside the
    header.  ``pad_to_alignment=False`` deliberately omits the trailing
    ``BPF_WORDALIGN`` padding to model a mis-framed buffer.
    """
    if datalen is None:
        datalen = len(frame)
    hdr = struct.pack("<IIIIH", 0x5EAF00D5, 123456, len(frame), datalen, hdrlen)
    assert len(hdr) == BPF_HDR_SIZE == 18
    hdr += b"\0" * (hdrlen - len(hdr))
    rec = hdr + frame
    if pad_to_alignment:
        rec += b"\0" * (bpf_wordalign(len(rec)) - len(rec))
    return rec


FRAME_A = bytes(range(0x10, 0x10 + 62))          # 62 bytes: 62 % 4 == 2
FRAME_B = b"\xDE\xAD\xBE\xEF" + b"\xA5" * 11     # 15 bytes: 15 % 4 == 3


def test_bpf_wordalign_matches_the_macro():
    # BPF_WORDALIGN(x) = ((x)+(4-1)) & ~(4-1)
    assert [bpf_wordalign(n) for n in range(0, 9)] == [0, 4, 4, 4, 4, 8, 8, 8, 8]
    assert bpf_wordalign(82) == 84
    assert bpf_wordalign(35) == 36


def test_single_record_yields_the_captured_frame():
    buf = _record(FRAME_A)
    assert len(buf) == 84  # 20 + 62 = 82 -> aligned 84
    assert parse_bpf_records(buf) == [FRAME_A]


def test_two_records_are_split_at_word_aligned_offsets():
    buf = _record(FRAME_A) + _record(FRAME_B)
    # 84 + (20 + 15 = 35 -> 36) = 120
    assert len(buf) == 120
    frames = parse_bpf_records(buf)
    assert frames == [FRAME_A, FRAME_B], (
        "second record must be read from BPF_WORDALIGN(20+62)=84, "
        f"got {[f[:4].hex() for f in frames]}"
    )


def test_unaligned_packing_is_rejected_not_misparsed():
    """Records glued at ``hdrlen+caplen`` (82) instead of 84 are malformed.

    A correct parser reads the second header from offset 84, which lands
    two bytes into the real header: the ``bh_hdrlen`` field it sees is
    the header's own padding (0), below the 18-byte struct size.  That
    must raise, never return a second "frame".
    """
    buf = _record(FRAME_A, pad_to_alignment=False) + _record(FRAME_B)
    assert len(buf) == 82 + 36
    with pytest.raises(BpfParseError):
        parse_bpf_records(buf)


def test_frame_offset_comes_from_bh_hdrlen_not_a_constant():
    """A kernel may report a longer header; the frame starts where it says."""
    buf = _record(FRAME_B, hdrlen=24)
    assert parse_bpf_records(buf) == [FRAME_B]


def test_caplen_shorter_than_datalen_returns_only_captured_bytes():
    truncated = FRAME_A[:41]  # 41 % 4 == 1
    buf = _record(truncated, datalen=1514)
    assert parse_bpf_records(buf) == [truncated]


def test_record_running_past_the_buffer_raises():
    buf = _record(FRAME_A)[:30]  # header promises 62 bytes, 10 are present
    with pytest.raises(BpfParseError):
        parse_bpf_records(buf)


def test_caplen_larger_than_datalen_raises():
    hdr = struct.pack("<IIIIH", 0, 0, 15, 8, 20) + b"\0\0"
    with pytest.raises(BpfParseError):
        parse_bpf_records(hdr + FRAME_B + b"\0")


def test_empty_buffer_yields_no_frames():
    assert parse_bpf_records(b"") == []


# ---------------------------------------------------------------------------
# CaptureUnavailable carries the remedy
# ---------------------------------------------------------------------------


def test_unsupported_platform_raises_capture_unavailable(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Plan9")
    with pytest.raises(CaptureUnavailable) as ei:
        open_capture("eth0")
    assert "Plan9" in str(ei.value)


def _fake_open_bpf(errnos: dict[int, int], exists: int):
    """``os.open`` stand-in: ``/dev/bpfN`` raises ``errnos[N]`` or ENOENT past *exists*."""
    def _open(path: str, flags: int) -> int:
        n = int(path.rsplit("bpf", 1)[1])
        if n >= exists:
            raise OSError(errno.ENOENT, "No such file or directory", path)
        raise OSError(errnos.get(n, errno.EACCES), "denied", path)
    return _open


def test_bpf_all_nodes_denied_names_the_chmod_remedy(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(capture_mod, "_open_node", _fake_open_bpf({}, exists=4))
    with pytest.raises(CaptureUnavailable) as ei:
        open_capture("feth0")
    msg = str(ei.value)
    assert "sudo chmod o+rw /dev/bpf*" in msg
    assert "EACCES" in msg and "4" in msg
    assert ei.value.remedy == "sudo chmod o+rw /dev/bpf*"


def test_bpf_pool_exhausted_names_holders_and_the_chmod_remedy(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        capture_mod, "_open_node",
        _fake_open_bpf({0: errno.EBUSY, 1: errno.EBUSY, 2: errno.EBUSY, 3: errno.EBUSY}, exists=8),
    )
    with pytest.raises(CaptureUnavailable) as ei:
        open_capture("feth0")
    msg = str(ei.value)
    assert "EBUSY" in msg and "netstat -B" in msg
    assert "sudo chmod o+rw /dev/bpf*" in msg


def test_bpf_no_nodes_at_all_is_reported(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(capture_mod, "_open_node", _fake_open_bpf({}, exists=0))
    with pytest.raises(CaptureUnavailable) as ei:
        open_capture("feth0")
    assert "/dev/bpf" in str(ei.value)


# ---------------------------------------------------------------------------
# netstat -B counters: the disambiguator for a silent wire
# ---------------------------------------------------------------------------
#
# When the TX test sees nothing, VICE's own descriptor says which side is
# at fault: Written=1 on the feth-bound descriptor means the chip handed
# the frame to pcap and our capture is bound to the wrong side/direction;
# Written=0 means the frame died inside the emulated chip.

NETSTAT_B = """\
Device    Netif          Flags              Recv     RDrop    RMatch        RSize   ReadCnt     Bsize     Sblen      Scnt     Hblen      Hcnt      Ccnt        Csize   Written     WDrop Command
bpf0      feth1          --f-IO------      1136      1104      1136        44300         0      4096      4088        17      3883        15         0            0         1         0 .11844
bpf1      ap1            p---IO------         7         0         7          420         0      4096         0         0         0         0         0            0         0         0 x64sc.4326
bpf2      feth0          p---IO------        12         0        12          768         3      4096         0         0         0         0         0            0         1         0 x64sc.4326
"""


def test_bpf_descriptors_parses_device_netif_counters_and_owner(monkeypatch):
    monkeypatch.setattr(capture_mod, "_run_netstat_B", lambda: NETSTAT_B)
    rows = bpf_descriptors()
    assert [r.device for r in rows] == ["bpf0", "bpf1", "bpf2"]
    feth0 = [r for r in rows if r.netif == "feth0"]
    assert feth0 == [BpfDescriptor(device="bpf2", netif="feth0", recv=12, written=1,
                                   command="x64sc", pid=4326)]
    # Command names may be empty (".11844") or contain dots; pid is split from the right.
    assert (rows[0].command, rows[0].pid) == ("", 11844)


def test_bpf_descriptors_filters_by_interface(monkeypatch):
    monkeypatch.setattr(capture_mod, "_run_netstat_B", lambda: NETSTAT_B)
    assert [r.device for r in bpf_descriptors("feth1")] == ["bpf0"]
    assert bpf_descriptors("nosuch") == []


def test_bpf_descriptor_summary_is_one_line_per_descriptor(monkeypatch):
    monkeypatch.setattr(capture_mod, "_run_netstat_B", lambda: NETSTAT_B)
    text = bpf_descriptor_summary("feth0")
    assert text == "netstat -B feth0: bpf2 Recv=12 Written=1 x64sc.4326"
    both = bpf_descriptor_summary()
    assert "bpf0 Recv=1136 Written=1 .11844" in both and "bpf2 Recv=12 Written=1 x64sc.4326" in both


def test_bpf_descriptor_summary_without_netstat_B_is_explicit(monkeypatch):
    monkeypatch.setattr(capture_mod, "_run_netstat_B", lambda: None)
    assert bpf_descriptors() == []
    assert bpf_descriptor_summary("feth0") == "netstat -B unavailable"

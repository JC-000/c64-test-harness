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


# ---------------------------------------------------------------------------
# S2: what the fakes above cannot see -- the ABI and the syscall sequence
# ---------------------------------------------------------------------------


def test_ioctl_request_numbers_match_the_sdk_compiled_values():
    """Derived by compiling ``printf("%#lx", BIOCxxx)`` against
    ``<net/bpf.h>`` on arm64 macOS (Xcode SDK); _IOW('B',108,struct ifreq)
    encodes sizeof(struct ifreq)=32 in bits 16-28, which is why a 16-byte
    ifreq would be a different request number, not merely a short arg."""
    from c64_test_harness.capture import (
        BIOCFLUSH, BIOCGBLEN, BIOCGDLT, BIOCIMMEDIATE, BIOCPROMISC,
        BIOCSETIF, BIOCSHDRCMPLT, BIOCSSEESENT,
    )
    assert (
        BIOCGBLEN, BIOCSETIF, BIOCIMMEDIATE, BIOCSHDRCMPLT,
        BIOCSSEESENT, BIOCPROMISC, BIOCFLUSH, BIOCGDLT,
    ) == (
        0x40044266, 0x8020426C, 0x80044270, 0x80044275,
        0x80044277, 0x20004269, 0x20004268, 0x4004426A,
    )


class _FakeBpfKernel:
    """Records the ioctl sequence and serves two hand-built records on read."""

    FD = 4242

    def __init__(self, records: bytes, dlt: int = 1, blen: int = 4096) -> None:
        self.records = records
        self.dlt = dlt
        self.blen = blen
        self.ioctls: list[tuple[int, int, bytes | None]] = []
        self.reads = 0
        self.writes: list[bytes] = []
        self.closed = False

    def open_node(self, path: str, flags: int) -> int:
        assert path == "/dev/bpf0"
        return self.FD

    def ioctl(self, fd: int, req: int, arg=None):
        assert fd == self.FD
        self.ioctls.append((fd, req, bytes(arg) if arg is not None else None))
        if req == capture_mod.BIOCGBLEN:
            return struct.pack("I", self.blen)
        if req == capture_mod.BIOCGDLT:
            return struct.pack("I", self.dlt)
        return arg if arg is not None else 0

    def read(self, fd: int, n: int) -> bytes:
        assert fd == self.FD and n == self.blen
        self.reads += 1
        return self.records

    def select(self, r, w, x, timeout):
        return (list(r), [], [])

    def write(self, fd: int, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def close(self, fd: int) -> None:
        assert fd == self.FD
        self.closed = True


def _install(monkeypatch, kernel: _FakeBpfKernel) -> None:
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(capture_mod, "_open_node", kernel.open_node)
    monkeypatch.setattr(capture_mod, "_ioctl", kernel.ioctl)
    monkeypatch.setattr(capture_mod, "_read", kernel.read)
    monkeypatch.setattr(capture_mod, "_select", kernel.select)
    monkeypatch.setattr(capture_mod, "_write", kernel.write)
    monkeypatch.setattr(capture_mod, "_close_node", kernel.close)


IPV4_FRAME = b"\x01\x00\x5e\x00\x00\xfb" + b"\x02\xc6\x40\x00\x00\x09" + b"\x08\x00" + b"\x45" * 47  # 61 B
TEST_FRAME = b"\xff" * 6 + b"\x00\x00\x00\x00\x00\x01" + b"\x88\xb5" + bytes([0xC6, 0x40] * 25)  # 64 B


def test_bpf_capture_issues_the_setup_ioctls_with_a_32_byte_ifreq(monkeypatch):
    kernel = _FakeBpfKernel(b"")
    _install(monkeypatch, kernel)

    cap = open_capture("feth0")
    reqs = [req for _, req, _ in kernel.ioctls]
    assert reqs.count(capture_mod.BIOCSETIF) == 1
    setif_arg = next(arg for _, req, arg in kernel.ioctls if req == capture_mod.BIOCSETIF)
    assert len(setif_arg) == 32, "struct ifreq is 32 bytes on Darwin"
    assert setif_arg[:16] == b"feth0".ljust(16, b"\0")
    for required in (capture_mod.BIOCIMMEDIATE, capture_mod.BIOCSHDRCMPLT,
                     capture_mod.BIOCSSEESENT, capture_mod.BIOCPROMISC, capture_mod.BIOCGDLT):
        assert required in reqs, f"missing ioctl {required:#x}"
    # Promiscuous mode is per-interface: it must follow the bind.
    assert reqs.index(capture_mod.BIOCPROMISC) > reqs.index(capture_mod.BIOCSETIF)
    # Immediate/hdrcmplt/seesent are u_int 1.
    for req in (capture_mod.BIOCIMMEDIATE, capture_mod.BIOCSHDRCMPLT, capture_mod.BIOCSSEESENT):
        assert next(arg for _, r, arg in kernel.ioctls if r == req) == struct.pack("I", 1)
    assert cap.buflen == 4096 and cap.dlt == 1
    cap.close()
    assert kernel.closed


def test_bpf_capture_recv_returns_the_matching_frame_not_the_buffer(monkeypatch):
    kernel = _FakeBpfKernel(_record(IPV4_FRAME) + _record(TEST_FRAME))
    _install(monkeypatch, kernel)

    with open_capture("feth0") as cap:
        got = cap.recv(1.0, match=lambda f: f[12:14] == b"\x88\xb5")
    assert got == TEST_FRAME
    assert kernel.reads == 1  # both records came from one read; the first was skipped, not re-read


def test_bpf_capture_recv_without_match_returns_the_first_frame(monkeypatch):
    kernel = _FakeBpfKernel(_record(IPV4_FRAME) + _record(TEST_FRAME))
    _install(monkeypatch, kernel)
    with open_capture("feth0") as cap:
        assert cap.recv(1.0) == IPV4_FRAME
        assert cap.recv(1.0) == TEST_FRAME  # second record served from the pending queue


def test_bpf_capture_rejects_a_non_ethernet_dlt(monkeypatch):
    kernel = _FakeBpfKernel(b"", dlt=0)  # DLT_NULL, e.g. lo0
    _install(monkeypatch, kernel)
    with pytest.raises(CaptureUnavailable) as ei:
        open_capture("lo0")
    assert "DLT 0" in str(ei.value)
    assert kernel.closed, "the node must be released on a failed setup"


def test_bpf_capture_send_writes_the_frame_verbatim(monkeypatch):
    kernel = _FakeBpfKernel(b"")
    _install(monkeypatch, kernel)
    with open_capture("feth0") as cap:
        cap.send(TEST_FRAME)
    assert kernel.writes == [TEST_FRAME]


class _FakeSocket:
    instances: list["_FakeSocket"] = []

    def __init__(self, family, type_, proto):
        self.args = (family, type_, proto)
        self.bound = None
        self.timeouts: list[float] = []
        self.inbox: list[bytes] = []
        self.sent: list[bytes] = []
        self.closed = False
        _FakeSocket.instances.append(self)

    def bind(self, addr):
        self.bound = addr

    def settimeout(self, t):
        self.timeouts.append(t)

    def recv(self, n):
        if not self.inbox:
            raise _FakeSocketModule.timeout()
        return self.inbox.pop(0)

    def send(self, data):
        self.sent.append(bytes(data))
        return len(data)

    def close(self):
        self.closed = True


class _FakeSocketModule:
    """Enough of the ``socket`` module for AfPacketCapture, on a host without AF_PACKET."""

    AF_PACKET = 17
    SOCK_RAW = 3

    class timeout(OSError):
        pass

    socket = _FakeSocket

    @staticmethod
    def htons(x: int) -> int:
        import socket as real
        return real.htons(x)


def test_af_packet_capture_opens_eth_p_all_and_binds_the_interface(monkeypatch):
    import socket as real_socket
    _FakeSocket.instances.clear()
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(capture_mod, "socket", _FakeSocketModule)

    with open_capture("tap-c64-0") as cap:
        sock = _FakeSocket.instances[-1]
        assert sock.args == (17, 3, real_socket.htons(0x0003)), (
            "ETH_P_ALL (0x0003), not a single ethertype: TX must see our 0x88b5 frame"
        )
        assert sock.bound == ("tap-c64-0", 0)
        sock.inbox[:] = [IPV4_FRAME, TEST_FRAME]
        assert cap.recv(1.0, match=lambda f: f[12:14] == b"\x88\xb5") == TEST_FRAME
        cap.send(TEST_FRAME)
        assert sock.sent == [TEST_FRAME]
    assert sock.closed


# ---------------------------------------------------------------------------
# S4: one open per test, no separate probe cycle
# ---------------------------------------------------------------------------


def test_no_separate_availability_probe_exists():
    """The fixture opens the capture once and acts on its exception.  A
    probe-then-open pair costs two full BPF setup cycles per test and can
    disagree with itself when the pool changes in between."""
    assert not hasattr(capture_mod, "capture_unavailable_reason")
    assert "capture_unavailable_reason" not in capture_mod.__all__


# ---------------------------------------------------------------------------
# S3: genuine absence (skip) versus a present-but-broken path (fail)
# ---------------------------------------------------------------------------
#
# Issue #158 was hidden for months behind a skip that looked like absence.
# The only conditions that are absence are: no node this process may open
# (every node EACCES, or none exist), no CAP_NET_RAW on Linux, no backend
# for the platform.  Everything else -- writable nodes all EBUSY (pool
# eaten; chmod o+rw opens bpf4-7, which exist), BIOCSETIF failing on an
# interface first_available_ethernet_iface() just found, a feth with a
# non-ethernet DLT, a Linux bind failure -- is a broken path and must fail.


def _cause_of(fn) -> tuple[str, bool]:
    with pytest.raises(CaptureUnavailable) as ei:
        fn()
    return ei.value.cause, ei.value.genuinely_absent


def test_all_nodes_denied_is_genuine_absence(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(capture_mod, "_open_node", _fake_open_bpf({}, exists=4))
    assert _cause_of(lambda: open_capture("feth0")) == ("denied", True)


def test_no_nodes_is_genuine_absence(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(capture_mod, "_open_node", _fake_open_bpf({}, exists=0))
    assert _cause_of(lambda: open_capture("feth0")) == ("no-nodes", True)


def test_busy_writable_nodes_with_root_only_rest_is_not_absence(monkeypatch):
    """The bench shape with one VICE up: bpf0-3 EBUSY, bpf4-7 EACCES."""
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        capture_mod, "_open_node",
        _fake_open_bpf({0: errno.EBUSY, 1: errno.EBUSY, 2: errno.EBUSY, 3: errno.EBUSY}, exists=8),
    )
    assert _cause_of(lambda: open_capture("feth0")) == ("busy", False)


def test_every_node_busy_is_not_absence(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        capture_mod, "_open_node",
        _fake_open_bpf({n: errno.EBUSY for n in range(4)}, exists=4),
    )
    assert _cause_of(lambda: open_capture("feth0")) == ("busy", False)


def test_biocsetif_failure_is_not_absence(monkeypatch):
    kernel = _FakeBpfKernel(b"")
    real_ioctl = kernel.ioctl

    def failing_setif(fd, req, arg=None):
        if req == capture_mod.BIOCSETIF:
            raise OSError(errno.ENXIO, "Device not configured")
        return real_ioctl(fd, req, arg)

    _install(monkeypatch, kernel)
    monkeypatch.setattr(capture_mod, "_ioctl", failing_setif)
    assert _cause_of(lambda: open_capture("feth0")) == ("bind", False)
    assert kernel.closed


def test_non_ethernet_dlt_is_not_absence(monkeypatch):
    _install(monkeypatch, _FakeBpfKernel(b"", dlt=0))
    assert _cause_of(lambda: open_capture("feth0")) == ("dlt", False)


def test_unsupported_platform_is_genuine_absence(monkeypatch):
    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Plan9")
    assert _cause_of(lambda: open_capture("eth0")) == ("platform", True)


def test_linux_missing_cap_net_raw_is_genuine_absence(monkeypatch):
    class Denied(_FakeSocketModule):
        class socket:  # noqa: N801 - stands in for socket.socket
            def __init__(self, *a):
                raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(capture_mod, "socket", Denied)
    cause, absent = _cause_of(lambda: open_capture("tap-c64-0"))
    assert (cause, absent) == ("cap-net-raw", True)


def test_linux_bind_failure_is_not_absence(monkeypatch):
    class BindFails(_FakeSocketModule):
        class socket(_FakeSocket):
            def bind(self, addr):
                raise OSError(errno.ENODEV, "No such device")

    monkeypatch.setattr(capture_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(capture_mod, "socket", BindFails)
    assert _cause_of(lambda: open_capture("tap-c64-9")) == ("linux-bind", False)


def test_unknown_cause_is_treated_as_present_but_broken():
    exc = CaptureUnavailable("something new")
    assert exc.cause == "unknown" and exc.genuinely_absent is False

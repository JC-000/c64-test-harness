"""Host-side raw ethernet capture and injection, selected by platform.

The ethernet tests need to see the frames a C64 program puts on the wire
and to put frames on the wire for it to receive.  Linux does that with an
``AF_PACKET`` socket; macOS has no such family, so until this module
existed the two TX/RX tests skipped there and the primary bench had no
host-side proof that emitted frames reach the wire at all (issue #158).

:func:`open_capture` returns a :class:`PacketCapture` for the platform:

* Linux -- :class:`AfPacketCapture`, an ``AF_PACKET``/``SOCK_RAW`` socket
  bound to the interface.  Needs ``CAP_NET_RAW`` (root).
* macOS -- :class:`BpfCapture`, a ``/dev/bpf*`` node bound with
  ``BIOCSETIF``, in immediate + promiscuous mode with
  ``BIOCSHDRCMPLT`` so injected source MACs are left alone.  Needs a
  node the process can open: macOS ships ``/dev/bpf0-3`` root-only and
  creates further nodes on demand *only for root*, so the unelevated
  path relies on ``sudo chmod o+rw /dev/bpf*`` having been run (the
  mode does not survive a reboot).  VICE, which the harness runs as
  root, takes the lowest two free nodes per instance -- the very ones
  the chmod opened -- so a bench with one VICE up typically has one
  node left for this module.

When no path is usable, :func:`open_capture` raises
:class:`CaptureUnavailable` whose message names the remedy verbatim.
Callers that want to skip a test should skip *with that message*, so the
operator reads the fix and not a paraphrase.

The BPF read buffer format is parsed by :func:`parse_bpf_records`, kept
pure so it can be pinned without a device (``tests/test_capture.py``).
"""

from __future__ import annotations

import errno
import os
import platform
import select
import shutil
import socket
import struct
import subprocess
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

try:  # POSIX only; absent on Windows, where no backend exists anyway
    import fcntl as _fcntl
except ImportError:  # pragma: no cover
    _fcntl = None

__all__ = [
    "BPF_HDR_SIZE",
    "AfPacketCapture",
    "BpfCapture",
    "BpfDescriptor",
    "BpfParseError",
    "CaptureTimeout",
    "CaptureUnavailable",
    "GENUINELY_ABSENT_CAUSES",
    "PacketCapture",
    "bpf_descriptor_summary",
    "bpf_descriptors",
    "bpf_wordalign",
    "open_capture",
    "parse_bpf_records",
]

#: Largest ethernet frame we expect to hand back (1500 MTU + 14 header + 4 FCS).
MAX_FRAME = 1518


#: ``CaptureUnavailable.cause`` values that mean the capability is genuinely
#: absent on this host -- the only cases a test may *skip* on.  Everything
#: else is a path that exists and is broken, which must fail.
GENUINELY_ABSENT_CAUSES = frozenset({
    "denied",       # every /dev/bpf* node is EACCES: nothing this uid may open
    "no-nodes",     # no /dev/bpf* exists at all
    "cap-net-raw",  # Linux: AF_PACKET refused for lack of CAP_NET_RAW
    "platform",     # no backend for this OS
})


class CaptureUnavailable(RuntimeError):
    """No host-side capture path is usable on this host for this interface.

    ``remedy`` is the exact command (or ``None``) that would make it
    usable; ``str(exc)`` already includes it.  ``cause`` is a short tag
    for *why* (see :data:`GENUINELY_ABSENT_CAUSES`); ``genuinely_absent``
    is the skip-vs-fail verdict: True only when nothing on this host could
    have captured, False when a path exists and something is wrong with
    it -- a pool eaten by other holders, a bind that failed on an
    interface that exists, a non-ethernet DLT, or a cause nobody has
    classified yet (treated as broken, never as absent).
    """

    def __init__(
        self,
        message: str,
        *,
        remedy: str | None = None,
        cause: str = "unknown",
    ) -> None:
        if remedy:
            message = f"{message} Remedy: {remedy}"
        super().__init__(message)
        self.remedy = remedy
        self.cause = cause

    @property
    def genuinely_absent(self) -> bool:
        return self.cause in GENUINELY_ABSENT_CAUSES


class CaptureTimeout(TimeoutError):
    """No (matching) frame arrived within the deadline.

    ``seen`` counts frames that arrived but did not satisfy ``match`` --
    the difference between a silent interface and a busy one carrying
    the wrong traffic.
    """

    def __init__(self, message: str, *, seen: int = 0) -> None:
        super().__init__(message)
        self.seen = seen


class BpfParseError(ValueError):
    """A ``/dev/bpf`` read buffer is not a well-formed run of records."""


@runtime_checkable
class PacketCapture(Protocol):
    """A bound, open capture on one host interface.

    Frames are whole ethernet frames (dst, src, ethertype, payload).
    """

    iface: str

    def recv(
        self,
        timeout: float,
        *,
        match: Callable[[bytes], bool] | None = None,
    ) -> bytes:
        """Return the next frame (for which ``match`` is true) or raise
        :class:`CaptureTimeout`."""
        ...

    def send(self, frame: bytes) -> None:
        """Inject *frame* on the interface as-is (source MAC untouched)."""
        ...

    def close(self) -> None: ...

    def __enter__(self) -> "PacketCapture": ...

    def __exit__(self, *exc) -> None: ...


# ---------------------------------------------------------------------------
# BPF buffer format (pure)
# ---------------------------------------------------------------------------

#: ``sizeof(struct bpf_hdr)`` with a ``timeval32`` timestamp: 4+4+4+4+2.
BPF_HDR_SIZE = 18
_BPF_HDR = struct.Struct("<IIIIH")
_BPF_ALIGNMENT = 4


def bpf_wordalign(n: int) -> int:
    """``BPF_WORDALIGN(x)`` from ``<net/bpf.h>``: round up to 4."""
    return (n + (_BPF_ALIGNMENT - 1)) & ~(_BPF_ALIGNMENT - 1)


def parse_bpf_records(buf: bytes) -> list[bytes]:
    """Split one ``read(2)`` result from ``/dev/bpf*`` into captured frames.

    Each record is a ``struct bpf_hdr`` whose ``bh_hdrlen`` says where the
    frame starts and ``bh_caplen`` how many bytes of it were captured;
    the next record begins at ``BPF_WORDALIGN(hdrlen + caplen)``.  Any
    inconsistency -- a header shorter than the struct, a capture longer
    than the original frame, a record running past the buffer, trailing
    bytes too short for a header -- raises :class:`BpfParseError` rather
    than yielding a frame assembled from the wrong bytes.
    """
    frames: list[bytes] = []
    off = 0
    n = len(buf)
    while off < n:
        if n - off < BPF_HDR_SIZE:
            raise BpfParseError(
                f"{n - off} trailing byte(s) at offset {off}: shorter than a bpf_hdr"
            )
        _sec, _usec, caplen, datalen, hdrlen = _BPF_HDR.unpack_from(buf, off)
        if hdrlen < BPF_HDR_SIZE:
            raise BpfParseError(
                f"bh_hdrlen={hdrlen} at offset {off} is below sizeof(bpf_hdr)={BPF_HDR_SIZE}; "
                "record boundary is not word-aligned or the buffer is corrupt"
            )
        if caplen > datalen:
            raise BpfParseError(
                f"bh_caplen={caplen} exceeds bh_datalen={datalen} at offset {off}"
            )
        start = off + hdrlen
        end = start + caplen
        if end > n:
            raise BpfParseError(
                f"record at offset {off} claims {hdrlen}+{caplen} bytes but only "
                f"{n - off} remain"
            )
        frames.append(bytes(buf[start:end]))
        off += bpf_wordalign(hdrlen + caplen)
    return frames


# ---------------------------------------------------------------------------
# macOS: /dev/bpf*
# ---------------------------------------------------------------------------

# <sys/ioccom.h> encoding.  Lengths are the sizeof() of the argument type
# on a 64-bit Darwin: u_int 4, struct ifreq 32, struct timeval 16.
_IOC_VOID = 0x20000000
_IOC_OUT = 0x40000000
_IOC_IN = 0x80000000


def _ioc(inout: int, group: str, num: int, length: int) -> int:
    return inout | ((length & 0x1FFF) << 16) | (ord(group) << 8) | num


BIOCGBLEN = _ioc(_IOC_OUT, "B", 102, 4)
BIOCFLUSH = _ioc(_IOC_VOID, "B", 104, 0)
BIOCPROMISC = _ioc(_IOC_VOID, "B", 105, 0)
BIOCGDLT = _ioc(_IOC_OUT, "B", 106, 4)
BIOCSETIF = _ioc(_IOC_IN, "B", 108, 32)
BIOCIMMEDIATE = _ioc(_IOC_IN, "B", 112, 4)
BIOCSHDRCMPLT = _ioc(_IOC_IN, "B", 117, 4)
BIOCSSEESENT = _ioc(_IOC_IN, "B", 119, 4)

DLT_EN10MB = 1
_IFNAMSIZ = 16

#: Highest ``/dev/bpfN`` we will probe.  macOS creates nodes on demand
#: for root up to ``debug.bpf_maxdevices`` (256); an unprivileged process
#: stops at the first ENOENT anyway.
_BPF_MAX_NODES = 256

_CHMOD_REMEDY = "sudo chmod o+rw /dev/bpf*"


# The BPF backend reaches the kernel only through these names, so a unit
# test can stand in a fake kernel and check the exact ioctl sequence and
# arguments without a device (tests/test_capture.py).


def _open_node(path: str, flags: int) -> int:
    """``os.open`` behind a name so tests can substitute node behaviour."""
    return os.open(path, flags)


def _close_node(fd: int) -> None:
    os.close(fd)


def _ioctl(fd: int, request: int, arg=None):
    if _fcntl is None:  # pragma: no cover
        raise CaptureUnavailable("fcntl is unavailable on this platform")
    if arg is None:
        return _fcntl.ioctl(fd, request)
    return _fcntl.ioctl(fd, request, arg)


def _read(fd: int, n: int) -> bytes:
    return os.read(fd, n)


def _write(fd: int, data: bytes) -> int:
    return os.write(fd, data)


def _select(rlist, wlist, xlist, timeout):
    return select.select(rlist, wlist, xlist, timeout)


def _open_first_bpf() -> tuple[int, str]:
    """Open the lowest ``/dev/bpfN`` this process may, or raise with the remedy."""
    failures: dict[str, int] = {}
    for n in range(_BPF_MAX_NODES):
        path = f"/dev/bpf{n}"
        try:
            fd = _open_node(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as e:
            if e.errno == errno.ENOENT:
                break
            failures[path] = e.errno
            continue
        return fd, path
    if not failures:
        raise CaptureUnavailable(
            "no /dev/bpf* nodes exist on this host (BPF is compiled out or "
            "devfs is not exposing it); host-side capture is impossible.",
            cause="no-nodes",
        )
    counts: dict[str, int] = {}
    for eno in failures.values():
        name = errno.errorcode.get(eno, str(eno))
        counts[name] = counts.get(name, 0) + 1
    summary = ", ".join(f"{k} x{v}" for k, v in sorted(counts.items()))
    busy = [p for p, eno in failures.items() if eno == errno.EBUSY]
    denied = [p for p, eno in failures.items() if eno == errno.EACCES]
    detail = (
        f"could not open any of {len(failures)} /dev/bpf* node(s) ({summary})."
    )
    if busy:
        detail += (
            f" {len(busy)} node(s) are held by other captures (`netstat -B` "
            "lists holders; a root VICE takes two per instance)."
        )
    if denied:
        detail += (
            f" {len(denied)} node(s) are root-only, and macOS creates further "
            "nodes only for root; the chmod mode does not survive a reboot."
        )
    # Any EBUSY means a node this uid may open exists and is merely held:
    # a present path, not an absent one.  Only "every node denied" is absence.
    raise CaptureUnavailable(
        detail, remedy=_CHMOD_REMEDY, cause="busy" if busy else "denied"
    )


class BpfCapture:
    """Capture on and inject into one interface through ``/dev/bpf*`` (macOS)."""

    def __init__(self, iface: str) -> None:
        self.iface = iface
        self._fd, self.node = _open_first_bpf()
        self._pending: deque[bytes] = deque()
        try:
            self.buflen = struct.unpack(
                "I", _ioctl(self._fd, BIOCGBLEN, struct.pack("I", 0))
            )[0]
            # Deliver each packet as it arrives instead of waiting for the
            # buffer to fill or the read timeout to elapse.
            _ioctl(self._fd, BIOCIMMEDIATE, struct.pack("I", 1))
            # Leave the source MAC of frames we write() untouched.
            _ioctl(self._fd, BIOCSHDRCMPLT, struct.pack("I", 1))
            # Report frames the host itself transmits on the interface too
            # (the default, made explicit: a VICE pcap_inject on this feth
            # is an *outgoing* frame from the interface's point of view).
            _ioctl(self._fd, BIOCSSEESENT, struct.pack("I", 1))
            # struct ifreq: char ifr_name[IFNAMSIZ=16] + a 16-byte union.
            ifr = iface.encode().ljust(_IFNAMSIZ, b"\0") + b"\0" * 16
            try:
                _ioctl(self._fd, BIOCSETIF, ifr)
            except OSError as e:
                raise CaptureUnavailable(
                    f"BIOCSETIF {iface!r} on {self.node} failed: "
                    f"{errno.errorcode.get(e.errno, e.errno)} {e.strerror}"
                    + (" (interface does not exist)" if e.errno == errno.ENXIO else ""),
                    cause="bind",
                ) from e
            self.dlt = struct.unpack(
                "I", _ioctl(self._fd, BIOCGDLT, struct.pack("I", 0))
            )[0]
            if self.dlt != DLT_EN10MB:
                raise CaptureUnavailable(
                    f"{iface} is not an ethernet interface (DLT {self.dlt}, "
                    f"expected DLT_EN10MB={DLT_EN10MB})",
                    cause="dlt",
                )
            # Promiscuous mode is a property of the bound interface: after SETIF.
            _ioctl(self._fd, BIOCPROMISC)
            _ioctl(self._fd, BIOCFLUSH)
        except Exception:
            _close_node(self._fd)
            self._fd = -1
            raise

    # -- PacketCapture --------------------------------------------------

    def recv(
        self,
        timeout: float,
        *,
        match: Callable[[bytes], bool] | None = None,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            while self._pending:
                frame = self._pending.popleft()
                seen += 1
                if match is None or match(frame):
                    return frame
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureTimeout(
                    f"no {'matching ' if match else ''}frame on {self.iface} "
                    f"({self.node}) within {timeout:.1f}s"
                    + (f"; {seen} non-matching frame(s) seen" if seen else ""),
                    seen=seen,
                )
            readable, _, _ = _select([self._fd], [], [], remaining)
            if not readable:
                continue
            try:
                buf = _read(self._fd, self.buflen)
            except BlockingIOError:
                continue
            self._pending.extend(parse_bpf_records(buf))

    def send(self, frame: bytes) -> None:
        written = _write(self._fd, frame)
        if written != len(frame):
            raise OSError(
                f"short write to {self.node}: {written} of {len(frame)} bytes"
            )

    def close(self) -> None:
        if self._fd >= 0:
            _close_node(self._fd)
            self._fd = -1

    def __enter__(self) -> "BpfCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Linux: AF_PACKET
# ---------------------------------------------------------------------------

_ETH_P_ALL = 0x0003


class AfPacketCapture:
    """Capture on and inject into one interface through ``AF_PACKET`` (Linux)."""

    def __init__(self, iface: str) -> None:
        self.iface = iface
        af_packet = getattr(socket, "AF_PACKET", None)
        if af_packet is None:
            raise CaptureUnavailable(
                "socket.AF_PACKET does not exist on this platform", cause="platform"
            )
        try:
            self._sock = socket.socket(af_packet, socket.SOCK_RAW, socket.htons(_ETH_P_ALL))
        except PermissionError as e:
            raise CaptureUnavailable(
                f"AF_PACKET socket refused ({e.strerror}); needs CAP_NET_RAW.",
                remedy="run the tests as root, or grant the interpreter "
                "`sudo setcap cap_net_raw+ep $(readlink -f $(command -v python3))`",
                cause="cap-net-raw",
            ) from e
        try:
            self._sock.bind((iface, 0))
        except OSError as e:
            self._sock.close()
            raise CaptureUnavailable(
                f"AF_PACKET bind to {iface!r} failed: {e.strerror}", cause="linux-bind"
            ) from e

    def recv(
        self,
        timeout: float,
        *,
        match: Callable[[bytes], bool] | None = None,
    ) -> bytes:
        deadline = time.monotonic() + timeout
        seen = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CaptureTimeout(
                    f"no {'matching ' if match else ''}frame on {self.iface} "
                    f"within {timeout:.1f}s"
                    + (f"; {seen} non-matching frame(s) seen" if seen else ""),
                    seen=seen,
                )
            self._sock.settimeout(remaining)
            try:
                frame = self._sock.recv(MAX_FRAME)
            except socket.timeout:
                continue
            seen += 1
            if match is None or match(frame):
                return frame

    def send(self, frame: bytes) -> None:
        self._sock.send(frame)

    def close(self) -> None:
        self._sock.close()

    def __enter__(self) -> "AfPacketCapture":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# ---------------------------------------------------------------------------
# netstat -B: who else holds a BPF descriptor, and what it has moved
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BpfDescriptor:
    """One row of ``netstat -B``: a BPF descriptor, its interface, its counters."""

    device: str
    netif: str
    recv: int
    written: int
    command: str
    pid: int


def _run_netstat_B() -> str | None:
    """``netstat -B`` stdout, or ``None`` where it does not exist (Linux) or fails."""
    if shutil.which("netstat") is None:
        return None
    try:
        out = subprocess.run(
            ["netstat", "-B"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or "Device" not in out.stdout:
        return None
    return out.stdout


def bpf_descriptors(iface: str | None = None) -> list[BpfDescriptor]:
    """Every BPF descriptor on the host (optionally only those bound to *iface*).

    Needs no privilege and reads root-owned holders, so it sees the
    descriptors an elevated VICE holds.  ``Written`` on VICE's feth-bound
    descriptor is the fact that separates a direction fault from a chip
    fault when a TX test sees nothing: 1 means the chip handed the frame
    to pcap and the harness capture was bound to the wrong side; 0 means
    the frame died inside the emulated CS8900a.
    """
    text = _run_netstat_B()
    if text is None:
        return []
    lines = text.splitlines()
    header = next((l.split() for l in lines if l.split()[:1] == ["Device"]), None)
    if header is None or "Recv" not in header or "Written" not in header:
        return []
    i_recv, i_written = header.index("Recv"), header.index("Written")
    rows: list[BpfDescriptor] = []
    for line in lines:
        fields = line.split()
        if len(fields) < len(header) or fields[0] == "Device":
            continue
        if iface is not None and fields[1] != iface:
            continue
        command, _, pid = fields[-1].rpartition(".")
        try:
            rows.append(BpfDescriptor(
                device=fields[0], netif=fields[1],
                recv=int(fields[i_recv]), written=int(fields[i_written]),
                command=command, pid=int(pid),
            ))
        except ValueError:
            continue
    return rows


def bpf_descriptor_summary(iface: str | None = None) -> str:
    """One line for a failure message: ``netstat -B <iface>: bpf2 Recv=12 Written=1 x64sc.4326``."""
    if _run_netstat_B() is None:
        return "netstat -B unavailable"
    rows = bpf_descriptors(iface)
    label = f"netstat -B {iface}" if iface else "netstat -B"
    if not rows:
        return f"{label}: no BPF descriptors bound"
    return f"{label}: " + "; ".join(
        f"{r.device} Recv={r.recv} Written={r.written} {r.command}.{r.pid}" for r in rows
    )


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def open_capture(iface: str) -> PacketCapture:
    """Open the platform's capture path on *iface* or raise :class:`CaptureUnavailable`."""
    system = platform.system()
    if system == "Linux":
        return AfPacketCapture(iface)
    if system == "Darwin":
        return BpfCapture(iface)
    raise CaptureUnavailable(
        f"no host-side packet capture implementation for {system}", cause="platform"
    )


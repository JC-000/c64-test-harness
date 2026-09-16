"""Which local interface a U64 stream capture joins its multicast group on.

Private: shared by :class:`~.u64_audio_capture.AudioCapture`,
:class:`~.u64_debug_capture.DebugCapture` and
:class:`~.u64_video_capture.VideoCapture`.

All three used to join with ``inet_aton("0.0.0.0")`` (INADDR_ANY) and no way
to say otherwise, which leaves the interface to the kernel's route for the
group address.  On this bench ``route -n get 239.0.1.65`` resolves via
**utun0** (the VPN), not the interface that reaches the device, so the join
landed on an interface the device's traffic never arrives on (#399).

**Fixing the interface is not known to make multicast capture work.**  #399's
measured table is zero packets in every arm, including a raw socket joined on
the en0 address that routes to the device (n=3 paired, interleaved, U64E
10.43.23.81, fw bce4535e, 2026-09-15).  The remaining candidates -- the
device's multicast TTL or egress, switch IGMP snooping -- are unmeasured, and
there is no capture-level evidence because tcpdump needs root.  This module
removes a latent defect in what the harness asks for; it establishes nothing
about delivery.
"""
from __future__ import annotations

import logging
import socket
import struct

_log = logging.getLogger(__name__)

#: The kernel-picks-it default, which is what every capture sent before #399.
INADDR_ANY = "0.0.0.0"


def local_address_towards(host: str, port: int = 80) -> str:
    """The local address the OS would use to reach *host*.

    A UDP ``connect`` only selects a route: it puts nothing on the wire, so
    this is safe against a device that must not be touched.  Same trick
    ``render_wav_u64._detect_local_ip`` and ``ultimate64.capture_frame``
    already use.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((host, port))
        return str(sock.getsockname()[0])


def validate_request(
    multicast_group: str | None,
    multicast_interface: str | None,
    device_host: str | None,
) -> None:
    """Refuse an interface request that can never take effect.

    Called from the captures' ``__init__`` so a typo raises where the caller
    wrote it, not inside a capture thread at ``start()`` time.
    """
    if multicast_interface is None and device_host is None:
        return
    if not multicast_group:
        named = (
            "multicast_interface" if multicast_interface is not None
            else "device_host"
        )
        raise ValueError(
            f"{named}= names the interface for a multicast join, but no "
            "multicast_group= was given, so there is no join to steer"
        )
    if multicast_interface is not None:
        try:
            socket.inet_aton(multicast_interface)
        except OSError as exc:
            raise ValueError(
                f"multicast_interface={multicast_interface!r} is not a dotted "
                f"IPv4 address of a local interface: {exc}"
            ) from exc


def resolve_interface(
    multicast_interface: str | None, device_host: str | None
) -> str:
    """The interface address to join on.

    An explicit ``multicast_interface`` wins; otherwise the local address
    towards ``device_host``; otherwise INADDR_ANY, which is the pre-#399
    behaviour and stays the default for callers that name neither.
    """
    if multicast_interface is not None:
        return multicast_interface
    if device_host is None:
        return INADDR_ANY
    try:
        return local_address_towards(device_host)
    except OSError as exc:
        _log.warning(
            "Cannot resolve the local address towards %s (%s); joining the "
            "multicast group on INADDR_ANY, so the kernel picks the "
            "interface -- on a host with a VPN up that is often not the one "
            "the device reaches (#399)",
            device_host, exc,
        )
        return INADDR_ANY


def join_group(
    sock: socket.socket,
    group: str,
    multicast_interface: str | None = None,
    device_host: str | None = None,
) -> str:
    """``IP_ADD_MEMBERSHIP`` for *group*; returns the interface joined on."""
    interface = resolve_interface(multicast_interface, device_host)
    mreq = struct.pack(
        "4s4s", socket.inet_aton(group), socket.inet_aton(interface)
    )
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    _log.info("Joined multicast group %s on interface %s", group, interface)
    return interface

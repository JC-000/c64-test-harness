"""Which local interface a U64 stream capture joins its multicast group on.

Private: shared by :class:`~.u64_audio_capture.AudioCapture`,
:class:`~.u64_debug_capture.DebugCapture` and
:class:`~.u64_video_capture.VideoCapture`.

All three used to join with ``inet_aton("0.0.0.0")`` (INADDR_ANY) and no way
to say otherwise, which leaves the interface to the kernel's route for the
group address.  On this bench ``route -n get 239.0.1.65`` resolves via
**utun0** (the VPN), not the interface that reaches the device, so the join
landed on an interface the device's traffic never arrives on (#399).

**A join towards the device receives the group; the default does not**
(U64E, fw bce4535e, 2026-09-23, host wired on the device's LAN, n=3 per arm).
All three capture classes received the stream when joined via
``device_host=``.  The INADDR_ANY default received 0 in 6/6 arms while the
same frames were on the wire, because the kernel routed 224/4 via the VPN.
Over Wi-Fi, no join received anything, because the frames never reached the
host's interface (#461).  The default stays INADDR_ANY because a capture
does not know the device.  When the kernel's route for the group is a
tunnel, the join logs a WARNING naming it (owner decision, #399).
"""
from __future__ import annotations

import logging
import platform
import re
import socket
import struct
import subprocess

_log = logging.getLogger(__name__)

#: The kernel-picks-it default, which is what every capture sent before #399.
INADDR_ANY = "0.0.0.0"

#: Interface-name prefixes a default join warns about: a tunnel is not where
#: a device on the LAN sends its group traffic.  ``utun`` is macOS's VPN
#: interface, the one this bench's 224/4 route used (#399); the rest are the
#: Linux tun, WireGuard, Tailscale, ZeroTier, PPP and IPsec families.
TUNNEL_INTERFACE_PREFIXES = ("utun", "tun", "wg", "tailscale", "zt", "ppp", "ipsec")

#: Seconds a route lookup may take before it is given up (it is advisory).
_ROUTE_LOOKUP_TIMEOUT = 2.0


def multicast_route_interface(group: str) -> str | None:
    """Name of the interface the kernel routes *group* through, or ``None``.

    ``route -n get`` on macOS, ``ip -o route get`` elsewhere.  Reads the
    routing table only; nothing goes on the wire.  Any failure -- no such
    binary, a timeout, a non-zero exit, output that names no interface --
    returns ``None``: the lookup only decides whether to warn.
    """
    if platform.system() == "Darwin":
        argv, pattern = ["route", "-n", "get", group], r"^\s*interface:\s*(\S+)"
    else:
        argv, pattern = ["ip", "-o", "route", "get", group], r"\bdev\s+(\S+)"
    try:
        out = subprocess.run(
            argv, capture_output=True, text=True, timeout=_ROUTE_LOOKUP_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log.debug("Route lookup for %s failed: %s", group, exc)
        return None
    if out.returncode != 0:
        return None
    match = re.search(pattern, out.stdout or "", re.MULTILINE)
    return match.group(1) if match else None


def _warn_if_default_join_uses_a_tunnel(group: str) -> None:
    """The INADDR_ANY join lands on the kernel's route; say so if it is a tunnel."""
    iface = multicast_route_interface(group)
    if iface is not None and iface.startswith(TUNNEL_INTERFACE_PREFIXES):
        _log.warning(
            "Joining multicast group %s on INADDR_ANY: the kernel routes it "
            "via %s, a tunnel, so a device on the LAN is unlikely to reach "
            "this join.  Pass device_host= (or multicast_interface=) to join "
            "on the interface towards the device (#399)",
            group, iface,
        )


def local_address_towards(host: str, port: int = 80) -> str:
    """The local address the OS would use to reach *host*.

    A UDP ``connect`` only selects a route, so for a dotted address this
    puts nothing on the wire and is safe against a device that must not be
    touched.  A *hostname* is resolved first, which is DNS traffic and can
    block; a resolution failure raises :class:`socket.gaierror`, a subclass
    of ``OSError``, so :func:`resolve_interface` falls back to INADDR_ANY
    with a WARNING.  Same trick ``render_wav_u64._detect_local_ip`` and
    ``ultimate64.capture_frame`` already use.
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
    if interface == INADDR_ANY:
        _warn_if_default_join_uses_a_tunnel(group)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    _log.info("Joined multicast group %s on interface %s", group, interface)
    return interface

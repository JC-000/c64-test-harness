"""Which interface the U64 capture classes join a multicast group on (#399).

``AudioCapture``, ``DebugCapture`` and ``VideoCapture`` all joined with
``inet_aton("0.0.0.0")`` (INADDR_ANY), which leaves the interface to the
kernel's route for the group address.  On this bench ``route -n get
239.0.1.65`` goes via **utun0** (the VPN), not the interface that reaches
the device, so the join landed on the wrong interface.  These tests pin the
``ip_mreq`` the capture actually sends.

Delivery itself was measured on the device, not here.  A join towards the
device received the group on a wired host, and the INADDR_ANY default
received nothing while the kernel routed 224/4 via a tunnel (#399,
2026-09-23).  The full table, with the device address, is in #399 -- a module
under ``tests/`` does not carry bench addresses, because an address in a
usage line is one someone later types (``test_u64_runner_script_gates.py``).

What is under test here is the socket option the capture sends, against a
fake socket.  No device, no packets.
"""
from __future__ import annotations

import itertools
import logging
import socket
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import _stream_iface
from c64_test_harness.backends.u64_audio_capture import (
    EPHEMERAL_AUDIO_PORT,
    AudioCapture,
)
from c64_test_harness.backends.u64_debug_capture import DebugCapture
from c64_test_harness.backends.u64_video_capture import VideoCapture

_GROUP = "239.0.1.65"

#: Stand-ins from RFC 5737 TEST-NET-1, which routes nowhere.  Nothing here
#: needs a real device: the socket is a fake and the resolver is patched.
_DEVICE = "192.0.2.1"
_IFACE = "192.0.2.200"

#: name -> (factory, module whose ``socket`` is patched)
CLASSES = {
    "audio": (
        lambda **kw: AudioCapture(port=EPHEMERAL_AUDIO_PORT, **kw),
        "c64_test_harness.backends.u64_audio_capture",
    ),
    "debug": (
        lambda **kw: DebugCapture(port=0, **kw),
        "c64_test_harness.backends.u64_debug_capture",
    ),
    "video": (
        lambda **kw: VideoCapture(port=0, **kw),
        "c64_test_harness.backends.u64_video_capture",
    ),
}


def _fake_socket() -> MagicMock:
    sock = MagicMock()
    sock.recvfrom = MagicMock(side_effect=itertools.repeat(socket.timeout()))
    sock.getsockname = MagicMock(return_value=("127.0.0.1", 12345))
    return sock


def _joins(sock: MagicMock) -> list[bytes]:
    """The ``ip_mreq`` structures passed to IP_ADD_MEMBERSHIP."""
    return [
        call.args[2]
        for call in sock.setsockopt.call_args_list
        if call.args[:2] == (socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP)
    ]


def _run(name: str, **kwargs) -> MagicMock:
    """Start and stop a capture against a fake socket; returns the socket."""
    factory, module = CLASSES[name]
    sock = _fake_socket()
    with patch(f"{module}.socket.socket", return_value=sock):
        cap = factory(**kwargs)
        cap.start()
        cap.stop()
    return sock


@pytest.mark.parametrize("name", list(CLASSES))
def test_explicit_interface_is_the_one_joined(name: str) -> None:
    sock = _run(name, multicast_group=_GROUP, multicast_interface=_IFACE)
    assert _joins(sock) == [
        socket.inet_aton(_GROUP) + socket.inet_aton(_IFACE)
    ]


@pytest.mark.parametrize("name", list(CLASSES))
def test_device_host_resolves_to_the_local_address_towards_it(name: str) -> None:
    """Naming the device, not an interface, is the documented default path."""
    with patch.object(
        _stream_iface, "local_address_towards", return_value=_IFACE
    ) as resolve:
        sock = _run(name, multicast_group=_GROUP, device_host=_DEVICE)
    assert resolve.call_args.args[0] == _DEVICE
    assert _joins(sock) == [
        socket.inet_aton(_GROUP) + socket.inet_aton(_IFACE)
    ]


@pytest.mark.parametrize("name", list(CLASSES))
def test_default_is_still_inaddr_any(name: str) -> None:
    """Back-compat: callers that name neither keep the old behaviour."""
    sock = _run(name, multicast_group=_GROUP)
    assert _joins(sock) == [socket.inet_aton(_GROUP) + socket.inet_aton("0.0.0.0")]


@pytest.mark.parametrize("name", list(CLASSES))
def test_an_explicit_interface_wins_over_device_host(name: str) -> None:
    with patch.object(
        _stream_iface, "local_address_towards", return_value=_IFACE
    ):
        sock = _run(
            name,
            multicast_group=_GROUP,
            multicast_interface="192.168.9.9",
            device_host=_DEVICE,
        )
    assert _joins(sock) == [
        socket.inet_aton(_GROUP) + socket.inet_aton("192.168.9.9")
    ]


@pytest.mark.parametrize("name", list(CLASSES))
@pytest.mark.parametrize(
    "kwargs",
    [
        {"multicast_interface": _IFACE},
        {"device_host": _DEVICE},
    ],
    ids=["interface", "device_host"],
)
def test_naming_an_interface_without_a_group_is_refused(name: str, kwargs) -> None:
    """Silently ignoring it would read as a join that never happened."""
    factory, _ = CLASSES[name]
    with pytest.raises(ValueError, match="multicast_group"):
        factory(**kwargs)


@pytest.mark.parametrize("name", list(CLASSES))
def test_a_failed_resolution_falls_back_to_inaddr_any_and_warns(
    name: str, caplog
) -> None:
    """A capture that cannot resolve the route still starts, loudly.

    Refusing to start would turn a routing hiccup into a failed test run
    for a join that was INADDR_ANY before this change anyway.
    """
    with patch.object(
        _stream_iface, "local_address_towards", side_effect=OSError("no route")
    ):
        with caplog.at_level(logging.WARNING):
            sock = _run(name, multicast_group=_GROUP, device_host=_DEVICE)
    assert _joins(sock) == [socket.inet_aton(_GROUP) + socket.inet_aton("0.0.0.0")]
    assert any(
        _DEVICE in r.getMessage() and "#399" in r.getMessage()
        for r in caplog.records
    )


@pytest.mark.parametrize("name", list(CLASSES))
def test_a_malformed_interface_address_is_refused_at_construction(
    name: str,
) -> None:
    """``inet_aton`` at start() would raise inside the capture instead."""
    factory, _ = CLASSES[name]
    with pytest.raises(ValueError, match="multicast_interface"):
        factory(multicast_group=_GROUP, multicast_interface="not-an-address")


def test_local_address_towards_loopback() -> None:
    """The resolver sends no traffic: a UDP connect only picks a route."""
    assert _stream_iface.local_address_towards("127.0.0.1") == "127.0.0.1"


# ------------------------------------ the INADDR_ANY default warns via a tunnel

#: What ``route -n get <group>`` prints on macOS, cut to the lines parsed.
_DARWIN_ROUTE = """\
   route to: 239.0.1.65
destination: 224.0.0.0
       mask: 240.0.0.0
  interface: {iface}
      flags: <UP,DONE,CLONING,STATIC,MULTICAST,IFSCOPE>
"""
#: What ``ip -o route get <group>`` prints on Linux.
_LINUX_ROUTE = "multicast 239.0.1.65 dev {iface} src 192.0.2.200 uid 1000 \\    cache \n"


def _route_says(iface: str):
    """A fake ``subprocess.run`` whose route table sends 224/4 via *iface*."""
    def run(argv, **kwargs):
        text = (_LINUX_ROUTE if argv[0] == "ip" else _DARWIN_ROUTE).format(iface=iface)
        return MagicMock(returncode=0, stdout=text)
    return run


def _tunnel_warnings(caplog) -> list[str]:
    return [
        r.getMessage() for r in caplog.records
        if r.levelno == logging.WARNING and "INADDR_ANY" in r.getMessage()
        and "#399" in r.getMessage()
    ]


@pytest.mark.parametrize("name", list(CLASSES))
def test_a_default_join_routed_via_a_tunnel_warns_and_names_it(
    name: str, caplog
) -> None:
    with patch.object(_stream_iface.subprocess, "run", side_effect=_route_says("utun4")):
        with caplog.at_level(logging.WARNING):
            sock = _run(name, multicast_group=_GROUP)
    assert _joins(sock) == [socket.inet_aton(_GROUP) + socket.inet_aton("0.0.0.0")]
    warnings = _tunnel_warnings(caplog)
    assert len(warnings) == 1 and "utun4" in warnings[0]
    assert "device_host" in warnings[0]


@pytest.mark.parametrize("name", list(CLASSES))
def test_a_default_join_routed_via_a_lan_interface_does_not_warn(
    name: str, caplog
) -> None:
    """Control: the same join, a route that is not a tunnel."""
    with patch.object(_stream_iface.subprocess, "run", side_effect=_route_says("en0")):
        with caplog.at_level(logging.WARNING):
            _run(name, multicast_group=_GROUP)
    assert _tunnel_warnings(caplog) == []


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("route"),
        _stream_iface.subprocess.TimeoutExpired("route", 2.0),
        # A failed command's output is not trusted, even if it names one.
        MagicMock(returncode=1, stdout="  interface: utun4\n"),
        MagicMock(returncode=0, stdout="nothing that names an interface\n"),
    ],
    ids=["no-binary", "timeout", "nonzero-exit", "unparseable"],
)
def test_a_failed_route_lookup_still_joins_and_does_not_warn(failure, caplog) -> None:
    """The lookup is advisory: it must never stop a capture from starting."""
    kw = {"side_effect": failure} if isinstance(failure, BaseException) else {"return_value": failure}
    with patch.object(_stream_iface.subprocess, "run", **kw):
        with caplog.at_level(logging.WARNING):
            sock = _run("audio", multicast_group=_GROUP)
    assert _joins(sock) == [socket.inet_aton(_GROUP) + socket.inet_aton("0.0.0.0")]
    assert _tunnel_warnings(caplog) == []


@pytest.mark.parametrize("name", list(CLASSES))
def test_a_join_towards_the_device_neither_looks_up_nor_warns(
    name: str, caplog
) -> None:
    with patch.object(
        _stream_iface, "local_address_towards", return_value=_IFACE
    ), patch.object(
        _stream_iface.subprocess, "run", side_effect=_route_says("utun4")
    ) as run:
        with caplog.at_level(logging.WARNING):
            _run(name, multicast_group=_GROUP, device_host=_DEVICE)
    assert run.call_count == 0
    assert _tunnel_warnings(caplog) == []


@pytest.mark.parametrize(
    "system, tool, template",
    [("Darwin", "route", _DARWIN_ROUTE), ("Linux", "ip", _LINUX_ROUTE)],
)
def test_the_route_interface_is_read_on_both_platforms(system, tool, template) -> None:
    with patch.object(_stream_iface.platform, "system", return_value=system), \
            patch.object(
                _stream_iface.subprocess, "run",
                return_value=MagicMock(returncode=0, stdout=template.format(iface="utun7")),
            ) as run:
        assert _stream_iface.multicast_route_interface(_GROUP) == "utun7"
    assert run.call_args.args[0][0] == tool

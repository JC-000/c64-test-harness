"""Which interface the U64 capture classes join a multicast group on (#399).

``AudioCapture``, ``DebugCapture`` and ``VideoCapture`` all joined with
``inet_aton("0.0.0.0")`` (INADDR_ANY), which leaves the interface to the
kernel's route for the group address.  On this bench ``route -n get
239.0.1.65`` goes via **utun0** (the VPN), not the interface that reaches
the device, so the join landed on the wrong interface.  These tests pin the
``ip_mreq`` the capture actually sends.

**They do not claim multicast capture works.**  #399's measured table is
zero packets received in every arm, *including* a raw socket joined on the
en0 address that routes to the device (n=3 paired, interleaved, U64E
10.43.23.81, fw bce4535e, 2026-09-15).  The cause of that zero is
unestablished and stays open.  What is under test here is the socket option
the capture sends, against a fake socket.  No device, no packets.
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
    sock = _run(name, multicast_group=_GROUP, multicast_interface="10.43.23.127")
    assert _joins(sock) == [
        socket.inet_aton(_GROUP) + socket.inet_aton("10.43.23.127")
    ]


@pytest.mark.parametrize("name", list(CLASSES))
def test_device_host_resolves_to_the_local_address_towards_it(name: str) -> None:
    """Naming the device, not an interface, is the documented default path."""
    with patch.object(
        _stream_iface, "local_address_towards", return_value="10.43.23.127"
    ) as resolve:
        sock = _run(name, multicast_group=_GROUP, device_host="10.43.23.81")
    assert resolve.call_args.args[0] == "10.43.23.81"
    assert _joins(sock) == [
        socket.inet_aton(_GROUP) + socket.inet_aton("10.43.23.127")
    ]


@pytest.mark.parametrize("name", list(CLASSES))
def test_default_is_still_inaddr_any(name: str) -> None:
    """Back-compat: callers that name neither keep the old behaviour."""
    sock = _run(name, multicast_group=_GROUP)
    assert _joins(sock) == [socket.inet_aton(_GROUP) + socket.inet_aton("0.0.0.0")]


@pytest.mark.parametrize("name", list(CLASSES))
def test_an_explicit_interface_wins_over_device_host(name: str) -> None:
    with patch.object(
        _stream_iface, "local_address_towards", return_value="10.43.23.127"
    ):
        sock = _run(
            name,
            multicast_group=_GROUP,
            multicast_interface="192.168.9.9",
            device_host="10.43.23.81",
        )
    assert _joins(sock) == [
        socket.inet_aton(_GROUP) + socket.inet_aton("192.168.9.9")
    ]


@pytest.mark.parametrize("name", list(CLASSES))
@pytest.mark.parametrize(
    "kwargs",
    [
        {"multicast_interface": "10.43.23.127"},
        {"device_host": "10.43.23.81"},
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
            sock = _run(name, multicast_group=_GROUP, device_host="10.43.23.81")
    assert _joins(sock) == [socket.inet_aton(_GROUP) + socket.inet_aton("0.0.0.0")]
    assert any(
        "10.43.23.81" in r.getMessage() and "#399" in r.getMessage()
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

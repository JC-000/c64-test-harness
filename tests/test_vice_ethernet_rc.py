"""The ethernet vicerc must use resource names VICE actually has.

``ViceProcess.start()`` writes a temporary vicerc to activate the
CS8900a, and two of the names in it were invented:  ``EthernetIOIF`` and
``EthernetIODriver`` do not exist in the VICE tree in any casing.  VICE
logged ``Unknown resource`` and ignored them.  They were inert rather
than harmful only because the ``-ethernetioif`` / ``-ethernetiodriver``
CLI flags are passed alongside and carry the same settings — so on any
path that did not also pass those flags, the rc silently configured
nothing.

The real names are ``ETHERNET_INTERFACE`` (S ``cs8900io.c:309``) and
``ETHERNET_DRIVER`` (S ``rawnetarch.c:146``).

``EthernetCartMode`` and ``EthernetCartBase`` are correct as written,
despite their mixed case: the *resource* table lookup is case-insensitive
(``util_strcasecmp`` at S ``resources.c:243``; ``resources_calc_hash_key``
lowercases every character under a comment saying so).  That is a
different lookup from the *option* table in ``cmdline.c``, which is
case-sensitive and prefix-matching — the mechanism behind the
``-eventimage`` / ``-eventsnapshot`` defects fixed elsewhere in this
branch.  ``test_every_rc_resource_name_is_known_to_vice`` covers all of
them at once and does not care which is which.

Why these tests do not activate the cart
----------------------------------------
``ETHERNETCART_ACTIVE=1`` makes an unelevated VICE SIGSEGV on the first
reset — ``rawnet_arch_driver`` is NULL and ``pre_reset`` is called
through it (S ``rawnetarch.c:251``).  Verified here: launching the rc
unmodified kills both builds with rc=-11.  So the launches below strip
that one line, which leaves every *other* resource in the file being set
by exactly the same code path.  Nothing else in the rc is changed, and
the CLI ethernet flags are deliberately not passed, so the rc is the only
thing that could set what is asserted.
"""

from __future__ import annotations

import shutil
import socket

import pytest
from conftest import connect_binary_transport

from c64_test_harness.backends.vice_lifecycle import (
    ViceConfig,
    ViceProcess,
    build_ethernet_rc,
)

pytestmark = pytest.mark.skipif(
    shutil.which("x64sc") is None, reason="x64sc not found on PATH"
)

IFACE = "feth0"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def ethernet_cfg(**kwargs) -> ViceConfig:
    return ViceConfig(
        ethernet=True,
        ethernet_interface=IFACE,
        ethernet_driver="pcap",
        **kwargs,
    )


def rc_resource_names(body: str) -> list[str]:
    """Every resource name assigned in *body*, in order."""
    names = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("[") or "=" not in line:
            continue
        name = line.split("=", 1)[0]
        if name == "ConfigVersion":  # file metadata, not a resource
            continue
        names.append(name)
    return names


def inert_rc(cfg: ViceConfig) -> str:
    """The generated rc with cart activation removed — see module docstring."""
    return "\n".join(
        line
        for line in build_ethernet_rc(cfg).splitlines()
        if not line.startswith("ETHERNETCART_ACTIVE")
    )


def resources_after_addconfig(
    body: str, tmp_path, *names: str
) -> dict[str, int | str]:
    """Launch with *body* as an ``-addconfig`` file and read *names* back."""
    rc = tmp_path / "ethernet.rc"
    rc.write_text(body)
    cfg = ViceConfig(port=free_port(), extra_args=["-addconfig", str(rc)])
    proc = ViceProcess(cfg)
    proc.start()
    try:
        transport = connect_binary_transport(cfg.port, proc=proc, timeout=25.0)
        try:
            return {name: transport.resource_get(name) for name in names}
        finally:
            transport.close()
    finally:
        proc.stop()


def test_rc_sets_the_ethernet_interface_resource(tmp_path):
    """The interface must arrive from the rc alone, with no CLI flags."""
    got = resources_after_addconfig(
        inert_rc(ethernet_cfg()), tmp_path, "ETHERNET_INTERFACE"
    )
    assert got["ETHERNET_INTERFACE"] == IFACE


def test_every_rc_resource_name_is_known_to_vice(tmp_path):
    """No line in the rc may name a resource VICE does not have.

    This is the general guard.  ``resource_get`` raises for an unknown
    name, so a single invented name anywhere in the file fails this —
    which is what was missing when ``EthernetIOIF`` was introduced, and
    what an assertion on the generated *text* can never provide.

    ``EthernetCartBase`` is only emitted for a non-default base, so the
    config here uses one.
    """
    cfg = ethernet_cfg(ethernet_base=0xDF00)
    names = rc_resource_names(build_ethernet_rc(cfg))
    assert "EthernetCartBase" in names, "expected the non-default base to be emitted"

    got = resources_after_addconfig(inert_rc(cfg), tmp_path, *names)
    assert set(got) == set(names)


def test_rc_carries_the_cart_mode_and_base(tmp_path):
    """Mixed-case names resolve too — the resource lookup ignores case."""
    cfg = ethernet_cfg(ethernet_base=0xDF00, ethernet_mode="rrnet")
    got = resources_after_addconfig(
        inert_rc(cfg), tmp_path, "EthernetCartMode", "EthernetCartBase"
    )
    assert got["EthernetCartMode"] == 1
    assert got["EthernetCartBase"] == 0xDF00


def test_driver_name_is_a_resource_vice_recognises(tmp_path):
    """``ETHERNET_DRIVER`` resolves; its *value* cannot be asserted here.

    ``set_ethernet_driver`` accepts ``pcap`` only when
    ``archdep_rawnet_capability()`` is true (S ``rawnetarch.c:107``), i.e.
    only for a process that can already open ``/dev/bpf*``.  These tests
    deliberately stay unelevated, so the driver falls back to ``none``.
    Asserting the name is known is the most this can honestly check; the
    value landing is covered by the live ethernet suite, which elevates.
    """
    got = resources_after_addconfig(
        inert_rc(ethernet_cfg()), tmp_path, "ETHERNET_DRIVER"
    )
    assert isinstance(got["ETHERNET_DRIVER"], str)

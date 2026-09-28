"""``probe_vice_pcap_ok`` must launch VICE with the production vicerc (#503).

The probe exists to answer "will the ethernet tests' VICE launch work on
this host?", so the rc it hands VICE has to be the one ``ViceProcess``
writes.  It used to hand-write ``EthernetIOIF`` / ``EthernetIODriver``,
which VICE 3.10 does not register (the resources are
``ETHERNET_INTERFACE``, S ``cs8900io.c:309``, and ``ETHERNET_DRIVER``,
S ``rawnetarch.c:146``): VICE ignored both lines and the probe only
worked through the CLI flags passed alongside.

No VICE is launched: ``subprocess.Popen`` is intercepted, the rc file
named after ``-addconfig`` is read at that moment, and the fake process
reports an immediate exit so the probe finishes without touching the
network.
"""
from __future__ import annotations

import subprocess
import sys
import types

import bridge_platform as bp
from c64_test_harness.backends.vice_lifecycle import ViceConfig, build_ethernet_rc

IFACE = "feth0"


def _rc_resources(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line and not line.startswith("["):
            key, _, value = line.partition("=")
            out[key.strip().upper()] = value.strip().strip('"')
    return out


def test_probe_hands_vice_the_production_rc(monkeypatch, tmp_path):
    fake_bin = tmp_path / "x64sc"
    fake_bin.write_text("#!/bin/sh\nexit 0\n")
    fake_bin.chmod(0o755)

    monkeypatch.setattr(bp, "_PROBE_CACHE", None)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("MACOS_PCAP_DISABLED", raising=False)
    monkeypatch.delenv("MACOS_PCAP_ENABLED", raising=False)
    monkeypatch.setattr(bp, "ethernet_vice_binary", lambda: str(fake_bin))
    # Already root: no sudo in front of the launch, nothing to authorise.
    monkeypatch.setattr(bp, "rawnet_capability", lambda as_root=False: True)
    monkeypatch.setattr(bp, "_probe_port", lambda: 65001)

    def fake_run(args, **kwargs):
        assert args[0] == "ifconfig", args
        return types.SimpleNamespace(
            stdout=f"{IFACE}: flags=8863<UP,BROADCAST,RUNNING> mtu 1500\n",
            returncode=0,
        )

    launched: dict = {}

    class _Exited:
        returncode = 139

        def poll(self):
            return self.returncode

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            pass

    def fake_popen(args, **kwargs):
        rc_path = args[args.index("-addconfig") + 1]
        with open(rc_path) as f:
            launched["rc"] = f.read()
        launched["args"] = list(args)
        return _Exited()

    monkeypatch.setattr(bp.subprocess, "run", fake_run)
    monkeypatch.setattr(bp.subprocess, "Popen", fake_popen)

    ok, reason = bp.probe_vice_pcap_ok(iface=IFACE)

    assert "rc" in launched, f"the probe never launched VICE: {reason}"
    assert launched["args"][0] == str(fake_bin)
    resources = _rc_resources(launched["rc"])
    # The resources VICE 3.10 actually registers carry the settings ...
    assert resources.get("ETHERNET_INTERFACE") == IFACE, launched["rc"]
    assert resources.get("ETHERNET_DRIVER") == "pcap", launched["rc"]
    assert resources.get("ETHERNETCART_ACTIVE") == "1", launched["rc"]
    # ... and the rc is exactly the one ViceProcess would launch with.
    assert launched["rc"] == build_ethernet_rc(
        ViceConfig(ethernet=True, ethernet_interface=IFACE, ethernet_driver="pcap")
    )
    # The fake VICE exited, so the probe must say so rather than pass.
    assert ok is False
    assert "code=139" in reason

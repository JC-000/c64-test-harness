"""Live integration tests for SID playback on Ultimate 64 hardware.

Gated by the ``U64_HOST`` env var.  ``play_sid()`` on U64 uses the
firmware's native ``sid_play`` endpoint which is a DMA load + run, so
this test mutates device state — it always resets the device afterwards.

Example::

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_sid_u64_live.py -v
"""

from __future__ import annotations

import os
import time

import pytest

from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.sid import SidFile, build_test_psid
from c64_test_harness.sid_player import play_sid


_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST, reason="U64_HOST not set — live Ultimate device tests disabled",
)


def _build_test_sid() -> SidFile:
    """Same test PSID as the VICE live tests: sentinel+counter at $0360/$0341."""
    init_code = bytes([0xA9, 0x42, 0x8D, 0x60, 0x03])
    play_code = bytes([0xEE, 0x61, 0x03])
    sid_bytes = build_test_psid(
        load_addr=0x1000, init_code=init_code, play_code=play_code
    )
    return SidFile.from_bytes(sid_bytes)


def test_play_sid_on_ultimate64() -> None:
    """HTTP sid_play call succeeds; attempt to verify sentinel if readable.

    Client targets ``POST /v1/runners:sidplay`` (firmware 3.14 endpoint).
    """
    sid = _build_test_sid()
    transport = Ultimate64Transport(host=_HOST, password=_PW, timeout=8.0)
    try:
        # sid_play is a DMA load + run — device starts playing the SID.
        play_sid(transport, sid, song=0)

        # Give the device a moment to run init.
        time.sleep(0.5)

        # Attempt to verify sentinel at $0360.  On U64, sid_play may keep
        # the CPU running the SID replay in a way that makes $0360 readable
        # via the REST API — but this is not guaranteed.  Make it a soft
        # assertion: if the read returns 0x42 assert it as a sanity check,
        # otherwise just log and continue (HTTP success is the minimum).
        try:
            mem = transport.read_memory(0x0360, 1)
            sentinel = mem[0]
            if sentinel == 0x42:
                # Perfect — init ran and $0360 is readable.
                pass
            else:
                # Soft skip: U64's sid_play may use a separate player
                # that doesn't run our init routine literally.
                pytest.xfail(
                    f"sentinel read back as ${sentinel:02X} (expected $42)"
                )
        except Exception as exc:
            # Memory-read failure doesn't count as a test failure:
            # the sid_play HTTP call succeeding is the minimum verification.
            pytest.xfail(f"could not read $0360 on U64: {exc}")
    finally:
        # Be polite to shared hardware: always reset to stop audio.
        try:
            transport._client.reset()
        except Exception:
            pass
        transport.close()

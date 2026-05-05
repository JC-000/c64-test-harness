"""c64-test-harness — Reusable test harness for Commodore 64 programs.

Public API re-exports for convenient ``from c64_test_harness import ...``.
"""

from .transport import C64Transport, TransportError, ConnectionError, TimeoutError
from .screen import ScreenGrid, wait_for_text, wait_for_stable
from .keyboard import send_text, send_key
from .memory import read_bytes, read_bytes_chunked, write_bytes, read_word_le, read_dword_le, hex_dump
from .labels import Labels
from .config import HarnessConfig
from .runner import TestRunner, TestScenario, TestResult, TestStatus
from .debug import dump_screen
from .verify import PrgFile
from .backends.vice import ViceTransport
from .backends.vice_lifecycle import ViceProcess, ViceConfig
from .backends.hardware import HardwareTransportBase

__all__ = [
    # Protocol + exceptions
    "C64Transport",
    "TransportError",
    "ConnectionError",
    "TimeoutError",
    # Screen
    "ScreenGrid",
    "wait_for_text",
    "wait_for_stable",
    # Input
    "send_text",
    "send_key",
    # Memory
    "read_bytes",
    "read_bytes_chunked",
    "write_bytes",
    "read_word_le",
    "read_dword_le",
    "hex_dump",
    # Labels
    "Labels",
    # Config
    "HarnessConfig",
    # Runner
    "TestRunner",
    "TestScenario",
    "TestResult",
    "TestStatus",
    # Debug
    "dump_screen",
    # Verify
    "PrgFile",
    # Backends
    "ViceTransport",
    "ViceProcess",
    "ViceConfig",
    "HardwareTransportBase",
]

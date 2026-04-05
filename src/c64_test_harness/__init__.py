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
from .execute import load_code, goto, jsr, wait_for_pc, set_breakpoint, delete_breakpoint, set_register
from .disk import DiskImage, DiskFormat, FileType, DirEntry, DiskImageError
from .backends.vice_binary import BinaryViceTransport
from .backends.vice_lifecycle import ViceProcess, ViceConfig
from .backends.hardware import HardwareTransportBase
from .backends.vice_manager import PortAllocator, ViceInstance, ViceInstanceManager
from .backends.ultimate64 import Ultimate64Transport
from .backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
    Ultimate64AuthError,
    Ultimate64TimeoutError,
    Ultimate64ProtocolError,
)
from .backends.ultimate64_helpers import (
    get_turbo_mhz,
    set_turbo_mhz,
    get_turbo_enabled,
    get_reu_config,
    set_reu,
    get_sid_config,
    set_sid_socket,
    mount_disk_file,
    unmount,
    run_prg_file,
    load_prg_file,
    U64StateSnapshot,
    snapshot_state,
    restore_state,
)
from .backends.ultimate64_manager import (
    Ultimate64Device,
    Ultimate64Instance,
    Ultimate64InstanceManager,
    Ultimate64ManagerError,
    Ultimate64PoolExhaustedError,
)
from .backends.ultimate64_schema import (
    CPU_SPEED_VALUES,
    CPU_SPEED_BY_MHZ,
    TURBO_CONTROL_VALUES,
    REU_SIZE_VALUES,
    REU_ENABLED_VALUES,
    SID_TYPE_VALUES,
    SID_ADDRESS_VALUES,
    DRIVE_TYPE_VALUES,
    DISK_IMAGE_TYPES,
    MOUNT_MODES,
    cpu_speed_enum,
    cpu_speed_mhz,
    reu_size_enum,
    validate_enum,
    SIDSocketConfig,
)
from .parallel import run_parallel, ParallelTestResult, SingleTestResult
from .sid import SidFile, SidError, SidFormatError, build_test_psid
from .sid_player import (
    play_sid,
    play_sid_vice,
    play_sid_ultimate64,
    stop_sid_vice,
    build_vice_stub,
    SidPlaybackError,
    DEFAULT_STUB_ADDR,
)

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
    # Execution control
    "load_code",
    "goto",
    "jsr",
    "wait_for_pc",
    "set_breakpoint",
    "delete_breakpoint",
    "set_register",
    # Disk
    "DiskImage",
    "DiskFormat",
    "FileType",
    "DirEntry",
    "DiskImageError",
    # Backends
    "BinaryViceTransport",
    "ViceProcess",
    "ViceConfig",
    "HardwareTransportBase",
    # Multi-instance
    "PortAllocator",
    "ViceInstance",
    "ViceInstanceManager",
    # Ultimate 64 hardware
    "Ultimate64Transport",
    "Ultimate64Client",
    "Ultimate64Error",
    "Ultimate64AuthError",
    "Ultimate64TimeoutError",
    "Ultimate64ProtocolError",
    "CPU_SPEED_VALUES",
    "CPU_SPEED_BY_MHZ",
    "TURBO_CONTROL_VALUES",
    "REU_SIZE_VALUES",
    "REU_ENABLED_VALUES",
    "SID_TYPE_VALUES",
    "SID_ADDRESS_VALUES",
    "DRIVE_TYPE_VALUES",
    "DISK_IMAGE_TYPES",
    "MOUNT_MODES",
    "cpu_speed_enum",
    "cpu_speed_mhz",
    "reu_size_enum",
    "validate_enum",
    "SIDSocketConfig",
    # Ultimate 64 helpers
    "get_turbo_mhz",
    "set_turbo_mhz",
    "get_turbo_enabled",
    "get_reu_config",
    "set_reu",
    "get_sid_config",
    "set_sid_socket",
    "mount_disk_file",
    "unmount",
    "run_prg_file",
    "load_prg_file",
    "U64StateSnapshot",
    "snapshot_state",
    "restore_state",
    # Ultimate 64 instance management
    "Ultimate64Device",
    "Ultimate64Instance",
    "Ultimate64InstanceManager",
    "Ultimate64ManagerError",
    "Ultimate64PoolExhaustedError",
    # Parallel execution
    "run_parallel",
    "ParallelTestResult",
    "SingleTestResult",
    # SID file parsing
    "SidFile",
    "SidError",
    "SidFormatError",
    "build_test_psid",
    # SID playback
    "play_sid",
    "play_sid_vice",
    "play_sid_ultimate64",
    "stop_sid_vice",
    "build_vice_stub",
    "SidPlaybackError",
    "DEFAULT_STUB_ADDR",
]

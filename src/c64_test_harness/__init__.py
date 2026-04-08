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
    get_sid_socket_types,
    get_sid_addresses,
    configure_multi_sid,
    get_physical_sid_sockets,
    get_ultisid_config,
    get_audio_mixer_config,
    set_audio_mixer_item,
    mount_disk_file,
    unmount,
    run_prg_file,
    load_prg_file,
    U64StateSnapshot,
    snapshot_state,
    restore_state,
    CAT_ULTISID,
    CAT_AUDIO_MIXER,
    CAT_DATA_STREAMS,
    get_data_streams_config,
    set_stream_destination,
    get_debug_stream_mode,
    set_debug_stream_mode,
    DEBUG_MODE_6510,
    DEBUG_MODE_VIC,
    DEBUG_MODE_6510_VIC,
    DEBUG_MODE_1541,
    DEBUG_MODE_6510_1541,
    DEBUG_MODES,
)
from .backends.ultimate64_manager import (
    Ultimate64Device,
    Ultimate64Instance,
    Ultimate64InstanceManager,
    Ultimate64ManagerError,
    Ultimate64PoolExhaustedError,
)
from .backends.ultimate64_probe import (
    ProbeResult,
    probe_u64,
    is_u64_reachable,
)
from .backends.device_lock import DeviceLock
from .backends.unified_manager import (
    TestTarget,
    BackendManager,
    UnifiedManager,
    create_manager,
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
from .backends.render_wav import render_wav, RenderResult, PAL_CLOCK_HZ, NTSC_CLOCK_HZ
from .backends.render_wav_u64 import capture_sid_u64, U64CaptureResult
from .backends.u64_audio_capture import (
    AudioCapture,
    CaptureResult,
    write_wav,
    DEFAULT_AUDIO_PORT,
    DEFAULT_SAMPLE_RATE,
    CHANNELS,
    SAMPLE_WIDTH,
)
from .backends.u64_debug_capture import (
    DebugCapture,
    DebugCaptureResult,
    BusCycle,
    DEFAULT_DEBUG_PORT,
    ENTRIES_PER_PACKET,
)
from .backends.u64_video_capture import (
    VideoCapture,
    VideoCaptureResult,
    VideoFrame,
    DEFAULT_VIDEO_PORT,
    VIC_PALETTE,
)
from .ethernet import generate_mac, parse_mac, format_mac, set_cs8900a_mac
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
    "get_sid_socket_types",
    "get_sid_addresses",
    "configure_multi_sid",
    "get_physical_sid_sockets",
    "get_ultisid_config",
    "get_audio_mixer_config",
    "set_audio_mixer_item",
    "CAT_ULTISID",
    "CAT_AUDIO_MIXER",
    "CAT_DATA_STREAMS",
    "get_data_streams_config",
    "set_stream_destination",
    "get_debug_stream_mode",
    "set_debug_stream_mode",
    "DEBUG_MODE_6510",
    "DEBUG_MODE_VIC",
    "DEBUG_MODE_6510_VIC",
    "DEBUG_MODE_1541",
    "DEBUG_MODE_6510_1541",
    "DEBUG_MODES",
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
    # Ultimate 64 liveness probe
    "ProbeResult",
    "probe_u64",
    "is_u64_reachable",
    # Device lock
    "DeviceLock",
    # Unified backend manager
    "TestTarget",
    "BackendManager",
    "UnifiedManager",
    "create_manager",
    # Ethernet MAC helpers
    "generate_mac",
    "parse_mac",
    "format_mac",
    "set_cs8900a_mac",
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
    # Batch WAV render
    "render_wav",
    "RenderResult",
    "PAL_CLOCK_HZ",
    "NTSC_CLOCK_HZ",
    # U64 SID capture
    "capture_sid_u64",
    "U64CaptureResult",
    # U64 audio capture (low-level)
    "AudioCapture",
    "CaptureResult",
    "write_wav",
    "DEFAULT_AUDIO_PORT",
    "DEFAULT_SAMPLE_RATE",
    "CHANNELS",
    "SAMPLE_WIDTH",
    # U64 debug stream capture
    "DebugCapture",
    "DebugCaptureResult",
    "BusCycle",
    "DEFAULT_DEBUG_PORT",
    "ENTRIES_PER_PACKET",
    # U64 video stream capture
    "VideoCapture",
    "VideoCaptureResult",
    "VideoFrame",
    "DEFAULT_VIDEO_PORT",
    "VIC_PALETTE",
]

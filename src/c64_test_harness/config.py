"""Harness configuration — TOML file, environment variables, or programmatic.

``HarnessConfig`` is a dataclass holding all settings.  Load from TOML with
``HarnessConfig.from_toml(path)`` or from environment with
``HarnessConfig.from_env()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from .memory_policy import MemoryPolicy


#: The one switch for the U64 reset-on-entry (issue #227), shared by
#: ``UnifiedManager`` and :meth:`HarnessConfig.from_env`.  The generic
#: ``C64TEST_U64_BASELINE_ON_ENTRY`` form exists because every field has
#: one by convention; when both are set the prefixed one wins (it is the
#: config's own override), see :meth:`HarnessConfig.from_env`.
U64_BASELINE_ON_ENTRY_ENV = "U64_BASELINE_ON_ENTRY"

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})


def env_flag(name: str) -> bool | None:
    """Parse an on/off environment switch; ``None`` when unset.

    ``1`` / ``true`` / ``yes`` / ``on`` (case-insensitive, surrounding
    whitespace ignored) are ``True``; any other value is ``False``.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUE_WORDS


def _permissive_policy() -> "MemoryPolicy":
    """Default-factory shim — defers the MemoryPolicy import so this
    module stays import-cheap and never participates in cycles."""
    from .memory_policy import MemoryPolicy

    return MemoryPolicy.permissive()


@dataclass
class HarnessConfig:
    """All configuration for the C64 test harness."""

    # Backend selection
    backend: str = "vice"

    # VICE connection
    vice_host: str = "127.0.0.1"
    vice_port: int = 6502
    # NOTE: port 6510 is VICE's default text monitor port and must be
    # avoided in port ranges — VICE misbehaves when the binary monitor
    # is bound to that port.
    vice_timeout: float = 5.0

    # VICE executable
    vice_executable: str = "x64sc"
    vice_prg_path: str = ""
    vice_warp: bool = True
    vice_ntsc: bool = True
    #: Run VICE's sound core.  **Not an output switch** -- ``False``
    #: emits ``+sound``, which stops reSID being clocked, and
    #: ``$D41B``/``$D41C`` then read back ``maincpu_clk % 256`` (VICE
    #: 3.10 ``sid.c:137,279``): a clean ramp that looks like a working
    #: oscillator.  ``sounddev="dummy"`` is *not* a headless-safe
    #: substitute -- it enables the core and then stalls it.
    #:
    #: The default stays ``False`` so a launch never opens a host audio
    #: device.  For SID measurement use
    #: :func:`~c64_test_harness.backends.vice_lifecycle.headless_sid_config`,
    #: which is ``sound=True`` + ``sounddev="wav"`` + ``-soundvolume 0``.
    #: See ``docs/sid_audio.md``.
    vice_sound: bool = False
    vice_console: bool = True
    vice_minimize: bool = True
    vice_extra_args: list[str] = field(default_factory=list)

    # Screen geometry
    screen_cols: int = 40
    screen_rows: int = 25
    screen_base: int = 0x0400

    # Keyboard buffer
    keybuf_addr: int = 0x0277
    keybuf_count_addr: int = 0x00C6
    keybuf_max: int = 10

    # Multi-instance
    vice_port_range_start: int = 6511
    vice_port_range_end: int = 6531
    vice_reuse_existing: bool = False
    vice_acquire_retries: int = 3

    # Timeouts
    startup_timeout: float = 30.0
    default_wait_timeout: float = 60.0

    # Poll intervals
    exec_poll_interval: float = 0.2
    screen_poll_interval: float = 2.0

    # Ethernet / RR-Net
    vice_ethernet: bool = False
    vice_ethernet_mode: str = "rrnet"
    vice_ethernet_interface: str = ""
    vice_ethernet_driver: str = ""
    vice_ethernet_base: int = 0xDE00

    # Ultimate 64: reset the covered config categories to the firmware's
    # factory defaults at run entry, inside the DeviceLock, then assert
    # ``current == default`` per item (issue #227).  TOML
    # ``[u64] baseline_on_entry = true``; env ``U64_BASELINE_ON_ENTRY`` (the
    # one name, shared with ``UnifiedManager``; the convention form
    # ``C64TEST_U64_BASELINE_ON_ENTRY`` wins when both are set).  Off by
    # default -- no requests are made.  Wire it with
    # ``UnifiedManager(..., baseline_on_entry=cfg.u64_baseline_on_entry)``.
    u64_baseline_on_entry: bool = False

    # Memory policy enforced at the transport boundary.  Default is
    # permissive (no checks) so existing configs see no behaviour
    # change; consumers opt in by declaring a ``[memory]`` section in
    # their TOML — see ``MemoryPolicy.from_config``.
    memory_policy: "MemoryPolicy" = field(default_factory=_permissive_policy)

    @classmethod
    def from_toml(cls, path: str | Path) -> HarnessConfig:
        """Load configuration from a TOML file (e.g., ``c64test.toml``)."""
        if tomllib is None:
            raise RuntimeError(
                "TOML support requires Python 3.11+ or the 'tomli' package"
            )
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls._from_dict(data)

    @classmethod
    def from_env(cls, prefix: str = "C64TEST_") -> HarnessConfig:
        """Load configuration from environment variables.

        Maps ``C64TEST_VICE_PORT=6511`` → ``vice_port=6511``, etc.
        Only fields with matching env vars are overridden.  Structured
        fields (``memory_policy``) cannot be expressed as an env string;
        setting their env var raises ``ValueError`` rather than smuggling
        a raw string into the config.

        ``u64_baseline_on_entry`` additionally reads the shared, unprefixed
        :data:`U64_BASELINE_ON_ENTRY_ENV` (the same switch ``UnifiedManager``
        reads), so one name opts both paths in.  The prefixed
        ``<prefix>U64_BASELINE_ON_ENTRY`` wins when both are set.
        """
        config = cls()
        shared = env_flag(U64_BASELINE_ON_ENTRY_ENV)
        if shared is not None:
            config.u64_baseline_on_entry = shared
        for fld in config.__dataclass_fields__:
            env_key = prefix + fld.upper()
            env_val = os.environ.get(env_key)
            if env_val is not None:
                current = getattr(config, fld)
                if isinstance(current, bool):
                    setattr(config, fld, env_val.lower() in ("1", "true", "yes"))
                elif isinstance(current, int):
                    setattr(config, fld, int(env_val, 0))
                elif isinstance(current, float):
                    setattr(config, fld, float(env_val))
                elif isinstance(current, list):
                    setattr(config, fld, env_val.split(","))
                elif isinstance(current, str):
                    setattr(config, fld, env_val)
                else:
                    raise ValueError(
                        f"{env_key} cannot be set via environment: "
                        f"{fld!r} is a structured field with no string "
                        f"form. Declare it in TOML (e.g. a [memory] "
                        f"section for memory_policy) or set it "
                        f"programmatically."
                    )
        return config

    @classmethod
    def _from_dict(cls, data: dict) -> HarnessConfig:
        """Build config from a flat or nested dict (TOML structure)."""
        from .memory_policy import MemoryPolicy

        config = cls()
        # The [memory] section is special: it builds a MemoryPolicy rather
        # than getting flattened into ``memory_*`` fields.  Pop it out
        # before the flat-flattener consumes the rest of the dict.
        memory_section = data.get("memory")
        if isinstance(memory_section, dict):
            config.memory_policy = MemoryPolicy.from_config(memory_section)
            data = {k: v for k, v in data.items() if k != "memory"}

        # Flatten remaining nested sections: [vice] port → vice_port
        flat: dict[str, object] = {}
        for key, val in data.items():
            if isinstance(val, dict):
                for subkey, subval in val.items():
                    flat[f"{key}_{subkey}"] = subval
            else:
                flat[key] = val
        for fld in config.__dataclass_fields__:
            if fld in flat:
                setattr(config, fld, flat[fld])
        return config

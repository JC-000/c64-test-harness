"""Harness configuration — TOML file, environment variables, or programmatic.

``HarnessConfig`` is a dataclass holding all settings.  Load from TOML with
``HarnessConfig.from_toml(path)`` or from environment with
``HarnessConfig.from_env()``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


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
    vice_sound: bool = False
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
        Only fields with matching env vars are overridden.
        """
        config = cls()
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
                else:
                    setattr(config, fld, env_val)
        return config

    @classmethod
    def _from_dict(cls, data: dict) -> HarnessConfig:
        """Build config from a flat or nested dict (TOML structure)."""
        config = cls()
        # Flatten nested sections: [vice] port → vice_port
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

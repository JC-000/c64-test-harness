"""ViceProcess — start/stop/wait for a VICE emulator instance.

Provides ``ViceConfig`` (what to launch) and ``ViceProcess`` (context
manager that handles the lifecycle).
"""

from __future__ import annotations

import socket
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class ViceConfig:
    """Configuration for launching a VICE instance."""

    executable: str = "x64sc"
    prg_path: str = ""
    port: int = 6510
    warp: bool = True
    ntsc: bool = True
    sound: bool = False
    extra_args: list[str] = field(default_factory=list)


class ViceProcess:
    """Context manager for a VICE emulator process.

    Usage::

        config = ViceConfig(prg_path="game.prg")
        with ViceProcess(config) as vice:
            vice.wait_for_monitor()
            transport = ViceTransport(port=config.port)
            ...
    """

    def __init__(self, config: ViceConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None  # type: ignore[type-arg]

    def __enter__(self) -> ViceProcess:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def start(self) -> None:
        """Stop any existing process on this instance, then launch VICE."""
        if self._proc is not None:
            self.stop()

        cfg = self.config

        args = [cfg.executable]
        if cfg.prg_path:
            args += ["-autostart", cfg.prg_path]
        if cfg.warp:
            args.append("-warp")
        if cfg.ntsc:
            args.append("-ntsc")
        args += ["-remotemonitor", "-remotemonitoraddress", f"ip4://127.0.0.1:{cfg.port}"]
        if not cfg.sound:
            args.append("+sound")
        args += cfg.extra_args

        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        """Terminate VICE: SIGTERM → wait 5s → SIGKILL fallback."""
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    def wait_for_monitor(self, timeout: float = 30.0) -> bool:
        """Poll the TCP monitor port until it accepts connections.

        Returns ``True`` if connected within *timeout*, ``False`` otherwise.
        """
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(2)
                s.connect(("127.0.0.1", self.config.port))
                s.close()
                return True
            except Exception:
                time.sleep(1)
        return False

    @staticmethod
    def kill_on_port(port: int) -> bool:
        """Kill a process listening on *port* using /proc/net/tcp (Linux).

        Returns True if a process was found and killed, False otherwise.
        This is an opt-in replacement for the old ``pkill -f`` approach.
        """
        import os
        import signal

        hex_port = f"{port:04X}"
        try:
            with open("/proc/net/tcp") as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) < 4:
                        continue
                    local = parts[1]
                    if local.endswith(f":{hex_port}") and parts[3] == "0A":
                        # 0A = LISTEN state
                        inode = parts[9] if len(parts) > 9 else None
                        if inode is None:
                            continue
                        # Find PID via /proc/*/fd
                        for pid_dir in os.listdir("/proc"):
                            if not pid_dir.isdigit():
                                continue
                            fd_dir = f"/proc/{pid_dir}/fd"
                            try:
                                for fd in os.listdir(fd_dir):
                                    link = os.readlink(f"{fd_dir}/{fd}")
                                    if f"socket:[{inode}]" in link:
                                        os.kill(int(pid_dir), signal.SIGTERM)
                                        return True
                            except (PermissionError, FileNotFoundError):
                                continue
        except FileNotFoundError:
            pass  # Not Linux or /proc not mounted
        return False

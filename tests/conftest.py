"""Shared test fixtures — MockTransport for unit testing without VICE."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from functools import lru_cache

import pytest

pytest_plugins = ["pytester"]

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.vice_binary import BinaryViceTransport
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess
from c64_test_harness.backends.vice_manager import PortAllocator
from c64_test_harness.screen import wait_for_text

# ---------------------------------------------------------------------------
# Device-lock adoption for live tests (issue #136)
# ---------------------------------------------------------------------------

_device_lock_log = logging.getLogger("c64_test_harness.tests.device_lock")

#: Module attributes a live test may use to name its device, checked
#: before falling back to ``U64_HOST``.
_HOST_ATTRS = ("_HOST", "HOST", "U64_HOST", "_U64_HOST")

#: Default seconds to queue for a busy device before giving up.  Long by
#: design: the point is to wait for the other job, not to barge in.
_DEFAULT_LOCK_TIMEOUT = 300.0


def is_live_test_file(path: str) -> bool:
    """Whether *path* is one of the opt-in live test modules.

    The suite's convention is a ``_live.py`` suffix; there are no pytest
    markers to key off.
    """
    return os.path.basename(str(path)).endswith("_live.py")


def live_device_host(module: object = None) -> str | None:
    """Resolve the device a live test module drives, or ``None``.

    Prefers a host the module has already resolved (``_HOST`` and
    friends — the shape every U64 live test in this repo uses) and falls
    back to ``U64_HOST``.  A comma- or space-separated list yields its
    first entry, matching ``UnifiedManager._parse_u64_hosts``.
    """
    for attr in _HOST_ATTRS:
        value = getattr(module, attr, None)
        if isinstance(value, str) and value.strip():
            return _first_host(value)
    env = os.environ.get("U64_HOST", "")
    return _first_host(env) if env.strip() else None


def _first_host(raw: str) -> str | None:
    for chunk in raw.replace(",", " ").split():
        if chunk:
            return chunk
    return None


def _lock_timeout() -> float:
    raw = os.environ.get("U64_DEVICE_LOCK_TIMEOUT", "").strip()
    try:
        return float(raw) if raw else _DEFAULT_LOCK_TIMEOUT
    except ValueError:
        return _DEFAULT_LOCK_TIMEOUT


@pytest.fixture(autouse=True)
def device_lock_guard(request):
    """Hold the machine-global :class:`DeviceLock` around every live test.

    Adoption by construction (issue #136): a live test no longer has to
    remember to lock, and a run of this suite can no longer reboot a
    device out from under another job's measurement.  Unit tests — the
    overwhelming majority — return before touching anything.

    ``allow_nested=True`` so the test body can still go through
    ``create_manager`` (or acquire its own ``DeviceLock``) without
    queueing behind the lock this fixture already holds.

    Scope note: a live test that drives only VICE will also take the
    device lock when ``U64_HOST`` is set.  That is deliberately
    conservative — over-serialising a rarely-run bridge test is cheaper
    than reasoning about which live tests reach the wire.
    """
    node_path = getattr(request.node, "path", None) or request.node.fspath
    if not is_live_test_file(node_path):
        yield None
        return
    host = live_device_host(getattr(request, "module", None))
    if host is None:
        yield None
        return

    lock = DeviceLock(host, allow_nested=True)
    _device_lock_log.info(
        "acquiring device lock for %s (%s)", host, request.node.name
    )
    lock.acquire_or_raise(timeout=_lock_timeout())
    _device_lock_log.info("device lock acquired for %s (pid=%d)", host, os.getpid())
    try:
        yield lock
    finally:
        lock.release()
        _device_lock_log.info("device lock released for %s", host)


class MockTransport:
    """In-memory C64Transport for testing screen/keyboard/memory modules.

    Set ``screen_codes`` to control what ``read_screen_codes()`` returns.
    Set ``memory`` dict to control ``read_memory()`` responses.
    Inspect ``written_memory`` and ``injected_keys`` to verify writes.
    """

    def __init__(
        self,
        screen_codes: list[int] | None = None,
        cols: int = 40,
        rows: int = 25,
    ) -> None:
        self._cols = cols
        self._rows = rows
        total = cols * rows
        self._screen_codes = screen_codes if screen_codes is not None else [32] * total
        self.memory: dict[int, list[int]] = {}
        self.written_memory: list[tuple[int, list[int]]] = []
        self.injected_keys: list[list[int]] = []
        self.injected_joysticks: list[tuple[int, int]] = []
        self.speed_calls: list[int | None] = []
        self._speed: int | None = 1
        self.reset_calls: list[tuple[str, str | int | None]] = []

    @property
    def screen_cols(self) -> int:
        return self._cols

    @property
    def screen_rows(self) -> int:
        return self._rows

    @property
    def screen_codes(self) -> list[int]:
        return self._screen_codes

    @screen_codes.setter
    def screen_codes(self, codes: list[int]) -> None:
        self._screen_codes = codes

    def read_memory(self, addr: int, length: int) -> bytes:
        if addr in self.memory:
            data = self.memory[addr][:length]
            return bytes(data + [0] * (length - len(data)))
        return bytes(length)

    def write_memory(
        self,
        addr: int,
        data: bytes | list[int],
        *,
        override: str | None = None,
    ) -> None:
        self.written_memory.append((addr, list(data)))

    def read_screen_codes(self) -> list[int]:
        return list(self._screen_codes)

    def inject_keys(self, petscii_codes: list[int]) -> None:
        self.injected_keys.append(list(petscii_codes))

    def inject_joystick(self, port: int, value: int) -> None:
        self.injected_joysticks.append((port, value))

    def read_framebuffer(self) -> dict:
        return {"debug_rect": (0, 0, 0, 0), "inner_rect": (0, 0, 0, 0), "bpp": 0, "palette": 0, "bytes": b""}

    def read_palette(self) -> list[tuple[int, int, int]]:
        return []

    def resume(self) -> None:
        pass

    def set_speed(self, multiplier: int | None) -> None:
        self.speed_calls.append(multiplier)
        self._speed = multiplier

    def get_speed(self) -> int | None:
        return self._speed

    def reset(self, scope: str = "cpu", *, drive: str | int | None = None) -> None:
        self.reset_calls.append((scope, drive))

    def close(self) -> None:
        pass


@pytest.fixture
def mock_transport():
    """Create a MockTransport with a blank screen (all spaces)."""
    return MockTransport()


@pytest.fixture
def labels_path():
    """Path to the real labels.txt fixture file."""
    import pathlib
    return pathlib.Path(__file__).parent / "fixtures" / "labels.txt"


def connect_binary_transport(
    port: int,
    timeout: float = 30.0,
    proc: ViceProcess | None = None,
    **kwargs,
) -> BinaryViceTransport:
    """Connect a BinaryViceTransport with retries.

    Polls until VICE's binary monitor accepts the persistent TCP connection.
    Keeps the first successful connection open — which is the correct
    lifecycle for the binary monitor.
    """
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        if proc is not None and proc._proc is not None and proc._proc.poll() is not None:
            raise RuntimeError("VICE process exited during binary monitor connect")
        try:
            return BinaryViceTransport(port=port, **kwargs)
        except Exception as e:
            last_err = e
            time.sleep(1)
    raise ConnectionError(
        f"Could not connect to VICE binary monitor on port {port} "
        f"within {timeout}s: {last_err}"
    )


@pytest.fixture(scope="module")
def binary_transport():
    """Boot VICE with binary monitor, yield a live BinaryViceTransport."""
    require_vice_or_skip()

    allocator = PortAllocator(port_range_start=6511, port_range_end=6531)
    port = allocator.allocate()
    reservation = allocator.take_socket(port)
    if reservation is not None:
        reservation.close()

    config = ViceConfig(
        port=port, warp=True, sound=False,
    )

    with ViceProcess(config) as vice:
        transport = connect_binary_transport(port, proc=vice)
        try:
            yield transport
        finally:
            transport.close()
            allocator.release(port)


# ---------------------------------------------------------------------------
# Bridge networking fixtures (two VICE instances on a platform-specific
# bridge — Linux TAP / macOS feth; see ``tests/bridge_platform.py``)
# ---------------------------------------------------------------------------

# Default MACs and IPs used by the bridge_vice_pair fixture
BRIDGE_MAC_A = bytes.fromhex("02C640000001")
BRIDGE_MAC_B = bytes.fromhex("02C640000002")
# Reserved harness range; see tests/bridge_platform.py.
from bridge_platform import BRIDGE_IP_A, BRIDGE_IP_B  # noqa: E402,F401


def _bridge_wait_ready(transport: BinaryViceTransport, timeout: float = 30.0) -> None:
    from c64_test_harness.screen import ScreenGrid
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            transport.resume()
            time.sleep(2.0)
            grid = ScreenGrid.from_transport(transport)
            if "READY" in grid.continuous_text().upper():
                return
        except Exception:
            time.sleep(1.0)
    raise AssertionError("BASIC READY prompt not found within timeout")


def _bridge_init_cs8900a(transport: BinaryViceTransport, scratch: int, code: int) -> None:
    """Initialise CS8900a for promiscuous RX + SerTxON/SerRxON.

    *code* is the load address used for the helper routines (e.g. 0xC000).
    *scratch* is a 2-byte scratch area for reading LineCTL (e.g. 0xC1E0).
    """
    from c64_test_harness.bridge_ping import (
        cs8900a_read_linectl_code,
        cs8900a_rxctl_code,
        cs8900a_write_linectl_code,
    )
    from c64_test_harness.execute import jsr, load_code
    from c64_test_harness.memory import read_bytes

    load_code(transport, code, cs8900a_rxctl_code())
    jsr(transport, code, timeout=5.0)
    load_code(transport, code, cs8900a_read_linectl_code(scratch))
    jsr(transport, code, timeout=5.0)
    linectl = read_bytes(transport, scratch, 2)
    load_code(transport, code, cs8900a_write_linectl_code(linectl[0] | 0xC0, linectl[1]))
    jsr(transport, code, timeout=5.0)


@pytest.fixture(scope="module")
def bridge_vice_pair():
    """Launch two VICE instances with RR-Net ethernet on an L2 bridge.

    Yields ``(transport_a, transport_b)`` -- both connected, at BASIC
    READY, CS8900a initialised, and with unique MACs programmed
    (``BRIDGE_MAC_A`` on interface A, ``BRIDGE_MAC_B`` on interface B).

    Platform dispatch lives in ``tests/bridge_platform.py``:
      - Linux: ``tap-c64-0``/``tap-c64-1`` + ``tuntap`` driver
      - macOS: ``feth0``/``feth1`` + ``pcap`` driver

    Skipped automatically if ``x64sc`` is not on PATH or if the required
    interfaces are not present. See ``docs/bridge_networking.md`` for the
    full pattern.
    """
    from bridge_platform import (
        ETHERNET_DRIVER,
        IFACE_A,
        IFACE_B,
        SETUP_HINT,
        iface_present,
    )

    if shutil.which("x64sc") is None:
        pytest.skip("x64sc not found on PATH")
    if not iface_present(IFACE_A):
        pytest.skip(f"{IFACE_A} not found ({SETUP_HINT})")
    if not iface_present(IFACE_B):
        pytest.skip(f"{IFACE_B} not found ({SETUP_HINT})")

    from c64_test_harness.ethernet import set_cs8900a_mac

    code = 0xC000
    scratch = 0xC1E0

    allocator = PortAllocator(port_range_start=6560, port_range_end=6580)
    port_a = allocator.allocate()
    port_b = allocator.allocate()
    res_a = allocator.take_socket(port_a)
    if res_a is not None:
        res_a.close()
    res_b = allocator.take_socket(port_b)
    if res_b is not None:
        res_b.close()

    config_a = ViceConfig(
        port=port_a, warp=False, sound=False,
        ethernet=True, ethernet_mode="rrnet",
        ethernet_interface=IFACE_A,
        ethernet_driver=ETHERNET_DRIVER,
    )
    config_b = ViceConfig(
        port=port_b, warp=False, sound=False,
        ethernet=True, ethernet_mode="rrnet",
        ethernet_interface=IFACE_B,
        ethernet_driver=ETHERNET_DRIVER,
    )

    vice_a = ViceProcess(config_a)
    vice_b = ViceProcess(config_b)

    try:
        vice_a.start()
        vice_b.start()
        transport_a = connect_binary_transport(port_a, proc=vice_a)
        transport_b = connect_binary_transport(port_b, proc=vice_b)
        try:
            _bridge_wait_ready(transport_a)
            _bridge_wait_ready(transport_b)
            _bridge_init_cs8900a(transport_a, scratch, code)
            _bridge_init_cs8900a(transport_b, scratch, code)
            set_cs8900a_mac(transport_a, BRIDGE_MAC_A)
            set_cs8900a_mac(transport_b, BRIDGE_MAC_B)
            yield transport_a, transport_b
        finally:
            transport_a.close()
            transport_b.close()
    finally:
        vice_a.stop()
        vice_b.stop()
        allocator.release(port_a)
        allocator.release(port_b)


# ---------------------------------------------------------------------------
# The VICE live gate (``C64_REQUIRE_VICE``)
# ---------------------------------------------------------------------------
#
# Every VICE live module used to carry its own
# ``pytest.mark.skipif(shutil.which("x64sc") is None, ...)``.  Eighteen
# copies of one predicate, and every one of them fails *open*: on a
# machine with no emulator the entire live VICE surface skips, the run
# is green, and what remains certifying the backend is the mocked layer
# -- which asserts the harness against its own assumptions.  That is a
# silent, total loss of coverage that looks exactly like success.
#
# The gate below replaces those copies.  Two properties matter:
#
# * **An operator can demand an emulator.**  With ``C64_REQUIRE_VICE=1``
#   a missing ``x64sc`` is a hard failure, not a skip.  A developer who
#   legitimately has no emulator leaves it unset and still gets the
#   bare skip.
#
# * **It is a class-level guard, not an instance fix.**  The
#   end-of-session check fires when *zero* live tests ran for any reason
#   at all -- a missing binary, an import error, a stray ``skipif``
#   someone adds next year, a ``-k`` that accidentally deselects the lot.
#   No future module can quietly opt out of it, because nothing has to
#   be remembered at the call site.
#
# **How much rides on it, measured -- once, and the figure is dated.**
# The argv contract needs ``x64sc -help`` and is therefore live.  A
# mutation sweep (``scripts/mutation/README.md``) put a number on what
# that means at the time: 41 flag mutations across 23 flag families
# survived the entire non-live suite and were killed only once the
# contract ran -- ``-binarymonitor``, ``-remotemonitor*``, ``-sound*``,
# ``-ethernetio*``, ``-addconfig``, ``-drive8type``, ``-default``,
# ``-warp``, ``-ntsc``/``-pal``, ``+saveres``, ``-jamaction``,
# ``-speed``, ``-limitcycles`` and more.  That sweep predates the
# generator's hyphen fix (9bfb005) and its no-op exclusions (7873cb6),
# and was taken against a population anchored at 6868168, which is not
# an ancestor of this branch; treat "41 across 23" as illustrating the
# shape, not as a current count.  The README carries the current
# population and the commit it was counted at.
#
# The shape is what matters here: on a machine with no emulator those
# families are validated by nothing at all, and this gate is the only
# thing standing between the project and their silent unvalidation.
# That also reframes the ``--ignore`` escape closed below: it was not a
# tidy edge case but a way to switch off the sole guard for those flag
# families and still exit green.

#: Env var by which an operator declares an emulator must be present.
REQUIRE_VICE_ENV = "C64_REQUIRE_VICE"

#: Marker naming a test that needs a real ``x64sc``.
VICE_LIVE_MARKER = "vice_live"

_TRUTHY = {"1", "true", "yes", "on"}

#: Count of live VICE tests whose body actually *executed*, read by the
#: end-of-session check.  Incremented from the report hook rather than
#: from setup: a test that setup lets through can still be skipped by a
#: later mark, and counting intent instead of execution is precisely the
#: vacuous pass this gate exists to stop.
_vice_live_ran = 0


def vice_is_required() -> bool:
    """Whether the operator has declared an emulator must be present."""
    return os.environ.get(REQUIRE_VICE_ENV, "").strip().lower() in _TRUTHY


def vice_missing_reason() -> str | None:
    """Why a VICE live test cannot run here, or ``None`` if it can."""
    if shutil.which("x64sc") is None:
        return "x64sc not found on PATH"
    return None


def require_vice_or_skip() -> None:
    """Gate a live VICE code path: skip, or fail when one was demanded.

    For fixtures and helpers that cannot carry a marker.  Test *modules*
    should use ``pytestmark = pytest.mark.vice_live`` instead.
    """
    reason = vice_missing_reason()
    if reason is None:
        return
    if vice_is_required():
        pytest.fail(
            f"{REQUIRE_VICE_ENV}=1 declares an emulator must be present, "
            f"but {reason}. Refusing to skip: this run would certify the "
            f"VICE backend entirely from mocks.",
            pytrace=False,
        )
    pytest.skip(reason)


# ---------------------------------------------------------------------------
# The elevation marker (``elevation(kind, **kwargs)``) and its gate
# ---------------------------------------------------------------------------
#
# Ten live test files each grew their own ad hoc ``skipif`` for one of
# four elevated prerequisites: (a) ``vice_root`` -- a NOPASSWD sudoers
# rule for the exact x64sc path VICE's ethernet driver needs root for;
# (b) ``bridge_scripts`` -- NOPASSWD rules for the bridge lifecycle
# scripts; (c) ``bpf_nodes`` -- a world-rw ``/dev/bpf*`` node for
# host-side capture; (d) ``bridge_iface`` -- the bridge interface(s)
# present and up.  On a bench missing one, the affected tests silently
# skip and the reason is visible only under ``-rs``.
#
# ``@pytest.mark.elevation(kind, **kwargs)`` replaces the ad hoc copies
# with one probe per (kind, kwargs), evaluated at most once per session
# (``check_elevation`` caches on it), and a session-end notice that
# always prints -- no ``-rs`` required -- naming the remedy verbatim.
# The kwargs let a test override what the default probe checks (a
# specific binary path, a specific interface set) while still sharing
# the one cache and the one notice.
#
# ``C64_REQUIRE_ELEVATION=1`` mirrors ``C64_REQUIRE_VICE`` above: a
# missing prerequisite becomes a hard failure at setup instead of a
# skip, and a green exit status is escalated 0 -> 1 (never masking
# INTERRUPTED=2 or INTERNAL_ERROR=3 -- the same rule the VICE gate
# follows, for the same reason: a worse status is already saying
# something this gate must not paper over).

ELEVATION_MARKER = "elevation"
REQUIRE_ELEVATION_ENV = "C64_REQUIRE_ELEVATION"

#: ``(nodeid, kind, remedy)`` for every test the gate skipped or failed
#: this session -- read by the end-of-session notice.
_elevation_skips: list[tuple[str, str, str]] = []

#: One probe result per ``(kind, sorted(kwargs.items()))`` -- a probe
#: never runs twice for the same question in one session.
_elevation_cache: dict[tuple, tuple[bool, str]] = {}


def elevation_is_required() -> bool:
    """Whether the operator has declared every elevated prerequisite
    must already be satisfied on this bench."""
    return os.environ.get(REQUIRE_ELEVATION_ENV, "").strip().lower() in _TRUTHY


def _resolve_ethernet_binary() -> str:
    from c64_test_harness.backends.vice_lifecycle import ethernet_vice_binary
    return ethernet_vice_binary() or shutil.which("x64sc") or "x64sc"


@lru_cache(maxsize=1)
def _canonical_repo_root() -> str:
    """The main (non-worktree) checkout's absolute path.

    The sudo'd bridge lifecycle scripts are allowlisted -- and any
    NOPASSWD sudoers rule is written -- against this path, never a
    worktree's copy under ``.claude/worktrees/agent-*`` (see CLAUDE.md
    "Permission notes for agents"). ``git worktree list``'s first row is
    always the main worktree, regardless of which worktree this process
    happens to be running from.
    """
    try:
        out = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return os.getcwd()
    for line in (out.stdout or "").splitlines():
        if line.startswith("worktree "):
            return line[len("worktree "):].strip()
    return os.getcwd()


def _probe_vice_root(binary: str | None = None) -> tuple[bool, str]:
    """(a): a NOPASSWD sudoers rule for the exact x64sc path.

    *binary* overrides the default resolved ethernet binary -- for a
    test that needs a specific bench binary (e.g. the Homebrew path on
    macOS) rather than whatever ``ethernet_vice_binary()``/PATH resolve
    to here.
    """
    from c64_test_harness.backends.vice_elevation import (
        _sudoers_entry,
        launch_path,
        rawnet_capability,
        sudo_can_run,
    )
    if rawnet_capability():
        return True, ""
    resolved = launch_path(binary or _resolve_ethernet_binary())
    if binary is not None and not os.path.exists(resolved):
        return False, f"{resolved} not found on this host"
    if sudo_can_run(resolved):
        return True, ""
    entry = _sudoers_entry(resolved)
    return False, (
        f"'sudo -n' is not authorised for {resolved}. Add to sudoers "
        f"(visudo; the rule must name this exact path and must not be "
        f"bash-wrapped):\n    {entry}"
    )


#: Canonical bridge-lifecycle scripts per platform (see docs/development.md
#: "Passwordless sudo").  ``bridge_iface``'s ``SETUP_HINT`` names only one
#: of these; this is the full NOPASSWD set the harness actually drives.
_BRIDGE_SCRIPT_SETS = {
    "darwin": (
        "setup-bridge-feth-macos.sh",
        "teardown-bridge-feth-macos.sh",
        "cleanup-bridge-feth-macos.sh",
    ),
    "linux": (
        "setup-bridge-tap.sh",
        "teardown-bridge-tap.sh",
        "cleanup-bridge-networking.sh",
    ),
}


def _probe_bridge_scripts(scripts: tuple[str, ...] | None = None) -> tuple[bool, str]:
    """(b): NOPASSWD rules for the bridge lifecycle scripts.

    Probed at their canonical repo path -- never a worktree's -- because
    that is the only path a NOPASSWD rule can be written against (see
    CLAUDE.md "Permission notes for agents").
    """
    from c64_test_harness.backends.vice_elevation import _sudoers_entry, sudo_can_run
    names = scripts or _BRIDGE_SCRIPT_SETS.get(sys.platform, _BRIDGE_SCRIPT_SETS["linux"])
    root = _canonical_repo_root()
    paths = [os.path.join(root, "scripts", name) for name in names]
    missing = [p for p in paths if not sudo_can_run(p)]
    if not missing:
        return True, ""
    lines = "\n".join(f"    {_sudoers_entry(p)}" for p in missing)
    return False, (
        f"'sudo -n' is not authorised for: {', '.join(missing)}. Add to "
        f"sudoers (visudo; each rule must name its exact path and must "
        f"not be bash-wrapped):\n{lines}"
    )


def _probe_bpf_nodes() -> tuple[bool, str]:
    """(c): a world-rw ``/dev/bpf*`` node for host-side capture.

    Opens -- and immediately closes -- at most one node, matching the
    same node the real capture path would take.  A no-op (satisfied) on
    every non-Darwin platform, where this prerequisite does not exist.
    """
    if sys.platform != "darwin":
        return True, ""
    from c64_test_harness.capture import CaptureUnavailable, _close_node, _open_first_bpf
    try:
        fd, _path = _open_first_bpf()
    except CaptureUnavailable as e:
        return False, e.remedy or str(e)
    _close_node(fd)
    return True, ""


def _probe_bridge_iface(ifaces: tuple[str, ...] | None = None) -> tuple[bool, str]:
    """(d): the bridge interface(s) present and up.

    Defaults to the platform's full pair-plus-bridge
    (``IFACE_A``, ``IFACE_B``, ``BRIDGE_NAME``); *ifaces* overrides it
    for a test that only depends on a subset.
    """
    import bridge_platform as bp
    names = ifaces or (bp.IFACE_A, bp.IFACE_B, bp.BRIDGE_NAME)
    missing = [n for n in names if not bp.iface_present(n)]
    if not missing:
        return True, ""
    return False, f"{', '.join(missing)} not found ({bp.SETUP_HINT})"


_ELEVATION_PROBES = {
    "vice_root": _probe_vice_root,
    "bridge_scripts": _probe_bridge_scripts,
    "bpf_nodes": _probe_bpf_nodes,
    "bridge_iface": _probe_bridge_iface,
}


def check_elevation(kind: str, **kwargs) -> tuple[bool, str]:
    """Probe *kind* at most once per session; returns ``(ok, remedy)``."""
    probe = _ELEVATION_PROBES.get(kind)
    if probe is None:
        raise ValueError(
            f"unknown elevation kind {kind!r}; expected one of "
            f"{sorted(_ELEVATION_PROBES)}"
        )
    key = (kind, tuple(sorted(kwargs.items())))
    if key not in _elevation_cache:
        _elevation_cache[key] = probe(**kwargs)
    return _elevation_cache[key]


def gate_elevation(nodeid: str, kind: str, remedy: str) -> None:
    """Skip *nodeid* -- or fail it, under ``C64_REQUIRE_ELEVATION=1`` --
    for a missing *kind* prerequisite, and record it for the
    end-of-session notice.  *remedy* is shown verbatim: it must already
    be the exact command the operator needs, not a paraphrase.
    """
    _elevation_skips.append((nodeid, kind, remedy))
    text = f"elevation required ({kind}): {remedy}"
    if elevation_is_required():
        pytest.fail(text, pytrace=False)
    pytest.skip(text)


def start_vice_or_skip(config, nodeid: str) -> ViceProcess:
    """Start a :class:`ViceProcess`, converting a mid-launch
    :class:`~c64_test_harness.backends.vice_elevation.ViceElevationRequiredError`
    into the same skip-or-fail + record the ``elevation("vice_root")``
    marker uses.

    Shared by every module-scoped ethernet fixture that launches VICE
    with ``ethernet=True`` (this module's ``bridge_vice_pair``,
    ``test_ethernet.py``'s ``vice_ethernet``,
    ``test_ethernet_bridge.py``'s ``vice_bridge_pair``): a bypassed
    preflight probe (``MACOS_PCAP_ENABLED=1``) means
    ``ViceProcess.start()`` itself is the last line of defence and can
    still refuse mid-fixture.
    """
    from c64_test_harness.backends.vice_elevation import ViceElevationRequiredError
    vice = ViceProcess(config)
    try:
        vice.start()
    except ViceElevationRequiredError as exc:
        gate_elevation(nodeid, "vice_root", str(exc))
    return vice


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{VICE_LIVE_MARKER}: needs a real x64sc; gated by ${REQUIRE_VICE_ENV}",
    )
    config.addinivalue_line(
        "markers",
        f"{ELEVATION_MARKER}(kind, **kwargs): needs an elevated prerequisite "
        f"({', '.join(sorted(_ELEVATION_PROBES))}); gated by "
        f"${REQUIRE_ELEVATION_ENV}",
    )


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Enforce the VICE and elevation gates before pytest's own skipif."""
    if item.get_closest_marker(VICE_LIVE_MARKER) is not None:
        require_vice_or_skip()
    for marker in item.iter_markers(ELEVATION_MARKER):
        if not marker.args:
            raise TypeError(
                f"{item.nodeid}: @pytest.mark.elevation(...) needs a kind "
                f"as its first positional argument"
            )
        kind, kwargs = marker.args[0], dict(marker.kwargs)
        ok, remedy = check_elevation(kind, **kwargs)
        if not ok:
            gate_elevation(item.nodeid, kind, remedy)


def pytest_runtest_logreport(report):
    """Count live tests that genuinely ran a body (passed or failed).

    A ``pytest.skip()`` *inside* the body still produces a ``call``
    report, with ``outcome == "skipped"``.  It exercised no emulator, so
    it must not count -- otherwise a run made entirely of such skips
    would satisfy the gate while certifying the backend from mocks.
    """
    global _vice_live_ran
    if (
        report.when == "call"
        and report.outcome != "skipped"
        and VICE_LIVE_MARKER in getattr(report, "keywords", {})
    ):
        _vice_live_ran += 1


def _elevation_group() -> dict[tuple[str, str], int]:
    """(kind, remedy) -> count, for the notice below."""
    counts: dict[tuple[str, str], int] = {}
    for _nodeid, kind, remedy in _elevation_skips:
        counts[(kind, remedy)] = counts.get((kind, remedy), 0) + 1
    return counts


def _elevation_sessionfinish(session, exitstatus) -> None:
    """Print the elevation notice and, if required, escalate the exit
    status -- unconditionally, unlike the VICE gate below: this fires
    whenever anything was skipped for elevation this session, not only
    when nothing at all ran.
    """
    if not _elevation_skips:
        return
    if elevation_is_required() and session.exitstatus == 0:
        session.exitstatus = 1
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        return
    reporter.write_sep(
        "=",
        f"ELEVATION REQUIRED: {len(_elevation_skips)} test(s) skipped",
        red=True,
    )
    for (kind, remedy), count in sorted(_elevation_group().items()):
        reporter.write_line(f"[{kind}] {count} test(s): {remedy}")


def pytest_sessionfinish(session, exitstatus):
    """Fail a required run in which no live VICE test actually executed.

    The missing-binary case is already caught per-test.  This catches
    every *other* way the surface can vanish, without having to
    anticipate the reason.

    **It deliberately does not exempt a run that collected no live tests.**
    An earlier version returned early in that case, to leave room for
    someone deliberately running a single mocked module.  That exemption
    was the hole: ``pytest --ignore=tests/test_vice_core.py ...`` and
    ``pytest tests/test_vice_binary_unit.py`` are both ordinary CI
    invocations, both collect zero live tests, and both therefore exited
    0 -- producing exactly the mocks-only green run the gate exists to
    prevent, silently.

    **The trade-off chosen.**  ``C64_REQUIRE_VICE=1`` now means "this
    invocation must exercise a real emulator", and any run that does not
    fails -- including running one mocked module, or a module with no
    VICE tests at all.  The escape is to not make that claim: leave the
    variable unset for subset and development runs, and set it on the
    invocation that is supposed to cover the backend.  That is stricter
    than necessary for a developer typing one module name, and it is the
    only version that cannot be opted out of by an argument list, which
    is the property being bought.
    """
    _elevation_sessionfinish(session, exitstatus)
    if not vice_is_required() or _vice_live_ran:
        return
    # A run that collected nothing at all has a different problem (an
    # empty selection or a collection error), already reported as such.
    if session.testscollected == 0:
        return
    collected = getattr(session, "_vice_live_collected", 0)
    # Escalate a green run to a failure; never overwrite a status that is
    # already saying something worse (INTERRUPTED=2, INTERNAL_ERROR=3), or
    # a run that already failed on its own merits.
    if session.exitstatus == 0:
        session.exitstatus = 1
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        detail = (
            f"{collected} were collected but none ran"
            if collected
            else "none were even collected — this selection contains no "
                 "live VICE tests"
        )
        reporter.write_sep(
            "=",
            f"{REQUIRE_VICE_ENV}=1 declares this run must exercise a real "
            f"emulator, but no {VICE_LIVE_MARKER} test ran ({detail}). "
            f"The VICE surface was certified by mocks alone. Unset "
            f"{REQUIRE_VICE_ENV} for a deliberately mocked-only run.",
            red=True,
        )


def pytest_collection_modifyitems(session, config, items):
    """Record how many live VICE tests this run intended to execute."""
    session._vice_live_collected = sum(
        1 for i in items if i.get_closest_marker(VICE_LIVE_MARKER) is not None
    )

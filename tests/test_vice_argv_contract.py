"""Every flag the harness can emit must be a flag VICE actually accepts.

This is a **class-level guard**, not a set of per-flag assertions.  The
defect it exists to catch has already happened once at scale: six flag
names were added in af479ee with unit tests that asserted the flag text
appeared in a mocked ``Popen`` argv, and every one of the names was
wrong.  Four aborted VICE outright; two silently bound to a different
resource.  The mocks certified them green for months, because an argv
assertion's oracle is *the string the author typed* — it can detect
drift from the author's intent, and it can never detect that the intent
was wrong.

So these tests do not enumerate flags.  They enumerate the *config
surface*, ask ``ViceProcess`` what it would launch, and check every
token that comes back against VICE itself.  A flag invented next year is
caught with nobody having anticipated it.

VICE rejects a bad flag in three distinct ways (VICE 3.10 source), and
no single oracle sees all three:

* **Unknown name.** ``-soundrecord`` does not exist.  Absent from
  ``-help``; caught by :func:`test_every_emitted_flag_is_a_real_vice_option`.
* **Silently prefix-matched.** ``lookup()`` (S ``cmdline.c:172-196``)
  accepts any unambiguous prefix, so ``-eventsnapshot 1`` set
  ``EventSnapshotDir="1"`` and VICE started up perfectly normally.  Also
  absent from ``-help`` as an exact string, so the same test catches it —
  which is precisely why that test matches **exactly** and never by
  prefix.
* **Present in ``-help`` but rejected at parse time.**  ``-ethernetcart``
  is listed by ``-help`` and still dies with "Option '-ethernetcart' not
  valid."  No amount of ``-help`` parsing sees this one; only a real
  launch does.  That is :func:`test_a_fully_populated_config_launches`.

Both guards carry a negative control that seeds a known-bad flag and
proves the guard fails on it.  A guard that cannot fail is worth less
than no guard, because it also reports success.
"""

from __future__ import annotations

import re
import shutil
import socket
import subprocess
from functools import lru_cache
from unittest.mock import MagicMock, patch

import pytest
from conftest import connect_binary_transport

from c64_test_harness.backends.vice_elevation import sudo_authorisation, vice_features
from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess

pytestmark = pytest.mark.vice_live


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# ---------------------------------------------------------------------------
# The oracle: VICE's own option table
# ---------------------------------------------------------------------------

#: An option line in ``-help`` starts at column 0 with ``-``/``+`` and may
#: contain internal hyphens (``-autostart-warp``) and digits (``-8``).
_OPTION_LINE = re.compile(r"^([-+][A-Za-z0-9][A-Za-z0-9-]*)", re.M)


@lru_cache(maxsize=1)
def vice_option_names() -> frozenset[str]:
    """Every option name ``x64sc -help`` lists, exactly as written."""
    binary = shutil.which("x64sc")
    assert binary is not None, "gated by the vice_live marker"
    proc = subprocess.run(
        [binary, "-help"], capture_output=True, text=True, timeout=60
    )
    return frozenset(_OPTION_LINE.findall(proc.stdout))


def test_the_help_oracle_is_not_vacuous():
    """A dead oracle must not be able to produce a passing run.

    If ``-help`` parsing silently returned everything (or the launch
    changed so the text no longer parses), every flag would "resolve" and
    the guard below would pass while measuring nothing.  Pin both ends:
    a name VICE really has, and a name it cannot possibly have.
    """
    options = vice_option_names()
    assert len(options) > 500, f"implausibly small option table: {len(options)}"
    assert "-autostart-warp" in options, "known-good option went missing"
    assert "-definitelynotavicerflag" not in options, "oracle accepts anything"


# ---------------------------------------------------------------------------
# Capturing what a config would launch
# ---------------------------------------------------------------------------

def _prewarm_elevation_caches() -> None:
    """Run the elevation probes *before* ``Popen`` is patched.

    ``vice_features`` and ``sudo_authorisation`` shell out, and a blanket
    ``subprocess.Popen`` patch breaks ``subprocess.run`` underneath them
    (it unpacks ``communicate()``'s return).  Both are cached, so calling
    them here keeps the patch confined to the spawn under test.
    """
    binary = shutil.which("x64sc")
    if binary is not None:
        vice_features(binary)
    sudo_authorisation()


def emitted_argv(cfg: ViceConfig) -> list[str]:
    """The argv *cfg* would launch, without launching it."""
    _prewarm_elevation_caches()
    proc = ViceProcess(cfg)
    with patch("subprocess.Popen") as mock_popen:
        mock_popen.return_value = MagicMock()
        proc.start()
        argv = list(mock_popen.call_args[0][0])
    proc._proc = None  # never let stop() touch the mock
    proc._cleanup_tmp_vicerc()
    return argv


def emitted_flags(cfg: ViceConfig) -> set[str]:
    """Flag tokens in *cfg*'s argv, with any ``sudo`` wrapper stripped.

    A token is a flag when it leads with ``-``/``+`` **and** the harness
    put it there as an option rather than as a value.  Values in this
    argv are paths, integers and ``ip4://`` URLs, none of which lead with
    a sign — except ``sudo``'s own ``-n``, which is not VICE's to
    validate and is dropped with the wrapper.
    """
    argv = emitted_argv(cfg)
    if argv and argv[0].endswith("sudo"):
        argv = argv[2:]  # drop "sudo" and its "-n"
    return {tok for tok in argv[1:] if re.match(r"^[-+][A-Za-z0-9]", tok)}


# ---------------------------------------------------------------------------
# The config surface: every branch in ViceProcess.start() that emits a flag
# ---------------------------------------------------------------------------

#: Named configs chosen to cover every flag-emitting branch, including the
#: mutually exclusive ones (``sounddev`` set vs unset, ``console`` vs
#: ``minimize``, ``warp`` on vs off, disk attached vs not).  A branch that
#: no config here reaches is a flag no test validates, so adding a config
#: is the cost of adding a flag.
CONFIG_SURFACE: dict[str, ViceConfig] = {
    "defaults": ViceConfig(port=6510),
    "text-monitor": ViceConfig(port=6510, text_monitor_port=6520),
    "warp-and-autostart": ViceConfig(port=6510, prg_path="/tmp/a.prg", warp=True),
    "no-warp": ViceConfig(port=6510, warp=False),
    "ntsc": ViceConfig(port=6510, ntsc=True),
    "pal": ViceConfig(port=6510, ntsc=False),
    "console": ViceConfig(port=6510, console=True),
    "windowed-minimised": ViceConfig(port=6510, console=False, minimize=True),
    "sound-on": ViceConfig(port=6510, sound=True),
    "sound-device": ViceConfig(
        port=6510, sounddev="dummy", soundarg="x", soundrate=22050, soundoutput=1
    ),
    "ambient-config": ViceConfig(port=6510, load_user_config=True),
    "limit-cycles": ViceConfig(port=6510, limit_cycles=1_000_000),
    "seed": ViceConfig(port=6510, seed=1234),
    "event-replay": ViceConfig(
        port=6510,
        event_snapshot_mode=2,
        event_snapshot_dir="/tmp/snaps",
        event_image_include=False,
    ),
    "event-image-include-on": ViceConfig(port=6510, event_image_include=True),
    "sound-recording": ViceConfig(
        port=6510, sound_record_driver="wav", sound_record_file="/tmp/o.wav"
    ),
    "exit-screenshot": ViceConfig(port=6510, exit_screenshot="/tmp/e.png"),
    "ethernet": ViceConfig(
        port=6510, ethernet=True, ethernet_interface="feth0", ethernet_driver="pcap"
    ),
}


@pytest.mark.parametrize("name", sorted(CONFIG_SURFACE))
def test_every_emitted_flag_is_a_real_vice_option(name):
    """Exact-match every emitted flag against VICE's own option table.

    Matching exactly is the whole point.  VICE's parser accepts unambiguous
    *prefixes*, so a prefix-tolerant check here would accept exactly the
    names that silently bind to the wrong resource.
    """
    options = vice_option_names()
    unknown = sorted(emitted_flags(CONFIG_SURFACE[name]) - options)
    assert not unknown, (
        f"config {name!r} emits flags VICE 3.10 does not define: {unknown}. "
        f"VICE would reject the launch, or prefix-match it onto a different "
        f"resource and start up looking healthy."
    )


def test_the_flag_guard_catches_an_invented_flag():
    """Negative control for the test above.

    Seeds the exact defect class that shipped: a flag name that looks
    plausible and does not exist.  If this passes, the guard above is
    decorative.
    """
    cfg = ViceConfig(port=6510, extra_args=["-eventsnapshot", "1"])
    unknown = emitted_flags(cfg) - vice_option_names()
    assert "-eventsnapshot" in unknown, (
        "the guard failed to notice a flag that does not exist — it would "
        "not have caught the af479ee regression either"
    )


def test_the_flag_guard_catches_a_prefix_match():
    """A prefix of a real option is not a real option.

    ``-eventstart`` is a prefix of both ``-eventstartmode`` and
    ``-eventstartsnapshot``; VICE reports it as ambiguous.  A guard that
    matched by prefix would wave it through.
    """
    cfg = ViceConfig(port=6510, extra_args=["-eventstart", "1"])
    assert "-eventstart" in emitted_flags(cfg) - vice_option_names()


# ---------------------------------------------------------------------------
# The launch guard: -help is necessary but not sufficient
# ---------------------------------------------------------------------------

def launches_and_answers(cfg: ViceConfig) -> bool:
    """Whether *cfg* starts a VICE whose binary monitor answers.

    A flag VICE rejects makes the process exit before the monitor ever
    listens, so this is the only oracle that sees the "listed in -help
    but not valid" class.
    """
    proc = ViceProcess(cfg)
    proc.start()
    try:
        transport = connect_binary_transport(cfg.port, proc=proc, timeout=25.0)
        try:
            transport.resource_get("Speed")
            return True
        finally:
            transport.close()
    except Exception:
        return False
    finally:
        proc.stop()


def test_a_fully_populated_config_launches(tmp_path):
    """Every non-ethernet flag the harness emits, all at once, for real.

    Individually-valid flags can still be collectively fatal: VICE's
    ``cmdline_parse`` breaks at the first unconsumed argument
    (S ``cmdline.c:273``) and leaves everything after it unparsed, so an
    arity mistake anywhere poisons the rest of the line.  Only a launch
    that carries the whole line at once tests that.

    Ethernet is excluded deliberately: it needs elevation, and an
    unelevated ``-ethernetioif`` launch SIGSEGVs on a NULL rawnet driver
    rather than reporting anything useful.
    """
    cfg = ViceConfig(
        port=free_port(),
        text_monitor_port=free_port(),
        seed=99,
        warp=True,
        console=True,
        sound=False,
        limit_cycles=0,
        event_snapshot_mode=1,
        event_snapshot_dir=str(tmp_path),
        event_image_include=False,
        sound_record_driver="wav",
        sound_record_file=str(tmp_path / "o.wav"),
        exit_screenshot=str(tmp_path / "e.png"),
    )
    assert launches_and_answers(cfg), (
        "a config using every flag field at once failed to start a VICE "
        "whose monitor answers"
    )


def test_the_launch_guard_catches_a_flag_help_lists_but_vice_rejects():
    """Negative control for the test above, and the reason it exists.

    ``-ethernetcart`` is in ``-help`` and is rejected at parse time, so it
    passes the option-table guard and must fail this one.  Safe to seed:
    the launch dies during argument parsing, long before any rawnet
    driver is touched, so this never reaches the unelevated SIGSEGV.
    """
    assert "-ethernetcart" in vice_option_names(), (
        "premise changed: -ethernetcart is no longer listed by -help, so "
        "this no longer tests what it claims"
    )
    cfg = ViceConfig(port=free_port(), console=True, extra_args=["-ethernetcart"])
    assert not launches_and_answers(cfg), (
        "the launch guard failed to notice a flag VICE rejects at parse "
        "time — the one defect class -help cannot see"
    )


# ---------------------------------------------------------------------------
# Keeping the config surface honest
# ---------------------------------------------------------------------------

def _flag_literals_in_source() -> set[str]:
    """Every flag-shaped literal in ``ViceProcess.start``'s argv building.

    Read from the source rather than a hand-kept list, so a flag added
    next year is compared against the config surface automatically.

    Scoped to ``start`` on purpose.  A whole-module scan also picks up
    flags belonging to *other* programs the module runs -- ``ps -axo`` in
    the sudo-child resolver -- and asserting those against VICE's option
    table would be nonsense.  f-string fragments are skipped for the same
    reason: ``f"-drive{unit}type"`` contributes the stem ``-drive``, which
    is not a flag anyone emits.
    """
    import ast
    import inspect

    from c64_test_harness.backends import vice_lifecycle

    tree = ast.parse(inspect.getsource(vice_lifecycle))
    start = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "start"
    )
    # Constants inside an f-string are fragments, not flags.
    fragments = {
        id(c)
        for j in ast.walk(start)
        if isinstance(j, ast.JoinedStr)
        for c in ast.walk(j)
        if isinstance(c, ast.Constant)
    }
    return {
        node.value
        for node in ast.walk(start)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in fragments
        and re.fullmatch(r"[-+][a-z][a-z0-9-]{2,}", node.value)
    }


def test_the_config_surface_reaches_every_flag_the_source_can_emit():
    """No flag may exist in the source that no config here exercises.

    This is what makes the guard general rather than a list.  Without it,
    ``CONFIG_SURFACE`` is hand-maintained and a new flag added beside a
    new config field is validated by nothing — which is exactly the state
    the af479ee flags shipped in.  With it, adding a flag and not adding
    a config is a test failure.
    """
    reachable: set[str] = set()
    for cfg in CONFIG_SURFACE.values():
        reachable |= emitted_flags(cfg)
    missing = sorted(_flag_literals_in_source() - reachable)
    assert not missing, (
        f"vice_lifecycle.py can emit {missing}, but no entry in "
        f"CONFIG_SURFACE produces them, so no test checks them against "
        f"VICE. Add a config that exercises the branch."
    )


# ---------------------------------------------------------------------------
# Position, not just presence
# ---------------------------------------------------------------------------

#: Flags VICE handles in its pre-UI argv scan (S ``main.c:267-303``).  The
#: scan ``break``s at the first argument it does not recognise, so one of
#: these placed after any ordinary flag is silently ineffective — VICE
#: still starts, still parses the flag later as a resource option, and
#: does none of the thing the flag exists to do.
EARLY_SCAN_FLAGS = {
    "-default": "sets loadconfig=false before resources_load() (S main.c:285-291)",
    "-console": "sets console_mode, read by ui_init_with_args (S main.c:385)",
    "-seed": "calls lib_rand_seed(), reached from the early scan only",
}


@pytest.mark.parametrize("name", sorted(CONFIG_SURFACE))
def test_early_scan_flags_precede_every_other_flag(name):
    """An early-scan flag emitted late is a silent no-op, not an error.

    ``-console`` had exactly this defect: emitted after ``-autostart`` /
    ``-warp`` it left VICE opening a window on macOS while every argv
    assertion in the suite still passed, because presence was all anyone
    checked.  ``test_vice_headless`` now catches ``-console`` by measuring
    GUI-ness, but that is an instance fix — it says nothing about
    ``-seed``, which has the identical shape and no observable effect to
    measure.  This pins the rule instead of the instance, so a flag added
    to the early scan next year is covered on the day it is added.
    """
    argv = emitted_argv(CONFIG_SURFACE[name])
    if argv and argv[0].endswith("sudo"):
        argv = argv[2:]
    flags = [(i, t) for i, t in enumerate(argv[1:]) if re.match(r"^[-+][A-Za-z0-9]", t)]
    early = [i for i, t in flags if t in EARLY_SCAN_FLAGS]
    late = [i for i, t in flags if t not in EARLY_SCAN_FLAGS]
    if not early or not late:
        pytest.skip("config emits no early flag, or no ordinary flag")
    offenders = sorted(
        {argv[1:][i] for i in early if i > min(late)}
    )
    assert not offenders, (
        f"config {name!r} emits {offenders} after an ordinary flag. "
        f"VICE's pre-UI scan stops at the first argument it does not "
        f"recognise, so these are parsed too late to do anything: "
        + "; ".join(f"{f} {EARLY_SCAN_FLAGS[f]}" for f in offenders)
    )

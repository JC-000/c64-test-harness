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
* **Present in ``-help`` but rejected at parse time.**  ``-jamaction`` is
  listed by ``-help``, and ``-jamaction 99`` still dies with "Argument
  '99' not valid for option `-jamaction'" because the resource setter
  refuses it (S ``machine.c:461-473``, ``cmdline.c:262-269``).  No amount
  of ``-help`` parsing sees this one; only a real launch does.  That is
  :func:`test_a_fully_populated_config_launches`.  (An earlier revision
  named ``-ethernetcart`` here; that flag parses fine and the unelevated
  VICE it activates SIGSEGVs later -- a different failure entirely.)

Both guards carry a negative control that seeds a known-bad flag and
proves the guard fails on it.  A guard that cannot fail is worth less
than no guard, because it also reports success.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
from functools import lru_cache
from unittest.mock import MagicMock, patch

import pytest
from conftest import connect_binary_transport

from c64_test_harness.backends.vice_elevation import (
    ALLOW_UNELEVATED_ENV,
    sudo_authorisation,
    vice_features,
)
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
    """The argv *cfg* would launch, without launching it.

    ``plan_vice_launch`` runs for real here, and for an ethernet config it
    refuses outright when this user cannot ``sudo -n`` the binary.  That
    refusal protects a *launch*; nothing is launched, so it is lifted for
    the duration of the capture -- the flags a config emits do not depend
    on whether this host may run them elevated.  Any ``sudo -n`` wrapper
    the plan does add is stripped by :func:`emitted_flags`.
    """
    _prewarm_elevation_caches()
    proc = ViceProcess(cfg)
    with patch("subprocess.Popen") as mock_popen, \
            patch.dict(os.environ, {ALLOW_UNELEVATED_ENV: "1"}):
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


def test_the_argv_capture_does_not_depend_on_sudo_authorisation(monkeypatch):
    """Capturing an argv must never require the host to be able to run it.

    ``emitted_argv`` mocks only ``Popen``, so ``plan_vice_launch`` runs for
    real -- and for the ``ethernet`` entry it asks sudo whether this user
    may run x64sc as root without a password.  On any host without that
    NOPASSWD rule the plan raised ``ViceElevationRequiredError`` and three
    contract tests ERRORed before checking a single flag.  That is a
    guard switched off by the host's sudoers, which is the opposite of
    what a class-level guard is for: the flags exist whether or not this
    machine may launch them elevated.

    Simulated here rather than assumed: the sudo probe is forced to say
    no, and the capture must still return the ethernet flags.
    """
    from c64_test_harness.backends import vice_elevation as ve

    _prewarm_elevation_caches()
    monkeypatch.setattr(ve, "sudo_can_run", lambda binary: False)
    flags = emitted_flags(CONFIG_SURFACE["ethernet"])
    assert "-ethernetioif" in flags, flags


# ---------------------------------------------------------------------------
# The config surface: every branch in ViceProcess.start() that emits a flag
# ---------------------------------------------------------------------------

class _StubDisk:
    """Stands in for :class:`DiskImage` for argv purposes only.

    ``start()`` reads exactly two attributes off the disk image when it
    builds the command line, so a stub emits a byte-identical argv while
    needing neither ``c1541`` nor a real ``.d64`` on disk.  Real disk
    behaviour is covered live in ``tests/test_disk_vice.py``; what is
    under test *here* is only which flags come out.

    If ``start()`` ever grows a third call on the disk image this stub
    fails loudly with ``AttributeError`` rather than quietly diverging.
    """

    def __init__(self, path: str, drive_type: int = 1541) -> None:
        self.path = path
        self.drive_type = drive_type


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
    # Volume 0 is the interesting value, not a placeholder: it is what
    # headless_sid_config() passes, and 0 must survive the "is it set?"
    # test rather than being read as unset (issue #193).
    "sound-volume-zero": ViceConfig(port=6510, sound=True, soundvolume=0),
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
    # The disk branch interpolates its own flag names --
    # f"-{cfg.drive_unit}" and f"-drive{cfg.drive_unit}type" -- which is
    # the only place in start() where a flag name is *computed* rather
    # than written out.  Both units are covered because a single unit
    # would validate one interpolation and leave the arithmetic untested.
    "disk-drive-8": ViceConfig(
        port=6510, disk_image=_StubDisk("/tmp/a.d64"), drive_unit=8
    ),
    "disk-drive-9": ViceConfig(
        port=6510, disk_image=_StubDisk("/tmp/a.d64", drive_type=1571), drive_unit=9
    ),
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

    ``-jamaction 99`` is listed by ``-help`` and rejected at parse time,
    so it passes the option-table guard and must fail this one.  The
    rejection is genuine and traceable (VICE 3.10 source): ``-jamaction``
    is a ``SET_RESOURCE`` with ``NEED_ARGS`` (S ``machine.c:539-541``), so
    ``cmdline_parse`` hands the argument to ``resources_set_value_string``
    (S ``cmdline.c:248``), whose int setter ``set_jam_action`` returns -1
    for anything outside 0-5 (S ``machine.c:461-473``); ``cmdline_parse``
    then logs "Argument '99' not valid for option `-jamaction'" and
    returns -1 (S ``cmdline.c:262-269``), and ``initcmdline.c:527`` bails
    out.  No emulation starts, so the monitor never listens.

    An earlier version seeded ``-ethernetcart`` and claimed *it* was
    rejected at parse time.  It is not: it is a plain ``SET_RESOURCE``
    (S ``ethernetcart.c:434-436``), parsing succeeds, the cart activates,
    and an unelevated VICE then SIGSEGVs on the NULL rawnet driver
    (docs/vice_upstream_bugs.md § 2).  That test passed -- the monitor
    never answered -- but for the wrong reason, and by crashing a VICE.
    """
    assert "-jamaction" in vice_option_names(), (
        "premise changed: -jamaction is no longer listed by -help, so "
        "this no longer tests what it claims"
    )
    cfg = ViceConfig(port=free_port(), console=True, extra_args=["-jamaction", "99"])
    assert not launches_and_answers(cfg), (
        "the launch guard failed to notice an argument VICE rejects at "
        "parse time — the one defect class -help cannot see"
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

"""The ``elevation(kind, ...)`` marker and its gate in ``tests/conftest.py``.

Ten live test files each grew their own ad hoc ``skipif`` for one of four
elevated prerequisites (a NOPASSWD sudoers rule for the exact x64sc path,
NOPASSWD rules for the bridge lifecycle scripts, world-rw ``/dev/bpf*``
nodes, or the bridge interface being up).  On a bench missing one, the
affected tests silently skip and the reason is visible only under
``-rs``.  ``@pytest.mark.elevation(kind)`` replaces the ad hoc copies
with one probe per kind, evaluated once per session (cached), and a
session-end notice that always prints, naming the remedy verbatim.

Most of these tests drive the conftest hook functions directly with fake
item/session objects, matching ``tests/test_vice_live_gate.py``'s style.
A handful use ``pytester`` (enabled via ``pytest_plugins`` in conftest)
for end-to-end proof that a marked test really does skip through the
full pytest machinery, and that the session-end notice really is what
prints to the terminal.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import conftest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeMarker:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs


class _FakeItem:
    def __init__(self, nodeid, markers=(), vice_live=False):
        self.nodeid = nodeid
        self._markers = list(markers)
        self._vice_live = vice_live

    def get_closest_marker(self, name):
        if name == conftest.VICE_LIVE_MARKER and self._vice_live:
            return object()
        return None

    def iter_markers(self, name):
        if name == conftest.ELEVATION_MARKER:
            return iter(self._markers)
        return iter(())


def _session(*, exitstatus: int = 0, reporter=None):
    pm = SimpleNamespace(get_plugin=lambda name: reporter)
    return SimpleNamespace(exitstatus=exitstatus, config=SimpleNamespace(pluginmanager=pm))


class _FakeReporter:
    def __init__(self):
        self.seps: list[tuple] = []
        self.lines: list[str] = []

    def write_sep(self, sep, title, **kwargs):
        self.seps.append((sep, title, kwargs))

    def write_line(self, line):
        self.lines.append(line)


@pytest.fixture(autouse=True)
def fresh_elevation_state(monkeypatch):
    """Every test in this module gets a clean slate: the record list and
    the probe cache are both module-global and would otherwise leak
    between tests (and between this file and a real live run)."""
    monkeypatch.setattr(conftest, "_elevation_skips", [])
    monkeypatch.setattr(conftest, "_elevation_cache", {})
    monkeypatch.delenv(conftest.REQUIRE_ELEVATION_ENV, raising=False)
    yield


# ---------------------------------------------------------------------------
# Marker registration
# ---------------------------------------------------------------------------


def test_marker_is_registered_next_to_vice_live():
    config = SimpleNamespace(_lines=[])

    def addinivalue_line(section, line):
        assert section == "markers"
        config._lines.append(line)

    config.addinivalue_line = addinivalue_line
    conftest.pytest_configure(config)
    joined = "\n".join(config._lines)
    assert conftest.ELEVATION_MARKER in joined
    assert conftest.VICE_LIVE_MARKER in joined


# ---------------------------------------------------------------------------
# check_elevation: cache, at most once per session
# ---------------------------------------------------------------------------


def test_check_elevation_calls_the_probe_at_most_once(monkeypatch):
    calls = []

    def fake_probe(**kwargs):
        calls.append(kwargs)
        return False, "fix it"

    monkeypatch.setitem(conftest._ELEVATION_PROBES, "vice_root", fake_probe)
    conftest.check_elevation("vice_root")
    conftest.check_elevation("vice_root")
    conftest.check_elevation("vice_root")
    assert len(calls) == 1


def test_check_elevation_caches_per_distinct_kwargs(monkeypatch):
    calls = []

    def fake_probe(**kwargs):
        calls.append(kwargs)
        return False, "fix it"

    monkeypatch.setitem(conftest._ELEVATION_PROBES, "vice_root", fake_probe)
    conftest.check_elevation("vice_root", binary="/a/x64sc")
    conftest.check_elevation("vice_root", binary="/a/x64sc")
    conftest.check_elevation("vice_root", binary="/b/x64sc")
    assert len(calls) == 2


def test_check_elevation_returns_the_probe_result(monkeypatch):
    monkeypatch.setitem(
        conftest._ELEVATION_PROBES, "vice_root", lambda **k: (True, "")
    )
    assert conftest.check_elevation("vice_root") == (True, "")


def test_check_elevation_rejects_an_unknown_kind():
    with pytest.raises(ValueError):
        conftest.check_elevation("not_a_real_kind")


# ---------------------------------------------------------------------------
# N3 (adversarial review): _probe_vice_root must existence-check the
# binary on BOTH the default-resolution path and the binary= override
# path. Before this fix only the override path did -- a missing binary
# on the default path fell through to sudo_can_run(), which reported
# "sudo -n not authorised" for a binary that was never there to
# authorise in the first place.
# ---------------------------------------------------------------------------


def test_probe_vice_root_existence_checks_the_default_resolution_path(monkeypatch):
    from c64_test_harness.backends import vice_elevation as ve

    monkeypatch.setattr(ve, "rawnet_capability", lambda **k: False)
    monkeypatch.setattr(conftest, "_resolve_ethernet_binary", lambda: "/nonexistent/x64sc")
    monkeypatch.setattr(
        ve, "sudo_can_run",
        lambda b: pytest.fail("must not probe sudo for a binary that doesn't exist"),
    )

    ok, remedy = conftest._probe_vice_root()
    assert ok is False
    assert "not found on this host" in remedy
    assert "/nonexistent/x64sc" in remedy


def test_probe_vice_root_existence_checks_the_binary_override_too(monkeypatch):
    """The override path already did this; pin it so a refactor can't
    silently regress it back to being the only checked path."""
    from c64_test_harness.backends import vice_elevation as ve

    monkeypatch.setattr(ve, "rawnet_capability", lambda **k: False)
    monkeypatch.setattr(
        ve, "sudo_can_run",
        lambda b: pytest.fail("must not probe sudo for a binary that doesn't exist"),
    )

    ok, remedy = conftest._probe_vice_root(binary="/nonexistent/x64sc")
    assert ok is False
    assert "not found on this host" in remedy


# ---------------------------------------------------------------------------
# gate_elevation: skip-or-fail
#
# Recording for the notice is NOT gate_elevation()'s job (adversarial
# review S3 follow-up) -- see pytest_runtest_makereport's tests further
# down for that. gate_elevation() only decides skip vs. fail and raises
# with the right text.
# ---------------------------------------------------------------------------


def test_gate_elevation_skips_with_the_remedy_text():
    with pytest.raises(pytest.skip.Exception) as excinfo:
        conftest.gate_elevation("vice_root", "sudo -n /x64sc ...")
    assert "sudo -n /x64sc ..." in str(excinfo.value)


def test_gate_elevation_fails_instead_when_required(monkeypatch):
    monkeypatch.setenv(conftest.REQUIRE_ELEVATION_ENV, "1")
    with pytest.raises(pytest.fail.Exception) as excinfo:
        conftest.gate_elevation("vice_root", "the remedy")
    assert "the remedy" in str(excinfo.value)


# ---------------------------------------------------------------------------
# pytest_runtest_setup: marked test, prerequisite missing / present
# ---------------------------------------------------------------------------


def test_setup_skips_a_marked_test_on_a_missing_prerequisite(monkeypatch):
    monkeypatch.setitem(
        conftest._ELEVATION_PROBES, "vice_root", lambda **k: (False, "run sudo -n x64sc")
    )
    item = _FakeItem("t::test_x", markers=[_FakeMarker("vice_root")])
    with pytest.raises(pytest.skip.Exception) as excinfo:
        conftest.pytest_runtest_setup(item)
    assert "run sudo -n x64sc" in str(excinfo.value)


def test_setup_does_not_skip_when_the_prerequisite_is_present(monkeypatch):
    monkeypatch.setitem(conftest._ELEVATION_PROBES, "vice_root", lambda **k: (True, ""))
    item = _FakeItem("t::test_x", markers=[_FakeMarker("vice_root")])
    conftest.pytest_runtest_setup(item)  # must not raise
    assert conftest._elevation_skips == []


def test_setup_passes_marker_kwargs_to_the_probe(monkeypatch):
    seen = {}

    def fake_probe(**kwargs):
        seen.update(kwargs)
        return True, ""

    monkeypatch.setitem(conftest._ELEVATION_PROBES, "bridge_iface", fake_probe)
    item = _FakeItem(
        "t::test_x", markers=[_FakeMarker("bridge_iface", ifaces=("feth0", "feth1"))]
    )
    conftest.pytest_runtest_setup(item)
    assert seen == {"ifaces": ("feth0", "feth1")}


# ---------------------------------------------------------------------------
# The end-of-session notice
# ---------------------------------------------------------------------------


def test_notice_prints_without_dash_r_s(monkeypatch):
    monkeypatch.setattr(
        conftest, "_elevation_skips", [("t::test_x", "vice_root", "the remedy")]
    )
    reporter = _FakeReporter()
    session = _session(reporter=reporter)
    conftest.pytest_sessionfinish(session, 0)
    assert any("ELEVATION REQUIRED" in title for _, title, _ in reporter.seps)
    assert any("1 test(s)" in title for _, title, _ in reporter.seps)
    assert any("the remedy" in line for line in reporter.lines)
    assert any("vice_root" in line for line in reporter.lines)


def test_notice_groups_by_kind_and_counts(monkeypatch):
    monkeypatch.setattr(
        conftest,
        "_elevation_skips",
        [
            ("t::a", "vice_root", "fix a"),
            ("t::b", "vice_root", "fix a"),
            ("t::c", "bpf_nodes", "fix c"),
        ],
    )
    reporter = _FakeReporter()
    session = _session(reporter=reporter)
    conftest.pytest_sessionfinish(session, 0)
    vice_lines = [l for l in reporter.lines if "vice_root" in l]
    bpf_lines = [l for l in reporter.lines if "bpf_nodes" in l]
    assert len(vice_lines) == 1 and "2 test(s)" in vice_lines[0]
    assert len(bpf_lines) == 1 and "1 test(s)" in bpf_lines[0]


def test_no_notice_when_nothing_was_skipped_for_elevation():
    reporter = _FakeReporter()
    session = _session(reporter=reporter)
    conftest.pytest_sessionfinish(session, 0)
    assert reporter.seps == []
    assert reporter.lines == []


# ---------------------------------------------------------------------------
# C64_REQUIRE_ELEVATION exit-status escalation
# ---------------------------------------------------------------------------


def test_required_escalates_a_green_exit_status(monkeypatch):
    monkeypatch.setenv(conftest.REQUIRE_ELEVATION_ENV, "1")
    monkeypatch.setattr(
        conftest, "_elevation_skips", [("t::test_x", "vice_root", "the remedy")]
    )
    session = _session(exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 1


@pytest.mark.parametrize("status", [2, 3], ids=["INTERRUPTED", "INTERNAL_ERROR"])
def test_required_does_not_mask_a_worse_status(monkeypatch, status):
    monkeypatch.setenv(conftest.REQUIRE_ELEVATION_ENV, "1")
    monkeypatch.setattr(
        conftest, "_elevation_skips", [("t::test_x", "vice_root", "the remedy")]
    )
    session = _session(exitstatus=status)
    conftest.pytest_sessionfinish(session, status)
    assert session.exitstatus == status


def test_not_required_leaves_a_green_status_alone(monkeypatch):
    monkeypatch.setattr(
        conftest, "_elevation_skips", [("t::test_x", "vice_root", "the remedy")]
    )
    session = _session(exitstatus=0)
    conftest.pytest_sessionfinish(session, 0)
    assert session.exitstatus == 0


# ---------------------------------------------------------------------------
# Mid-fixture refusal: ViceElevationRequiredError -> the same skip/fail
# ---------------------------------------------------------------------------


def test_start_vice_or_skip_converts_the_refusal(monkeypatch):
    from c64_test_harness.backends.vice_elevation import ViceElevationRequiredError

    class _RefusingViceProcess:
        def __init__(self, config):
            pass

        def start(self):
            raise ViceElevationRequiredError(
                "need root", argv=["sudo", "x64sc"], binary="/x64sc",
                sudoers_entry="me ALL=(root) NOPASSWD: /x64sc",
            )

    monkeypatch.setattr(conftest, "ViceProcess", _RefusingViceProcess)
    with pytest.raises(pytest.skip.Exception) as excinfo:
        conftest.start_vice_or_skip(object())
    assert "need root" in str(excinfo.value)


def test_start_vice_or_skip_fails_instead_when_required(monkeypatch):
    from c64_test_harness.backends.vice_elevation import ViceElevationRequiredError

    monkeypatch.setenv(conftest.REQUIRE_ELEVATION_ENV, "1")

    class _RefusingViceProcess:
        def __init__(self, config):
            pass

        def start(self):
            raise ViceElevationRequiredError(
                "need root", argv=["sudo", "x64sc"], binary="/x64sc",
                sudoers_entry="me ALL=(root) NOPASSWD: /x64sc",
            )

    monkeypatch.setattr(conftest, "ViceProcess", _RefusingViceProcess)
    with pytest.raises(pytest.fail.Exception):
        conftest.start_vice_or_skip(object())


def test_start_vice_or_skip_returns_the_process_when_it_starts(monkeypatch):
    started = []

    class _OkViceProcess:
        def __init__(self, config):
            self.config = config

        def start(self):
            started.append(self.config)

    monkeypatch.setattr(conftest, "ViceProcess", _OkViceProcess)
    vice = conftest.start_vice_or_skip("cfg")
    assert started == ["cfg"]
    assert isinstance(vice, _OkViceProcess)
    assert conftest._elevation_skips == []


# ---------------------------------------------------------------------------
# End-to-end, through real pytest collection (pytester)
# ---------------------------------------------------------------------------


def test_e2e_marked_test_skips_with_remedy_and_records(pytester):
    pytester.makeconftest(
        """
        import pytest

        def pytest_configure(config):
            config.addinivalue_line("markers", "elevation(kind, **kw): test")

        _PROBES = {"vice_root": lambda **k: (False, "THE REMEDY TEXT")}

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(item):
            for marker in item.iter_markers("elevation"):
                ok, remedy = _PROBES[marker.args[0]]()
                if not ok:
                    pytest.skip(f"elevation required ({marker.args[0]}): {remedy}")
        """
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.elevation("vice_root")
        def test_needs_root():
            assert False, "must not run"
        """
    )
    result = pytester.runpytest("-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*THE REMEDY TEXT*"])


def test_e2e_session_notice_prints_without_dash_r_s(pytester):
    pytester.makeconftest(
        """
        import pytest

        _skips = []

        def pytest_configure(config):
            config.addinivalue_line("markers", "elevation(kind, **kw): test")

        _PROBES = {"vice_root": lambda **k: (False, "THE REMEDY TEXT")}

        @pytest.hookimpl(tryfirst=True)
        def pytest_runtest_setup(item):
            for marker in item.iter_markers("elevation"):
                ok, remedy = _PROBES[marker.args[0]]()
                if not ok:
                    _skips.append((item.nodeid, marker.args[0], remedy))
                    pytest.skip(f"elevation required ({marker.args[0]}): {remedy}")

        def pytest_sessionfinish(session, exitstatus):
            if not _skips:
                return
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is None:
                return
            reporter.write_sep("=", f"ELEVATION REQUIRED: {len(_skips)} test(s) skipped", red=True)
            for nodeid, kind, remedy in _skips:
                reporter.write_line(f"[{kind}] {remedy}")
        """
    )
    pytester.makepyfile(
        """
        import pytest

        @pytest.mark.elevation("vice_root")
        def test_needs_root():
            pass
        """
    )
    result = pytester.runpytest()
    result.stdout.fnmatch_lines(["*ELEVATION REQUIRED: 1 test(s) skipped*", "*THE REMEDY TEXT*"])


# ---------------------------------------------------------------------------
# The real ethernet fixtures route through start_vice_or_skip
# ---------------------------------------------------------------------------
#
# Direct proof that the shared helper is actually wired into the fixture
# bodies that need it, not just exercised in isolation above.  Each
# fixture is a generator function; calling ``.__wrapped__`` gets past
# pytest's FixtureFunctionMarker to the raw function so it can be driven
# without a full pytest session.


def test_bridge_vice_pair_converts_a_mid_launch_refusal(monkeypatch):
    from c64_test_harness.backends.vice_elevation import ViceElevationRequiredError

    monkeypatch.setattr(conftest.shutil, "which", lambda name: "/usr/bin/x64sc")
    import bridge_platform
    monkeypatch.setattr(bridge_platform, "iface_present", lambda name: True)

    class _RefusingViceProcess:
        def __init__(self, config):
            self.config = config

        def start(self):
            raise ViceElevationRequiredError(
                "need root", argv=["sudo", "x64sc"], binary="/x64sc",
                sudoers_entry="me ALL=(root) NOPASSWD: /x64sc",
            )

        def stop(self):
            pass

    monkeypatch.setattr(conftest, "ViceProcess", _RefusingViceProcess)

    gen = conftest.bridge_vice_pair.__wrapped__()
    with pytest.raises(pytest.skip.Exception) as excinfo:
        next(gen)
    assert "need root" in str(excinfo.value)


# ---------------------------------------------------------------------------
# B1 (adversarial review): the four "ground truth against the real
# binary" tests in test_vice_elevation.py call vice_features() /
# vice_binary_supports_ethernet(), which run `x64sc -features`
# unprivileged -- no launch, no sudo. They must NOT be gated by
# elevation("vice_root"): on a bench with the binary but no sudoers
# rule, that marker would silently skip them even though their own
# docstrings promise to "fail loudly". Only the two tests in
# test_bpf_attach_detection.py that launch a real elevated VICE need
# vice_root.
# ---------------------------------------------------------------------------


def _real_elevation_marks(func):
    return [m for m in getattr(func, "pytestmark", []) if m.name == conftest.ELEVATION_MARKER]


def test_the_ground_truth_x64sc_features_tests_are_not_gated_by_sudo(monkeypatch):
    import test_vice_elevation as tve
    from c64_test_harness.backends import vice_elevation as ve

    monkeypatch.setattr(ve, "sudo_can_run", lambda binary: False)
    monkeypatch.setattr(ve, "rawnet_capability", lambda **k: False)

    names = [
        "test_homebrew_x64sc_is_ethernet_capable",
        "test_homebrew_x64sc_reports_rawnet_and_pcap",
        "test_the_features_fixtures_match_the_real_output_shape",
        "test_the_features_probe_ignores_an_ambient_vicerc",
    ]
    for name in names:
        func = getattr(tve, name)
        marks = _real_elevation_marks(func)
        item = _FakeItem(f"test_vice_elevation.py::{name}", markers=marks)
        # Must NOT skip: these tests need no sudo at all. A bare
        # pytest.skip.Exception escaping this test body would report as
        # SKIPPED, not FAILED, so catch it explicitly and fail loudly --
        # that IS the over-gating bug this test exists to catch.
        try:
            conftest.pytest_runtest_setup(item)
        except pytest.skip.Exception as e:
            pytest.fail(
                f"{name} was gated by elevation({marks[0].args[0] if marks else '?'}) "
                f"even though it never launches VICE: {e}",
                pytrace=False,
            )


def test_bpf_attach_detection_launch_tests_still_require_vice_root(monkeypatch):
    """The control: the two tests that DO launch a real elevated VICE
    must still be gated -- this is not "remove elevation() everywhere"."""
    import test_bpf_attach_detection as tbad
    from c64_test_harness.backends import vice_elevation as ve

    monkeypatch.setattr(ve, "sudo_can_run", lambda binary: False)
    monkeypatch.setattr(ve, "rawnet_capability", lambda **k: False)

    for name in (
        "test_attach_is_detected_for_a_root_owned_vice",
        "test_no_attach_reported_for_a_root_owned_vice_without_the_cart",
    ):
        func = getattr(tbad, name)
        marks = _real_elevation_marks(func)
        assert marks, f"{name} lost its elevation marker entirely"
        item = _FakeItem(f"test_bpf_attach_detection.py::{name}", markers=marks)
        with pytest.raises(pytest.skip.Exception):
            conftest.pytest_runtest_setup(item)


# ---------------------------------------------------------------------------
# S2 (adversarial review): vice_ethernet must release its port even when
# start_vice_or_skip() refuses -- it currently calls start_vice_or_skip
# BEFORE the try, so a refusal skips straight past
# allocator.release(port).
# ---------------------------------------------------------------------------


def test_vice_ethernet_releases_the_port_when_start_vice_or_skip_refuses(monkeypatch):
    import test_ethernet as te

    def _raising_start_vice_or_skip(config):
        pytest.skip("elevation required (vice_root): fix it")

    monkeypatch.setattr(te, "start_vice_or_skip", _raising_start_vice_or_skip)

    released = []

    class _FakeAllocator:
        def __init__(self, **kwargs):
            pass

        def allocate(self):
            return 6511

        def take_socket(self, port):
            return None

        def release(self, port):
            released.append(port)

    monkeypatch.setattr(te, "PortAllocator", _FakeAllocator)

    gen = te.vice_ethernet.__wrapped__()
    with pytest.raises(pytest.skip.Exception):
        next(gen)
    assert released == [6511], (
        "allocator.release(port) must run even when start_vice_or_skip refuses"
    )


# ---------------------------------------------------------------------------
# S3 (adversarial review): the notice must count once per AFFECTED
# TEST, not once per fixture invocation. A module-scoped fixture's
# refusal executes gate_elevation() exactly once (the fixture body runs
# once, however many tests depend on it); pytest's own fixture-error
# caching then reports every OTHER dependent test as "skipped" too,
# with its own real nodeid, from a report pytest builds without ever
# calling into this fixture's code again. pytest_runtest_makereport is
# the only place any of that gets recorded (see its docstring), for
# every affected test, deduplicated by the full (nodeid, kind, remedy)
# tuple so an identical report seen twice is not double counted.
# ---------------------------------------------------------------------------


def test_makereport_records_each_affected_test_for_a_shared_fixture_refusal():
    exc = pytest.skip.Exception("elevation required (vice_root): THE REMEDY")
    # test_a's report has already been recorded once (e.g. by an
    # earlier delivery of the exact same TestReport) -- must not be
    # double counted when its report is (re)observed below.
    conftest._elevation_skips.append(("t::test_a", "vice_root", "THE REMEDY"))

    for nodeid in ("t::test_a", "t::test_b", "t::test_c"):
        item = SimpleNamespace(nodeid=nodeid)
        call = SimpleNamespace(when="setup", excinfo=SimpleNamespace(value=exc))
        gen = conftest.pytest_runtest_makereport(item, call)
        next(gen)
        with pytest.raises(StopIteration):
            gen.send(None)

    assert len(conftest._elevation_skips) == 3, conftest._elevation_skips
    assert {n for n, _, _ in conftest._elevation_skips} == {
        "t::test_a", "t::test_b", "t::test_c",
    }


def test_makereport_ignores_non_setup_phases():
    exc = pytest.skip.Exception("elevation required (vice_root): THE REMEDY")
    item = SimpleNamespace(nodeid="t::test_a")
    call = SimpleNamespace(when="call", excinfo=SimpleNamespace(value=exc))
    gen = conftest.pytest_runtest_makereport(item, call)
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(None)
    assert conftest._elevation_skips == []


def test_makereport_ignores_an_unrelated_skip():
    exc = pytest.skip.Exception("some other reason entirely")
    item = SimpleNamespace(nodeid="t::test_a")
    call = SimpleNamespace(when="setup", excinfo=SimpleNamespace(value=exc))
    gen = conftest.pytest_runtest_makereport(item, call)
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(None)
    assert conftest._elevation_skips == []


def test_makereport_ignores_a_clean_setup():
    item = SimpleNamespace(nodeid="t::test_a")
    call = SimpleNamespace(when="setup", excinfo=None)
    gen = conftest.pytest_runtest_makereport(item, call)
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(None)
    assert conftest._elevation_skips == []


def test_e2e_notice_counts_per_affected_test_not_per_fixture_call(pytester):
    """A module-scoped fixture shared by three tests skips once (the
    fixture body runs once); the notice must say 3, not 1."""
    pytester.makeconftest(
        """
        import pytest

        _skips = []

        def pytest_configure(config):
            config.addinivalue_line("markers", "elevation(kind, **kw): test")

        @pytest.fixture(scope="module")
        def shared():
            _skips.append(("<fixture>", "vice_root", "THE REMEDY"))
            pytest.skip("elevation required (vice_root): THE REMEDY")

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(item, call):
            outcome = yield
            if call.when != "setup" or call.excinfo is None:
                return
            exc = call.excinfo.value
            if not isinstance(exc, (pytest.skip.Exception, pytest.fail.Exception)):
                return
            text = str(exc)
            prefix = "elevation required ("
            if not text.startswith(prefix):
                return
            rest = text[len(prefix):]
            kind, sep, remedy = rest.partition("): ")
            if sep and (item.nodeid, kind, remedy) not in _skips:
                _skips.append((item.nodeid, kind, remedy))

        def pytest_sessionfinish(session, exitstatus):
            recorded = [s for s in _skips if s[0] != "<fixture>"]
            if not recorded:
                return
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is None:
                return
            reporter.write_sep(
                "=", f"ELEVATION REQUIRED: {len(recorded)} test(s) skipped", red=True
            )
        """
    )
    pytester.makepyfile(
        """
        def test_a(shared): pass
        def test_b(shared): pass
        def test_c(shared): pass
        """
    )
    result = pytester.runpytest()
    result.assert_outcomes(skipped=3)
    result.stdout.fnmatch_lines(["*ELEVATION REQUIRED: 3 test(s) skipped*"])

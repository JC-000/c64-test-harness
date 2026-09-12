"""The entry baseline is default-on, and it costs the C64U nothing (#266).

Two owner decisions, both of which change what a lane gets *without*
asking for it, so each is pinned rather than described:

* **Default on for the Ultimate line, off for the CBM line, off for
  unknown.**  The default consults the device
  (``DeviceCapabilities.generation``) rather than being one global
  boolean, because the two lines are not equally recoverable: the C64U
  reaches the bench over WiFi whose reconnection after a power cycle is
  known unreliable, and nobody is there.  An unreadable or timed-out
  probe (#262) grades ``unknown`` and must fall to **off** — a reset must
  never arm on a device the harness failed to identify.  The switch stays
  tri-state above that: an explicit ``baseline_on_entry=`` wins, then the
  env var (either way, which is how you opt a C64U in), then the
  generation default.  ``HarnessConfig.u64_baseline_on_entry=None`` still
  means "nobody asked".
* **It is bodyless throughout**, so arming it on the leak-prone,
  unattended, WiFi-reached C64 Ultimate costs zero ``/Temp``
  attachments.  That is the safety property the default rests on, and it
  is asserted on the real :class:`Ultimate64Client` through its own
  attachment counter, not by reading the source.

The third pin here is a **scope rule**, in the form the repo's review
standard asks for (a fixture, not a sentence in a docstring): no module
under ``src/`` or ``tests/`` configures an ``Ethernet Settings`` item.
The network stores re-effectuate, so a write there re-applies config to
the live stack the device is reached over.
"""

from __future__ import annotations

import ast
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.ultimate64_baseline import (
    BASELINE_CATEGORIES,
    BASELINE_NEVER_TOUCH,
    BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION,
    BASELINE_ON_ENTRY_ENV,
    apply_factory_baseline,
    baseline_default_for_generation,
    baseline_on_entry_enabled,
    resolve_baseline_on_entry,
)
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.backends.unified_manager import (
    UnifiedManager,
    _LockedU64Manager,
)

from test_entry_baseline import (  # noqa: E402  (same-directory test helper)
    _DRIFT,
    _FACTORY,
    FakeBaselineU64,
    HOST,
    _instance_of_generation,
    _decode_category,
    _mock_instance,
)

_PREFIXED = "C64TEST_U64_BASELINE_ON_ENTRY"
_MANAGER_LOGGER = "c64_test_harness.backends.unified_manager"

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither switch set: the state this file is about."""
    monkeypatch.delenv(BASELINE_ON_ENTRY_ENV, raising=False)
    monkeypatch.delenv(_PREFIXED, raising=False)


# --------------------------------------------------------------------------- #
# Default on                                                                   #
# --------------------------------------------------------------------------- #

class TestGenerationGatedDefault:
    def test_the_default_is_per_generation_not_one_boolean(self) -> None:
        assert BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION == {
            "ultimate": True, "cbm": False, "unknown": False,
        }

    @pytest.mark.parametrize("generation,expected", [
        ("ultimate", True), ("cbm", False), ("unknown", False),
    ])
    def test_each_known_generation(self, generation: str, expected: bool) -> None:
        assert baseline_default_for_generation(generation) is expected
        assert baseline_on_entry_enabled(generation) is expected

    @pytest.mark.parametrize("bad", [
        None, "", "Ultimate", "cbm2", "c64u", 3, True, object(),
        # Unhashable: these reach dict.get() and raise TypeError unless the
        # type is rejected first, so they are a crash, not a wrong answer.
        [], {}, {"generation": "ultimate"},
    ])
    def test_anything_unrecognised_falls_to_off(self, bad: object) -> None:
        """Not just the unknown string: a future generation name, a
        mis-cased one, a non-string, ``None``, an unhashable.  Guessing on
        means a config reset on a device that may not come back."""
        assert baseline_default_for_generation(bad) is False  # type: ignore[arg-type]

    def test_no_generation_in_hand_is_off(self) -> None:
        assert baseline_on_entry_enabled() is False

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_the_env_var_forces_it_off_on_the_ultimate_line(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, value)
        assert baseline_on_entry_enabled("ultimate") is False

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_the_env_var_is_the_explicit_opt_in_for_the_c64u(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Opting a C64U in must stay possible — for someone at the bench."""
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, value)
        assert baseline_on_entry_enabled("cbm") is True
        assert baseline_on_entry_enabled("unknown") is True

    def test_an_explicit_request_beats_the_env_and_the_generation(self) -> None:
        assert resolve_baseline_on_entry("ultimate", requested=False)[0] is False
        assert resolve_baseline_on_entry("cbm", requested=True)[0] is True

    def test_the_prefixed_form_still_wins_over_the_bare_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_PREFIXED, "0")
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        assert baseline_on_entry_enabled("ultimate") is False

    @pytest.mark.parametrize("generation,expected_in_reason", [
        ("ultimate", "generation 'ultimate'"), ("cbm", "generation 'cbm'"),
    ])
    def test_the_reason_names_what_decided_it(
        self, generation: str, expected_in_reason: str
    ) -> None:
        """A default that varies by hardware has to say so out loud."""
        _enabled, why = resolve_baseline_on_entry(generation)
        assert expected_in_reason in why

    def test_the_reason_distinguishes_env_from_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        assert BASELINE_ON_ENTRY_ENV in resolve_baseline_on_entry("cbm")[1]
        assert "explicit" in resolve_baseline_on_entry("cbm", requested=True)[1]

    def test_harness_config_stays_tri_state(self) -> None:
        """``None`` means "nobody asked" — it must not become a bool, or the
        documented wiring would stop deferring to the env and the device."""
        from c64_test_harness.config import HarnessConfig

        assert HarnessConfig().u64_baseline_on_entry is None
        assert HarnessConfig.from_env().u64_baseline_on_entry is None


def _graded(client: Any, generation: str) -> Any:
    """Give a FakeBaselineU64 a capability grade, as a real client has."""
    client.capabilities = SimpleNamespace(generation=generation)
    return client


class TestManagerResolvesPerDevice:
    """``UnifiedManager`` cannot decide this at construction — no device is
    in hand — so it passes the tri-state through and ``_LockedU64Manager``
    resolves it at acquire, against the generation the device reported."""

    def test_the_manager_passes_the_tri_state_through_undecided(self) -> None:
        with patch("c64_test_harness.backends.unified_manager._LockedU64Manager") as Locked:
            UnifiedManager(backend="u64", u64_hosts=[HOST])
        assert Locked.call_args.kwargs["baseline_on_entry"] is None, (
            "collapsing to a bool here would decide before the device is known"
        )

    def test_the_documented_wiring_also_stays_undecided(self) -> None:
        from c64_test_harness.config import HarnessConfig

        with patch("c64_test_harness.backends.unified_manager._LockedU64Manager") as Locked:
            UnifiedManager(backend="u64", u64_hosts=[HOST],
                           baseline_on_entry=HarnessConfig().u64_baseline_on_entry)
        assert Locked.call_args.kwargs["baseline_on_entry"] is None

    def test_explicit_false_is_the_opt_out(self) -> None:
        with patch("c64_test_harness.backends.unified_manager._LockedU64Manager") as Locked:
            UnifiedManager(backend="u64", u64_hosts=[HOST], baseline_on_entry=False)
        assert Locked.call_args.kwargs["baseline_on_entry"] is False

    @pytest.mark.parametrize("generation,should_reset", [
        ("ultimate", True), ("cbm", False), ("unknown", False),
    ])
    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_acquire_resets_only_on_the_ultimate_line(
        self, MockDeviceLock: MagicMock, generation: str, should_reset: bool
    ) -> None:
        MockDeviceLock.return_value = MagicMock()
        client = _graded(FakeBaselineU64(drift=_DRIFT), generation)
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(client)
        _LockedU64Manager(inner, baseline_on_entry=None).acquire()
        if should_reset:
            assert client.mismatches() == {}
            assert [c for k, c in client.requests if k == "reset"]
        else:
            assert client.requests == [] and client.gets == [], (
                f"a {generation!r} device must not be reset by the default"
            )
            assert client.mismatches(), "the drift is left untouched"

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_a_client_that_will_not_grade_is_treated_as_unknown(
        self, MockDeviceLock: MagicMock
    ) -> None:
        """#262: the probe can time out on a real, slow device — and the
        C64U is exactly the device a slow probe mis-grades.  A grade that
        raises must resolve off, not crash the acquire and not arm."""
        MockDeviceLock.return_value = MagicMock()
        client = FakeBaselineU64(drift=_DRIFT)       # no .capabilities at all
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(client)
        _LockedU64Manager(inner, baseline_on_entry=None).acquire()
        assert client.requests == [] and client.gets == []

        boom = _graded(FakeBaselineU64(drift=_DRIFT), "ultimate")
        type(boom).capabilities = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("probe timed out"))
        )
        try:
            inner2 = MagicMock()
            inner2.acquire.return_value = _mock_instance(boom)
            _LockedU64Manager(inner2, baseline_on_entry=None).acquire()
            assert boom.requests == [], "a raising probe must not arm the reset"
        finally:
            del type(boom).capabilities

    def test_the_env_var_also_decides_without_a_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting the switch is asking, so it must work on a device that
        will not grade — the C64U on a slow probe is exactly that case."""
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        instance = MagicMock()
        type(instance.transport.client).capabilities = property(
            lambda self: pytest.fail("the env var must not consult the probe")
        )
        mgr = _LockedU64Manager(MagicMock(), baseline_on_entry=None)
        enabled, why, generation = mgr._resolve_baseline(instance)
        assert enabled is True and generation is None
        assert BASELINE_ON_ENTRY_ENV in why

    @pytest.mark.parametrize("grade", [3, None, object()])
    def test_a_non_string_grade_is_normalised_to_unknown(self, grade: object) -> None:
        """A miswired or mocked client hands back something that is not a
        generation name.  Passing it through would put an arbitrary object
        into the default lookup — which, for a MagicMock, is exactly how a
        test harness ends up arming a reset it never asked for."""
        instance = MagicMock()
        instance.transport.client.capabilities.generation = grade
        assert _LockedU64Manager._generation_of(instance) == "unknown"
        mgr = _LockedU64Manager(MagicMock(), baseline_on_entry=None)
        assert mgr._resolve_baseline(instance)[0] is False

    def test_a_bare_magicmock_client_does_not_arm(self) -> None:
        """The shape that bites in tests: every attribute of a MagicMock
        answers, so ``.generation`` is a MagicMock, not ``"ultimate"``."""
        instance = MagicMock()
        mgr = _LockedU64Manager(MagicMock(), baseline_on_entry=None)
        enabled, _why, generation = mgr._resolve_baseline(instance)
        assert generation == "unknown" and enabled is False

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_the_env_var_opts_a_c64u_in_at_acquire(
        self, MockDeviceLock: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For someone standing at the bench.  The opt-in must be
        expressible per run and must beat the generation default."""
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        MockDeviceLock.return_value = MagicMock()
        client = _graded(FakeBaselineU64(drift=_DRIFT), "cbm")
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(client)
        _LockedU64Manager(inner, baseline_on_entry=None).acquire()
        assert client.mismatches() == {}

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_an_explicit_false_beats_the_ultimate_default_at_acquire(
        self, MockDeviceLock: MagicMock
    ) -> None:
        MockDeviceLock.return_value = MagicMock()
        client = _graded(FakeBaselineU64(drift=_DRIFT), "ultimate")
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(client)
        _LockedU64Manager(inner, baseline_on_entry=False).acquire()
        assert client.requests == []

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_an_opted_out_acquire_never_touches_the_probe(
        self, MockDeviceLock: MagicMock
    ) -> None:
        """Off means no requests at all — including the capability probe.
        The grade is consulted for the *log* only when something is about
        to be reset; an opted-out lane must reach the device for nothing."""
        MockDeviceLock.return_value = MagicMock()
        instance = _mock_instance(FakeBaselineU64())
        type(instance.transport.client).capabilities = property(
            lambda self: pytest.fail("an opted-out lane must not probe")
        )
        inner = MagicMock()
        inner.acquire.return_value = instance
        try:
            _LockedU64Manager(inner, baseline_on_entry=False).acquire()
        finally:
            del type(instance.transport.client).capabilities

    def test_an_explicit_value_does_not_probe_the_device(self) -> None:
        """Opting in or out must not depend on a grade, so an unreachable
        or slow device can still be driven deliberately."""
        instance = MagicMock()
        type(instance.transport.client).capabilities = property(
            lambda self: pytest.fail("explicit value must not consult the probe")
        )
        for requested in (True, False):
            mgr = _LockedU64Manager(MagicMock(), baseline_on_entry=requested)
            enabled, _why, generation = mgr._resolve_baseline(instance)
            assert enabled is requested
            assert generation is None, "an explicit value consults no device"


class TestAcquireSaysWhatItDid:
    """The resolved value is logged at INFO on every acquire, naming the
    device, the generation and why — a default that varies silently by
    hardware is how "it worked on my device" happens a month later."""

    @pytest.mark.parametrize("generation,ran", [("ultimate", True), ("cbm", False)])
    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_the_info_line_names_device_generation_and_outcome(
        self, MockDeviceLock: MagicMock, caplog: pytest.LogCaptureFixture,
        generation: str, ran: bool,
    ) -> None:
        MockDeviceLock.return_value = MagicMock()
        client = _graded(FakeBaselineU64(), generation)
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(client)
        with caplog.at_level(logging.INFO, logger=_MANAGER_LOGGER):
            _LockedU64Manager(inner, baseline_on_entry=None).acquire()
        text = caplog.text
        assert HOST in text, "the line must name the device"
        # NOT a bare `generation in text`: the baseline module's own logger
        # is named ...backends.ultimate64_baseline, so "ultimate" matches
        # the logger name and that assertion passes with the field gone.
        assert f"[generation={generation}]" in text, (
            f"the line must carry the generation as its own field: {text!r}"
        )
        assert f"baseline {'RAN' if ran else 'SKIPPED'}" in text
        assert "default for generation" in text, (
            "the line must say what decided it"
        )

    @pytest.mark.parametrize("how", ["env", "param"])
    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_arming_a_cbm_device_explicitly_warns_at_the_moment_of_arming(
        self, MockDeviceLock: MagicMock, caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch, how: str,
    ) -> None:
        """``U64_BASELINE_ON_ENTRY=1`` in a shell profile, later pointed at
        the C64U, arms a reset on the one device that cannot be recovered
        remotely — without anyone re-deciding.  The switch still decides;
        the grade is consulted for the log so the WARNING can name the
        device it is about."""
        MockDeviceLock.return_value = MagicMock()
        requested = None
        if how == "env":
            monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        else:
            requested = True
        client = _graded(FakeBaselineU64(), "cbm")
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(client)
        with caplog.at_level(logging.WARNING, logger=_MANAGER_LOGGER):
            _LockedU64Manager(inner, baseline_on_entry=requested).acquire()
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings, "arming a cbm device explicitly must warn"
        text = "\n".join(r.getMessage() for r in warnings)
        assert "generation=cbm" in text and HOST in text, (
            "pin the formatted field, not the bare token"
        )
        assert client.mismatches() == {}, "and it must still arm"

    @pytest.mark.parametrize("generation", ["ultimate", "unknown"])
    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_arming_explicitly_elsewhere_does_not_warn(
        self, MockDeviceLock: MagicMock, caplog: pytest.LogCaptureFixture,
        generation: str,
    ) -> None:
        """Only ``cbm`` is the device-loss case; warning everywhere would
        train the reader to ignore it."""
        MockDeviceLock.return_value = MagicMock()
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(
            _graded(FakeBaselineU64(), generation))
        with caplog.at_level(logging.WARNING, logger=_MANAGER_LOGGER):
            _LockedU64Manager(inner, baseline_on_entry=True).acquire()
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_a_failed_grade_does_not_block_an_explicit_opt_in(
        self, MockDeviceLock: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Consulting the grade for the log must not make the opt-in depend
        on it: a device that will not grade is still opted in, and the line
        says 'not consulted' rather than failing."""
        MockDeviceLock.return_value = MagicMock()
        client = FakeBaselineU64()
        instance = _mock_instance(client)
        type(instance.transport.client).capabilities = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("probe timed out"))
        )
        inner = MagicMock()
        inner.acquire.return_value = instance
        try:
            with caplog.at_level(logging.INFO, logger=_MANAGER_LOGGER):
                _LockedU64Manager(inner, baseline_on_entry=True).acquire()
        finally:
            del type(instance.transport.client).capabilities
        assert "baseline RAN" in caplog.text
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    @patch("c64_test_harness.backends.unified_manager.DeviceLock")
    def test_the_skip_line_says_the_lane_inherits_state(
        self, MockDeviceLock: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        MockDeviceLock.return_value = MagicMock()
        inner = MagicMock()
        inner.acquire.return_value = _mock_instance(_graded(FakeBaselineU64(), "cbm"))
        with caplog.at_level(logging.INFO, logger=_MANAGER_LOGGER):
            _LockedU64Manager(inner, baseline_on_entry=None).acquire()
        assert "inherits" in caplog.text


class TestDefaultDoesNotSilentlyArmWithoutTheLock:
    """The reset must run inside the ``DeviceLock``.  Explicitly asking for
    it on a host with no ``DeviceLock`` is still a hard refusal; *inheriting
    it from the generation default* must not turn a working configuration
    into a RuntimeError — it degrades to off, loudly."""

    def _no_device_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "c64_test_harness.backends.unified_manager._HAS_DEVICE_LOCK", False
        )

    def test_explicit_on_without_the_lock_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_device_lock(monkeypatch)
        with patch("c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
                   create=True), \
             patch("c64_test_harness.backends.unified_manager.Ultimate64Device", create=True):
            with pytest.raises(RuntimeError, match="DeviceLock"):
                UnifiedManager(backend="u64", u64_hosts=[HOST], baseline_on_entry=True)

    def test_env_on_without_the_lock_is_still_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Setting the switch is asking for it, wherever it is set."""
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "1")
        self._no_device_lock(monkeypatch)
        with patch("c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
                   create=True), \
             patch("c64_test_harness.backends.unified_manager.Ultimate64Device", create=True):
            with pytest.raises(RuntimeError, match="DeviceLock"):
                UnifiedManager(backend="u64", u64_hosts=[HOST])

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
    def test_the_documented_opt_out_does_not_raise_here(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """Explicitly *off* is not "asked for it" — it is asked for it not
        to happen, which a host with no DeviceLock can honour trivially.
        Conflating the two made the documented opt-out raise."""
        monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, value)
        self._no_device_lock(monkeypatch)
        with patch("c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
                   create=True), \
             patch("c64_test_harness.backends.unified_manager.Ultimate64Device", create=True):
            mgr = UnifiedManager(backend="u64", u64_hosts=[HOST])
        assert mgr._baseline_on_entry is False

    def test_the_warnings_own_advice_is_followable(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The degrade WARNING tells the reader to set the switch to 0 to
        silence it.  Doing exactly that must not convert the warning into a
        RuntimeError — advice a caller cannot follow is worse than none."""
        self._no_device_lock(monkeypatch)
        with patch("c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
                   create=True), \
             patch("c64_test_harness.backends.unified_manager.Ultimate64Device", create=True):
            with caplog.at_level(logging.WARNING, logger=_MANAGER_LOGGER):
                UnifiedManager(backend="u64", u64_hosts=[HOST])
            advice = caplog.text
            assert f"{BASELINE_ON_ENTRY_ENV}=0" in advice
            monkeypatch.setenv(BASELINE_ON_ENTRY_ENV, "0")
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger=_MANAGER_LOGGER):
                mgr = UnifiedManager(backend="u64", u64_hosts=[HOST])
        assert mgr._baseline_on_entry is False
        assert caplog.text == "", "following the advice must also silence it"

    def test_explicit_false_without_the_lock_does_not_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_device_lock(monkeypatch)
        with patch("c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
                   create=True), \
             patch("c64_test_harness.backends.unified_manager.Ultimate64Device", create=True):
            mgr = UnifiedManager(backend="u64", u64_hosts=[HOST],
                                 baseline_on_entry=False)
        assert mgr._baseline_on_entry is False

    def test_the_inherited_default_degrades_to_off_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        self._no_device_lock(monkeypatch)
        with patch("c64_test_harness.backends.unified_manager.Ultimate64InstanceManager",
                   create=True), \
             patch("c64_test_harness.backends.unified_manager.Ultimate64Device", create=True):
            with caplog.at_level(logging.WARNING, logger=_MANAGER_LOGGER):
                mgr = UnifiedManager(backend="u64", u64_hosts=[HOST])
        assert mgr._baseline_on_entry is False, (
            "tri-state None must be decided to False here, not left to acquire"
        )
        assert "DeviceLock" in caplog.text
        assert BASELINE_ON_ENTRY_ENV in caplog.text, (
            "the warning must name the switch that turns it back on"
        )


# --------------------------------------------------------------------------- #
# The safety property the default rests on: no /Temp attachments              #
# --------------------------------------------------------------------------- #

class TestBodylessThroughout:
    """Every request the entry baseline makes is a GET or a bodyless PUT.

    The C64 Ultimate (fw 1.1.0) has no ``/Temp`` garbage collection: a
    body-carrying POST leaves a managed file behind and enough of them
    crash the firmware, recoverable only by a physical power-cycle that
    nobody is there to perform.  Arming anything by default on that
    device is only safe if it costs zero attachments, so the claim is
    measured on the client's own counter.
    """

    def _wire_client(self) -> tuple[Ultimate64Client, list[tuple[str, str, bytes | None]], Any]:
        client = Ultimate64Client(
            HOST,
            write_mem_query_threshold=128,   # the leak-prone (C64U) grade
            warn_unlocked=False,
            temp_hygiene=True,               # armed, as it is on the C64U
        )
        wire: list[tuple[str, str, bytes | None]] = []

        def _uncounted(method, path, *, body=None, content_type=None, query=None):
            wire.append((method, path, body))
            if path == "/v1/configs":
                return 200, json.dumps(
                    {"categories": list(_FACTORY), "errors": []}
                ).encode()
            if path.endswith(":reset_to_default"):
                cat = _decode_category(path[len("/v1/configs/"):-len(":reset_to_default")])
                return 200, json.dumps({"reset": [cat], "errors": []}).encode()
            parts = path[len("/v1/configs/"):].split("/")
            cat = _decode_category(parts[0])
            if len(parts) == 1:
                return 200, json.dumps({cat: {
                    item: cur for item, (cur, _d) in _FACTORY[cat].items()
                }, "errors": []}).encode()
            item = _decode_category(parts[1])
            cur, default = _FACTORY[cat][item]
            item_map: dict[str, Any] = {"current": cur}
            if default is not None:
                item_map["default"] = default
                item_map["values"] = [default]
            else:
                item_map["presets"] = [""]
            return 200, json.dumps({cat: {item: item_map}, "errors": []}).encode()

        return client, wire, _uncounted

    def test_the_entry_baseline_creates_no_temp_attachment(self) -> None:
        client, wire, uncounted = self._wire_client()
        assert client.pending_temp_attachments == 0
        with patch.object(client, "_request_uncounted", side_effect=uncounted):
            report = apply_factory_baseline(client)
        assert report.ok
        assert wire, "sanity: the baseline did make requests"
        assert client.pending_temp_attachments == 0, (
            "the entry baseline must cost the C64 Ultimate nothing in /Temp; "
            f"counter says {client.pending_temp_attachments} after "
            f"{len(wire)} requests"
        )

    def test_no_request_it_makes_carries_a_body(self) -> None:
        client, wire, uncounted = self._wire_client()
        with patch.object(client, "_request_uncounted", side_effect=uncounted):
            apply_factory_baseline(client)
        with_body = [(m, p) for m, p, b in wire if b]
        assert with_body == [], f"body-carrying requests on the entry path: {with_body}"

    def test_it_issues_no_post_at_all(self) -> None:
        """A POST route binds the firmware's attachment writer; a PUT
        binds NULL.  The entry path uses GET and PUT only."""
        client, wire, uncounted = self._wire_client()
        with patch.object(client, "_request_uncounted", side_effect=uncounted):
            apply_factory_baseline(client)
        methods = {m for m, _p, _b in wire}
        assert methods == {"GET", "PUT"}, f"unexpected methods on the entry path: {methods}"

    @staticmethod
    def _flat(text: str) -> str:
        """Docstring text with RST markup and line wrapping removed.

        The pins below are verbatim phrases, so they must survive the
        paragraph being reflowed or a word being emphasised — but nothing
        else.  Asserting on loose tokens instead is what let a sentence
        stating no assumption at all satisfy this test.
        """
        return re.sub(r"\s+", " ", text.replace("``", "").replace("*", ""))

    @staticmethod
    def _contains_verbatim(phrase: str, text: str) -> bool:
        """Is *phrase* present in *text*, verbatim, modulo RST formatting?

        Extracted so the comparison is a named thing a test can exercise.
        Inlined, its case-sensitivity was untestable without asserting on
        this file's own source text — which tests the test rather than the
        property.  Extraction is the third option (#282): the property is
        pinned, no source text is, and the pin survives reflowing and
        renaming.
        """
        return phrase in TestBodylessThroughout._flat(text)

    #: Each claim, pinned as a distinctive contiguous phrase.  A token pair
    #: ("1.1.0" and "3.15") was the previous assertion and it passed against
    #: "The U64E runs 3.15 and the C64U runs 1.1.0." — a sentence that makes
    #: no claim whatsoever.  Fifth substring-width false positive in this
    #: lane; the fix each time is to pin the sentence, not its vocabulary.
    _CAVEAT_PHRASES = (
        # (c) the body gate is the primary argument
        "attachment_writer returns NULL for a body-less request",
        "routes.cc:40-46",
        "route_configs.cc:473",
        # (b) and the residual's direction is named, the right way round
        "counting POST-with-body only is the permissive side, "
        "not the conservative one",
        "the gap to close, not the margin to rely on",
        # the residual stays honest about being an inference
        "inference, not a read",
        "v3.15-84-g871ad034",
    )

    @pytest.mark.parametrize("phrase", _CAVEAT_PHRASES)
    def test_both_claim_sites_carry_the_caveat_verbatim(self, phrase: str) -> None:
        from c64_test_harness.backends import ultimate64_baseline as mod

        for where, text in (("module docstring", mod.__doc__ or ""),
                            ("apply_factory_baseline",
                             apply_factory_baseline.__doc__ or "")):
            # The comparison is case-SENSITIVE and that is now TESTED, not
            # merely reviewed (#282): it lives in _contains_verbatim, which
            # TestTheCaveatNormaliser exercises directly with a
            # case-differing needle.  Fold case in that helper and the
            # guard goes red.
            #
            # It was nearly shipped as "reviewable, not testable" on the
            # premise that the only alternative was asserting on this
            # file's own source text.  That premise was false: extracting
            # the comparison pins the property without pinning any text.
            assert self._contains_verbatim(phrase, text), (
                f"{where}: missing {phrase!r}"
            )

    def test_neither_site_calls_the_permissive_half_conservative(self) -> None:
        """The reversed label is the safety-critical half: an uncounted
        attachment never advances the budget, so the hygiene pass never
        fires and the counter reads zero while the device accumulates.
        'conservative' may appear only inside the phrase that corrects it."""
        from c64_test_harness.backends import ultimate64_baseline as mod

        for where, text in (("module docstring", mod.__doc__ or ""),
                            ("apply_factory_baseline",
                             apply_factory_baseline.__doc__ or "")):
            flat = self._flat(text)
            assert flat.count("conservative") == flat.count(
                "not the conservative one"
            ), (
                f"{where}: 'conservative' appears outside the correction — "
                f"the POST-with-body-only rule is the permissive side"
            )
            assert "conservative side of it" not in flat, (
                f"{where}: the reversed label is back"
            )

    def test_the_module_still_names_the_live_check_that_closes_it(self) -> None:
        from c64_test_harness.backends import ultimate64_baseline as mod

        flat = self._flat(mod.__doc__ or "")
        assert "count, one reset_to_default PUT, count again" in flat

    def test_a_hypothetical_post_would_be_counted(self) -> None:
        """Positive control for the instrument: the counter is live on this
        client, so a zero above is evidence and not a dead gauge."""
        client, _wire, uncounted = self._wire_client()
        with patch.object(client, "_request_uncounted", side_effect=uncounted):
            client._request("POST", "/v1/configs", body=b"{}",
                            content_type="application/json")
        assert client.pending_temp_attachments == 1


# --------------------------------------------------------------------------- #
# Ethernet Settings: the reason, corrected                                     #
# --------------------------------------------------------------------------- #

class TestTheCaveatNormaliser:
    """``TestBodylessThroughout._flat`` is load-bearing for seven verbatim
    pins, so it is pinned itself.

    A verbatim assertion is only as narrow as the normaliser underneath
    it.  If ``_flat`` ever strips something semantic, every pin that runs
    through it widens **invisibly** — the assertions still read as exact
    phrases while matching text that no longer says the same thing.  That
    is the substring-width defect one layer down, which is why this is a
    test and not a note: a transcript proving it today cannot be re-run
    against tomorrow's edit.

    Both directions are covered on purpose.  Only checking that reflowed
    text still matches would pass for a ``_flat`` that deleted everything.
    """

    _flat = staticmethod(TestBodylessThroughout._flat)

    #: A real pin from :data:`TestBodylessThroughout._CAVEAT_PHRASES`, not a
    #: synthetic string — the thing actually being protected.
    PHRASE = ("counting POST-with-body only is the permissive side, "
              "not the conservative one")

    # -- must still match: the formatting _flat exists to absorb ---------

    @pytest.mark.parametrize("label,mangle", [
        ("reflowed across lines",
         lambda p: p.replace(" only", "\n  only").replace(", not", ",\n    not")),
        ("emphasis added", lambda p: p.replace("permissive", "**permissive**")),
        ("literal markup added",
         lambda p: p.replace("POST-with-body", "``POST-with-body``")),
        ("whitespace doubled", lambda p: p.replace(" ", "  ")),
    ])
    def test_formatting_changes_do_not_break_a_pin(self, label, mangle) -> None:
        """Rewrapping a docstring paragraph must not fail an unchanged claim."""
        assert self.PHRASE in self._flat(mangle(self.PHRASE)), label

    # -- must still fail: everything _flat must NOT absorb ---------------

    @pytest.mark.parametrize("label,mangle", [
        ("hyphen removed",
         lambda p: p.replace("POST-with-body", "POST with body")),
        ("comma removed", lambda p: p.replace("side, not", "side not")),
        ("case changed", lambda p: p.replace("permissive", "Permissive")),
        ("word order swapped",
         lambda p: p.replace("permissive side", "side permissive")),
        # THE ONE THAT MATTERS MOST.  This phrase exists to record that
        # counting POST-with-body only is the PERMISSIVE side; dropping the
        # "not" inverts the safety claim into the reversed label we already
        # shipped once.  If a future edit makes this pin inconvenient, the
        # answer is to fix the docstring, NEVER to relax this case.
        ("negation dropped — inverts the safety claim",
         lambda p: p.replace("not the conservative", "the conservative")),
    ])
    def test_semantic_changes_still_fail_a_pin(self, label, mangle) -> None:
        mangled = mangle(self.PHRASE)
        assert mangled != self.PHRASE, f"{label}: the mangle did nothing"
        assert self.PHRASE not in self._flat(mangled), (
            f"_flat absorbed a semantic difference ({label}) — every verbatim "
            f"pin built on it is wider than it looks"
        )

    # -- and exactly what it does / does not touch -----------------------

    # -- the comparison itself, not just the normaliser -----------------

    _contains = staticmethod(TestBodylessThroughout._contains_verbatim)

    def test_the_comparison_is_case_sensitive(self) -> None:
        """THE PROPERTY, pinned without pinning any source text (#282).

        The seven caveat pins run through ``_contains_verbatim``.  If that
        comparison is ever case-folded, all seven widen silently — nothing
        else in this file notices, because every other case here exercises
        ``_flat`` rather than the comparison.  Exercising the helper is
        what makes the property falsifiable; the earlier attempt to guard
        it by asserting on this file's own source text was rejected, and
        the premise that those were the only two options was wrong.
        """
        haystack = self.PHRASE.replace("permissive", "Permissive")
        assert haystack != self.PHRASE, "the mangle did nothing"
        assert not self._contains(self.PHRASE, haystack), (
            "the comparison matched a case-differing needle — it has been "
            "case-folded, and all seven caveat pins are now wider than they "
            "look"
        )

    def test_the_comparison_still_matches_an_exact_needle(self) -> None:
        """Positive direction: the guard above must not be satisfied by a
        comparison that matches nothing at all."""
        assert self._contains(self.PHRASE, f"… {self.PHRASE} …")

    def test_it_removes_only_markup_and_whitespace_runs(self) -> None:
        assert self._flat("a``b") == "ab"
        assert self._flat("a*b") == "ab"
        assert self._flat("a \n\t b") == "a b"

    @pytest.mark.parametrize("text", ["a-b", "a,b", "AbC", "3.15", "v3.15-84-g871ad034",
                                      "routes.cc:40-46", "NULL"])
    def test_it_preserves_everything_else(self, text: str) -> None:
        """Hyphens, commas, case, digits, dots and colons all survive —
        several pins are file:line citations and version strings."""
        assert self._flat(text) == text


class TestEthernetReason:
    """Measured on the bench U64E, 2026-09-10: ``Use DHCP`` is ``Enabled``
    and all five items already equal their defaults, so a reset of the
    category changes no value at all.  The static fields are unused
    factory values; the stranding claim was wrong.  The category stays
    never-touch for the accurate reason: the network stores re-effectuate,
    so a reset re-applies config to the live stack and can bounce the
    interface the device is reached over."""

    def test_the_category_is_still_never_touched(self) -> None:
        assert "Ethernet Settings" in BASELINE_NEVER_TOUCH
        assert "Ethernet Settings" not in BASELINE_CATEGORIES

    def test_the_stranding_claim_is_retracted_not_merely_reworded(self) -> None:
        """The reason may *quote* the old claim — it must mark it retracted
        and say what was measured instead."""
        reason = BASELINE_NEVER_TOUCH["Ethernet Settings"]
        assert "RETRACTED" in reason, (
            "a claim this file previously asserted must be retracted by name, "
            f"not quietly dropped — reason reads {reason!r}"
        )
        assert "already on DHCP" in reason and "unused factory defaults" in reason
        assert "2026-09-10" in reason, (
            "the retraction must carry the measurement that overturned it"
        )
        # NOT "not a stranding risk": that phrasing belonged to the second
        # wrong reading, which treated the reset as harmless on a DHCP
        # device.  It drops the lease.  The claim being retracted is the
        # *static-address* story, not the severity.

    def test_the_reason_names_the_mechanism_not_just_the_word(self) -> None:
        """"Re-effectuates" on its own is the same kind of unbacked claim
        the stranding one was.  The reason must name the code path that
        does the damage — and it is ``dhcp_stop`` on the *DHCP* branch, not
        only the static one: lwIP's dhcp_release_and_stop zeroes the
        address the request arrived on."""
        reason = BASELINE_NEVER_TOUCH["Ethernet Settings"]
        assert "effectuate" in reason.lower()
        for token in ("NetworkInterface::effectuate_settings", "dhcp_stop",
                      "dhcp_release_and_stop", "netif_set_addr",
                      "IP4_ADDR_ANY4", "dhcp.c"):
            assert token in reason, f"reason must cite {token!r}"

    def test_the_reason_does_not_claim_the_reset_is_harmless(self) -> None:
        """The 'live no-op' reading was refuted by the lines it cited: the
        guard is a post-tag fork commit, not a property of 3.15, and both
        upstream and the C64U's 1.1.0 call dhcp_stop unconditionally.  A
        reason must not present a one-device property as a line-wide one."""
        reason = BASELINE_NEVER_TOUCH["Ethernet Settings"]
        assert "UNCONDITIONALLY" in reason
        for token in ("6b5ffc21", "1.1.0", "fork", "v3.15-8"):
            assert token in reason, (
                f"the provenance of the narrowing must be cited: {token!r}"
            )
        assert "RETRACTED" in reason
        # The narrowing may be *described* (it is true of one flashed
        # device) but never asserted bare: this exact phrasing is the
        # refuted reading, and it would otherwise sit beside its own
        # retraction without contradicting any assertion here.
        # The phrase may appear exactly once — inside the quoted reading
        # being retracted.  A second occurrence is the claim restated as
        # fact, which is how it would creep back beside its own retraction
        # without contradicting anything else asserted here.
        assert reason.count("is a live no-op") == 1, (
            "the 'live no-op' reading belongs only in the quoted retraction; "
            "it is refuted for every build except the U64E's fork"
        )

    def test_both_retracted_readings_are_named(self) -> None:
        """Two wrong readings now precede this text.  Dropping either would
        let it be re-derived; the entry carries both, with what refuted
        them."""
        reason = BASELINE_NEVER_TOUCH["Ethernet Settings"]
        # NOT `"no-op" in reason`: that is also satisfied by the surviving
        # narrowing sentence ("the no-op holds for exactly one device"), so
        # the quoted reading (2) could be deleted and this stayed green.
        # Pin each retracted reading by a fragment only its quotation has.
        assert "and strands it" in reason, "reading (1) must be quoted"
        assert "only re-starts DHCP when it is not already running" in reason, (
            "reading (2) must be quoted"
        )
        assert "2026-09-10" in reason, "the measurement that refuted (1)"
        assert "#805" in reason, "the commit that explains (2)"

    def test_the_reason_forbids_a_static_address_in_tests(self) -> None:
        # NOT `"static address" in r and "never" in r`: "never" also occurs
        # in "must never be generalised", so that pair stayed green when the
        # rule itself was flipped to "sometimes configure a static address".
        # Same shape as the logger-name false positive — pin the verbatim
        # rule, which is the owner's wording and defensible as such.
        assert "MUST NEVER CONFIGURE A STATIC ADDRESS" in (
            BASELINE_NEVER_TOUCH["Ethernet Settings"]
        )

    def test_no_stranding_claim_survives_outside_the_retraction(self) -> None:
        """The claim was asserted in three places: the module docstring, the
        never-touch reason, and the ValueError a network store raises.  Only
        the reason may still mention it, and only as a retraction."""
        from c64_test_harness.backends import ultimate64_baseline as mod

        assert "strand" not in (mod.__doc__ or "").lower(), (
            "the module docstring still asserts the stranding claim"
        )
        client = FakeBaselineU64()
        with pytest.raises(ValueError) as exc:
            apply_factory_baseline(client, categories=("Ethernet Settings 2",))
        assert "strand" not in str(exc.value).lower(), (
            f"the network-store refusal still says it: {exc.value}"
        )
        assert "effectuate" in str(exc.value).lower()


class TestTheMarkerScanIsNotANetworkFilter:
    """Measured against the live validator, 2026-09-11.  The marker list is
    a spelling guard for the three never-touch names; it is not a network
    detector, and the module must not claim otherwise."""

    #: Plausibly network-named stores the validator accepts today.  This is
    #: a characterization, not a wish: it fails if the marker list silently
    #: widens, which would be a behaviour change deserving its own review.
    ACCEPTED_NETWORK_ISH = (
        "LAN Settings", "IP Configuration", "TCP/IP", "Wireless",
        "ESP32 Settings", "Modem Settings",
    )

    @pytest.mark.parametrize("name", ACCEPTED_NETWORK_ISH)
    def test_a_network_named_store_is_not_caught_by_the_markers(
        self, name: str
    ) -> None:
        from c64_test_harness.backends.ultimate64_baseline import _validate_categories

        assert _validate_categories((name,)) == (name,), (
            f"{name!r} is now refused — if that is intended, the note on "
            f"_EXCLUDED_MARKERS saying this is only a spelling guard needs "
            f"rewriting too"
        )

    @pytest.mark.parametrize("name", [
        "Ethernet Settings", "WiFi settings", "Network Settings",
        "ethernet settings", "ETHERNET SETTINGS", "Ethernet Settings 2",
        "WiFi Client Settings", "Wi-Fi Settings",
    ])
    def test_the_three_named_stores_and_their_near_misses_are_refused(
        self, name: str
    ) -> None:
        from c64_test_harness.backends.ultimate64_baseline import _validate_categories

        with pytest.raises(ValueError):
            _validate_categories((name,))

    @staticmethod
    def _markers_note() -> str:
        """The ``#:`` comment block attached to ``_EXCLUDED_MARKERS``, flat.

        Scoped deliberately, not grepped from the whole file: the phrase
        "not a general network-store filter" occurs **twice** in this
        module — once in this note and once in the ValueError message a
        few hundred lines away — so a file-wide substring assertion is
        satisfied by the message whatever the note says.  That is the same
        trap as a bare "dhcp_stop" matching the Ethernet reason.  Reading
        only the block makes the haystack the thing under test.
        """
        from c64_test_harness.backends import ultimate64_baseline as mod

        lines = Path(mod.__file__).read_text().splitlines()
        i = next(n for n, l in enumerate(lines)
                 if l.startswith("_EXCLUDED_MARKERS"))
        block = []
        while i > 0 and lines[i - 1].startswith("#:"):
            i -= 1
            block.append(lines[i][2:])
        assert block, "no #: note found attached to _EXCLUDED_MARKERS"
        return re.sub(r"\s+", " ", " ".join(reversed(block)).replace("``", "").replace("*", "")).strip()

    def test_the_note_itself_says_it_is_not_a_network_filter(self) -> None:
        """The module docstring sends the reader to this note by name, so
        it is the one sentence that must stay true — and it was the one
        sentence nothing covered: inverting it to "This IS a general
        network-store filter" left the suite green."""
        note = self._markers_note()
        assert ("This is not a general network-store filter and must not be "
                "described as one.") in note, (
            f"the note no longer denies being a network filter: {note[:200]!r}"
        )

    def test_the_scoped_haystack_really_is_only_the_note(self) -> None:
        """Guard on the test above, both directions.

        If the block walk ever returned the whole file, the assertion above
        would be satisfied by the ValueError message instead of the note —
        green while blind.  So: the note must contain its own opening line,
        and must NOT contain text that exists only in the error message.
        """
        note = self._markers_note()
        assert note.startswith(
            "Extra spelling guard for the three named stores"
        ), f"the walk did not start at the note: {note[:80]!r}"
        for elsewhere in ("is a near-miss spelling of a never-touch",
                          "widen BASELINE_CATEGORIES by review"):
            assert elsewhere not in note, (
                f"the walk escaped the note and swallowed the ValueError "
                f"message ({elsewhere!r}) — the pin above would then be "
                f"satisfied by the message whatever the note says"
            )
        assert "raise ValueError" not in note, "the walk swallowed source, not a comment"

    def test_the_module_does_not_claim_the_markers_filter_network_stores(
        self
    ) -> None:
        """The sentence that would drift.  The refusal message must present
        the check as a spelling guard, not as proof a store is safe."""
        from c64_test_harness.backends.ultimate64_baseline import _validate_categories

        with pytest.raises(ValueError) as exc:
            _validate_categories(("Ethernet Settings 2",))
        msg = str(exc.value)
        assert "spelling guard" in msg and "not a general network-store filter" in msg

    def test_modem_settings_is_covered_and_the_reason_is_recorded(self) -> None:
        """A store whose effectuate binds a network listener IS in the
        covered set, deliberately.  If that stops being written down, the
        next reviewer re-derives the marker scan as a safety net."""
        from c64_test_harness.backends import ultimate64_baseline as mod

        assert "Modem Settings" in BASELINE_CATEGORIES
        # NOT a bare "dhcp_stop": that token also occurs in the Ethernet
        # Settings reason, so it is satisfied whether or not the Modem note
        # says anything.  Pin the Modem-specific clause instead.
        flat = re.sub(r"\s+", " ", Path(mod.__file__).read_text().replace("``", "").replace("*", ""))
        for phrase in ("modem.cc:889-890", "listenerSocket->Start(newPort)",
                       "does not call dhcp_stop/netif_set_addr",
                       "not the device-loss shape"):
            assert phrase in flat, f"the Modem Settings decision must cite {phrase!r}"


class TestDocumentedBoundaries:
    def test_the_client_call_bypasses_the_never_touch_list(self) -> None:
        """Stated, not fixed: the guard lives in apply_factory_baseline, so
        a direct client call can reset a never-touch store.  Pinned so the
        claim in the module docstring stays true of the code."""
        client = Ultimate64Client(HOST, write_mem_query_threshold=128,
                                  warn_unlocked=False)
        sent: list[tuple[str, str]] = []

        def _uncounted(method, path, *, body=None, content_type=None, query=None):
            sent.append((method, path))
            return 200, b'{"reset": ["Ethernet Settings"], "errors": []}'

        with patch.object(client, "_request_uncounted", side_effect=_uncounted):
            client.reset_config_category_to_default("Ethernet Settings")
        assert sent and "Ethernet" in sent[0][1], (
            "if the client ever gains its own guard, the docstring boundary "
            "note must be updated with it"
        )
        # The behaviour above is the boundary; the docstring must say so,
        # or the next reader assumes the guard is on the wire.
        from c64_test_harness.backends import ultimate64_baseline as mod

        doc = mod.__doc__ or ""
        flat = re.sub(r"\s+", " ", doc.replace("``", "").replace("*", ""))
        assert "bypasses the never-touch list entirely" in flat, (
            "the module docstring must state that a direct client call is "
            "not covered by the never-touch guard"
        )
        # ... while the module-level entry point refuses the same name.
        with pytest.raises(ValueError):
            apply_factory_baseline(FakeBaselineU64(),
                                   categories=("Ethernet Settings",))

    def test_the_request_count_is_not_claimed_for_the_cbm_line(self) -> None:
        doc = apply_factory_baseline.__doc__ or ""
        flat = re.sub(r"\s+", " ", doc.replace("``", "").replace("*", ""))
        assert "never been read, so any figure quoted for the Ultimate line" in flat, (
            "the U64E request-count figure must not be presented as covering "
            "the C64U, whose category list has never been read"
        )
        # And no bare total may be quoted here at all: a number beside the
        # caveat is what gets copied out without it.
        for figure in ("150", "~150", "about 150"):
            assert figure not in doc, (
                f"{figure!r} is a U64E observation from PATTERNS.md; quoting "
                f"it here re-attaches it to both device lines"
            )


class TestWiFiReasonIsOwnerTestimony:
    """Owner testimony, 2026-09-11: the C64U has failed to rejoin its
    wireless network after a hard power cycle; the working configuration
    was saved to flash and the rejoin confirmed only by someone standing at
    the device.  That makes a reset of this store a device-loss scenario,
    and it is the reason the cbm generation defaults off."""

    def test_the_reason_is_the_measured_unreliability_not_a_caution(self) -> None:
        reason = BASELINE_NEVER_TOUCH["WiFi settings"]
        assert "has never been read" not in reason, (
            "the inherited 'unassessed' framing understates a known failure"
        )
        for token in ("2026-09-11", "power cycle", "flash"):
            assert token in reason, f"reason must carry {token!r}"
        assert "KNOWN UNRELIABLE" in reason

    def test_the_reason_says_there_is_no_remote_remedy(self) -> None:
        reason = BASELINE_NEVER_TOUCH["WiFi settings"]
        assert "no remote remedy" in reason.lower()
        for token in ("reboot", "FTP"):
            assert token in reason, (
                f"the reason must rule out {token!r} by name — both have been "
                f"reached for before"
            )

    def test_the_reason_links_the_generation_default(self) -> None:
        """The store being never-touch and the cbm default being off are
        the same fact; if one moves the other must be re-argued."""
        assert "BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION" in (
            BASELINE_NEVER_TOUCH["WiFi settings"]
        )
        assert BASELINE_ON_ENTRY_DEFAULT_BY_GENERATION["cbm"] is False


class TestStaticAddressIsOutOfScopeForTests:
    """Scope rule, pinned rather than written down: a test must not
    configure a static address on the device.  Nothing in the tree may
    write an ``Ethernet Settings`` or WiFi item.

    ``Network Settings`` is deliberately **not** on this list even though
    it is never-touch for the entry baseline: it carries service flags
    rather than addresses, and the ``/Temp`` hygiene pass legitimately
    writes ``FTP File Service`` there
    (``ultimate64_client.py``, ``_run_temp_hygiene``).  The rule the owner
    set is about addressing the device, not about the store list.
    """

    _WRITERS = {"set_config_item", "set_config_items", "set_config_items_batch"}
    _FORBIDDEN = {"ethernet settings", "wifi settings"}

    def _offenders(
        self, root: Path, forbidden: set[str] | None = None
    ) -> tuple[list[str], list[str]]:
        """``(offenders, files_parsed)``.

        The second element exists because the first cannot distinguish "no
        violations" from "scanned nothing": ``rglob`` on a path that does
        not exist yields no files and returns a clean ``[]``.  An earlier
        version asserted only on the offenders and its positive control
        planted the violation in ``tmp_path``, so the whole class passed
        with ``_REPO`` pointed at ``/nonexistent/typo/repo`` -- green while
        blind.  Callers must assert on what was parsed.
        """
        forbidden = self._FORBIDDEN if forbidden is None else forbidden
        found: list[str] = []
        parsed: list[str] = []
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            parsed.append(path.name)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                if name not in self._WRITERS or not node.args:
                    continue
                first = node.args[0]
                if (isinstance(first, ast.Constant) and isinstance(first.value, str)
                        and first.value.lower() in forbidden):
                    where = path.relative_to(root)
                    found.append(f"{where}:{node.lineno} -> {first.value!r}")
        return found, parsed

    # (root, a file that must be among those parsed) — names the target so a
    # mistyped or moved root fails loudly instead of scanning an empty set.
    _ROOTS = [
        ("tests", "test_entry_baseline_default_on.py"),
        ("src", "ultimate64_baseline.py"),
    ]

    @pytest.mark.parametrize("root_name,sentinel", _ROOTS)
    def test_the_scan_actually_reaches_the_tree(
        self, root_name: str, sentinel: str
    ) -> None:
        """Vacuity guard: prove the root resolved and was walked."""
        _offenders, parsed = self._offenders(_REPO / root_name)
        assert len(parsed) > 50, (
            f"{root_name}/ yielded only {len(parsed)} files — the root did "
            f"not resolve, so every scan below is vacuously green"
        )
        assert sentinel in parsed, f"{sentinel} was not parsed under {root_name}/"

    @pytest.mark.parametrize("root_name,_sentinel", _ROOTS)
    def test_the_scan_fires_on_a_real_write_in_the_real_tree(
        self, root_name: str, _sentinel: str
    ) -> None:
        """Positive control **against the target**, not a synthetic copy.

        ``Network Settings`` is deliberately not forbidden (see the class
        docstring), and both roots really do write it — the ``/Temp``
        hygiene pass enables FTP File Service, and a test exercises that.
        Pointing the matcher at it must therefore produce hits from the
        real tree; if it does not, the scan is not seeing this tree.
        """
        offenders, _parsed = self._offenders(
            _REPO / root_name, forbidden={"network settings"}
        )
        assert offenders, (
            f"the scan found no 'Network Settings' write under {root_name}/, "
            f"but the harness and its tests both contain one — the scan is "
            f"not looking at the real tree"
        )

    def test_no_test_configures_a_static_address(self) -> None:
        offenders, parsed = self._offenders(_REPO / "tests")
        assert parsed, "vacuity: nothing scanned"
        assert offenders == [], (
            "tests must not configure a static address: the Ethernet/WiFi "
            "stores re-effectuate, so the write lands on the live stack the "
            f"device is reached over — {offenders}"
        )

    def test_no_harness_module_configures_a_static_address(self) -> None:
        offenders, parsed = self._offenders(_REPO / "src")
        assert parsed, "vacuity: nothing scanned"
        assert offenders == [], f"harness addresses the device: {offenders}"

    def test_the_matcher_recognises_the_call_shape(self, tmp_path: Path) -> None:
        """Unit-level: the matcher itself.  This proves nothing about the
        real roots — see the two tests above for that."""
        (tmp_path / "bad.py").write_text(
            'client.set_config_item("Ethernet Settings", "IP Address", "10.0.0.9")\n'
        )
        offenders, parsed = self._offenders(tmp_path)
        assert offenders, "the matcher must catch a real write"
        assert parsed == ["bad.py"]

"""Function-time ``DeviceLock`` acquisitions in live tests must nest.

``tests/conftest.py``'s autouse ``device_lock_guard`` holds a real
``DeviceLock`` for the duration of every live test whenever ``U64_HOST``
is set.  ``allow_nested`` is a property of the *acquirer*, not of the
holder, so a test body that constructs a plain ``DeviceLock(host)``
queues behind a flock its own thread already holds -- a wait nothing in
that thread can end (issue #273 class 2).

Module-, class-, session- and package-scoped fixtures are safe: they run
*before* the function-scoped guard and win the flock first.  Only
acquisitions that happen at function time can self-deadlock, so that is
exactly what this module checks, structurally, without a device.

It also pins part of the second half — a live test that cannot take the
device lock must **fail**, not skip.  ``test_sid_u64_live`` skipped, which
is why its 120 s self-deadlock went unnoticed in every run.

**Part, not all of it, and the gap is in the scanner rather than in the
rule** (#279).  ``_skip_offenders`` matches one shape:
``if not <x>.acquire(...)`` guarding a ``pytest.skip`` whose callee is an
attribute.  ``_device_lock_calls`` matches a callee spelled literally
``DeviceLock``.  Five plausible shapes therefore evade them, measured by
driving both scanners on synthetic sources:
``import DeviceLock as DL``; a module-level helper returning a lock;
``from pytest import skip`` then a bare ``skip(...)``;
``got = lock.acquire(...)`` then ``if not got:``; and
``if lock.acquire(...) is False:``.

That is a floor, not a guarantee, and it is a floor in the safe
direction: the guard is fail-closed where it does match (a variable
``allow_nested=`` is flagged), and it is not vacuous on the real corpus —
26 ``DeviceLock(`` sites across the live modules, the 5 that run at
function time all carrying ``allow_nested=True``.  Read this module as
"these shapes cannot regress", not as "no live test can skip on a lock
failure".
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent

#: Fixture scopes that resolve before the function-scoped autouse guard.
_SAFE_FIXTURE_SCOPES = {"module", "class", "session", "package"}


def _fixture_scope(func: ast.FunctionDef) -> str | None:
    """The declared scope of a ``@pytest.fixture``, or ``None`` if not one."""
    for dec in func.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", None)
        )
        if name != "fixture":
            continue
        if isinstance(dec, ast.Call):
            for kw in dec.keywords:
                if kw.arg == "scope" and isinstance(kw.value, ast.Constant):
                    return str(kw.value.value)
        return "function"
    return None


def _runs_at_function_time(func: ast.FunctionDef) -> bool:
    scope = _fixture_scope(func)
    if scope is not None:
        return scope not in _SAFE_FIXTURE_SCOPES
    return func.name.startswith("test_")


def _device_lock_calls(node: ast.AST) -> list[ast.Call]:
    out = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        target = sub.func
        name = (
            target.attr
            if isinstance(target, ast.Attribute)
            else getattr(target, "id", None)
        )
        if name == "DeviceLock":
            out.append(sub)
    return out


def _has_allow_nested(call: ast.Call) -> bool:
    return any(
        kw.arg == "allow_nested"
        and isinstance(kw.value, ast.Constant)
        and kw.value.value is True
        for kw in call.keywords
    )


def _function_time_scopes(tree: ast.AST) -> list[ast.AST]:
    return [
        f
        for f in ast.walk(tree)
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _runs_at_function_time(f)
    ]


def _nesting_offenders(source: str, name: str) -> list[str]:
    """``DeviceLock(...)`` built at function time without ``allow_nested``."""
    tree = ast.parse(source, filename=name)
    offenders = []
    for func in _function_time_scopes(tree):
        for call in _device_lock_calls(func):
            if not _has_allow_nested(call):
                offenders.append(f"{name}:{call.lineno} (in {func.name})")
    return offenders


def _skip_offenders(source: str, name: str) -> list[str]:
    """``pytest.skip`` reached from ``if not <x>.acquire(...)`` at function time."""
    tree = ast.parse(source, filename=name)
    offenders = []
    for node in (n for scope in _function_time_scopes(tree) for n in ast.walk(scope)):
        if not isinstance(node, ast.If):
            continue
        # Match `if not <something>.acquire(...)`, whatever the receiver.
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)):
            continue
        inner = test.operand
        if not (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "acquire"
        ):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "skip"
            ):
                offenders.append(f"{name}:{sub.lineno}")
    return offenders


def _live_modules() -> list[Path]:
    mods = sorted(TESTS_DIR.glob("test_*_live.py"))
    assert mods, "no live test modules found -- the scan is looking in the wrong place"
    return mods


@pytest.mark.parametrize(
    "path", _live_modules(), ids=lambda p: p.name
)
def test_function_time_locks_allow_nesting(path: Path) -> None:
    offenders = _nesting_offenders(path.read_text(), path.name)
    assert not offenders, (
        "DeviceLock constructed at function time without allow_nested=True; "
        "the autouse device_lock_guard already holds the flock on this "
        "thread, so these acquisitions wait out their full timeout and then "
        "fail: " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    "path", _live_modules(), ids=lambda p: p.name
)
def test_a_failed_lock_acquire_never_skips(path: Path) -> None:
    """``pytest.skip`` on a lock failure hides a deadlock as a clean run.

    Scoped to function-time acquisitions, the same set the nesting check
    covers, because those are the ones that can self-deadlock: there the
    skip is not "another job has the device", it is "I queued behind
    myself", and reporting it as a skip is what kept it invisible.  What
    a module-scoped fixture does when a *foreign* holder has the device
    is a separate policy question this module does not take a view on.
    """
    offenders = _skip_offenders(path.read_text(), path.name)
    assert not offenders, (
        "a live test skips when it cannot acquire the device lock; a lock "
        "it cannot get is a failure to report, not a test to drop: "
        + ", ".join(offenders)
    )


class TestTheScannerItselfCanFail:
    """A clean sweep is only evidence if the sweep can report dirt.

    Every module under test passes now, so without these the two checks
    above would keep passing if the AST matching silently stopped
    matching anything.
    """

    OFFENDING = (
        "import pytest\n"
        "from c64_test_harness.backends.device_lock import DeviceLock\n"
        "def test_thing():\n"
        "    lock = DeviceLock(HOST)\n"
        "    if not lock.acquire(timeout=600.0):\n"
        "        pytest.skip('no lock')\n"
    )

    SAFE = (
        "import pytest\n"
        "from c64_test_harness.backends.device_lock import DeviceLock\n"
        "@pytest.fixture(scope='module')\n"
        "def client():\n"
        "    lock = DeviceLock(HOST)\n"
        "    if not lock.acquire(timeout=600.0):\n"
        "        pytest.skip('no lock')\n"
        "    yield lock\n"
        "def test_thing(client):\n"
        "    lock = DeviceLock(HOST, allow_nested=True)\n"
        "    assert lock.acquire(timeout=5.0)\n"
    )

    def test_nesting_scanner_flags_a_bare_construction(self) -> None:
        assert _nesting_offenders(self.OFFENDING, "x.py") == [
            "x.py:4 (in test_thing)"
        ]

    def test_skip_scanner_flags_a_skip_on_acquire_failure(self) -> None:
        assert _skip_offenders(self.OFFENDING, "x.py") == ["x.py:6"]

    def test_module_scoped_fixture_is_not_flagged(self) -> None:
        """It wins the flock before the function-scoped guard runs."""
        assert _nesting_offenders(self.SAFE, "x.py") == []
        assert _skip_offenders(self.SAFE, "x.py") == []


def test_the_module_docstring_does_not_overstate_the_scanners_reach():
    """#279: it claimed to pin "a live test that cannot take the device
    lock must fail, not skip" without qualification, while
    ``_skip_offenders`` matches exactly one shape.

    Pinned here because the overstatement is the kind that makes a later
    reader stop looking: a guard believed to be complete is not extended.
    The evaded shapes are named in the docstring, so the assertion is that
    the qualification is present, not merely that the claim is softened.
    """
    doc = __doc__ or ""
    assert "Part, not all of it" in doc
    assert "floor, not a guarantee" in doc
    for shape in ("import DeviceLock as DL", "from pytest import skip",
                  "is False:"):
        assert shape in doc, shape

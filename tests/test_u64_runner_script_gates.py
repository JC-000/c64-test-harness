"""No script under ``scripts/`` may invent an Ultimate device.

Named ``..._gates`` and not ``..._live_runner_scripts``: the old name
matched ``*_live*``, so ``--ignore-glob='*_live*'`` — which several
workflows use to go faster — silently dropped the invariants in this
file. An invariant that a common ignore-glob removes is absent exactly
when someone is running a reduced suite. Nothing here is a live test.

Issue #243, widened by the 2026-09-11 sweep. Six scripts fell back to a
hard-coded ``192.168.1.81`` when the caller named no host, and a seventh
(``rrnet_first_exchange_probe.py``) did ``os.environ.setdefault("U64_HOST",
"10.43.23.81")`` — inventing a device *and* exporting the live gate, with the
real bench address baked in. ``192.168.1.81`` belongs to no device on this
bench (U64E 10.43.23.81, C64U 10.53.21.158), so those defaults did not fail
cleanly: they reached whatever answers at that address on whatever network
the machine was on. Two of the scripts exist to put sustained parallel load
on a device.

Three layers are pinned here:

1. :mod:`scripts._u64_host` — the behaviour itself, defined once;
2. the two pytest-launching wrappers end to end, through ``main()``, since
   they are the ones that export the gate to a child process;
3. repo-wide invariants over ``scripts/``, ``examples/`` **and** ``tests/``
   (recursively): no device address appears as a runtime value — literal or
   built from literals — and no import-time read of a ``*HOST*`` variable
   carries a non-empty default, whatever the address. Prose may only carry
   a ``<device>`` placeholder. Issue #275 is the ``tests/`` half: its fix
   claimed this pin, but the runtime-value rule walked only ``scripts/``
   and ``examples/``, and no default rule existed, until section 4 below.

No device traffic: ``pytest.main`` is stubbed wherever a wrapper reaches it,
and the refusal cases assert it is never reached.
"""

from __future__ import annotations

import ast
import importlib.util
import io
import os
import sys
import re
from pathlib import Path
from types import ModuleType

import pytest


_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"

#: The address the scripts used to invent. Belongs to no device on this bench.
_PHANTOM_HOST = "192.168.1.81"

#: Real bench addresses. A script must not carry these as runtime literals
#: either — a live IP pinned into source is how the phantom got started.
_BENCH_HOSTS = ("10.43.23.81", "10.53.21.158")

_DEVICE_ADDRESSES = (_PHANTOM_HOST, *_BENCH_HOSTS)


def _address_tails(minimum_octets: int = 2) -> frozenset[str]:
    """Suffix-anchored fragments of each device address, longest to shortest.

    A literal check catches ``"10.43.23.81"`` and misses ``"10." +
    "43.23.81"`` — demonstrated, not theorised: a probe script building the
    address by concatenation, f-string, ``str.join`` and bytes-decode passed
    both rules. Matching *fragments* closes the accidental cases.

    Why suffix-anchored specifically, and not any run of octets: the
    head of an address is not distinctive. ``192.168`` appears in four
    unrelated mock addresses in this suite (``192.168.1.42``,
    ``192.168.1.10``, ``192.168.99.99``, ``192.168.2.64``), so a
    prefix-matching rule produces 14 false positives here and would be
    deleted by the next person it annoys — the same failure mode as an
    over-broad rule, where the cost is not the noise but the removal.
    A *tail* is distinctive because a constructed address has to end in the
    real final octets to route anywhere. Measured over ``scripts/``,
    ``examples/`` and ``tests/``: zero false positives at two octets.
    """
    tails: set[str] = set()
    for address in _DEVICE_ADDRESSES:
        octets = address.split(".")
        for i in range(len(octets) - minimum_octets + 1):
            tails.add(".".join(octets[i:]))
    return frozenset(tails)


_ADDRESS_TAILS = _address_tails()


def _names_a_device(text: str) -> bool:
    """Whether *text* contains a device address or a usable tail of one.

    **Known limits, stated rather than left to be discovered.** This catches
    what happens by accident — a pasted literal, a line split across a
    concatenation — and does not attempt to catch an author who is actively
    hiding an address. Specifically it will miss:

    * an f-string interpolating any part of the address
      (``f"10.43.23.{n}"``) — the value is not in the source to match;
    * anything computed at runtime — base64, ``chr()`` arithmetic, a
      lookup table, an address read from a file.

    That is the correct scope. An AST check that chases every construction
    becomes unmaintainable and gets deleted, and the threat model is not an
    adversary: it is someone reintroducing a plain default. The runtime
    tripwire (``test_script_refuses_cleanly_with_no_host``) is what covers
    the constructed cases, because it executes the script and a computed
    address still has to be *used*.
    """
    if any(address in text for address in _DEVICE_ADDRESSES):
        return True
    return any(tail in text for tail in _ADDRESS_TAILS)

#: The two wrappers that launch pytest and therefore export the live gate.
_RUNNERS = [
    pytest.param("run_all_u64_live.py", id="run_all"),
    pytest.param("run_sid_u64_live.py", id="run_sid"),
]


def _load(script_name: str) -> ModuleType:
    """Import a script by path, with ``scripts/`` importable as it would be.

    Running ``python3 scripts/foo.py`` puts ``scripts/`` on ``sys.path[0]``,
    which is how the siblings reach ``_u64_host``. Loading by file path here
    does not, so the scripts insert their own directory; this just mirrors
    the runtime shape for anything that resolves at import time.
    """
    path = _SCRIPTS / script_name
    spec = importlib.util.spec_from_file_location(f"_runner_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def u64_host_module() -> ModuleType:
    return _load("_u64_host.py")


@pytest.fixture
def stub_pytest_main(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record ``pytest.main`` invocations instead of running a live suite."""
    calls: list[list[str]] = []

    def _fake_main(args: list[str]) -> int:
        calls.append(list(args))
        return 0

    monkeypatch.setattr(pytest, "main", _fake_main)
    return calls


# ---------------------------------------------------------------------------
# 1. The shared resolver
# ---------------------------------------------------------------------------

def test_resolver_returns_none_when_no_host_is_named(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("U64_HOST", raising=False)
    assert u64_host_module.resolve_u64_host() is None


def test_resolver_honours_the_environment_verbatim(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("U64_HOST", "10.43.23.81")
    assert u64_host_module.resolve_u64_host() == "10.43.23.81"


def test_resolver_treats_an_empty_env_var_as_unset(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``U64_HOST=`` is not a device. Without this it becomes host ``""``."""
    monkeypatch.setenv("U64_HOST", "")
    assert u64_host_module.resolve_u64_host() is None


def test_explicit_argument_wins_and_says_so(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("U64_HOST", "10.43.23.81")
    err = io.StringIO()

    host = u64_host_module.resolve_u64_host("10.53.21.158", stderr=err)

    assert host == "10.53.21.158"
    assert "10.43.23.81" in err.getvalue(), (
        "an argument that overrides an exported U64_HOST must say so: "
        f"{err.getvalue()!r}"
    )


def test_agreeing_argument_and_env_produce_no_warning(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("U64_HOST", "10.43.23.81")
    err = io.StringIO()

    host = u64_host_module.resolve_u64_host("10.43.23.81", stderr=err)

    assert host == "10.43.23.81"
    assert err.getvalue() == ""


def test_require_refuses_with_exit_2_and_a_message(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("U64_HOST", raising=False)
    err = io.StringIO()

    with pytest.raises(SystemExit) as excinfo:
        u64_host_module.require_u64_host(argv0="scripts/whatever.py", stderr=err)

    assert excinfo.value.code == u64_host_module.NO_HOST_EXIT == 2
    message = err.getvalue()
    assert "U64_HOST" in message, f"refusal must name the variable: {message!r}"
    assert "scripts/whatever.py" in message, "refusal must show how to invoke"
    assert not any(a in message for a in _DEVICE_ADDRESSES), (
        f"the refusal must not teach a device address: {message!r}"
    )


def test_require_does_not_export_the_gate_by_default(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the pytest wrappers hand the gate to a child; nothing else does."""
    monkeypatch.delenv("U64_HOST", raising=False)

    host = u64_host_module.require_u64_host("10.43.23.81")

    assert host == "10.43.23.81"
    assert "U64_HOST" not in os.environ, (
        "a plain script exported the live gate into its own environment"
    )


def test_require_exports_the_gate_when_asked(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("U64_HOST", raising=False)

    u64_host_module.require_u64_host("10.43.23.81", export=True)

    assert os.environ["U64_HOST"] == "10.43.23.81"


# ---------------------------------------------------------------------------
# 2. The pytest wrappers, end to end
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("script", _RUNNERS)
def test_wrapper_refuses_when_no_host_given_anywhere(
    script: str,
    monkeypatch: pytest.MonkeyPatch,
    stub_pytest_main: list[list[str]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No argument, no ``U64_HOST``: refuse loudly rather than guess."""
    monkeypatch.delenv("U64_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", [script])

    module = _load(script)
    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 2, "refusal must be a non-zero exit"
    assert stub_pytest_main == [], "pytest ran despite no host being named"
    assert "U64_HOST" not in os.environ, (
        "the script exported a live gate the caller did not set"
    )
    message = capsys.readouterr().err
    assert "U64_HOST" in message, f"refusal must name the variable: {message!r}"


@pytest.mark.parametrize("script", _RUNNERS)
def test_wrapper_honours_a_host_the_caller_already_set(
    script: str,
    monkeypatch: pytest.MonkeyPatch,
    stub_pytest_main: list[list[str]],
) -> None:
    """``U64_HOST`` from the environment is used as-is, never overwritten."""
    monkeypatch.setenv("U64_HOST", "10.43.23.81")
    monkeypatch.setattr(sys, "argv", [script])

    module = _load(script)
    rc = module.main()

    assert os.environ["U64_HOST"] == "10.43.23.81", (
        "the script overrode a host the caller set deliberately"
    )
    assert rc == 0
    assert len(stub_pytest_main) == 1


@pytest.mark.parametrize("script", _RUNNERS)
def test_wrapper_exports_an_explicit_argument(
    script: str,
    monkeypatch: pytest.MonkeyPatch,
    stub_pytest_main: list[list[str]],
) -> None:
    """A named host is the caller's consent; export it for the test modules."""
    monkeypatch.delenv("U64_HOST", raising=False)
    monkeypatch.setattr(sys, "argv", [script, "10.53.21.158"])

    module = _load(script)
    rc = module.main()

    assert os.environ["U64_HOST"] == "10.53.21.158"
    assert rc == 0
    assert len(stub_pytest_main) == 1


# ---------------------------------------------------------------------------
# 3. The repo-wide invariant
# ---------------------------------------------------------------------------

#: ``examples/`` is scanned alongside ``scripts/``: it was missed by the
#: first sweep and is the directory a newcomer is most likely to copy from,
#: so a hard-coded default there propagates further than one in a script.
_EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _script_paths() -> list[Path]:
    """Python files under ``scripts/`` and ``examples/``, recursively.

    ``rglob``, not ``glob``: ``scripts/mutation/`` is a subdirectory and a
    flat glob would scan past it silently.
    """
    return sorted(
        p
        for root in (_SCRIPTS, _EXAMPLES)
        for p in root.rglob("*.py")
        if "__pycache__" not in p.parts
    )


#: Extensions the prose rule covers. Shell scripts and markdown teach an
#: invocation exactly as a docstring does — ``scripts/U64_DEVICE_PROBE.md``
#: carried six ``curl http://<address>/v1/...`` recipes and was swept by
#: hand. Work held by hand is work that comes undone; the invariant holds
#: it now.
_PROSE_EXTENSIONS = (".py", ".sh", ".md")


def _prose_paths() -> list[Path]:
    return sorted(
        p
        for root in (_SCRIPTS, _EXAMPLES)
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in _PROSE_EXTENSIONS
        and "__pycache__" not in p.parts
    )


def test_the_scanners_are_actually_scanning_something() -> None:
    """Guard against a vacuous pass.

    pytest treats an empty ``parametrize`` as a *skip*, not a failure, so a
    scanner whose file list comes back empty — a renamed directory, a glob
    that stopped matching, a ``rglob`` that should have been used — reports
    success while checking nothing. These asserts are the floor: both roots
    must be present, and named files known to live in each must be found.
    """
    paths = _script_paths()
    names = {p.name for p in paths}

    assert len(paths) > 20, f"only {len(paths)} files scanned; the glob is broken"
    for expected in ("_u64_host.py", "run_all_u64_live.py", "probe_u64.py"):
        assert expected in names, f"scripts/ not being scanned ({expected} missing)"
    for expected in ("ultimate64_hello.py", "play_sid.py"):
        assert expected in names, f"examples/ not being scanned ({expected} missing)"

    prose = {p.name for p in _prose_paths()}
    suffixes = {p.suffix for p in _prose_paths()}
    assert suffixes >= {".py", ".sh", ".md"}, (
        f"the prose scan is not reaching every file type: {sorted(suffixes)}"
    )
    assert "U64_DEVICE_PROBE.md" in prose, "markdown under scripts/ unscanned"
    assert any(p.parent.name == "mutation" for p in _script_paths()), (
        "scripts/mutation/ unscanned — the glob is not recursive"
    )

    modules = {p.name for p in _test_module_paths()}
    assert len(modules) > 100, f"only {len(modules)} test modules scanned"
    assert "test_u64_turbo_bench_live.py" in modules


def test_the_scanners_flag_a_known_offender() -> None:
    """Positive control: prove each scan *can* fail, on input it must reject.

    A scan that silently stopped matching — a changed literal, a broken AST
    walk, an address list that drifted — would go green across every file
    and read as "the repo is clean". These synthetic sources are what a
    regression actually looks like, one per rule.
    """
    # Rule 1 (code): a runtime literal, in the two shapes that bite.
    for source in (
        'HOST = "192.168.1.81"\n',
        'def connect(host="10.43.23.81"):\n    pass\n',  # rule-fixture
        'transport = T(host="10.53.21.158")\n',  # rule-fixture
    ):
        offenders = [
            (lineno, value)
            for lineno, value in _runtime_string_literals(source)
            if any(addr in value for addr in _DEVICE_ADDRESSES)
        ]
        assert offenders, f"rule 1 missed a runtime literal in: {source!r}"

    # ...and does not fire on a docstring, which is rule 2's business.
    clean = '"""Usage: U64_HOST=192.168.1.81 python3 x.py"""\nX = 1\n'  # rule-fixture
    assert not [
        v for _, v in _runtime_string_literals(clean)
        if any(addr in v for addr in _DEVICE_ADDRESSES)
    ], "rule 1 fired on a docstring; that is rule 2's job"

    # The evasions the reviewer demonstrated with planted probe scripts.
    # A literal-only check passed every one of these; each must now fail.
    for label, source in {
        "concatenation": 'HOST = "10." + "43.23.81"\n',
        "concat mid-octet": 'HOST = "10.4" + "3.23.81"\n',
        "f-string with a literal tail": 'P = "10."\nHOST = f"{P}43.23.81"\n',
        "str.join over single octets": 'HOST = ".".join(["10","43","23","81"])\n',
        "bytes decode": 'HOST = b"10.43.23.81".decode()\n',
        "split at the final octet": 'HOST = "10.43.23." + "81"\n',
        "phantom via join": 'HOST = ".".join(("192","168","1","81"))\n',
        "c64u via concat": 'HOST = "10.53." + "21.158"\n',
    }.items():
        assert any(
            _names_a_device(value)
            for _, value in _runtime_string_literals(source)
        ), f"rule 1 missed a constructed address ({label}): {source!r}"

    # The documented limit, asserted so it stays a known gap rather than
    # quietly becoming a surprise: an interpolated octet is not in the
    # source and cannot be matched here. The runtime tripwire covers it.
    interpolated = 'HOST = f"10.43.23.{n}"\n'
    assert not any(
        _names_a_device(value)
        for _, value in _runtime_string_literals(interpolated)
    ), (
        "rule 1 now catches an interpolated octet — good, but _names_a_device's "
        "documented limits say it does not. Update the docstring."
    )

    # Rule 2 (prose): the line-level scan the prose tests use.
    prose = "# run it with U64_HOST=192.168.1.81\n"  # rule-fixture
    assert [
        line for line in prose.splitlines()
        if any(addr in line for addr in _DEVICE_ADDRESSES)
    ], "rule 2 missed an address in a comment"


def _runtime_string_literals(source: str) -> list[tuple[int, str]]:
    """Every string constant that is *not* a docstring.

    Docstrings are handled separately (they cannot misdirect a program, only
    a person), so this isolates the literals that actually decide where
    packets go.
    """
    tree = ast.parse(source)
    docstring_nodes: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))

    found = []
    for node in ast.walk(tree):
        if id(node) in docstring_nodes:
            continue
        if isinstance(node, ast.Constant):
            value = node.value
            if isinstance(value, bytes):
                # b"10.43.23.81".decode() is a literal wearing a hat.
                value = value.decode("utf-8", "replace")
            if isinstance(value, str):
                found.append((node.lineno, value))
            continue
        # Two bounded folds, for the two builders that showed up in the
        # reviewer's evasion probe. Each is a single node shape, chosen
        # because it closes a demonstrated case without starting a chase:
        # anything more general belongs to the runtime tripwire, not here.
        folded = _fold_constant_string(node)
        if folded is not None:
            found.append((node.lineno, folded))
    return found


def _fold_constant_string(node: ast.AST) -> str | None:
    """Evaluate a string an author built out of literals, or ``None``.

    Handles ``"a" + "b"`` chains and ``"sep".join([...])`` over all-constant
    parts — the two shapes in the probe that slipped past a literal check.
    Deliberately does not handle f-strings with interpolated values or
    anything else computed: see :func:`_names_a_device` for why the line is
    drawn here.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _fold_constant_string(node.left)
        right = _fold_constant_string(node.right)
        if left is not None and right is not None:
            return left + right
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
        and len(node.args) == 1
        and isinstance(node.args[0], (ast.List, ast.Tuple))
    ):
        parts = [_fold_constant_string(e) for e in node.args[0].elts]
        if parts and all(part is not None for part in parts):
            return node.func.value.value.join(parts)  # type: ignore[arg-type]
    return None


@pytest.mark.parametrize(
    "script_path", _script_paths(), ids=lambda p: p.name
)
def test_no_device_address_is_a_runtime_literal(script_path: Path) -> None:
    """No script picks a device by hard-coded address — phantom or real.

    Loopback and peer addresses are untouched by this: only the three
    Ultimate device addresses are denied, because those are the ones a
    script can silently *drive*.
    """
    offenders = [
        (lineno, value)
        for lineno, value in _runtime_string_literals(script_path.read_text())
        if _names_a_device(value)
    ]
    assert offenders == [], (
        f"{script_path.name} picks a device by hard-coded address: {offenders}"
    )


#: Every script that resolves a device host. Each must refuse cleanly with
#: no host named — which is also the only end-to-end proof that the wiring
#: is right: the source-level tests below cannot see a missing import, and a
#: ``NameError`` in the refusal path is how one of these shipped mid-sweep.
_HOST_TAKING_SCRIPTS = [
    "bench_x25519_u64_turbo.py",
    "play_chromatic_u64.py",
    "play_scale_u64.py",
    "probe_u64.py",
    "probe_uci_network.py",
    "rrnet_first_exchange_probe.py",
    "run_all_u64_live.py",
    "run_sid_u64_live.py",
    "run_u64_parallel_locked.py",
    "stress_u64_queue.py",
]


#: Exit status the sandbox uses when the script under test tried to reach
#: the outside world. Distinct from 2 (refused, the pass) and from 1 (the
#: script's own failure) so the three outcomes never blur.
_BREACH_EXIT = 97

#: Runs a script with the outside world removed *before its first statement*.
#:
#: The earlier version of this test ran each script directly and relied on the
#: refusal happening before any network call. That assumption is exactly
#: backwards: the test is only interesting when the refusal is broken, and a
#: broken refusal means the script proceeds. Under a reintroduced default it
#: did — ``run_u64_parallel_locked.py`` fanned the live U64 suite across
#: parallel workers against the real Elite, and ``bench_x25519_u64_turbo.py``
#: / ``stress_u64_queue.py`` are body-carrying upload loops that would do the
#: same to the C64U, unbudgeted and unhygienic, presenting as a slow test run.
#:
#: So the outside world is removed by construction instead. Sockets cover
#: direct networking and everything urllib/http.client does on top of them;
#: ``fork_exec``/``fork``/``posix_spawn``/``exec*`` cover subprocess **and**
#: multiprocessing under either start method, which is what actually launched
#: the live suite — neutering sockets alone would not have stopped it, because
#: a spawned child is a fresh interpreter without this preamble.
#:
#: The sentinel derives from ``BaseException`` deliberately: these scripts
#: wrap device calls in ``except Exception``, and a breach must not be
#: swallowed and reported as an ordinary failure.
_SANDBOX = r"""
import os, runpy, socket, sys, _posixsubprocess

class _Breach(BaseException):
    pass

def _forbid(what):
    def _raise(*args, **kwargs):
        raise _Breach(what)
    return _raise

# Block what *reaches out*, not the socket class itself: ``ssl`` does
# ``class SSLSocket(socket)`` at import, so replacing the class breaks any
# script that imports urllib before it ever gets near a device.
for _mod, _name in (
    (socket, "create_connection"), (socket, "getaddrinfo"),
    (socket.socket, "connect"), (socket.socket, "connect_ex"),
    (socket.socket, "send"), (socket.socket, "sendall"),
    (socket.socket, "sendto"), (socket.socket, "sendmsg"),
    (_posixsubprocess, "fork_exec"),
    (os, "fork"), (os, "forkpty"), (os, "posix_spawn"), (os, "posix_spawnp"),
    (os, "execv"), (os, "execve"), (os, "execvp"), (os, "system"),
):
    if hasattr(_mod, _name):
        _label = getattr(_mod, "__name__", "socket.socket") + "." + _name
        setattr(_mod, _name, _forbid(_label))

_script = sys.argv[1]
# The script must see only its own name: with ``python -c`` the path would
# otherwise sit in argv[1] and be read as a HOST by the scripts that take a
# positional one, which would make this test pass for the wrong reason.
sys.argv = [_script]

try:
    runpy.run_path(_script, run_name="__main__")
except SystemExit:
    raise
except _Breach as exc:
    sys.stderr.write("SANDBOX BREACH: %s\n" % exc)
    sys.exit(97)
"""


#: Probes for the sandbox's positive control. Each is a thing a regressed
#: script really does. Kept at module scope so the fixture that gates the
#: tripwire and the control test that reports on it run the same list.
_BREACH_PROBES = {
    # RFC 5737 TEST-NET-1, reserved for documentation and guaranteed
    # non-routable. Deliberately NOT a bench address: if this control
    # ever fails open, the probe connects to nothing instead of opening
    # a TCP connection to the shared device this file exists to protect.
    # A safety test whose failure mode touches the hardware is not one.
    "socket": "import socket; socket.create_connection(('192.0.2.1', 80), 1)",
    "subprocess": "import subprocess; subprocess.run(['/bin/echo', 'hi'])",
    "multiprocessing": (
        "from concurrent.futures import ProcessPoolExecutor\n"
        "with ProcessPoolExecutor(max_workers=1) as p:\n"
        "    p.submit(print, 'hi').result()\n"
    ),
    # _SANDBOX claims it covers multiprocessing "under either start
    # method". The default was probed; fork was not, and an unpinned
    # "either" is the shape this review kept finding. macOS defaults to
    # spawn, so fork is the one that would silently stop being covered.
    "multiprocessing fork context": (
        "import multiprocessing as mp\n"
        "ctx = mp.get_context('fork')\n"
        "p = ctx.Process(target=print, args=('hi',))\n"
        "p.start(); p.join()\n"
    ),
    # Direct probe. Without it, blocking os.posix_spawn is unfalsifiable
    # here: with fork_exec and os.fork already blocked, nothing CPython
    # does on this platform reaches posix_spawn, so removing it from the
    # forbid list survived as an equivalent mutant. The forbid list is
    # defence in depth against a platform or CPython version that *does*
    # route through it, and this probe is what makes that claim testable
    # rather than decorative.
    "os.system directly": "import os\nos.system('echo hi')\n",
    "os.posix_spawn directly": (
        "import os\n"
        "os.posix_spawn('/bin/echo', ['/bin/echo', 'hi'], os.environ)\n"
    ),
    "multiprocessing spawn context": (
        "import multiprocessing as mp\n"
        "ctx = mp.get_context('spawn')\n"
        "p = ctx.Process(target=print, args=('hi',))\n"
        "p.start(); p.join()\n"
    ),
    # Direct socket-method probes, one per forbid-list entry that nothing
    # above reaches (``create_connection`` is blocked first, so removing
    # ``connect``/``sendto``/``getaddrinfo`` survived as mutants). Each is a
    # shape the harness really uses: UDP ``connect`` (render_wav_u64.py),
    # ``sendto`` (u64_socket_dma.py), and name resolution. TEST-NET-1 only.
    "socket.connect directly (UDP)": (
        "import socket\n"
        "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).connect(('192.0.2.1', 9))\n"
    ),
    "socket.sendto directly (UDP)": (
        "import socket\n"
        "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('192.0.2.1', 9))\n"
    ),
    "socket.getaddrinfo directly": (
        "import socket\nsocket.getaddrinfo('192.0.2.1', 9)\n"
    ),
    # ``sendmsg`` is on the forbid list but nothing above reaches it, so
    # removing it survived (#315). Nothing in src/ uses it today: this is
    # defence in depth, made falsifiable the same way as ``posix_spawn``.
    "socket.sendmsg directly (UDP)": (
        "import socket\n"
        "socket.socket(socket.AF_INET, socket.SOCK_DGRAM)"
        ".sendmsg([b'x'], [], 0, ('192.0.2.1', 9))\n"
    ),
}


def _run_in_sandbox(
    script: Path, *, env: dict[str, str] | None = None
) -> "subprocess.CompletedProcess[str]":
    """The one sandboxed invocation. The controls verify it; the tripwire uses it.

    Keeping these the same function is the point: a control that proves one
    command line and a tripwire that runs another proves nothing about the
    tripwire (review round 1 — a preamble-less tripwire stayed green).
    """
    import subprocess

    return subprocess.run(
        [sys.executable, "-c", _SANDBOX, str(script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _verify_sandbox(workdir: Path) -> dict[str, int]:
    """Run both sandbox controls; raise if either fails. Returns exit codes.

    Positive control: every probe in :data:`_BREACH_PROBES` must die with
    :data:`_BREACH_EXIT`. Negative control: a script that refuses properly
    must still exit 2 and keep its stderr, or the tripwire would pass
    vacuously for scripts that never reached their refusal.
    """
    results: dict[str, int] = {}
    for name, body in _BREACH_PROBES.items():
        script = workdir / f"breach_{name.replace(' ', '_')}.py"
        script.write_text(body)
        proc = _run_in_sandbox(script)
        results[name] = proc.returncode
        assert proc.returncode == _BREACH_EXIT, (
            f"the sandbox did not block a {name} breach "
            f"(exit {proc.returncode}) — it is failing open.\n"
            f"stdout:{proc.stdout[-500:]}\nstderr:{proc.stderr[-1000:]}"
        )

    clean = workdir / "clean.py"
    clean.write_text(
        "import sys\n"
        "sys.stderr.write('no U64_HOST named\\n')\n"
        "sys.exit(2)\n"
    )
    proc = _run_in_sandbox(clean)
    results["clean refusal"] = proc.returncode
    assert proc.returncode == 2, (
        f"the sandbox broke a clean refusal (exit {proc.returncode}):\n"
        f"{proc.stderr[-1000:]}"
    )
    assert "U64_HOST" in proc.stderr, "the sandbox swallowed a refusal's stderr"
    return results


@pytest.fixture(scope="module")
def verified_sandbox(tmp_path_factory: pytest.TempPathFactory) -> dict[str, int]:
    """The sandbox, proven to hold, before any real script runs under it.

    This is the ordering guarantee, and it is a *dependency*, not a
    collection order. pytest runs tests in definition order, so an earlier
    version of this file — which put the controls after the tripwire and
    claimed "alphabetical collection order happens to give this" — executed
    all ten real scripts before the sandbox was ever checked. A fixture
    cannot be reordered away: ``-k``, ``--lf``, ``-p randomly`` or a moved
    function still set it up first, and if a control fails the tripwire
    errors at setup and no script is executed at all.
    """
    return _verify_sandbox(tmp_path_factory.mktemp("sandbox_controls"))


@pytest.mark.parametrize("script_name", _HOST_TAKING_SCRIPTS)
def test_script_refuses_cleanly_with_no_host(
    script_name: str, verified_sandbox: dict[str, int]
) -> None:
    """Run each script with no host named; it must exit 2 and say why.

    This is the only test here that can see a missing import, so it has to
    actually execute the script. It has now caught two real defects, both
    that class, both invisible to every source-level check in this file: a
    missing ``require_u64_host`` import in ``probe_uci_network.py``, and a
    ``from pathlib import Path`` deleted from above the ``sys.path.insert``
    that uses it in ``stress_u64_queue.py`` (``NameError: name 'Path' is not
    defined``). If you find this test slow, that is what you would be
    giving up — the answer to its one genuine hazard was to sandbox it
    (see :data:`_SANDBOX`), not to delete it. What it must not do is let a *broken* refusal reach
    a device, which is precisely the case it exists to detect.

    Hence the sandbox in :data:`_SANDBOX`: the script runs with sockets and
    every process-spawning primitive already replaced, so a regression that
    gets past the refusal dies offline with :data:`_BREACH_EXIT` instead of
    driving the bench. The test cannot reach a device even when the code
    under test is wrong, which is the property the previous version lacked.

    Cost, measured rather than guessed, because "dies in under a second" is
    a *per-script* figure and understates the suite: all ten parametrized
    cases take ~1.7 s clean, and ~9-12 s when a regression is present (each
    script runs further before hitting the fork boundary). Ten subprocesses
    at ``timeout=10`` each is the worst case if one ever hangs instead.
    """
    env = {k: v for k, v in os.environ.items() if k != "U64_HOST"}
    proc = _run_in_sandbox(_SCRIPTS / script_name, env=env)

    assert proc.returncode != _BREACH_EXIT, (
        f"{script_name} got past the refusal and tried to reach the outside "
        f"world. The sandbox stopped it; without the sandbox this would have "
        f"been real device traffic.\nstderr:\n{proc.stderr[-2000:]}"
    )
    assert proc.returncode == 2, (
        f"{script_name} exited {proc.returncode}, not the refusal status 2.\n"
        f"stderr:\n{proc.stderr[-2000:]}"
    )
    assert "U64_HOST" in proc.stderr, (
        f"{script_name} refused without naming the variable:\n{proc.stderr[-2000:]}"
    )


def test_the_sandbox_actually_blocks_a_breach(
    verified_sandbox: dict[str, int],
) -> None:
    """Positive control: prove the sandbox stops what it claims to stop.

    Without this the sandbox could silently fail open — every script would
    still exit 2 for its own reasons and the suite would look fine while
    protecting nothing. The probes are :data:`_BREACH_PROBES`; the work is
    done once, in the :func:`verified_sandbox` fixture, so that the tripwire
    depends on the same verification rather than on this test happening to
    run first. See :func:`test_the_tripwire_cannot_run_before_its_controls`.
    """
    for name in _BREACH_PROBES:
        assert verified_sandbox[name] == _BREACH_EXIT, (
            f"{name}: exit {verified_sandbox[name]}"
        )


def test_the_sandbox_does_not_break_a_clean_refusal(
    verified_sandbox: dict[str, int],
) -> None:
    """Negative control: a script that refuses properly still exits 2.

    Guards the other direction — a sandbox that broke every script would
    make :func:`test_script_refuses_cleanly_with_no_host` pass vacuously for
    scripts that never reach their refusal at all.
    """
    assert verified_sandbox["clean refusal"] == 2


#: ``os`` attributes that start a process. Called inside the tripwire through
#: a name bound to ``os``, or imported from it, each bypasses the sandboxed
#: invocation just as ``subprocess`` would.
_SPAWNING_OS_ATTRS = frozenset({
    "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp",
    "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe",
    "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
})


def _tripwire_invocation_problems(source: str, module_source: str = "") -> list[str]:
    """What is wrong with how *source* (a test function) runs a script.

    Structural, not textual (#315): a ``Call`` whose callee is the name
    ``_run_in_sandbox`` must exist; ``subprocess`` must not be touched at all
    (no import of it, no call through a name bound to it); and no process-
    starting ``os`` function (:data:`_SPAWNING_OS_ATTRS`) may be called. The
    textual version passed a tripwire that ran ``subprocess.Popen`` with
    ``_run_in_sandbox(`` present only in a comment.

    Names are bound from the function's own imports **and** from
    *module_source*'s import-time imports (review round 1 of #361): a
    module-level ``import subprocess as sp`` or ``from subprocess import
    run`` is otherwise invisible to a scan of one function.

    Known limits: a primitive reached without a name bound by an import
    (``importlib.import_module("subprocess")``, ``getattr(os, "system")``,
    ``__import__``, ``ctypes``) is not modelled. That is a deliberate
    evasion; the threat model is an accidental rewrite.
    """
    import textwrap

    tree = ast.parse(textwrap.dedent(source))
    problems: list[str] = []
    if not any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_run_in_sandbox"
        for n in ast.walk(tree)
    ):
        problems.append("no call to _run_in_sandbox")

    subprocess_names = {"subprocess"}
    os_names = {"os"}
    spawn_names: set[str] = set()

    def bind(node: ast.AST, report: bool) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top == "subprocess":
                    subprocess_names.add(alias.asname or "subprocess")
                    if report:
                        problems.append(f"line {node.lineno}: imports subprocess")
                elif top == "os":
                    os_names.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top == "subprocess":
                spawn_names.update(a.asname or a.name for a in node.names)
                if report:
                    problems.append(f"line {node.lineno}: imports from subprocess")
            elif top == "os":
                spawn_names.update(
                    a.asname or a.name for a in node.names if a.name in _SPAWNING_OS_ATTRS
                )

    if module_source:
        for node in _import_time_nodes(ast.parse(module_source)):
            bind(node, report=False)
    for node in ast.walk(tree):
        bind(node, report=True)

    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        chain: list[str] = []
        root = n.func
        while isinstance(root, ast.Attribute):
            chain.append(root.attr)
            root = root.value
        if not isinstance(root, ast.Name):
            continue
        if (
            root.id in subprocess_names
            or (not chain and root.id in spawn_names)
            or (chain and root.id in os_names and chain[-1] in _SPAWNING_OS_ATTRS)
        ):
            problems.append(f"line {n.lineno}: calls {ast.unparse(n.func)}")
    return problems


def _tripwire_problems_for(fn) -> list[str]:
    """:func:`_tripwire_invocation_problems` for *fn*, with its module's source.

    The helper reads the defining module itself (review round 2 of #361), so
    there is no call site that can forget to pass it: a module-level
    ``import subprocess as sp`` plus ``sp.run`` inside *fn* is always seen.
    """
    import inspect

    module_source = Path(inspect.getsourcefile(fn)).read_text()
    return _tripwire_invocation_problems(inspect.getsource(fn), module_source)


def test_the_tripwire_helper_reads_the_defining_module(tmp_path: Path) -> None:
    """Positive control for :func:`_tripwire_problems_for` on a real module."""
    import inspect

    probe = tmp_path / "tripwire_module_probe.py"
    probe.write_text(
        "import subprocess as sp\n"
        "\n"
        "\n"
        "def test_x(script_name):\n"
        "    proc = _run_in_sandbox(script_name)\n"
        "    sp.run([script_name])\n"
    )
    spec = importlib.util.spec_from_file_location("tripwire_module_probe", probe)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # The function alone looks clean, so only the module read can flag it.
    assert _tripwire_invocation_problems(inspect.getsource(module.test_x)) == [], (
        "control broken: the function source alone is already flagged"
    )
    problems = _tripwire_problems_for(module.test_x)
    assert any("sp.run" in p for p in problems), (
        f"the helper did not see the module-level import: {problems}"
    )


def test_the_tripwire_invocation_check_can_fail() -> None:
    """Positive controls for :func:`_tripwire_invocation_problems`."""
    clean = (
        "def test_x(script_name, verified_sandbox):\n"
        "    proc = _run_in_sandbox(_SCRIPTS / script_name, env=env)\n"
    )
    assert _tripwire_invocation_problems(clean) == []
    must_flag = {
        # The review's shape: the name survives only in a comment.
        "Popen, sandbox call only in a comment": (
            "def test_x(script_name, verified_sandbox):\n"
            "    # _run_in_sandbox(\n"
            "    proc = subprocess.Popen([sys.executable, script_name])\n"
        ),
        "aliased import": (
            "def test_x(script_name, verified_sandbox):\n"
            "    proc = _run_in_sandbox(script_name)\n"
            "    import subprocess as sp\n"
            "    sp.run([sys.executable, script_name])\n"
        ),
        "from-import": (
            "def test_x(script_name, verified_sandbox):\n"
            "    from subprocess import Popen as P\n"
            "    proc = _run_in_sandbox(script_name)\n"
            "    P([sys.executable, script_name])\n"
        ),
        "sandbox referenced but never called": (
            "def test_x(script_name, verified_sandbox):\n"
            "    runner = _run_in_sandbox\n"
        ),
    }
    for label, source in must_flag.items():
        assert _tripwire_invocation_problems(source), f"missed: {label}"

    # Review round 1 of #361: names bound at module level. Each body passes
    # without the module's imports, so the control proves the seeding.
    fn = clean
    module_level = {
        "module-level `import subprocess as sp` + sp.run": (
            "import subprocess as sp\n",
            fn + "    sp.run([sys.executable, script_name])\n",
        ),
        "module-level `from subprocess import run` + run(...)": (
            "from subprocess import run\n",
            fn + "    run([sys.executable, script_name])\n",
        ),
        "module-level `import os as o` + o.posix_spawn": (
            "import os as o\n",
            fn + "    o.posix_spawn('/bin/echo', ['/bin/echo'], {})\n",
        ),
        "module-level `from os import system` + system(...)": (
            "from os import system\n",
            fn + "    system('echo hi')\n",
        ),
    }
    for label, (module, body) in module_level.items():
        assert _tripwire_invocation_problems(body, module), f"missed: {label}"
        assert _tripwire_invocation_problems(body) == [], (
            f"{label}: flagged without the module's imports, so this control "
            f"does not test the seeding"
        )
    assert _tripwire_invocation_problems(fn + "    os.system('echo hi')\n"), (
        "missed: os.system"
    )
    # ...while reading os.environ and building paths is not a spawn.
    assert _tripwire_invocation_problems(
        fn + "    env = {k: v for k, v in os.environ.items()}\n"
        + "    p = os.path.join('a', 'b')\n",
        "import os\n",
    ) == []


def test_the_tripwire_cannot_run_before_its_controls() -> None:
    """Pin the ordering as a dependency, since order in the file is not one.

    The tripwire executes real scripts and is safe only because the sandbox
    holds. It must request :func:`verified_sandbox`, and that fixture must
    run *both* controls. Either link removed and the tripwire can once more
    execute scripts under an unverified sandbox with every test green.
    """
    import inspect

    params = inspect.signature(test_script_refuses_cleanly_with_no_host).parameters
    assert "verified_sandbox" in params, (
        "test_script_refuses_cleanly_with_no_host no longer depends on "
        "verified_sandbox; it can run real scripts before the sandbox is proven"
    )
    fixture_source = inspect.getsource(verified_sandbox)
    assert "_verify_sandbox(" in fixture_source, (
        "verified_sandbox no longer runs the controls"
    )
    verify_source = inspect.getsource(_verify_sandbox)
    assert "_BREACH_PROBES" in verify_source and "sys.exit(2)" in verify_source, (
        "_verify_sandbox no longer runs both the breach probes and the clean refusal"
    )
    assert len(_BREACH_PROBES) >= 10, "breach probes were dropped"

    # The controls verify one invocation; the tripwire must use that same
    # invocation, not build its own. Review round 1 showed the gap: a
    # tripwire running ``[sys.executable, script]`` with no preamble left
    # every test here green while the scripts ran with the network intact.
    problems = _tripwire_problems_for(test_script_refuses_cleanly_with_no_host)
    assert problems == [], (
        "the tripwire no longer runs scripts only through _run_in_sandbox, the "
        f"invocation the controls verified: {problems}"
    )
    assert "_SANDBOX" in inspect.getsource(_run_in_sandbox) and (
        "_run_in_sandbox(" in verify_source
    ), "the controls and the tripwire no longer share one sandboxed invocation"


@pytest.mark.parametrize(
    "script_path", _prose_paths(), ids=lambda p: p.name
)
def test_no_device_address_is_taught_in_usage_text(script_path: Path) -> None:
    """Nor in docstrings and comments.

    These cannot misdirect a program, but they are how a person learns the
    wrong value and then types it — which is where the phantom came from.
    The replacement is a ``<device>`` placeholder, never a real bench
    address.
    """
    source = script_path.read_text()
    hits = [
        (i, line.strip())
        for i, line in enumerate(source.splitlines(), 1)
        if _names_a_device(line)
    ]
    assert hits == [], (
        f"{script_path.name} teaches a device address in prose: {hits}"
    )


# ---------------------------------------------------------------------------
# Issue #293: the phantom in README, docs/ and the c64-test skill
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[1]

#: The skill directory CLAUDE.md sends agents to before they write a test.
_SKILL_DIR = _REPO / ".claude" / "skills" / "c64-test"


def _doc_prose_paths() -> list[Path]:
    """Markdown outside ``scripts/``/``examples/``/``tests/`` that teaches API use.

    README, every ``docs/**/*.md`` (recursively) and the c64-test skill.
    """
    paths = [_REPO / "README.md"]
    paths += (_REPO / "docs").rglob("*.md")
    paths += _SKILL_DIR.rglob("*.md")
    return sorted(p for p in paths if p.is_file())


#: Suffix tails of the phantom only, three octets and up. Unlike the scans
#: above this rule does not cover the bench addresses: the docs name those
#: deliberately (recovery notes, device facts). Two-octet tails are not used
#: here because a ``1.81`` tail would match version strings in prose.
#:
#: **Stated limit:** so a phantom split at the two-octet boundary
#: (``"192.168." "1.81"``) is not flagged. Pinned in
#: :func:`test_the_doc_phantom_scan_is_not_vacuous` so it stays known (#327).
_PHANTOM_TAILS = frozenset(
    ".".join(_PHANTOM_HOST.split(".")[i:]) for i in range(2)
)


def _phantom_prose_hits(text: str) -> list[tuple[int, str]]:
    return [
        (i, line.strip())
        for i, line in enumerate(text.splitlines(), 1)
        if any(tail in line for tail in _PHANTOM_TAILS)
    ]


@pytest.mark.parametrize(
    "doc_path", _doc_prose_paths(), ids=lambda p: str(p.relative_to(_REPO))
)
def test_no_doc_teaches_the_phantom_host(doc_path: Path) -> None:
    """README, docs and the skill show ``<device>``, never the phantom.

    The skill files are what an agent reads before writing a test, so a
    pasted example there becomes a hard-coded host in a new module (#293).
    """
    hits = _phantom_prose_hits(doc_path.read_text(encoding="utf-8"))
    assert hits == [], (
        f"{doc_path.relative_to(_REPO)} teaches the phantom device address; "
        f"use a <device> placeholder: {hits}"
    )


def test_the_doc_phantom_scan_is_not_vacuous() -> None:
    """Vacuity guard and positive control for the #293 doc scan."""
    scanned = {str(p.relative_to(_REPO)) for p in _doc_prose_paths()}
    for expected in (
        "README.md",
        "docs/device_locking.md",
        ".claude/skills/c64-test/SKILL.md",
        ".claude/skills/c64-test/PATTERNS.md",
        ".claude/skills/c64-test/REFERENCE.md",
    ):
        assert expected in scanned, f"{expected} is not being scanned"
    assert len(scanned) > 10, f"only {len(scanned)} markdown files scanned"

    # The tails really are the phantom's, and nothing wider.
    assert _PHANTOM_HOST in _PHANTOM_TAILS
    assert all(t.count(".") >= 2 for t in _PHANTOM_TAILS)

    # Positive control: the real README with one planted example must fail,
    # both as the whole address and as a concatenated tail.
    readme = (_REPO / "README.md").read_text(encoding="utf-8")
    head, _, tail = _PHANTOM_HOST.partition(".")
    for planted in (
        f'client = Ultimate64Client("{_PHANTOM_HOST}")',
        f'addr = "{head}." + "{tail}"',
    ):
        mangled = readme + "\n" + planted + "\n"
        assert _phantom_prose_hits(mangled), f"planted line not flagged: {planted}"
    # ...and the placeholder the docs use instead is not flagged.
    assert _phantom_prose_hits('client = Ultimate64Client("<device>")') == []

    # Stated limit (#327): a split at the two-octet boundary is not flagged.
    octets = _PHANTOM_HOST.split(".")
    two_octet_split = f'"{".".join(octets[:2])}." "{".".join(octets[2:])}"'
    assert _phantom_prose_hits(two_octet_split) == [], (
        "the doc scan now catches a two-octet split; update the _PHANTOM_TAILS "
        "comment's stated limit"
    )


def test_the_doc_phantom_scan_recurses_into_docs_subdirectories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``docs/`` has no subdirectories today, so ``rglob`` vs ``glob`` was an
    equivalent mutant on the real tree (#327). A tmp repo root makes it not.
    """
    (tmp_path / "README.md").write_text("clean\n")
    nested = tmp_path / "docs" / "sub" / "x.md"
    nested.parent.mkdir(parents=True)
    nested.write_text(f'client = Ultimate64Client("{_PHANTOM_HOST}")\n')
    skill = tmp_path / ".claude" / "skills" / "c64-test"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("clean\n")

    this_module = sys.modules[__name__]
    monkeypatch.setattr(this_module, "_REPO", tmp_path)
    monkeypatch.setattr(this_module, "_SKILL_DIR", skill)

    scanned = _doc_prose_paths()
    assert all(tmp_path in p.parents for p in scanned), (
        "the doc scan did not follow the monkeypatched repo root"
    )
    assert nested in scanned, "docs/ is not scanned recursively"
    assert _phantom_prose_hits(nested.read_text()), "the planted phantom was not found"


def _test_module_paths(include_allowlisted: bool = False) -> list[Path]:
    """Test modules to scan.

    *include_allowlisted* is for the gating invariant, which is about
    ``skipif`` rather than addresses: a module exempted from the address
    rule (it mocks with one) still has to gate on its host.
    """
    return sorted(
        p for p in _py_files_under(Path(__file__).resolve().parent)
        if include_allowlisted or p.name not in _ADDRESS_ALLOWLIST_FILES
    )


def _py_files_under(root: Path) -> list[Path]:
    """Python files under *root*, **recursively**.

    ``glob("*.py")`` would scan past ``tests/fixtures/`` and any future
    subpackage silently — the same flat-glob hole ``scripts/mutation/`` once
    was. Pinned by :func:`test_the_tests_scans_flag_planted_regressions`.
    """
    return sorted(
        p for p in root.rglob("*.py") if "__pycache__" not in p.parts
    )


#: Matches **any uppercase name containing ``HOST``** — as a substring, so
#: ``LOCALHOST``, ``PROXY_HOST`` and even ``GHOSTSCRIPT_PATH`` match too.
#: That is deliberately broader than the device names it exists for
#: (``U64_HOST``, ``U64_NOTICE_HOST``, and the ``C64U_HOST`` someone will add
#: on a bench with two device generations), and the breadth is the point:
#: a false negative loses #275 protection silently and nobody learns, while a
#: false positive fails a test, someone reads the reason, and the exception
#: lands in the must-not-detect fixtures where the next reader sees it. An
#: ``environ`` read is required as well, so the live cost is zero — measured
#: over ``tests/``, the narrow ``^U64_[A-Z0-9_]*HOST$`` and this both give 38
#: modules and 0 ungated.
#:
#: Pinned by fixtures in both directions, including a non-device name, so
#: nobody "corrects" this into a narrow regex and silently reopens
#: ``C64U_HOST``.
_HOST_ENV_VAR = re.compile(r"HOST")


def _is_type_checking_guard(node: ast.If) -> bool:
    """``if TYPE_CHECKING:`` / ``if typing.TYPE_CHECKING:`` — false at runtime."""
    test = node.test
    if isinstance(test, ast.Name):
        return test.id == "TYPE_CHECKING"
    if isinstance(test, ast.Attribute):
        return test.attr == "TYPE_CHECKING"
    return False


def _module_level_assigns(tree: ast.Module) -> list[ast.Assign]:
    """Every assignment that runs at import, not just those in ``tree.body``.

    A binding inside a module-level ``try:``, ``if:`` or ``with:`` executes at
    import exactly like a bare one, but is not a child of ``tree.body`` — so
    iterating that list made such a module *invisible to the parametrization
    entirely*, rather than flagged. Caught by mutation: wrapping a live
    module's host binding in ``try:`` gave 38 passed.

    **Declared limit:** assignments inside a function or class body are not
    import-time bindings and are deliberately skipped. That is a limit rather
    than a drift, because this sentence and the code agree.
    """
    found: list[ast.Assign] = []

    def descend(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            # `if TYPE_CHECKING:` is false at runtime, so its body never
            # binds anything. Widening naively would have flagged it while
            # this docstring still said "runs at import" — the same
            # criterion/implementation drift, reopened in the opposite
            # direction. Both directions are pinned by fixtures in
            # test_the_binding_detector_scope_is_pinned_by_fixtures.
            if isinstance(child, ast.If) and _is_type_checking_guard(child):
                continue
            if isinstance(child, ast.Assign):
                found.append(child)
            descend(child)

    descend(tree)
    return found


def _env_var_reads(node: ast.AST) -> list[str]:
    """Environment variable names *read* in this subtree.

    Only the shapes that actually read one: ``os.environ.get("X")``,
    ``os.environ["X"]`` and ``os.getenv("X")``. Matching any string constant
    near an ``environ`` attribute was wrong — it read
    ``patch.dict(os.environ, {"U64_HOST": "x"})`` as a device binding, which
    the docstring already claimed it did not. Found by the scope fixtures,
    not by reading.
    """
    names: list[str] = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            is_environ_get = (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "environ"
            )
            is_getenv = (
                isinstance(func, ast.Attribute) and func.attr == "getenv"
            ) or (isinstance(func, ast.Name) and func.id == "getenv")
            if (is_environ_get or is_getenv) and sub.args:
                first = sub.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.append(first.value)
        elif isinstance(sub, ast.Subscript):
            value = sub.value
            if isinstance(value, ast.Attribute) and value.attr == "environ":
                if isinstance(sub.slice, ast.Constant) and isinstance(
                    sub.slice.value, str
                ):
                    names.append(sub.slice.value)
    return names


def _env_host_bindings(tree: ast.Module) -> list[tuple[str, str]]:
    """Import-time names bound to a device host read from the environment.

    Matches ``_HOST = os.environ.get("U64_HOST")`` and its variants, wherever
    they sit at import scope — including inside a module-level ``try:`` or
    ``if:``. The environment variable must be the one actually *read*
    (see :func:`_env_var_reads`) and its name must be uppercase and contain
    ``HOST``, so ``patch.dict(os.environ, {"U64_HOST": ...})`` in a unit test
    is not mistaken for a module arming itself against a device.
    """
    bindings: list[tuple[str, str]] = []
    for node in _module_level_assigns(tree):
        env_names = [
            name
            for name in _env_var_reads(node.value)
            if name.isupper() and _HOST_ENV_VAR.search(name)
        ]
        if not env_names:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                bindings.append((target.id, env_names[0]))
    return bindings


def _skipif_references(tree: ast.Module) -> set[str]:
    """Every name and string appearing inside any ``skipif`` in the module.

    Deliberately searched **anywhere**, not only in ``pytestmark``:
    ``test_blind_agent_probe.py``, ``test_bridge_ping_tod.py`` and
    ``test_stress_smoke.py`` gate with function-level decorators, and a
    ``pytestmark``-only check would report three false positives. An
    invariant that annoys people gets deleted, and then it protects nothing
    — the cost is the removal, not the noise.
    """
    references: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "skipif":
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    references.add(sub.id)
                elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    references.add(sub.value)
    return references


def _host_binding_modules() -> list[Path]:
    out = []
    for path in _test_module_paths(include_allowlisted=True):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - defensive
            continue
        if _env_host_bindings(tree):
            out.append(path)
    return out


@pytest.mark.parametrize(
    "module_path", _host_binding_modules(), ids=lambda p: p.name
)
def test_a_module_that_names_a_device_is_gated_on_it(module_path: Path) -> None:
    """The host must be part of the gate — the regression guard for #275.

    Three live modules read ``U64_HOST`` with a hard-coded default and gated
    only on their feature variable, so ``AUDIO_RATE_LIVE=1`` alone drove the
    real U64E with nobody naming a device. Removing the default fixed those
    three; nothing defended the *property*. Deleting a host ``skipif`` was a
    surviving mutation, so the next person to simplify one reopens #275
    silently.

    A module that binds a device host at import time must carry a ``skipif``
    referring to that binding or its environment variable — anywhere in the
    module, decorator or ``pytestmark`` alike. "Import time" includes a
    binding inside a module-level ``try:``/``if:``/``with:``; see
    :func:`_module_level_assigns`, which says exactly what it does and does
    not reach.
    """
    tree = ast.parse(module_path.read_text())
    references = _skipif_references(tree)

    ungated = [
        f"{name} (from ${env_var})"
        for name, env_var in _env_host_bindings(tree)
        if name not in references and env_var not in references
    ]
    assert ungated == [], (
        f"{module_path.name} binds a device host that no skipif gates on: "
        f"{ungated}. With only its feature gate set and no host named, this "
        "module would drive whatever that binding resolves to (#275)."
    )


#: The three modules that gate with a *function- or class-level* decorator
#: rather than module ``pytestmark``. They are the gating rule's hard cases,
#: so they are written down as things it must NOT flag — the same technique
#: that pinned the suffix-anchored address matcher by the three real files it
#: must not fire on. A design defended by the false positives it avoids
#: survives; one defended by a comment arguing it should does not.
_DECORATOR_GATED_MODULES = (
    "test_blind_agent_probe.py",   # module pytestmark, but "" default
    "test_bridge_ping_tod.py",     # class-level @pytest.mark.skipif
    "test_stress_smoke.py",        # function-level @pytest.mark.skipif
)


@pytest.mark.parametrize("module_name", _DECORATOR_GATED_MODULES)
def test_decorator_gated_modules_are_not_flagged(module_name: str) -> None:
    """Negative control: the gating rule must stay quiet on these.

    A ``pytestmark``-only check would report all three as ungated. Three
    false positives is not a noisy test, it is a deleted one — and a deleted
    invariant protects nothing, which is the failure mode that matters.
    These are the rule's specification, not a risk it carries.
    """
    path = Path(__file__).resolve().parent / module_name
    assert path.exists(), f"{module_name} moved; this control is now blind"

    tree = ast.parse(path.read_text())
    bindings = _env_host_bindings(tree)
    assert bindings, (
        f"{module_name} no longer binds a device host, so it no longer "
        "exercises the gating rule — pick a different negative control"
    )

    references = _skipif_references(tree)
    ungated = [
        name for name, env_var in bindings
        if name not in references and env_var not in references
    ]
    assert ungated == [], (
        f"the gating rule false-positives on {module_name}: {ungated}. It is "
        "gated by a decorator rather than module pytestmark; a rule that "
        "cannot see that will be deleted by the next person it annoys."
    )


def test_an_empty_default_is_not_a_default() -> None:
    """``os.environ.get("U64_HOST", "")`` is the correct unset idiom.

    ``test_blind_agent_probe.py`` writes it that way and gates on
    ``not U64_HOST``, which is right: the empty string is falsy, so the
    module skips when nobody named a device. A naive "has a default
    argument" rule would flag it, and flagging the correct idiom is how a
    rule teaches people to write the incorrect one.

    Pinned here because the distinction is the whole difference between
    ``""`` and ``"10.43.23.81"``, and nothing else in the suite says so.
    """
    path = Path(__file__).resolve().parent / "test_blind_agent_probe.py"
    source = path.read_text()

    assert 'os.environ.get("U64_HOST", "")' in source, (
        "test_blind_agent_probe.py no longer uses the empty-default idiom; "
        "this control needs a new subject"
    )
    tree = ast.parse(source)
    bindings = _env_host_bindings(tree)
    assert bindings, "the empty default must still count as a host binding"

    references = _skipif_references(tree)
    assert any(
        name in references or env_var in references
        for name, env_var in bindings
    ), "the empty-default module must still read as gated"

    # And the substance: an empty default really does leave the module
    # skipping, which is what makes it correct rather than merely tolerated.
    assert not bool(""), "an empty default must be falsy to work as a gate"


def test_the_binding_detector_scope_is_pinned_by_fixtures() -> None:
    """What counts as an import-time binding, as examples rather than prose.

    A scope rule stated only in a docstring drifts from its implementation
    silently — that is how the ``tree.body`` version made a ``try:``-wrapped
    binding invisible while claiming to cover "import time". Prose cannot
    fail; fixtures can. One case that must be detected and one that must
    not, so the sentence above is answerable by running the file.
    """
    must_detect = {
        "bare module-level": 'import os\n_HOST = os.environ.get("U64_HOST")\n',
        "inside try:": (
            "import os\ntry:\n"
            '    _HOST = os.environ.get("U64_HOST")\n'
            "except Exception:\n    _HOST = None\n"
        ),
        "inside if:": (
            'import os\nif True:\n    _HOST = os.environ.get("U64_HOST")\n'
        ),
        "inside with:": (
            "import os\nwith open('/dev/null') as _f:\n"
            '    _HOST = os.environ.get("U64_HOST")\n'
        ),
        "a C64U-specific gate name": 'import os\n_H = os.environ.get("C64U_HOST")\n',
        "a hostname-shaped gate name": 'import os\n_H = os.environ.get("U64_HOSTNAME")\n',
        # Not device names. They match because the pattern is a deliberate
        # substring, and they are pinned here so the breadth is a visible
        # decision rather than an accident: narrowing the regex to a device
        # enumeration breaks these, and whoever does it has to decide
        # knowingly that C64U_HOST no longer needs guarding.
        "a non-device host name": 'import os\n_H = os.environ.get("PROXY_HOST")\n',
        "a name merely containing HOST": (
            'import os\n_H = os.environ.get("GHOSTSCRIPT_PATH")\n'
        ),
    }
    for label, source in must_detect.items():
        assert _env_host_bindings(ast.parse(source)), (
            f"detector missed an import-time binding ({label}): {source!r}"
        )

    must_not_detect = {
        # Never executes at runtime, so it binds nothing.
        "under if TYPE_CHECKING:": (
            "import os\nfrom typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            '    _HOST = os.environ.get("U64_HOST")\n'
        ),
        # Function scope: a declared limit, not a drift.
        "inside a function": (
            "import os\ndef f():\n"
            '    _HOST = os.environ.get("U64_HOST")\n'
        ),
        # Not a host binding at all — a unit test patching the environment.
        "a patch.dict literal": (
            'import os\nfrom unittest.mock import patch\n'
            '_P = patch.dict(os.environ, {"U64_HOST": "x"})\n'
        ),
    }
    for label, source in must_not_detect.items():
        assert not _env_host_bindings(ast.parse(source)), (
            f"detector false-positived ({label}): {source!r}"
        )


def test_the_gating_scan_finds_the_modules_it_should() -> None:
    """Floor for the scan above, so it cannot pass by finding nothing."""
    modules = _host_binding_modules()
    names = {p.name for p in modules}
    assert len(modules) > 30, (
        f"only {len(modules)} host-binding modules found; the scan is broken"
    )
    for expected in (
        "test_audio_rate_lock_live.py",
        "test_unlocked_notice_live.py",
        "test_u64_turbo_bench_live.py",
    ):
        assert expected in names, f"{expected} not detected as host-binding"


#: Lines that legitimately contain a device address, as
#: (filename, fragment, addresses) triples. Line-granular on purpose: a
#: file-level exemption means one mock string disables the check for
#: everything else in that file, and the next real regression hides behind
#: it. Each fragment names *why* the line is allowed — a lock identity, a
#: mock attribute, a parser input — and *addresses* names which device
#: addresses that reason covers. Without the third element an entry granted
#: for the phantom mock host also excused a real bench address on the same
#: kind of line (review round 1).
_ADDRESS_ALLOWLIST_LINES = {
    # Lock-file identity: the address is an arbitrary opaque key here.
    ("test_device_lock.py", "DeviceLock(", frozenset({_PHANTOM_HOST})),
    ("test_device_lock.py", "_sanitize_device_id(", frozenset({_PHANTOM_HOST})),
    ("test_device_lock.py", 'info["device_host"]', frozenset({_PHANTOM_HOST})),
    # Mock attributes and assertions about them.
    ("test_render_wav_u64.py", "mock_client.host", frozenset({_PHANTOM_HOST})),
    ("test_render_wav_u64.py", "mock_detect.assert_called_once_with(", frozenset({_PHANTOM_HOST})),
    # Parser / protocol inputs and their expected outputs.
    ("test_uci_network.py", "_make_mock_transport(", frozenset({_PHANTOM_HOST})),
    ("test_uci_network.py", "assert result ==", frozenset({_PHANTOM_HOST})),
    ("test_uci_network.py", "uci_tcp_connect(", frozenset({_PHANTOM_HOST})),
    ("test_unified_manager.py", "_make_mock_u64_instance(", frozenset({_PHANTOM_HOST})),
    ("test_unified_manager.py", "_parse_u64_hosts(", frozenset({_PHANTOM_HOST})),
    ("test_unified_manager.py", "patch.dict(", frozenset({_PHANTOM_HOST})),
    # Parametrizes over host strings to prove hygiene is not keyed on the
    # device's address; the addresses are the test data.
    ("test_ultimate64_temp_hygiene.py", "for host in (", frozenset(_BENCH_HOSTS)),
}

#: The single whole-file exemption, and the only place file granularity is
#: the right unit: this module *defines* the rule, so every address in it is
#: either the rule's own input data or prose explaining the rule. Exempting
#: a rule from itself is not the same defect as exempting a file that merely
#: happens to contain one mock — and
#: :func:`test_only_this_file_is_exempt_wholesale` stops a second appearing.
_ADDRESS_ALLOWLIST_FILES = {Path(__file__).name}


def _addresses_named(text: str) -> frozenset[str]:
    """Which device addresses *text* names, whole or by a distinctive tail."""
    named = set()
    for address in _DEVICE_ADDRESSES:
        octets = address.split(".")
        tails = {".".join(octets[i:]) for i in range(len(octets) - 1)}
        if any(tail in text for tail in tails):
            named.add(address)
    return frozenset(named)


def _allowed_address_line(filename: str, line: str) -> bool:
    """Allowed only if a granted entry's fragment is on the line **and** every
    address the line names is one that entry was granted for."""
    named = _addresses_named(line)
    return any(
        filename == allowed_file and fragment in line and named <= addresses
        for allowed_file, fragment, addresses in _ADDRESS_ALLOWLIST_LINES
    )


#: Shapes that *teach someone to type an address*, as opposed to merely
#: naming one. Rule 2's real criterion is invocation, not mention.
_INVOCATION_SHAPES = (
    "U64_HOST=",
    "U64_NOTICE_HOST=",
    "python3 scripts/",
    "python3 examples/",
    "curl http",
    "host=",
)


def test_only_this_file_is_exempt_wholesale() -> None:
    """A whole-file exemption is a hole; there must be exactly one, here."""
    assert _ADDRESS_ALLOWLIST_FILES == {Path(__file__).name}, (
        "a second file has been exempted from the address rule wholesale. "
        "Use a line-granular entry in _ADDRESS_ALLOWLIST_LINES instead — a "
        "file-level exemption hides every future regression in that file."
    )


def _is_fixture_line(line: str) -> bool:
    """Whether *line* carries the fixture marker as a trailing comment.

    ``endswith``, not ``in``: prose *about* the marker (this docstring, the
    count test's message) contains the string without being a marker. An
    earlier version skipped on ``in`` while counting on something else, so
    the two rules disagreed about what a marker was — the same
    prose-and-code drift this file keeps finding elsewhere.
    """
    return line.rstrip().endswith("# rule-fixture")


#: How many ``# rule-fixture`` markers this file is expected to carry.
#: Every one is a line where an address genuinely is scanner input.
_EXPECTED_RULE_FIXTURES = 4


def test_the_fixture_marker_is_not_spreading() -> None:
    """The marker excuses a whole line, so its use must stay visible.

    Stated plainly rather than defended: ``# rule-fixture`` does excuse an
    invocation on the line it marks. A comment reading
    ``# U64_HOST=<addr> python3 scripts/probe_u64.py  # rule-fixture``
    passes. The earlier claim in this file — that an explicit marker beats a
    heuristic because a loose heuristic "would also excuse a real
    invocation" — was only half true: the marker excuses one too, it just
    requires someone to type it.

    That is a deliberate-act threat model, and the rule's threat model is
    accidental reintroduction, so the marker is acceptable — but not
    unbounded. Pinning the count makes adding one a visible act that fails
    this test until somebody updates the number and says why, which turns a
    silent hole into a reviewed one.
    """
    marked = [
        (i, line.strip())
        for i, line in enumerate(Path(__file__).read_text().splitlines(), 1)
        if _is_fixture_line(line)
    ]
    assert len(marked) == _EXPECTED_RULE_FIXTURES, (
        f"expected {_EXPECTED_RULE_FIXTURES} fixture markers, found "
        f"{len(marked)}. Each one excuses its whole line from the invocation "
        f"rule, so adding one is a decision, not a formatting fix:\n  "
        + "\n  ".join(f"line {i}: {t[:90]}" for i, t in marked)
    )
    # Every marker must sit on a line that really is scanner input: an
    # address inside a quoted fragment, not a bare instruction.
    for i, text in marked:
        assert '"' in text or "'" in text, (
            f"line {i} carries the fixture marker but quotes nothing, so it "
            f"is an instruction rather than data: {text[:90]}"
        )


def test_this_file_names_addresses_as_data_and_never_as_an_invocation() -> None:
    """The exemption is from *mention*, not from *teaching*.

    This file is exempt wholesale because its ~37 address lines are fixtures
    and specification — the rule's own input data — and marking them
    individually would churn on every fixture edit, which is how a rule
    becomes annoying and then absent. But "the file is special" is too wide
    an exemption to leave unqualified. Rule 2's actual criterion is
    *invocation*: a line that teaches someone to type an address.

    So the exemption is narrowed here. This file may name an address as
    data; it may not carry one in a shape a reader would copy — no
    ``U64_HOST=<address>``, no ``python3 scripts/… <address>``, no
    ``curl http://<address>/``, no constructor kwarg carrying one. An
    address inside a quoted *source fragment* under test is still data:
    those are inputs to the scanner, not instructions to a reader.
    """
    offenders = []
    for lineno, line in enumerate(Path(__file__).read_text().splitlines(), 1):
        if not _names_a_device(line):
            continue
        stripped = line.strip()
        # Fixture sources are quoted Python passed to the scanners; they are
        # data even when they contain an assignment that looks like code.
        # Marked explicitly rather than inferred: a heuristic loose enough to
        # recognise them would also excuse a real invocation, and the whole
        # point of this test is that the exemption stays narrow.
        # Comments and docstrings are NOT skipped, and there is no
        # startswith-a-quote escape either. Both were holes, both found by
        # mutation, both two lines below prose asserting they could not
        # exist: a comment form (`# U64_HOST=<addr> python3 scripts/...`)
        # and then a single-line docstring form of the identical invocation,
        # which opened with `"` and sailed through the quote skip. A
        # docstring is the teaching surface — `verify_tod_warp.py` taught
        # the phantom from exactly that — so nothing is excused by its
        # punctuation. The only exemption is the explicit marker below.
        if _is_fixture_line(line):
            continue
        for shape in _INVOCATION_SHAPES:
            if shape in line:
                offenders.append(f"line {lineno}: {stripped[:90]}")
                break

    assert offenders == [], (
        "this file's wholesale exemption covers naming addresses as data, "
        "not teaching an invocation. These lines teach one:\n  "
        + "\n  ".join(offenders)
    )


def test_an_allowlisted_file_is_not_exempt_as_a_whole() -> None:
    """A new address in an allowlisted file must still fail.

    This is the granularity itself, pinned. Reverting to a file-level
    allowlist is a silent weakening: the suite goes green, and a real
    regression planted in ``test_device_lock.py`` is simply not reported —
    verified by mutation. Nothing above catches that, because the weakening
    lives in this file's logic while the regression lives in another, so it
    needs asserting directly.
    """
    # A line in an allowlisted file matching none of its fragments.
    regression = 'HOST = "10.53.21.158"'
    assert _names_a_device(regression)
    assert not _allowed_address_line("test_device_lock.py", regression), (
        "test_device_lock.py is exempt wholesale — one mock string would "
        "then hide every future address regression in that file"
    )

    # ...while the lines the allowlist exists for stay allowed.
    legitimate = '        lock = DeviceLock("192.168.1.81", lock_dir=lock_dir)'
    assert _allowed_address_line("test_device_lock.py", legitimate)

    # ...and an entry is scoped to its address, not just its fragment:
    # "uci_tcp_connect(" was granted for the phantom mock host, so a bench
    # address on such a line is a new regression (review round 1).
    bench_on_phantom_entry = '    uci_tcp_connect(None, "10.43.23.81", 80)'
    assert _names_a_device(bench_on_phantom_entry)
    assert not _allowed_address_line("test_uci_network.py", bench_on_phantom_entry), (
        "an allowlist entry granted for one address excused a different one"
    )
    assert _allowed_address_line(
        "test_uci_network.py", '    uci_tcp_connect(None, "192.168.1.81", 80)'
    )

    # ...and a fragment does not leak across files.
    assert not _allowed_address_line("test_u64_syslog.py", legitimate), (
        "an allowlist fragment matched in a file it was not granted for"
    )


def test_every_allowlisted_line_is_still_needed() -> None:
    """No dead allowlist entries.

    An entry that no longer matches anything is debt: it silently widens
    the rule for a line someone may add later. This fails when a fragment
    stops matching, so entries are removed when their reason goes away.
    """
    unused = []
    for allowed_file, fragment, addresses in sorted(
        _ADDRESS_ALLOWLIST_LINES, key=lambda e: (e[0], e[1])
    ):
        path = Path(__file__).resolve().parent / allowed_file
        if not path.exists():
            unused.append(f"{allowed_file} (file is gone)")
            continue
        # Each granted address must still be used on such a line, so an
        # entry cannot keep excusing an address its reason no longer needs.
        lines = [
            line for line in path.read_text().splitlines() if fragment in line
        ]
        for address in sorted(addresses):
            if not any(address in _addresses_named(line) for line in lines):
                unused.append(f"{allowed_file}: {fragment!r} for {address}")
    assert unused == [], f"dead allowlist entries, remove them: {unused}"


@pytest.mark.parametrize(
    "module_path", _test_module_paths(), ids=lambda p: p.name
)
def test_no_test_module_teaches_a_device_address(module_path: Path) -> None:
    """Live-test docstrings must show ``U64_HOST=<device>``, not an address.

    These usage lines are how a person learns a value and then types it —
    which is where the phantom host came from in the first place. Roughly
    twenty of them taught it.
    """
    hits = [
        (i, line.strip())
        for i, line in enumerate(module_path.read_text().splitlines(), 1)
        if _names_a_device(line)
        and not _allowed_address_line(module_path.name, line)
    ]
    assert hits == [], (
        f"{module_path.name} teaches a device address: {hits}"
    )


# ---------------------------------------------------------------------------
# 4. #275's pin, in tests/ — where #275 actually happened
# ---------------------------------------------------------------------------
#
# The rule above that folds constructed addresses (rule 1) walked only
# scripts/ and examples/. In tests/ the only address rule was the line-level
# one, which sees a pasted literal but not a ``".".join`` of octets, and no
# rule at all saw ``os.environ.get("U64_HOST", "<any other address>")`` in a
# module that *is* gated on ``_HOST`` — ``skipif(not _HOST)`` never skips when
# the default is truthy. That is #275 exactly, with a different number.


def _runtime_device_values(path: Path) -> list[tuple[int, str]]:
    """Rule 1 applied to one test module, honouring the line allowlist."""
    source = path.read_text()
    lines = source.splitlines()
    return [
        (lineno, value)
        for lineno, value in _runtime_string_literals(source)
        if _names_a_device(value)
        and not _allowed_address_line(path.name, lines[lineno - 1])
    ]


@pytest.mark.parametrize(
    "module_path", _test_module_paths(), ids=lambda p: p.name
)
def test_no_test_module_builds_a_device_address(module_path: Path) -> None:
    """No test module names a device as a runtime value, however assembled."""
    offenders = _runtime_device_values(module_path)
    assert offenders == [], (
        f"{module_path.name} carries a device address as a runtime value "
        f"(literal or built from literals): {offenders}"
    )


def _is_host_env_name(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.isupper()
        and bool(_HOST_ENV_VAR.search(node.value))
    )


def _host_env_access(node: ast.AST) -> str | None:
    """``"read"`` or ``"setdefault"`` if *node* touches a ``*HOST*`` env var."""
    if isinstance(node, ast.Call) and node.args and _is_host_env_name(node.args[0]):
        func = node.func
        on_environ = isinstance(func, ast.Attribute) and (
            (isinstance(func.value, ast.Attribute) and func.value.attr == "environ")
            or (isinstance(func.value, ast.Name) and func.value.id == "environ")
        )
        if on_environ and func.attr == "get":
            return "read"
        if on_environ and func.attr == "setdefault":
            return "setdefault"
        if (isinstance(func, ast.Attribute) and func.attr == "getenv") or (
            isinstance(func, ast.Name) and func.id == "getenv"
        ):
            return "read"
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "environ"
        and _is_host_env_name(node.slice)
    ):
        return "read"
    return None


def _is_unset_marker(node: ast.AST) -> bool:
    """``""`` and ``None`` are the two idioms that leave a gate closed."""
    return isinstance(node, ast.Constant) and node.value in ("", None)


def _import_time_nodes(tree: ast.Module) -> list[ast.AST]:
    """Every node that executes at import.

    Class bodies are included: they run when the module is imported, so a
    ``HOST = os.environ.get("U64_HOST", ...)`` class attribute arms a module
    exactly like a module-level one (review round 1). Function and lambda
    bodies, including methods, are not import-time and are skipped.
    """
    found: list[ast.AST] = []

    def descend(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
            ):
                continue
            if isinstance(child, ast.If) and _is_type_checking_guard(child):
                continue
            found.append(child)
            descend(child)

    descend(tree)
    return found


def _host_default_offenders(source: str) -> list[str]:
    """Import-time ``*HOST*`` env reads that supply a device when none is named.

    Structural, not address-matching: a default is wrong whatever it says,
    so this catches an address no list knows about. Flags a non-empty
    default argument (positional or ``default=``), an ``or <fallback>`` after
    the read, any ``setdefault`` (which also exports the gate), and an
    assignment into ``os.environ[...HOST...]``, and a conditional expression
    that touches a ``*HOST*`` variable while one of its branches supplies a
    non-empty value (``env["U64_HOST"] if env.get("U64_HOST") else "..."``).
    Class bodies are in scope (they run at import); function, method and
    lambda bodies are not.

    **Declared limits**, each asserted by the planted-regressions test so it
    stays known rather than becoming a surprise:

    * the variable name must be a string constant at the call.
      ``_VAR = "U64_HOST"; os.environ.get(_VAR, "...")`` is not resolved and
      is not flagged;
    * an **immediately invoked lambda** is not flagged:
      ``_HOST = (lambda: os.environ.get("U64_HOST", "..."))()`` runs at
      import, but lambda bodies are skipped as not-import-time (#315).
    """
    offenders: list[str] = []
    for node in _import_time_nodes(ast.parse(source)):
        kind = _host_env_access(node)
        if kind == "setdefault":
            offenders.append(f"line {node.lineno}: setdefault invents and exports a host")
        elif kind == "read" and isinstance(node, ast.Call):
            defaults = list(node.args[1:]) + [
                k.value for k in node.keywords if k.arg == "default"
            ]
            if any(not _is_unset_marker(d) for d in defaults):
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for i, value in enumerate(node.values[:-1]):
                if _host_env_access(value) == "read" and any(
                    not _is_unset_marker(v) for v in node.values[i + 1:]
                ):
                    offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
                    break
        if isinstance(node, ast.IfExp) and any(
            _host_env_access(sub) for sub in ast.walk(node)
        ):
            for branch in (node.body, node.orelse):
                if _host_env_access(branch) != "read" and not _is_unset_marker(branch):
                    offenders.append(f"line {node.lineno}: {ast.unparse(node)}")
                    break
        if (
            isinstance(node, ast.Assign)
            and any(_host_env_access(t) for t in node.targets)
            and not _is_unset_marker(node.value)
        ):
            offenders.append(f"line {node.lineno}: exports a host at import")
    return offenders


def _default_rule_paths() -> list[Path]:
    return sorted(
        {*_script_paths(), *_test_module_paths(include_allowlisted=True)}
    )


@pytest.mark.parametrize(
    "module_path", _default_rule_paths(), ids=lambda p: p.name
)
def test_no_import_time_host_read_has_a_default(module_path: Path) -> None:
    """The host is named by the caller or the module stays closed (#275)."""
    offenders = _host_default_offenders(module_path.read_text())
    assert offenders == [], (
        f"{module_path.name} supplies a device host when none is named, so "
        f"its host gate can never close: {offenders}"
    )


def test_the_tests_scans_flag_planted_regressions(tmp_path: Path) -> None:
    """Positive controls for section 4, through the real entry points.

    Each planted module is a regression the earlier scans passed. They sit
    in a *subdirectory* so the same run proves the walk is recursive. The
    default cases use TEST-NET-3 (203.0.113.0/24) — on no list — because the
    default rule must not depend on knowing the address.
    """
    sub = tmp_path / "tests" / "nested"
    sub.mkdir(parents=True)

    joined = sub / "test_planted_join_live.py"
    joined.write_text(
        'import os, pytest\n'
        '_HOST = os.environ.get("U64_HOST") or ".".join(["10", "43", "23", "81"])\n'
        'pytestmark = pytest.mark.skipif(not _HOST, reason="U64_HOST not set")\n'
    )
    assert _runtime_device_values(joined), "rule 1 missed a joined address in tests/"
    assert _host_default_offenders(joined.read_text()), "or-fallback default missed"

    unknown = sub / "test_planted_default_live.py"
    unknown.write_text(
        'import os, pytest\n'
        '_HOST = os.environ.get("U64_HOST", "203.0.113.9")\n'
        'pytestmark = pytest.mark.skipif(not _HOST, reason="U64_HOST not set")\n'
    )
    # Every pre-existing rule passes this module — that is the hole.
    assert not _runtime_device_values(unknown)
    assert not _names_a_device(unknown.read_text())
    unknown_tree = ast.parse(unknown.read_text())
    assert {n for n, _ in _env_host_bindings(unknown_tree)} <= _skipif_references(
        unknown_tree
    )
    assert _host_default_offenders(unknown.read_text()), (
        "a gated module with a non-device default passed the default rule"
    )

    must_flag = {
        "getenv default=": 'import os\nH = os.getenv("C64U_HOST", default="203.0.113.9")\n',
        "setdefault": 'import os\nH = os.environ.setdefault("U64_HOST", "203.0.113.9")\n',
        "environ export": 'import os\nos.environ["U64_HOST"] = "203.0.113.9"\n',
        "inside try:": (
            'import os\ntry:\n    H = os.environ.get("U64_HOST", "x")\n'
            "except Exception:\n    pass\n"
        ),
        # A class body executes at import (review round 1).
        "class body": (
            'import os\nclass TestX:\n'
            '    HOST = os.environ.get("U64_HOST", "203.0.113.9")\n'
        ),
        # A conditional expression is a default in other clothes.
        "IfExp fallback": (
            'import os\n_HOST = os.environ["U64_HOST"] '
            'if os.environ.get("U64_HOST") else "203.0.113.9"\n'
        ),
    }
    for label, source in must_flag.items():
        assert _host_default_offenders(source), f"default rule missed ({label})"

    # Declared limit, asserted so it stays known: an env name held in a
    # variable is not a constant the rule can read. If this starts being
    # caught, update _host_default_offenders' docstring.
    held_in_variable = 'import os\n_VAR = "U64_HOST"\n_H = os.environ.get(_VAR, "203.0.113.9")\n'
    assert not _host_default_offenders(held_in_variable), (
        "the default rule now resolves a variable-held env name; its "
        "docstring's declared limits say it does not"
    )
    invoked_lambda = (
        'import os\n_HOST = (lambda: os.environ.get("U64_HOST", "203.0.113.9"))()\n'
    )
    assert not _host_default_offenders(invoked_lambda), (
        "the default rule now catches an immediately invoked lambda; update "
        "its docstring's declared limits"
    )

    must_pass = {
        "no default": 'import os\nH = os.environ.get("U64_HOST")\n',
        "empty default": 'import os\nH = os.environ.get("U64_HOST", "")\n',
        "or None": 'import os\nH = os.environ.get("U64_HOST") or None\n',
        "function scope": 'import os\ndef f():\n    return os.environ.get("U64_HOST", "x")\n',
        "method inside a class": (
            'import os\nclass T:\n    def f(self):\n'
            '        return os.environ.get("U64_HOST", "x")\n'
        ),
        "IfExp with only unset branches": (
            'import os\n_H = os.environ["U64_HOST"] if os.environ.get("U64_HOST") else None\n'
        ),
        "not a host var": 'import os\nP = os.environ.get("U64_PASSWORD", "secret")\n',
    }
    for label, source in must_pass.items():
        assert not _host_default_offenders(source), (
            f"default rule false-positived ({label})"
        )

    found = {p.name for p in _py_files_under(tmp_path / "tests")}
    assert {joined.name, unknown.name} <= found, "the walk is not recursive"


def test_the_tests_scans_are_not_vacuous() -> None:
    """Floors, since an empty parametrize is a skip rather than a failure."""
    import inspect

    assert "_py_files_under(" in inspect.getsource(_test_module_paths), (
        "the real tests/ walk no longer uses the recursive helper"
    )
    modules = _test_module_paths()
    names = {p.name for p in modules}
    assert len(modules) > 100, f"only {len(modules)} test modules scanned"
    for expected in (
        "test_audio_rate_lock_live.py",
        "test_unlocked_notice_live.py",
        "test_sid_addressing_isolation_live.py",
        "test_device_lock.py",
    ):
        assert expected in names, f"{expected} not scanned"

    default_paths = _default_rule_paths()
    roots = {p.relative_to(_SCRIPTS.parent).parts[0] for p in default_paths}
    assert roots >= {"scripts", "examples", "tests"}, roots
    reading = [
        p for p in default_paths
        if any(
            _host_env_access(n) for n in _import_time_nodes(ast.parse(p.read_text()))
        )
    ]
    assert len(reading) > 30, (
        f"only {len(reading)} modules read a host at import; the default rule "
        "is scanning nothing it could fail on"
    )



# ---------------------------------------------------------------------------
# 5. Scripts hold the DeviceLock while they drive a device (issue #244)
# ---------------------------------------------------------------------------
#
# Live *tests* are locked by construction (``tests/conftest.py``
# ``device_lock_guard``). Scripts were not: eight device-driving scripts took
# no lock, so a ``run_prg``/``sid_play``/``reset`` from one of them replaced a
# neighbouring lane's program and presented to that lane as device
# degradation (#194). Three shapes are accepted, and each is pinned:
#
# 1. **``with hold_device_lock(host):``** from ``scripts/_u64_host.py`` around
#    every construct that reaches the device — the one helper, so the budget
#    always goes through ``resolve_lock_timeout``;
# 2. **the manager path** — ``with create_manager(...)`` / ``UnifiedManager``,
#    which lock for you (``allow_nested=True`` in ``unified_manager``);
# 3. **the pytest runners**, which reach the device only through ``*_live.py``
#    modules that conftest locks per test. They deliberately do *not* hold the
#    lock across ``pytest.main``: the client's ``/Temp`` drain runs on the
#    *outermost* release, so an outer hold would defer every per-test drain
#    on the C64U to the end of the run.
#
# What reaches a device, for this scan: constructing a harness client,
# transport or device manager, or issuing HTTP. Anything reached *through*
# one of those (``play_sid(transport)``, ``set_reu(client)``) is covered by
# the construction being covered.

#: Callee names that reach a device. Resolved through import aliases.
_DEVICE_CALLEES = frozenset({
    "Ultimate64Client",
    "Ultimate64Transport",
    "Ultimate64InstanceManager",
    "SocketDMAClient",
    "urlopen",
    "HTTPConnection",
    "HTTPSConnection",
})

#: ``with`` context expressions that hold the device for their body.
_LOCKING_WITH_CALLEES = frozenset({"hold_device_lock", "create_manager"})

_ACQUIRE_METHODS = frozenset({"acquire", "acquire_or_raise"})


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> original name, for every ``import ... as`` in *tree*."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                original = alias.name.rsplit(".", 1)[-1]
                aliases[alias.asname or original] = original
    return aliases


def _callee_name(call: ast.Call, aliases: dict[str, str]) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return aliases.get(func.id, func.id)
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _device_lock_offenders(source: str) -> list[str]:
    """Every device-reaching construct in *source* not held under the lock.

    A node is **covered** when it is lexically inside the body of a ``with``
    whose context expression calls one of :data:`_LOCKING_WITH_CALLEES`, or
    inside a named function whose every direct call site is covered
    (computed to a fixpoint, so ``_get`` -> ``_get_json`` -> ``main`` chains
    resolve). A function with no direct call site is uncovered: a callback
    or a dead helper cannot be proven to run under the lock.

    Also flags a literal acquire budget (the lock must honour
    ``U64_DEVICE_LOCK_TIMEOUT``) and a ``hold_device_lock`` that is used but
    never imported — the ``NameError`` class that only execution sees.

    A device object bound inside a locking ``with`` (``c = Ultimate64Client(...)``
    or ``with Ultimate64Transport(...) as t``) and then referenced after the
    block, in the same scope, is flagged too: the construction was locked but
    the ``c.reset()`` in a trailing ``finally`` is not.

    Known limits: functions are matched by bare name, so two same-named
    functions share one verdict; a call through an attribute
    (``self._get()``) is not a call site. Both err towards flagging, because
    an uncounted call site leaves the function uncovered. Three evasive
    shapes are **missed**, by design, since the threat model is a forgotten
    lock and not an author hiding one: ``functools.partial(Ultimate64Client,
    h)`` called outside the lock (the constructor is an argument, not a
    callee); ``getattr(module, "Ultimate64Client")(h)`` (the callee is not a
    name); and a local ``def hold_device_lock`` that does not lock, which
    also satisfies the missing-import rule. Escape tracking follows plain
    names only: an object stored on an attribute or in a container, or
    returned out of the block, is not followed.
    """
    tree = ast.parse(source)
    aliases = _import_aliases(tree)
    parents = _parents(tree)
    offenders: list[str] = []

    def locking_with(node: ast.AST) -> bool:
        return isinstance(node, (ast.With, ast.AsyncWith)) and any(
            isinstance(item.context_expr, ast.Call)
            and _callee_name(item.context_expr, aliases) in _LOCKING_WITH_CALLEES
            for item in node.items
        )

    call_sites: dict[str, list[ast.Call]] = {
        node.name: []
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in call_sites
        ):
            call_sites[node.func.id].append(node)

    covered_functions: set[str] = set()

    def covered(node: ast.AST) -> bool:
        child, parent = node, parents.get(node)
        while parent is not None:
            # Only the *body* of a with is held; its context expressions
            # run before the lock is taken.
            if locking_with(parent) and child in parent.body:
                return True
            if (
                isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef))
                and parent.name in covered_functions
            ):
                return True
            child, parent = parent, parents.get(parent)
        return False

    changed = True
    while changed:
        changed = False
        for name, sites in call_sites.items():
            if name in covered_functions or not sites:
                continue
            if all(covered(site) for site in sites):
                covered_functions.add(name)
                changed = True

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _callee_name(node, aliases)
        if name in _DEVICE_CALLEES and not covered(node):
            offenders.append(
                f"line {node.lineno}: {name}(...) reaches the device outside "
                f"hold_device_lock/create_manager"
            )
        budgets = [kw.value for kw in node.keywords if kw.arg == "lock_timeout"]
        if name in _ACQUIRE_METHODS and isinstance(node.func, ast.Attribute):
            budgets += [kw.value for kw in node.keywords if kw.arg == "timeout"]
            budgets += node.args[:1]
        for value in budgets:
            inner = value.operand if isinstance(value, ast.UnaryOp) else value
            if isinstance(inner, ast.Constant) and isinstance(inner.value, (int, float)):
                offenders.append(
                    f"line {node.lineno}: literal lock budget {inner.value!r} "
                    f"ignores U64_DEVICE_LOCK_TIMEOUT; use resolve_lock_timeout"
                )

    offenders += _escaped_device_objects(tree, aliases, parents, locking_with)

    uses_helper = any(
        isinstance(n, ast.Name) and n.id == "hold_device_lock" for n in ast.walk(tree)
    )
    imports_helper = any(
        isinstance(n, ast.ImportFrom)
        and n.module == "_u64_host"
        and any(a.name == "hold_device_lock" and a.asname is None for a in n.names)
        for n in ast.walk(tree)
    )
    if uses_helper and not (imports_helper or "hold_device_lock" in call_sites):
        offenders.append("hold_device_lock is used but never imported from _u64_host")
    return offenders


def _escaped_device_objects(tree, aliases, parents, locking_with) -> list[str]:
    """Names bound to a device object inside a locking ``with``, used after it.

    Binding shapes: ``name = <device call>`` and ``with <device call> as name``
    anywhere in the block's body. A later reference is any ``Load`` of that
    name in the same scope (the nearest enclosing function, else the module)
    that starts after the block ends and is not **hidden by a rebinding**
    (#338). A rebinding statement S hides the object only from a ``Load``
    that is either nested inside S and evaluated after the binding takes
    effect (``for c in range(3): print(c)``, while ``c = c.reset()`` still
    reads the object), or nested inside a later sibling of S in the
    statement list that owns S (``c = None`` then ``print(c)``). So a
    rebinding in one branch (``if x: c = None``) does not hide a
    ``c.reset()`` after the ``if``, and that read is flagged. This is a
    dominance approximation, not flow analysis (review round 1 of #361).

    Stated limits (asserted in the planted-regressions test):

    * **conservative:** a rebinding in *every* branch (``if x: c = None`` /
      ``else: c = None``) does not hide a read after the ``if``, which is
      flagged although no path reaches it holding the device object;
    * a tuple target ``c, n = Ultimate64Client(h), 1`` is not a binding;
    * an attribute target (``self.c = ...`` then ``self.c.reset()``);
    * a walrus ``(c := Ultimate64Client(h))``;
    * a read at the top of the next loop iteration, on a line above the
      ``with``.
    """
    offenders: list[str] = []

    def is_device_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and _callee_name(node, aliases) in _DEVICE_CALLEES

    for block in ast.walk(tree):
        if not locking_with(block):
            continue
        bound: set[str] = set()
        for stmt in block.body:
            for node in ast.walk(stmt):
                if isinstance(node, ast.Assign) and is_device_call(node.value):
                    bound |= {t.id for t in node.targets if isinstance(t, ast.Name)}
                elif isinstance(node, (ast.With, ast.AsyncWith)):
                    for item in node.items:
                        if is_device_call(item.context_expr) and isinstance(
                            item.optional_vars, ast.Name
                        ):
                            bound.add(item.optional_vars.id)
        if not bound:
            continue
        scope = parents.get(block)
        while scope is not None and not isinstance(
            scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)
        ):
            scope = parents.get(scope)
        end = block.end_lineno or block.lineno
        region = scope if scope is not None else tree
        hiders: dict[str, list[tuple[ast.stmt, tuple[int, int]]]] = {}
        for node in ast.walk(region):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Store)
                and node.id in bound
                and node.lineno > end
            ):
                position = _rebinding_position(node, parents)
                stmt = _enclosing_statement(node, parents)
                if position is not None and stmt is not None:
                    hiders.setdefault(node.id, []).append((stmt, position))
        for node in ast.walk(region):
            if (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in bound
                and node.lineno > end
                and not any(
                    _rebinding_hides(stmt, position, node, parents)
                    for stmt, position in hiders.get(node.id, ())
                )
            ):
                offenders.append(
                    f"line {node.lineno}: {node.id} was built under the lock at "
                    f"line {block.lineno} but is used after the lock is released"
                )
    return offenders


def _enclosing_statement(node: ast.AST, parents) -> ast.stmt | None:
    while node is not None and not isinstance(node, ast.stmt):
        node = parents.get(node)
    return node


def _is_within(node: ast.AST, ancestor: ast.AST, parents) -> bool:
    while node is not None:
        if node is ancestor:
            return True
        node = parents.get(node)
    return False


def _rebinding_hides(stmt: ast.stmt, position, load: ast.Name, parents) -> bool:
    """Whether the rebinding statement *stmt* hides the device object from *load*.

    Nested inside *stmt* and evaluated after the binding takes effect, or
    nested inside a later sibling of *stmt* in the statement list that owns it.
    """
    if _is_within(load, stmt, parents):
        return (load.lineno, load.col_offset) >= position
    owner = parents.get(stmt)
    for field in ("body", "orelse", "finalbody"):
        siblings = getattr(owner, field, None)
        if not isinstance(siblings, list):
            continue
        for index, sibling in enumerate(siblings):
            if sibling is stmt:
                later = siblings[index + 1:]
                return any(_is_within(load, s, parents) for s in later)
    return False


def _rebinding_position(name: ast.Name, parents) -> tuple[int, int] | None:
    """Where a ``Store`` of *name* takes effect, or ``None`` if it does not rebind.

    A binding takes effect once its value is evaluated: after the right-hand
    side of an assignment, the iterable of a ``for``, the context expression
    of a ``with``, the value of a walrus. An augmented assignment reads the
    old object first, so it is a use and not a rebinding.
    """
    node: ast.AST = name
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, ast.AugAssign):
            return None
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is not None:
            return (parent.value.end_lineno, parent.value.end_col_offset)
        if isinstance(parent, (ast.For, ast.AsyncFor)):
            return (parent.iter.end_lineno, parent.iter.end_col_offset)
        if isinstance(parent, ast.withitem):
            expr = parent.context_expr
            return (expr.end_lineno, expr.end_col_offset)
        if isinstance(parent, ast.NamedExpr):
            return (parent.value.end_lineno, parent.value.end_col_offset)
        if isinstance(parent, ast.stmt):
            break
        node, parent = parent, parents.get(parent)
    return (name.end_lineno, name.end_col_offset)


def _device_reaching_calls(source: str) -> int:
    tree = ast.parse(source)
    aliases = _import_aliases(tree)
    return sum(
        1 for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _callee_name(n, aliases) in _DEVICE_CALLEES
    )


def _scripts_only() -> list[Path]:
    return sorted(p for p in _script_paths() if _SCRIPTS in p.parents)


@pytest.mark.parametrize("script_path", _scripts_only(), ids=lambda p: p.name)
def test_every_script_holds_the_device_lock(script_path: Path) -> None:
    """No script reaches a device without holding its ``DeviceLock`` (#244)."""
    offenders = _device_lock_offenders(script_path.read_text())
    assert not offenders, (
        f"{script_path.name} drives a device without the DeviceLock. Wrap "
        f"the device work in `with hold_device_lock(host):` from "
        f"scripts/_u64_host.py (or go through create_manager):\n  "
        + "\n  ".join(offenders)
    )


#: Scripts that must be found reaching a device. If the scan stops seeing
#: them it is scanning nothing it could fail on.
_KNOWN_DEVICE_SCRIPTS = frozenset({
    "bench_x25519_u64_turbo.py",
    "play_chromatic_u64.py",
    "play_scale_u64.py",
    "probe_u64.py",
    "probe_uci_network.py",
    "verify_tod_warp.py",
})


def test_the_lock_scan_sees_the_device_scripts() -> None:
    """Vacuity guard: the scan finds real device work, in the scripts known to do it."""
    counts = {
        p.name: _device_reaching_calls(p.read_text()) for p in _scripts_only()
    }
    reaching = {name for name, n in counts.items() if n}
    assert reaching == _KNOWN_DEVICE_SCRIPTS, (
        f"device-reaching scripts changed: found {sorted(reaching)}. A new one "
        f"must be added to _KNOWN_DEVICE_SCRIPTS and to the runtime probe (or "
        f"its exclusion list)."
    )
    # Measured 8 at this commit (bench 2, probe_uci 2, one each elsewhere).
    assert sum(counts.values()) >= 8, counts
    assert "rrnet_first_exchange_probe.py" in counts, "scripts/ not scanned"


def test_the_lock_scan_flags_planted_regressions() -> None:
    """Positive control: each shape the scan claims to reject, it rejects."""
    helper = "from _u64_host import hold_device_lock\n"
    client = "from c64_test_harness.backends.ultimate64_client import Ultimate64Client\n"
    must_flag = {
        "no lock at all": client + "c = Ultimate64Client(host='h')\n",
        "client built before the lock": (
            helper + client
            + "c = Ultimate64Client(host='h')\n"
            + "with hold_device_lock('h'):\n    c.reset()\n"
        ),
        "client built in the with's own expression": (
            helper + client
            + "with hold_device_lock('h'), Ultimate64Client(host='h') as c:\n    pass\n"
        ),
        "aliased import": (
            "from c64_test_harness.backends.ultimate64 import "
            "Ultimate64Transport as T\nt = T(host='h')\n"
        ),
        "http helper called once outside the lock": (
            helper + "import urllib.request\n"
            + "def _get(u):\n    return urllib.request.urlopen(u)\n"
            + "def main():\n"
            + "    with hold_device_lock('h'):\n        _get('a')\n"
            + "    _get('b')\n"
        ),
        "http helper never called directly": (
            "import urllib.request\n"
            "def _get(u):\n    return urllib.request.urlopen(u)\n"
            "CALLBACK = _get\n"
        ),
        "literal acquire timeout": (
            "from c64_test_harness.backends.device_lock import DeviceLock\n"
            "lock = DeviceLock('h')\nlock.acquire(timeout=60.0)\n"
        ),
        "literal positional acquire budget": (
            "from c64_test_harness.backends.device_lock import DeviceLock\n"
            "DeviceLock('h').acquire_or_raise(120)\n"
        ),
        "literal manager lock_timeout": (
            "from c64_test_harness import create_manager\n"
            "with create_manager(backend='u64', lock_timeout=600.0) as m:\n    pass\n"
        ),
        "helper used without its import": (
            client + "with hold_device_lock('h'):\n    Ultimate64Client(host='h')\n"
        ),
        "device object used after the lock is released": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "        c.run_prg(b'')\n"
            + "    c.reset()\n"
        ),
        "rebinding reads the old object (`c = c.reset()`)": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    c = c.reset()\n"
        ),
        "augmented assignment does not stop tracking": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    c += 1\n"
            + "    c.reset()\n"
        ),
        "use after the lock, rebinding only later": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    c.reset()\n"
            + "    c = None\n"
        ),
        # Review round 1 of #361: a rebinding in one branch does not hide a
        # read after the branch.
        "rebinding in one branch does not hide a later read": (
            helper + client
            + "def main(h, x):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    if x:\n"
            + "        c = None\n"
            + "    c.reset()\n"
        ),
        "a store before the block does not hide a read after it": (
            helper + client
            + "def main(h):\n"
            + "    c = None\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    c.reset()\n"
        ),
        "rebinding in a loop body does not hide a read after the loop": (
            helper + client
            + "def main(h, items):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    while items:\n"
            + "        c = None\n"
            + "    c.reset()\n"
        ),
        "device object from `with ... as` used after the lock": (
            helper
            + "from c64_test_harness.backends.ultimate64 import Ultimate64Transport\n"
            + "with hold_device_lock('h'):\n"
            + "    with Ultimate64Transport(host='h') as t:\n"
            + "        pass\n"
            + "try:\n    pass\nfinally:\n    t.close()\n"
        ),
        "a with that is not a lock": (
            client + "from contextlib import nullcontext as hold_lock\n"
            "with hold_lock():\n    Ultimate64Client(host='h')\n"
        ),
    }
    for label, source in must_flag.items():
        assert _device_lock_offenders(source), f"the lock scan missed: {label}"

    must_pass = {
        "locked client": (
            helper + client + "with hold_device_lock('h'):\n    Ultimate64Client(host='h')\n"
        ),
        "http helper only called under the lock, through a chain": (
            helper + "import urllib.request\n"
            + "def _get(u):\n    return urllib.request.urlopen(u)\n"
            + "def _json(u):\n    return _get(u)\n"
            + "def main():\n    with hold_device_lock('h'):\n        _json('a')\n"
        ),
        "manager path": (
            "from c64_test_harness import create_manager\n"
            "from c64_test_harness.backends.device_lock import resolve_lock_timeout\n"
            "with create_manager(backend='u64',\n"
            "        lock_timeout=resolve_lock_timeout(None, default=600.0)) as m:\n"
            "    pass\n"
        ),
        "resolved acquire budget": (
            "from c64_test_harness.backends.device_lock import DeviceLock, resolve_lock_timeout\n"
            "DeviceLock('h').acquire(timeout=resolve_lock_timeout(None, default=120.0))\n"
        ),
        "device object used only inside the lock, name reused before it": (
            helper + client
            + "def main(h):\n"
            + "    c = None\n"
            + "    print(c)\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "        c.reset()\n"
            + "    print('done')\n"
        ),
        # #338: a rebinding hides the object from what it dominates.
        "name rebound after the lock, then read": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "        c.reset()\n"
            + "    c = None\n"
            + "    print(c)\n"
        ),
        "name rebound after the lock on the same line as the read": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    c = None; print(c)\n"
        ),
        "name rebound by a for loop after the lock": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    for c in range(3):\n"
            + "        print(c)\n"
        ),
        "rebinding, then a read nested in a later block": (
            helper + client
            + "def main(h, x):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    c = None\n"
            + "    if x:\n"
            + "        print(c)\n"
        ),
        "rebinding inside a branch hides a read later in that branch": (
            helper + client
            + "def main(h, x):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    if x:\n"
            + "        c = None\n"
            + "        print(c)\n"
        ),
        # Review round 2 of #361: the rebinding and the read share a statement
        # list other than ``body``.
        "rebinding and read in the same `else:` list": (
            helper + client
            + "def main(h, x):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    if x:\n"
            + "        pass\n"
            + "    else:\n"
            + "        c = None\n"
            + "        print(c)\n"
        ),
        "rebinding and read in the same `finally:` list": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    try:\n"
            + "        pass\n"
            + "    finally:\n"
            + "        c = None\n"
            + "        print(c)\n"
        ),
        "rebinding and read in the same `except:` handler": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c = Ultimate64Client(host=h)\n"
            + "    try:\n"
            + "        pass\n"
            + "    except Exception:\n"
            + "        c = None\n"
            + "        print(c)\n"
        ),
        "nested function defined under the lock": (
            helper + client
            + "def run(h):\n    with hold_device_lock(h):\n"
            + "        t = Ultimate64Client(host=h)\n"
            + "        def _read():\n            return t.read_memory(0, 1)\n"
            + "        return _read()\n"
        ),
    }
    for label, source in must_pass.items():
        assert not _device_lock_offenders(source), (
            f"the lock scan false-positived ({label}): {_device_lock_offenders(source)}"
        )

    # Stated limits of the escape tracking (_escaped_device_objects), asserted
    # so each stays known. If one starts being flagged, update that docstring.
    known_misses = {
        "tuple target": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        c, n = Ultimate64Client(host=h), 1\n"
            + "    c.reset()\n"
        ),
        "attribute target": (
            helper + client
            + "class R:\n    def main(self, h):\n"
            + "        with hold_device_lock(h):\n"
            + "            self.c = Ultimate64Client(host=h)\n"
            + "        self.c.reset()\n"
        ),
        "walrus": (
            helper + client
            + "def main(h):\n"
            + "    with hold_device_lock(h):\n"
            + "        (c := Ultimate64Client(host=h))\n"
            + "    c.reset()\n"
        ),
        "read at the top of the next loop iteration": (
            helper + client
            + "def main(h):\n"
            + "    c = None\n"
            + "    for _ in range(2):\n"
            + "        if c:\n"
            + "            c.reset()\n"
            + "        with hold_device_lock(h):\n"
            + "            c = Ultimate64Client(host=h)\n"
        ),
    }
    for label, source in known_misses.items():
        assert not _device_lock_offenders(source), (
            f"a stated limit of the escape tracking is now flagged ({label}); "
            f"update _escaped_device_objects' docstring"
        )

    # Conservative by design, stated in the docstring: a rebinding in every
    # branch does not hide the read after the ``if``.
    every_branch = (
        helper + client
        + "def main(h, x):\n"
        + "    with hold_device_lock(h):\n"
        + "        c = Ultimate64Client(host=h)\n"
        + "    if x:\n"
        + "        c = None\n"
        + "    else:\n"
        + "        c = None\n"
        + "    c.reset()\n"
    )
    assert _device_lock_offenders(every_branch), (
        "a rebinding in every branch now hides the read after the if; update "
        "the conservative limit in _escaped_device_objects' docstring"
    )


#: Scripts that launch pytest over live modules, and the module-level list of
#: modules each launches.  All of them rely on conftest's per-test guard, so
#: none may hold the device lock itself (#323).
_PYTEST_RUNNERS = {
    "run_all_u64_live.py": "_MODULES",
    "run_sid_u64_live.py": "_MODULES",
    "run_u64_parallel_locked.py": "TEST_FILES",
}

#: Scripts that launch pytest but are not live runners, and why.
_PYTEST_LAUNCHERS_EXCLUDED = {
    "run_all_tests.py": "runs the offline unit and integration phases; it "
    "launches no *_live.py module",
}

#: Calls that take or join the device lock.
_LOCK_TAKING_CALLS = frozenset({
    "DeviceLock", "hold_device_lock", "acquire", "acquire_or_raise", "create_manager",
})


def _launches_pytest(tree: ast.AST) -> bool:
    """``pytest.main(...)`` in-process, or ``"pytest"`` in a child's argv."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "main"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pytest"
        ):
            return True
        if isinstance(node, ast.Constant) and node.value == "pytest":
            return True
    return False


def _pytest_runner_problems(source: str, list_name: str, is_live) -> list[str]:
    """What stops a pytest runner relying on conftest's per-test lock.

    Three requirements: it launches pytest; the module-level *list_name* is a
    non-empty list of modules conftest's guard locks; and it takes **no**
    device lock itself.  The last is #323: ``run_u64_parallel_locked.py`` held
    ``DeviceLock(host)`` in a pool worker around a ``python -m pytest`` child,
    whose autouse guard then queued on that flock (``allow_nested`` joins holds
    within one process only) until the parent killed it.
    """
    tree = ast.parse(source)
    problems: list[str] = []
    values = [
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == list_name for t in node.targets)
    ]
    if not values or not isinstance(values[0], (ast.List, ast.Tuple)) or not values[0].elts:
        problems.append(f"launches nothing: no non-empty module-level {list_name}")
    else:
        for elt in values[0].elts:
            name = getattr(elt, "value", None)
            if not isinstance(name, str) or not is_live(name):
                problems.append(
                    f"launches {name!r}, which conftest's device_lock_guard does not lock"
                )
    aliases = _import_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _callee_name(node, aliases) in _LOCK_TAKING_CALLS:
            problems.append(
                f"line {node.lineno}: {ast.unparse(node.func)}(...) takes the device "
                f"lock around children whose conftest guard queues on it (#323)"
            )
    if not _launches_pytest(tree):
        problems.append("does not launch pytest")
    return problems


def test_the_pytest_runner_check_can_fail() -> None:
    """Positive and negative controls for :func:`_pytest_runner_problems`."""
    import conftest

    live = conftest.is_live_test_file
    subprocess_runner = (
        "import subprocess, sys\n"
        'TEST_FILES = ["tests/test_a_live.py", "tests/test_b_live.py"]\n'
        "def run(f, host):\n"
        '    subprocess.run([sys.executable, "-m", "pytest", f])\n'
    )
    in_process_runner = (
        "import pytest\n"
        '_MODULES = ["tests/test_a_live.py"]\n'
        "def main():\n"
        "    return pytest.main([*_MODULES])\n"
    )
    assert _pytest_runner_problems(subprocess_runner, "TEST_FILES", live) == []
    assert _pytest_runner_problems(in_process_runner, "_MODULES", live) == []

    must_flag = {
        "the #323 shape: DeviceLock held around the child": (
            "import subprocess, sys\n"
            "from c64_test_harness.backends.device_lock import DeviceLock\n"
            'TEST_FILES = ["tests/test_a_live.py"]\n'
            "def run(f, host):\n"
            "    lock = DeviceLock(host)\n"
            "    lock.acquire(timeout=t)\n"
            '    subprocess.run([sys.executable, "-m", "pytest", f])\n'
        ),
        "hold_device_lock around pytest.main": (
            "import pytest\n"
            "from _u64_host import hold_device_lock\n"
            '_MODULES = ["tests/test_a_live.py"]\n'
            "def main(host):\n"
            "    with hold_device_lock(host):\n"
            "        return pytest.main([*_MODULES])\n"
        ),
        "an aliased DeviceLock": (
            "import subprocess, sys\n"
            "from c64_test_harness.backends.device_lock import DeviceLock as L\n"
            'TEST_FILES = ["tests/test_a_live.py"]\n'
            "def run(f, host):\n"
            "    L(host)\n"
            '    subprocess.run([sys.executable, "-m", "pytest", f])\n'
        ),
        "a module conftest does not lock": subprocess_runner.replace(
            "tests/test_b_live.py", "tests/test_b.py"
        ),
        "an empty module list": subprocess_runner.replace(
            '["tests/test_a_live.py", "tests/test_b_live.py"]', "[]"
        ),
        "no pytest launched": subprocess_runner.replace('"pytest"', '"pyflakes"'),
        # #381 review round 1 (G1, G2): each remaining lock-taking name is the
        # only one in its plant, so dropping it from _LOCK_TAKING_CALLS fails.
        "acquire on a lock from a factory not named DeviceLock": (
            "import subprocess, sys\n"
            "from somewhere import make_lock\n"
            'TEST_FILES = ["tests/test_a_live.py"]\n'
            "def run(f, host, t):\n"
            "    lock = make_lock(host)\n"
            "    lock.acquire(timeout=t)\n"
            '    subprocess.run([sys.executable, "-m", "pytest", f])\n'
        ),
        "acquire_or_raise on any receiver": (
            "import subprocess, sys\n"
            'TEST_FILES = ["tests/test_a_live.py"]\n'
            "def run(f, x):\n"
            "    x.acquire_or_raise()\n"
            '    subprocess.run([sys.executable, "-m", "pytest", f])\n'
        ),
        "create_manager around pytest": (
            "import subprocess, sys\n"
            "from c64_test_harness import create_manager\n"
            'TEST_FILES = ["tests/test_a_live.py"]\n'
            "def run(f, cfg):\n"
            "    create_manager(cfg)\n"
            '    subprocess.run([sys.executable, "-m", "pytest", f])\n'
        ),
    }
    for label, source in must_flag.items():
        assert source not in (subprocess_runner, in_process_runner), f"{label}: plant changed nothing"
        assert _pytest_runner_problems(source, "TEST_FILES" if "TEST_FILES" in source else "_MODULES", live), (
            f"the runner check missed: {label}"
        )


def test_every_pytest_launcher_is_classified() -> None:
    """Vacuity guard: the runner table is every script that launches pytest."""
    launchers = {
        p.name for p in _scripts_only() if _launches_pytest(ast.parse(p.read_text()))
    }
    assert launchers == set(_PYTEST_RUNNERS) | set(_PYTEST_LAUNCHERS_EXCLUDED), (
        f"scripts launching pytest: {sorted(launchers)}; classify each in "
        f"_PYTEST_RUNNERS or _PYTEST_LAUNCHERS_EXCLUDED"
    )
    assert not set(_PYTEST_RUNNERS) & set(_PYTEST_LAUNCHERS_EXCLUDED)
    for excluded in _PYTEST_LAUNCHERS_EXCLUDED:
        assert "_live.py" not in (_SCRIPTS / excluded).read_text(), (
            f"{excluded} is excluded as not a live runner but names a live module"
        )


def test_the_pytest_runners_are_locked_by_conftest() -> None:
    """Accepted shape 3, pinned rather than assumed.

    The runners hold no lock of their own, so they are safe only while every
    module they launch is one conftest's autouse ``device_lock_guard`` locks.
    ``run_u64_parallel_locked.py`` is classified with them since #323: a lock
    taken by the runner around a pytest child is one the child's guard waits
    on, so "hold no lock" is a requirement, not just a description.
    """
    import inspect

    conftest_path = Path(__file__).resolve().parent / "conftest.py"
    tree = ast.parse(conftest_path.read_text())
    guard = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "device_lock_guard"
    )
    autouse = any(
        isinstance(dec, ast.Call)
        and any(
            kw.arg == "autouse" and getattr(kw.value, "value", None) is True
            for kw in dec.keywords
        )
        for dec in guard.decorator_list
    )
    assert autouse, "conftest's device_lock_guard is no longer autouse"
    guard_source = ast.get_source_segment(conftest_path.read_text(), guard) or ""
    assert "is_live_test_file(" in guard_source and "acquire_or_raise(" in guard_source

    import conftest  # the suite's own conftest, already imported by pytest

    for runner, list_name in _PYTEST_RUNNERS.items():
        problems = _pytest_runner_problems(
            (_SCRIPTS / runner).read_text(), list_name, conftest.is_live_test_file
        )
        assert problems == [], f"{runner}: {problems}"
    assert inspect.isfunction(conftest.is_live_test_file)


# -- The helper's behaviour, in-process, against a private lock directory --

_TEST_NET_HOST = "192.0.2.1"  # RFC 5737: reaches nothing if anything leaks


@pytest.fixture
def private_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every DeviceLock in the test resolves here, never the shared lock dir."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("U64_DEVICE_LOCK_TIMEOUT", raising=False)
    return tmp_path


@pytest.fixture
def acquire_spy(monkeypatch: pytest.MonkeyPatch) -> list:
    from c64_test_harness.backends import device_lock

    seen: list = []
    real = device_lock.DeviceLock.acquire_or_raise

    def spy(self, timeout=None, **kwargs):
        seen.append(timeout)
        return real(self, timeout, **kwargs)

    monkeypatch.setattr(device_lock.DeviceLock, "acquire_or_raise", spy)
    # A timeout's diagnostics probe the device's REST API; never here.
    monkeypatch.setattr(
        device_lock.DeviceLock, "_probe_rest_reachable", lambda self: None
    )
    return seen


def test_hold_device_lock_holds_for_the_block_and_releases(
    u64_host_module: ModuleType, private_lock_dir: Path, acquire_spy: list
) -> None:
    from c64_test_harness.backends.device_lock import DeviceLock

    assert not DeviceLock.held_by_this_process(_TEST_NET_HOST)
    with u64_host_module.hold_device_lock(_TEST_NET_HOST):
        assert DeviceLock.held_by_this_process(_TEST_NET_HOST)
        assert any(private_lock_dir.rglob("*.lock")), "not the private lock dir"
    assert not DeviceLock.held_by_this_process(_TEST_NET_HOST)

    with pytest.raises(SystemExit):
        with u64_host_module.hold_device_lock(_TEST_NET_HOST):
            raise SystemExit(1)
    assert not DeviceLock.held_by_this_process(_TEST_NET_HOST), (
        "the lock outlived a script exiting inside the block"
    )


def test_hold_device_lock_budget_goes_through_the_resolver(
    u64_host_module: ModuleType,
    private_lock_dir: Path,
    acquire_spy: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env wins over the script's default; the default wins over the manager's.

    None of the expected values is a system default, so each can only pass
    if the helper actually read the source it claims to.
    """
    from c64_test_harness.backends import unified_manager

    monkeypatch.setattr(unified_manager, "DEFAULT_LOCK_TIMEOUT", 43.0)
    with u64_host_module.hold_device_lock(_TEST_NET_HOST):
        pass
    with u64_host_module.hold_device_lock(_TEST_NET_HOST, default_timeout=7.5):
        pass
    monkeypatch.setenv("U64_DEVICE_LOCK_TIMEOUT", "12.5")
    with u64_host_module.hold_device_lock(_TEST_NET_HOST, default_timeout=7.5):
        pass
    assert acquire_spy == [43.0, 7.5, 12.5]


def test_hold_device_lock_refuses_a_malformed_budget_before_locking(
    u64_host_module: ModuleType,
    private_lock_dir: Path,
    acquire_spy: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from c64_test_harness.backends.device_lock import (
        DeviceLock,
        DeviceLockTimeoutConfigError,
    )

    monkeypatch.setenv("U64_DEVICE_LOCK_TIMEOUT", "30m")
    entered = False
    with pytest.raises(DeviceLockTimeoutConfigError):
        with u64_host_module.hold_device_lock(_TEST_NET_HOST):
            entered = True
    assert not entered and acquire_spy == []
    assert not DeviceLock.held_by_this_process(_TEST_NET_HOST)


def test_hold_device_lock_fails_closed_on_timeout(
    u64_host_module: ModuleType,
    private_lock_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from c64_test_harness.backends import device_lock

    def timed_out(self, timeout=None, **kwargs):
        raise device_lock.DeviceLockTimeout(
            device_host=self.device_host, holder_pid=4242, pid_alive=True,
            lockfile_age_seconds=1.0, device_reachable_rest=None,
            timeout=timeout, progress_window=60.0,
        )

    monkeypatch.setattr(device_lock.DeviceLock, "acquire_or_raise", timed_out)
    err = io.StringIO()
    entered = False
    with pytest.raises(SystemExit) as exc:
        with u64_host_module.hold_device_lock(_TEST_NET_HOST, stderr=err):
            entered = True
    assert not entered, "the script body ran without the lock"
    assert exc.value.code == u64_host_module.NO_LOCK_EXIT == 3
    assert "4242" in err.getvalue(), "the holder diagnostics were not reported"


def test_hold_device_lock_fails_closed_without_the_harness(
    u64_host_module: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "c64_test_harness.backends.device_lock", None)
    err = io.StringIO()
    entered = False
    with pytest.raises(SystemExit) as exc:
        with u64_host_module.hold_device_lock(_TEST_NET_HOST, stderr=err):
            entered = True
    assert not entered
    assert exc.value.code == 3
    assert "will not import" in err.getvalue()


def test_hold_device_lock_joins_a_hold_this_process_already_has(
    u64_host_module: ModuleType,
    private_lock_dir: Path,
    acquire_spy: list,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``allow_nested``: a script re-entering the helper must not wait on itself.

    Without it the inner acquire queues on its own thread's flock (#273),
    serves the whole budget and exits 3. The budget is kept short so that
    failure mode costs well under a second.
    """
    import time as _time

    from c64_test_harness.backends.device_lock import DeviceLock

    monkeypatch.setenv("U64_DEVICE_LOCK_TIMEOUT", "0.3")
    with u64_host_module.hold_device_lock(_TEST_NET_HOST):
        start = _time.monotonic()
        with u64_host_module.hold_device_lock(_TEST_NET_HOST):
            assert DeviceLock.held_by_this_process(_TEST_NET_HOST)
        assert _time.monotonic() - start < 0.25
        assert DeviceLock.held_by_this_process(_TEST_NET_HOST), (
            "the inner release dropped the outer hold"
        )


# -- Runtime: at the moment a script first reaches out, does it hold the lock? --
#
# The AST scan proves lexical containment; this proves it is real when the
# script runs: the helper imports from where the script runs, the lock is
# actually held, and no request precedes it through a path the scan does not
# model. It reuses :data:`_SANDBOX` verbatim except for two checked
# substitutions, so the forbid list is the one the sandbox controls verified.

_PROBE_FORBID_OLD = '''def _forbid(what):
    def _raise(*args, **kwargs):
        raise _Breach(what)
    return _raise
'''
_PROBE_FORBID_NEW = '''def _lock_state():
    try:
        from c64_test_harness.backends.device_lock import DeviceLock
        held = DeviceLock.held_by_this_process(os.environ["U64_HOST"])
        return "HELD" if held else "NOT-HELD"
    except BaseException as exc:
        return "UNKNOWN(%r)" % (exc,)

def _forbid(what):
    def _raise(*args, **kwargs):
        sys.stderr.write("LOCK STATE AT BREACH: %s %s\\n" % (_lock_state(), what))
        raise _Breach(what)
    return _raise
'''
_PROBE_ARGV_OLD = "sys.argv = [_script]\n"
_PROBE_ARGV_NEW = "sys.argv = [_script, *sys.argv[2:]]\n"


def _lock_probe_preamble() -> str:
    assert _SANDBOX.count(_PROBE_FORBID_OLD) == 1, "_SANDBOX's _forbid changed shape"
    assert _SANDBOX.count(_PROBE_ARGV_OLD) == 1, "_SANDBOX's argv reset changed shape"
    return _SANDBOX.replace(_PROBE_FORBID_OLD, _PROBE_FORBID_NEW).replace(
        _PROBE_ARGV_OLD, _PROBE_ARGV_NEW
    )


def _run_lock_probe(
    script: Path, lock_dir: Path, *args: str
) -> "subprocess.CompletedProcess[str]":
    import subprocess

    env = {
        k: v for k, v in os.environ.items()
        if k not in ("U64_HOST", "U64_DEVICE_LOCK_TIMEOUT")
    }
    env["U64_HOST"] = _TEST_NET_HOST
    env["XDG_RUNTIME_DIR"] = str(lock_dir)
    return subprocess.run(
        [sys.executable, "-c", _lock_probe_preamble(), str(script), *args],
        capture_output=True, text=True, env=env, timeout=30,
    )


def _first_lock_state(proc: "subprocess.CompletedProcess[str]") -> str | None:
    for line in proc.stderr.splitlines():
        if line.startswith("LOCK STATE AT BREACH: "):
            return line.split(": ", 1)[1].split(" ", 1)[0]
    return None


@pytest.fixture(scope="module")
def verified_lock_probe(
    verified_sandbox: dict[str, int], tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """The lock probe, proven able to report both answers, before any script runs."""
    work = tmp_path_factory.mktemp("lock_probe_controls")
    reach = "import socket\nsocket.create_connection(('192.0.2.1', 80), 1)\n"
    unlocked = work / "unlocked.py"
    unlocked.write_text(reach)
    locked = work / "locked.py"
    locked.write_text(
        f"import sys\nsys.path.insert(0, {str(_SCRIPTS)!r})\n"
        "from _u64_host import hold_device_lock\n"
        "with hold_device_lock('192.0.2.1'):\n"
        + "".join("    " + line + "\n" for line in reach.splitlines())
    )
    for script, expected in ((unlocked, "NOT-HELD"), (locked, "HELD")):
        proc = _run_lock_probe(script, work)
        assert proc.returncode == _BREACH_EXIT, (script.name, proc.stderr[-1500:])
        assert _first_lock_state(proc) == expected, (
            f"the lock probe reported {_first_lock_state(proc)!r} for "
            f"{script.name}, expected {expected}:\n{proc.stderr[-1500:]}"
        )
    return work


def _bench_args(work: Path) -> tuple[str, ...]:
    prg = work / "x25519.prg"
    prg.write_bytes(b"\x01\x08" + bytes(16))
    labels = work / "labels.txt"
    labels.write_text("".join(
        f"al C:{0xC000 + i:04x} .{name}\n"
        for i, name in enumerate((
            "x25519_base", "x25_scalar", "x25_result",
            "bench_ticks", "vic_blank", "vic_unblank", "main_loop",
        ))
    ))
    return ("--prg", str(prg), "--labels", str(labels), "--speeds", "1")


#: Scripts the runtime probe drives, with the arguments that get them past
#: their offline setup to the first device contact.
_LOCK_PROBED_SCRIPTS = (
    "bench_x25519_u64_turbo.py",
    "play_chromatic_u64.py",
    "play_scale_u64.py",
    "probe_u64.py",
    "probe_uci_network.py",
)

#: Device scripts the runtime probe cannot drive, and why. The AST scan still
#: covers them; the partition test keeps this list from absorbing new scripts.
_LOCK_PROBE_EXCLUDED = {
    "verify_tod_warp.py": "launches VICE before its U64 case; the first "
    "breach is that spawn, which is local, not device traffic",
}


@pytest.mark.parametrize("script_name", _LOCK_PROBED_SCRIPTS)
def test_script_holds_the_lock_at_its_first_device_contact(
    script_name: str, verified_lock_probe: Path, tmp_path: Path
) -> None:
    args = _bench_args(tmp_path) if script_name.startswith("bench_") else ()
    proc = _run_lock_probe(_SCRIPTS / script_name, tmp_path, *args)
    assert proc.returncode == _BREACH_EXIT, (
        f"{script_name} never reached out (exit {proc.returncode}), so this "
        f"probe proved nothing about it:\n{proc.stderr[-2000:]}"
    )
    assert _first_lock_state(proc) == "HELD", (
        f"{script_name} reached out without holding the DeviceLock:\n"
        f"{proc.stderr[-2000:]}"
    )


def test_the_lock_probe_covers_every_device_script() -> None:
    assert set(_LOCK_PROBED_SCRIPTS) | set(_LOCK_PROBE_EXCLUDED) == _KNOWN_DEVICE_SCRIPTS
    assert not set(_LOCK_PROBED_SCRIPTS) & set(_LOCK_PROBE_EXCLUDED)
    assert len(_LOCK_PROBED_SCRIPTS) >= 5

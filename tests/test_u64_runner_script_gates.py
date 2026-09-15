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
}


def _run_in_sandbox(script: Path) -> "subprocess.CompletedProcess[str]":
    import subprocess

    return subprocess.run(
        [sys.executable, "-c", _SANDBOX, str(script)],
        capture_output=True,
        text=True,
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
    import subprocess

    env = {k: v for k, v in os.environ.items() if k != "U64_HOST"}
    proc = subprocess.run(
        [sys.executable, "-c", _SANDBOX, str(_SCRIPTS / script_name)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )

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
    assert len(_BREACH_PROBES) >= 7, "breach probes were dropped"


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


#: Lines that legitimately contain a device address, as (filename, fragment)
#: pairs. Line-granular on purpose: a file-level exemption means one mock
#: string disables the check for everything else in that file, and the next
#: real regression hides behind it. Each fragment names *why* the line is
#: allowed — a lock identity, a mock attribute, a parser input — so a new
#: address on a new line still fails even in these files.
_ADDRESS_ALLOWLIST_LINES = {
    # Lock-file identity: the address is an arbitrary opaque key here.
    ("test_device_lock.py", "DeviceLock("),
    ("test_device_lock.py", "_sanitize_device_id("),
    ("test_device_lock.py", 'info["device_host"]'),
    # Mock attributes and assertions about them.
    ("test_render_wav_u64.py", "mock_client.host"),
    ("test_render_wav_u64.py", "mock_detect.assert_called_once_with("),
    # Parser / protocol inputs and their expected outputs.
    ("test_uci_network.py", "_make_mock_transport("),
    ("test_uci_network.py", "assert result =="),
    ("test_uci_network.py", "uci_tcp_connect("),
    ("test_unified_manager.py", "_make_mock_u64_instance("),
    ("test_unified_manager.py", "_parse_u64_hosts("),
    ("test_unified_manager.py", "patch.dict("),
    # Parametrizes over host strings to prove hygiene is not keyed on the
    # device's address; the addresses are the test data.
    ("test_ultimate64_temp_hygiene.py", "for host in ("),
}

#: The single whole-file exemption, and the only place file granularity is
#: the right unit: this module *defines* the rule, so every address in it is
#: either the rule's own input data or prose explaining the rule. Exempting
#: a rule from itself is not the same defect as exempting a file that merely
#: happens to contain one mock — and
#: :func:`test_only_this_file_is_exempt_wholesale` stops a second appearing.
_ADDRESS_ALLOWLIST_FILES = {Path(__file__).name}


def _allowed_address_line(filename: str, line: str) -> bool:
    return any(
        filename == allowed_file and fragment in line
        for allowed_file, fragment in _ADDRESS_ALLOWLIST_LINES
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
    for allowed_file, fragment in sorted(_ADDRESS_ALLOWLIST_LINES):
        path = Path(__file__).resolve().parent / allowed_file
        if not path.exists():
            unused.append(f"{allowed_file} (file is gone)")
            continue
        if not any(
            fragment in line and _names_a_device(line)
            for line in path.read_text().splitlines()
        ):
            unused.append(f"{allowed_file}: {fragment!r}")
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
    """Every node that executes at import — the binding rule's scope."""
    found: list[ast.AST] = []

    def descend(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(
                child,
                (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda),
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
    assignment into ``os.environ[...HOST...]``. Function bodies are out of
    scope, as for the binding rule: they are not import-time.
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
    }
    for label, source in must_flag.items():
        assert _host_default_offenders(source), f"default rule missed ({label})"

    must_pass = {
        "no default": 'import os\nH = os.environ.get("U64_HOST")\n',
        "empty default": 'import os\nH = os.environ.get("U64_HOST", "")\n',
        "or None": 'import os\nH = os.environ.get("U64_HOST") or None\n',
        "function scope": 'import os\ndef f():\n    return os.environ.get("U64_HOST", "x")\n',
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

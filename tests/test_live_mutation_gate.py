"""A live module that writes device config must be gated on ``U64_ALLOW_MUTATE``.

``U64_ALLOW_MUTATE`` is how an operator says "you may change state on this
device", and its value is that it is uniform: somebody who has not set it
reasonably believes no suite will reconfigure the shared device under them
(issue #268).  #268 filed three suites that wrote config without it; the scan
in this module found eleven.  This pins the rule without a device, with no
allowlist.

**What counts as a config write** is derived from the source, not listed by
hand.  :data:`CONFIG_PRIMITIVES` are the ``Ultimate64Client`` methods that
send a non-GET request to ``/v1/configs`` (and that set is itself checked
against the client source).  The vocabulary is every function in
``src/c64_test_harness`` that reaches one of them by name, transitively --
``enable_uci``, ``set_turbo_mhz``, ``Ultimate64Transport.set_speed``,
``restore_state``, ``isolated_sid_addressing`` and the rest -- minus the
named :data:`EXCLUDED` entry points, each with its reason.

**What counts as gated**: a ``skipif`` condition, or the test of an ``if``
that calls ``skip``, that reads ``U64_ALLOW_MUTATE`` (directly, or through
a name bound from such a read) **and is true when the variable is unset**.
The condition is evaluated, not just searched:

* a gate read evaluates to its default -- ``None`` without one, the literal
  with a literal default (so ``not get(GATE, '0')`` never skips), and a
  non-literal default fails closed;
* ``U64_HOST`` reads as set; every other environment variable is unknown,
  and the condition must skip for **every** set/unset combination of those
  it reads (at most six; more fails closed), so ``not <gate> and get('CI')``
  and ``not get('X_LIVE') or get(GATE)`` are refused while the multi-gate
  ``not _LIVE or not <gate>`` stays a gate;
* only a single-Name assignment at module level binds a name, in source
  order -- a literal to itself, anything else to its expression -- and a
  name rebound to anything the evaluator cannot decide, stored any other
  way (tuple or ``for``/``with`` target, ``+=``, walrus, import alias,
  parameter, function, class, or any assignment inside a function or
  class), or never bound, fails closed;
* only a small expression grammar (boolean operators, comparisons,
  constants, ``bool``/``str``/``int``) is evaluated, and an evaluation that
  raises is not a gate.

So
``skipif(os.environ.get(GATE) == '1')`` -- which skips when the gate is
*set* and writes when it is not -- is not a gate, nor is
``skipif(False and ...)``, nor a condition outside the grammar (fail closed).
A module that only mentions the variable in a docstring, or reads it and
never skips on it, is not gated.

**A floor, not a proof -- the limits are the scanner's, stated so nobody
reads more into a green run:**

* Two granularities.  Every ``test_*`` function that calls a config writer
  **itself** must be gated -- by the module ``pytestmark``, its class's
  decorator or ``pytestmark``, its own decorator, an ``if`` on the gate
  that skips in its body, or a module fixture it requests (directly or
  through another fixture) that skips on the gate.  Fixtures and module
  helpers that write are held only to the module-level rule: a restore
  fixture that writes back what a gated test moved passes, and so would a
  fixture that writes on its own in a module that gates some other test.
  Writes reached through a module helper the test calls are not
  attributed to the test.  ``module_is_gated`` accepts any gating
  ``skipif`` call anywhere in the module, even one bound to a name that is
  later rebound or never applied; the test-level check, which follows
  marks by name, is what catches that.
* Matching is **by name**.  ``set_speed`` is a writer because
  ``Ultimate64Transport.set_speed`` is one, so a VICE-only live module
  calling ``transport.set_speed`` would be flagged (none does today) -- the
  safe direction.  A write through an alias (``f = client.set_config_item``)
  or through a helper defined in the test module itself is missed.
  :data:`EXCLUDED` matches by bare name too: excluding ``acquire`` excludes
  every function of that name in ``src`` (``DeviceLock.acquire`` included),
  so a future ``acquire`` that writes config would be missed.
* It covers **config writes only**, which is the whole contract: resets,
  RAM writes and stream start/stop are outside ``U64_ALLOW_MUTATE`` (owner
  decision 2026-09-15, #333) and are not scanned.
* **Setting the gate is always an offence**, whatever the module skips on:
  a module that stores ``U64_ALLOW_MUTATE`` itself (:func:`gate_stores`:
  ``os.environ[...] =``, ``setdefault``, ``update``, ``putenv``,
  ``monkeypatch.setenv``, anywhere in the file) arms its own gate.  A store
  through an alias or a computed key is not seen.
* An unknown environment variable is tried only **unset (or its literal
  default) and as ``"1"``**, so a comparison against another literal escapes:
  ``get('X_LIVE') != 'yes' or get(GATE) == '1'`` never skips with
  ``X_LIVE=yes`` and the gate unset, yet is scored as a gate.  Tracked as
  #353; pinned by two ``xfail(strict=True)`` cases that flip when it is fixed.
* A rebinding through ``globals()['_M'] = '1'`` (or ``setattr`` on the
  module) is not seen by the binder.
"""
from __future__ import annotations

import ast
import copy
import itertools
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).parent
SRC_DIR = TESTS_DIR.parent / "src" / "c64_test_harness"
CLIENT_PATH = SRC_DIR / "backends" / "ultimate64_client.py"

GATE = "U64_ALLOW_MUTATE"

#: ``Ultimate64Client`` methods that send a non-GET request to ``/v1/configs``.
#: Checked against the client source by
#: :func:`test_config_primitives_match_the_client_source`, both ways.
CONFIG_PRIMITIVES = frozenset({
    "set_config_item",
    "set_config_items_batch",
    "save_config_to_flash",
    "load_config_from_flash",
    "reset_config_to_default",
    "reset_config_category_to_default",
})

#: Entry points that reach a config primitive but are not a test asking for a
#: config change.  Neither counted nor propagated through.  Each must still
#: exist and still reach the vocabulary, or the exclusion is stale
#: (:func:`test_every_exclusion_is_still_needed`).
EXCLUDED: dict[str, str] = {
    "_run_temp_hygiene": (
        "the /Temp hygiene pass enables FTP File Service on a failed pass "
        "(#259); it is reached from Ultimate64Client._request, so "
        "propagating through it would make every REST call a config write"
    ),
    "acquire": (
        "_LockedU64Manager.acquire runs the generation-resolved entry "
        "baseline (#285) -- the harness's own reconciliation, on every "
        "manager lane -- and the name collides with DeviceLock.acquire"
    ),
    "run_prg_via_sys": (
        "re-PUTs Cartridge Preference only when it already reads External "
        "(#217) and PUTs that same value back: it restores the preference "
        "rather than changing it (a same-value PUT still effectuates -- "
        "config.cc set_item -> setChanged -> set_need_effectuate -- which "
        "is how it reselects the cartridge) -- and it is also the VICE "
        "start path (test_run_prg_via_sys_vice_live.py)"
    ),
}

#: Names the vocabulary must contain, or the derivation stopped working.
VOCABULARY_ANCHORS = frozenset({
    "set_config_item", "set_config_items", "enable_uci", "disable_uci",
    "set_turbo_mhz", "set_speed", "set_reu", "restore_state",
    "apply_factory_baseline", "set_debug_stream_mode",
    "isolated_sid_addressing",
})


# --------------------------------------------------------------------------- #
# Vocabulary                                                                  #
# --------------------------------------------------------------------------- #

def _callee_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Attribute):
        return f.attr
    if isinstance(f, ast.Name):
        return f.id
    return None


def _called_names(node: ast.AST) -> set[str]:
    return {
        name
        for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and (name := _callee_name(sub))
    }


def _functions(tree: ast.AST):
    return (
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    )


def _string_parts(node: ast.AST) -> list[str]:
    return [
        sub.value
        for sub in ast.walk(node)
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str)
    ]


def derive_config_primitives(client_source: str) -> set[str]:
    """Client methods whose body names ``/v1/configs`` and sends a non-GET.

    Non-GET means a call to ``_put_no_body``/``_put_json``/``_post_binary``,
    or ``_request`` with a literal ``"PUT"``/``"POST"``/``"DELETE"`` method.
    ``set_config_items`` loops over ``set_config_item`` and so is not a
    primitive; the closure picks it up.
    """
    tree = ast.parse(client_source)
    out: set[str] = set()
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        if cls.name != "Ultimate64Client":
            continue
        for fn in _functions(cls):
            if not any(s.startswith("/v1/configs") for s in _string_parts(fn)):
                continue
            for sub in ast.walk(fn):
                if not isinstance(sub, ast.Call):
                    continue
                name = _callee_name(sub)
                if name in {"_put_no_body", "_put_json", "_post_binary"}:
                    out.add(fn.name)
                elif (
                    name == "_request"
                    and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and sub.args[0].value in {"PUT", "POST", "DELETE"}
                ):
                    out.add(fn.name)
    return out


def _src_calls(src_dir: Path) -> dict[str, list[set[str]]]:
    calls: dict[str, list[set[str]]] = {}
    for path in sorted(src_dir.rglob("*.py")):
        for fn in _functions(ast.parse(path.read_text(), filename=str(path))):
            calls.setdefault(fn.name, []).append(_called_names(fn))
    return calls


def config_writer_vocabulary(
    src_dir: Path = SRC_DIR,
    primitives: frozenset[str] = CONFIG_PRIMITIVES,
    excluded: dict[str, str] | None = None,
) -> set[str]:
    """Every src function name that reaches a primitive, transitively."""
    excluded = EXCLUDED if excluded is None else excluded
    calls = _src_calls(src_dir)
    writers = set(primitives)
    changed = True
    while changed:
        changed = False
        for name, bodies in calls.items():
            if name in writers or name in excluded:
                continue
            if any(body & writers for body in bodies):
                writers.add(name)
                changed = True
    return writers


# --------------------------------------------------------------------------- #
# Gate detection                                                              #
# --------------------------------------------------------------------------- #

def _is_environ(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "environ") or (
        isinstance(node, ast.Name) and node.id == "environ"
    )


def _env_read(node: ast.AST) -> tuple[str, ast.AST | None] | None:
    """``(variable, default node or None)`` for an environment read, else ``None``.

    Recognised, with the variable a string literal: ``os.environ.get(NAME
    [, default])``, ``os.getenv(NAME[, default])`` (``default=`` too) and
    ``os.environ[NAME]``.
    """
    if isinstance(node, ast.Call):
        func = node.func
        environ_get = isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value)
        if not (environ_get or _callee_name(node) == "getenv"):
            return None
        if not (node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            return None
        default = node.args[1] if len(node.args) > 1 else next(
            (kw.value for kw in node.keywords if kw.arg == "default"), None
        )
        return node.args[0].value, default
    if (
        isinstance(node, ast.Subscript)
        and _is_environ(node.value)
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    ):
        return node.slice.value, None
    return None


def _is_gate_env_read(node: ast.AST) -> bool:
    """``os.environ.get(GATE)``, ``os.getenv(GATE)`` or ``os.environ[GATE]``."""
    read = _env_read(node)
    return read is not None and read[0] == GATE


#: The one other requirement assumed met while a condition is evaluated.
#: Every other environment variable is unknown: a condition must skip for
#: every set/unset combination of the ones it reads.  That refuses
#: ``not _MUTATE and get('CI')`` and ``not get('X_LIVE') or get(GATE)``,
#: while the multi-gate ``not _LIVE or not _MUTATE`` stays a gate.
_ASSUMED_SET = frozenset({"U64_HOST"})

#: Most other environment variables a condition may combine with the gate;
#: above this it fails closed rather than enumerate.
_MAX_OTHER_ENV_VARS = 6


class _FailClosed(Exception):
    """A condition, or a binding it depends on, cannot be decided."""


def _read_value(read: tuple[str, ast.AST | None], env: dict[str, bool]) -> object:
    """What an environment read evaluates to for one combination *env*.

    ``U64_HOST`` reads as set, and a variable *env* marks set reads as
    ``"1"``.  Otherwise -- always, for the gate -- it reads as its default:
    ``None`` when there is none, the literal when the default is one, and a
    non-literal default fails closed.
    """
    variable, default = read
    if variable in _ASSUMED_SET:
        return "set"
    if env.get(variable):
        return "1"
    if default is None:
        return None
    if isinstance(default, ast.Constant):
        return default.value
    raise _FailClosed(f"non-literal default for {variable}")


def _other_env_vars(expr: ast.AST) -> list[str]:
    """The environment variables *expr* reads, other than the gate and ``U64_HOST``."""
    found: set[str] = set()
    for node in ast.walk(expr):
        read = _env_read(node)
        if read is not None and read[0] != GATE and read[0] not in _ASSUMED_SET:
            found.add(read[0])
    return sorted(found)


def _unsafe_names(tree: ast.AST) -> set[str]:
    """Names stored any way other than a single-Name module-level assignment."""
    body = tree.body if isinstance(tree, ast.Module) else []
    blessed = {
        id(stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target)
        for stmt in body
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
        )
        or (
            isinstance(stmt, ast.AnnAssign)
            and stmt.value is not None
            and isinstance(stmt.target, ast.Name)
        )
    }
    unsafe: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Name)
            and isinstance(node.ctx, (ast.Store, ast.Del))
            and id(node) not in blessed
        ):
            unsafe.add(node.id)
        elif isinstance(node, ast.alias):
            unsafe.add((node.asname or node.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            unsafe.add(node.name)
        elif isinstance(node, ast.arg):
            unsafe.add(node.arg)
    return unsafe


def _evaluate_once(expr: ast.AST, env: dict[str, bool]) -> tuple[bool, object]:
    """``(True, value)`` of an inlined *expr* for one combination; ``(False, None)`` if not.

    Anything outside :data:`_SAFE_NODES` / :data:`_SAFE_CALLS` is refused, and
    an evaluation that raises proves nothing -- fail closed.
    """
    class _Substitute(ast.NodeTransformer):
        def _read(self, node: ast.AST) -> ast.AST:
            read = _env_read(node)
            if read is not None:
                return ast.copy_location(ast.Constant(_read_value(read, env)), node)
            return self.generic_visit(node)

        visit_Call = _read
        visit_Subscript = _read

    try:
        tree = ast.fix_missing_locations(
            _Substitute().visit(ast.Expression(body=copy.deepcopy(expr)))
        )
    except _FailClosed:
        return False, None
    for node in ast.walk(tree):
        if not isinstance(node, _SAFE_NODES):
            return False, None
        if isinstance(node, ast.Name) and node.id not in _SAFE_CALLS:
            return False, None
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name)
            and node.func.id in _SAFE_CALLS
            and not node.keywords
        ):
            return False, None
    try:
        code = compile(tree, "<gate condition>", "eval")
        return True, eval(code, {"__builtins__": {}}, dict(_SAFE_CALLS))  # noqa: S307
    except Exception:  # noqa: BLE001 -- unevaluable means not a gate
        return False, None


#: Callables a condition may use and still be evaluated.
_SAFE_CALLS = {"bool": bool, "str": str, "int": int}

#: The expression grammar a condition must stay inside to be evaluated.
_SAFE_NODES = (
    ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not,
    ast.Compare, ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn,
    ast.Constant, ast.Name, ast.Load, ast.Call, ast.Tuple, ast.List,
)

#: What a binding may contain before it is worth evaluating (env reads add
#: attribute and subscript nodes that the substitution removes).
_BINDING_NODES = _SAFE_NODES + (ast.Attribute, ast.Subscript, ast.keyword)

#: Shown when a module fails the gate rules, so an author knows what to write.
ACCEPTED_GATE_SHAPES = (
    f"pytest.mark.skipif(not os.environ.get({GATE!r}), reason=...)",
    f"pytest.mark.skipif(os.environ.get({GATE!r}) != '1', reason=...)",
    f"_MUTATE = os.environ.get({GATE!r})  # or os.getenv(...) / os.environ[...]; "
    "then skipif(not _MUTATE, ...), a mark bound to that skipif, or "
    "`if not _MUTATE: pytest.skip(...)` in a fixture or test",
    "the variable name and any default must be string literals; conditions "
    "may use and/or/not, comparisons, literals and bool()/str()/int(); every "
    "other environment variable except U64_HOST may be set or unset and the "
    "condition must skip in every combination; bind names with a plain "
    "module-level `NAME = ...`",
)


class _GateContext:
    """What one module's source says about the gate, evaluated with it unset.

    One pass over the module body's single-Name assignments, in source order:

    * ``values`` -- each name bound to an expression the scanner can decide,
      mapped to that expression with earlier names inlined (a literal is its
      own expression).  A later binding replaces it; a binding to anything
      undecidable removes the name.  A name stored any other way
      (:func:`_unsafe_names`) is never bound, and referencing a name not in
      ``values`` fails closed.
    * ``derived`` -- the names whose current binding reads the gate (so
      ``_M = get(GATE)`` then ``_M = '1'`` is no longer the gate).
    * ``marks`` -- names bound to a ``skipif`` mark that gates.
    * ``rejected`` -- conditions met after construction that mention the gate
      but were not accepted, with the reason, for failure messages.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.values: dict[str, ast.AST] = {}
        self.derived: set[str] = set()
        self.marks: set[str] = set()
        self.rejected: dict[str, str] = {}
        self._recording = False
        unsafe = _unsafe_names(tree)
        for node in tree.body if isinstance(tree, ast.Module) else []:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                continue
            name = targets[0].id
            if name in unsafe:
                continue
            if self.mark_gates(node.value):
                self.marks.add(name)
                continue
            self.marks.discard(name)
            decided = (
                self.decide(node.value)
                if all(isinstance(n, _BINDING_NODES) for n in ast.walk(node.value))
                else None
            )
            if decided is None:
                self.values.pop(name, None)
                self.derived.discard(name)
                continue
            inlined, _results = decided
            self.values[name] = inlined
            if any(_is_gate_env_read(n) for n in ast.walk(inlined)):
                self.derived.add(name)
            else:
                self.derived.discard(name)
        self._recording = True

    def references_gate(self, expr: ast.AST) -> bool:
        return any(
            _is_gate_env_read(sub)
            or (isinstance(sub, ast.Name) and sub.id in self.derived)
            for sub in ast.walk(expr)
        )

    def mentions_gate(self, expr: ast.AST) -> bool:
        """Names the gate at all -- a literal, or a name bound to the literal."""
        return self.references_gate(expr) or any(
            (isinstance(sub, ast.Constant) and sub.value == GATE)
            or (
                isinstance(sub, ast.Name)
                and isinstance(self.values.get(sub.id), ast.Constant)
                and self.values[sub.id].value == GATE
            )
            for sub in ast.walk(expr)
        )

    def _inline(self, expr: ast.AST) -> ast.AST:
        """A copy of *expr* with bound names replaced by their expressions."""
        values = self.values

        class _Inline(ast.NodeTransformer):
            def _read(self, node: ast.AST) -> ast.AST:
                return node if _env_read(node) is not None else self.generic_visit(node)

            visit_Call = _read
            visit_Subscript = _read

            def visit_Name(self, node: ast.Name) -> ast.AST:
                if node.id in values:
                    return copy.deepcopy(values[node.id])
                if node.id in _SAFE_CALLS:
                    return node
                raise _FailClosed(f"unbound name {node.id!r}")

        return _Inline().visit(copy.deepcopy(expr))

    def decide(self, expr: ast.AST) -> tuple[ast.AST, list[object]] | None:
        """``(inlined expression, value per combination)``, or ``None`` if undecidable.

        Bound names are inlined, then the expression is evaluated for every
        set/unset combination of the other environment variables it reads
        (:func:`_read_value`).  An unbound name, more than
        :data:`_MAX_OTHER_ENV_VARS` other variables, or a combination that
        :func:`_evaluate_once` cannot decide makes it undecidable.
        """
        try:
            inlined = self._inline(expr)
        except _FailClosed:
            return None
        others = _other_env_vars(inlined)
        if len(others) > _MAX_OTHER_ENV_VARS:
            return None
        results: list[object] = []
        for bits in itertools.product((False, True), repeat=len(others)):
            ok, value = _evaluate_once(inlined, dict(zip(others, bits)))
            if not ok:
                return None
            results.append(value)
        return inlined, results

    def _reject(self, condition: ast.AST, why: str) -> None:
        if self._recording:
            self.rejected.setdefault(ast.unparse(condition), why)

    def condition_gates(self, condition: ast.AST) -> bool:
        """Reads the gate and is true -- skips -- when the gate is unset."""
        if not self.references_gate(condition):
            if self.mentions_gate(condition):
                self._reject(
                    condition,
                    "names the gate but does not read it with a literal key",
                )
            return False
        decided = self.decide(condition)
        if decided is None:
            self._reject(condition, "cannot be evaluated")
            return False
        _inlined, results = decided
        if not all(results):
            self._reject(condition, "does not skip when the gate is unset")
            return False
        return True

    def mark_gates(self, node: ast.AST) -> bool:
        """A ``skipif`` mark, a list of marks, or a name bound to one, that gates."""
        if isinstance(node, ast.Name):
            return node.id in self.marks
        if isinstance(node, (ast.List, ast.Tuple)):
            return any(self.mark_gates(element) for element in node.elts)
        if isinstance(node, ast.Call) and _callee_name(node) == "skipif":
            conditions = list(node.args[:1]) + [
                kw.value for kw in node.keywords if kw.arg == "condition"
            ]
            return any(self.condition_gates(c) for c in conditions)
        return False

    def if_skip_gates(self, node: ast.AST) -> bool:
        """``if <condition>: ... skip(...)`` whose condition gates."""
        return (
            isinstance(node, ast.If)
            and any(
                isinstance(sub, ast.Call) and _callee_name(sub) == "skip"
                for stmt in node.body
                for sub in ast.walk(stmt)
            )
            and self.condition_gates(node.test)
        )


def module_is_gated(tree: ast.AST) -> bool:
    ctx = _GateContext(tree)
    return any(
        (isinstance(node, ast.Call) and ctx.mark_gates(node)) or ctx.if_skip_gates(node)
        for node in ast.walk(tree)
    )


def gate_hint(tree: ast.AST) -> str:
    """Failure-message text: rejected gate conditions, then the accepted shapes."""
    ctx = _GateContext(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            ctx.mark_gates(node)
        else:
            ctx.if_skip_gates(node)
    text = ""
    if ctx.rejected:
        text += f"\nConditions that mention {GATE} but were not accepted as a gate:\n"
        text += "\n".join(f"  {cond}   -- {why}" for cond, why in ctx.rejected.items())
    text += "\nAccepted gate shapes:\n" + "\n".join(f"  {s}" for s in ACCEPTED_GATE_SHAPES)
    return text


def config_writes(tree: ast.AST, vocabulary: set[str]) -> set[str]:
    return _called_names(tree) & vocabulary


#: Calls whose first argument is an environment key they set.
_KEY_SETTERS = frozenset({"setdefault", "putenv", "setenv", "__setitem__"})


def gate_stores(tree: ast.AST) -> list[str]:
    """Every place the module sets ``U64_ALLOW_MUTATE`` itself, anywhere in the file.

    A module that arms the gate skips on it and writes anyway, and an
    import-time store leaks into every later module in the run.  Recognised:
    a subscript store on ``environ`` keyed with the gate; ``setdefault`` /
    ``putenv`` / ``setenv`` (``monkeypatch.setenv`` included) / ``__setitem__``
    with the gate as the first argument; ``update`` with the gate as a
    dict-literal key or a keyword.  Only the key counts -- the gate's name as
    a *value* is not a store.
    """
    found: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and _is_environ(node.value)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == GATE
        ):
            found.append(f"line {node.lineno}: {ast.unparse(node)} = ...")
        elif isinstance(node, ast.Call):
            name = _callee_name(node)
            first = node.args[0] if node.args else None
            if (
                name in _KEY_SETTERS
                and isinstance(first, ast.Constant)
                and first.value == GATE
            ):
                found.append(f"line {node.lineno}: {ast.unparse(node)}")
            elif name == "update" and (
                any(
                    isinstance(arg, ast.Dict)
                    and any(isinstance(k, ast.Constant) and k.value == GATE for k in arg.keys)
                    for arg in node.args
                )
                or any(kw.arg == GATE for kw in node.keywords)
            ):
                found.append(f"line {node.lineno}: {ast.unparse(node)}")
    return found


def gate_offence(source: str, name: str, vocabulary: set[str]) -> set[str]:
    """The module's offences; empty when it is fine.

    Its config writes, when it never skips on the gate; and, whatever it
    skips on, ``"sets U64_ALLOW_MUTATE"`` plus its writes when it sets the
    gate itself (:func:`gate_stores`).
    """
    tree = ast.parse(source, filename=name)
    writes = config_writes(tree, vocabulary)
    offence: set[str] = set()
    if writes and not module_is_gated(tree):
        offence |= writes
    if gate_stores(tree):
        offence |= writes | {f"sets {GATE}"}
    return offence


def _pytestmark_gated(body: list[ast.stmt], ctx: _GateContext) -> bool:
    return any(
        isinstance(stmt, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets)
        and ctx.mark_gates(stmt.value)
        for stmt in body
    )


def _skips_on_gate(fn: ast.AST, ctx: _GateContext) -> bool:
    return any(ctx.if_skip_gates(node) for node in ast.walk(fn))


def _is_fixture(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = (
            target.attr if isinstance(target, ast.Attribute)
            else getattr(target, "id", None)
        )
        if name == "fixture":
            return True
    return False


def _param_names(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    args = fn.args
    return {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}


def _gated_fixtures(tree: ast.Module, ctx: _GateContext) -> set[str]:
    """Module-level fixtures that skip on the gate, or request one that does."""
    fixtures = [
        n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _is_fixture(n)
    ]
    gated = {f.name for f in fixtures if _skips_on_gate(f, ctx)}
    changed = True
    while changed:
        changed = False
        for f in fixtures:
            if f.name not in gated and _param_names(f) & gated:
                gated.add(f.name)
                changed = True
    return gated


def ungated_tests_that_write(tree: ast.Module, vocabulary: set[str]) -> list[str]:
    """``test_*`` functions that call a config writer and are not gated themselves."""
    ctx = _GateContext(tree)
    # A module that sets the gate itself is covered by none of its skips.
    arms_gate = bool(gate_stores(tree))
    if _pytestmark_gated(tree.body, ctx) and not arms_gate:
        return []
    gated_fixtures = _gated_fixtures(tree, ctx)
    offenders: list[str] = []

    def visit(node: ast.AST, inherited: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(
                    child,
                    inherited
                    or any(ctx.mark_gates(d) for d in child.decorator_list)
                    or _pytestmark_gated(child.body, ctx),
                )
            elif (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name.startswith("test")
            ):
                writes = sorted(_called_names(child) & vocabulary)
                covered = not arms_gate and (
                    inherited
                    or any(ctx.mark_gates(d) for d in child.decorator_list)
                    or _skips_on_gate(child, ctx)
                    or bool(_param_names(child) & gated_fixtures)
                )
                if writes and not covered:
                    offenders.append(f"{child.name} ({', '.join(writes)})")

    visit(tree, False)
    return offenders


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def vocabulary() -> set[str]:
    return config_writer_vocabulary()


def _live_modules() -> list[Path]:
    mods = sorted(TESTS_DIR.glob("test_*_live.py"))
    assert mods, "no live test modules found -- the scan is looking in the wrong place"
    return mods


@pytest.mark.parametrize("path", _live_modules(), ids=lambda p: p.name)
def test_a_live_module_that_writes_config_is_gated(path: Path, vocabulary) -> None:
    source = path.read_text()
    writes = gate_offence(source, path.name, vocabulary)
    stores = gate_stores(ast.parse(source))
    assert not writes, (
        f"{path.name}: {', '.join(sorted(writes))}. A live module that writes "
        f"device config must skip when {GATE} is unset, and must never set it "
        f"itself; an operator who has not set it expects no suite to "
        f"reconfigure the shared device (#268)."
        + (f"\nIt sets {GATE} at: " + "; ".join(stores) if stores else "")
        + gate_hint(ast.parse(source))
    )


@pytest.mark.parametrize("path", _live_modules(), ids=lambda p: p.name)
def test_every_test_that_writes_config_is_gated_itself(path: Path, vocabulary) -> None:
    """A module gate somewhere is not enough: each writing test carries one."""
    tree = ast.parse(path.read_text())
    offenders = ungated_tests_that_write(tree, vocabulary)
    assert not offenders, (
        f"{path.name}: these tests write device config but are not themselves "
        f"gated on {GATE} (module pytestmark, class or test marker, an in-body "
        f"skip, or a gated fixture they request): " + "; ".join(offenders)
        + gate_hint(tree)
    )


def test_config_primitives_match_the_client_source() -> None:
    derived = derive_config_primitives(CLIENT_PATH.read_text())
    assert derived == set(CONFIG_PRIMITIVES), (
        "Ultimate64Client's config-writing methods changed: "
        f"new {sorted(derived - CONFIG_PRIMITIVES)}, "
        f"gone {sorted(CONFIG_PRIMITIVES - derived)}"
    )


def test_the_vocabulary_reaches_the_helpers(vocabulary) -> None:
    missing = VOCABULARY_ANCHORS - vocabulary
    assert not missing, f"config-writer derivation lost {sorted(missing)}"
    assert not (set(EXCLUDED) & vocabulary)


def test_every_exclusion_is_still_needed() -> None:
    """An exclusion must exist in src and must reach the vocabulary."""
    calls = _src_calls(SRC_DIR)
    without = config_writer_vocabulary(excluded={})
    for name in EXCLUDED:
        assert name in calls, f"EXCLUDED names {name!r}, which src no longer defines"
        assert name in without, (
            f"EXCLUDED names {name!r}, which no longer reaches a config write"
        )


def test_the_corpus_scan_is_not_vacuous(vocabulary) -> None:
    """Most of the live corpus writes config; a scan that sees none is broken."""
    writing = [
        p.name for p in _live_modules()
        if config_writes(ast.parse(p.read_text()), vocabulary)
    ]
    gated = [
        p.name for p in _live_modules() if module_is_gated(ast.parse(p.read_text()))
    ]
    assert len(writing) >= 20, writing
    assert len(gated) >= 17, gated


class TestTheScannerItselfCanFail:
    """A clean sweep is only evidence if the sweep can report dirt."""

    VOCAB = {"set_config_item", "enable_uci"}

    def _offence(self, source: str) -> set[str]:
        return gate_offence(source, "synthetic_live.py", self.VOCAB)

    def test_ungated_write_is_flagged(self) -> None:
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(not os.environ.get('U64_HOST'), reason='x')\n"
            "def test_x(client):\n"
            "    client.set_config_item('C', 'I', 'V')\n"
        )
        assert self._offence(src) == {"set_config_item"}

    def test_ungated_helper_write_is_flagged(self) -> None:
        src = "def test_x(client):\n    enable_uci(client)\n"
        assert self._offence(src) == {"enable_uci"}

    def test_docstring_mention_is_not_a_gate(self) -> None:
        src = (
            '"""Needs U64_ALLOW_MUTATE=1."""\n'
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_read_that_never_skips_is_not_a_gate(self) -> None:
        src = (
            "import os\n"
            "_M = os.environ.get('U64_ALLOW_MUTATE')\n"
            "def test_x(client):\n    print(_M)\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_skipif_on_another_variable_is_not_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "_M = os.environ.get('U64_ALLOW_MUTATE')\n"
            "pytestmark = pytest.mark.skipif(not os.environ.get('UCI_UDP_LIVE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_direct_skipif_is_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    os.environ.get('U64_ALLOW_MUTATE') != '1', reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    def test_bound_name_skipif_is_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "_MUTATE = os.environ.get('U64_ALLOW_MUTATE') == '1'\n"
            "needs = pytest.mark.skipif(not _MUTATE, reason='x')\n"
            "@needs\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    def test_fixture_skip_is_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "_HOST = os.environ.get('U64_HOST')\n"
            "_MUTATE = os.environ['U64_ALLOW_MUTATE']\n"
            "@pytest.fixture\n"
            "def client():\n"
            "    if not (_HOST and _MUTATE):\n"
            "        pytest.skip('x')\n"
            "def test_x(client):\n    client.set_config_item('C', 'I', 'V')\n"
        )
        assert self._offence(src) == set()

    def test_an_inverted_skipif_is_not_a_gate(self) -> None:
        """Skips when the gate is SET and writes when it is unset (#268's hazard)."""
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    os.environ.get('U64_ALLOW_MUTATE') == '1', reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_skipif_that_can_never_skip_is_not_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    False and os.environ.get('U64_ALLOW_MUTATE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_an_inverted_bound_name_is_not_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "_ON = os.environ.get('U64_ALLOW_MUTATE') == '1'\n"
            "pytestmark = pytest.mark.skipif(_ON, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_an_inverted_fixture_skip_is_not_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "_M = os.environ.get('U64_ALLOW_MUTATE')\n"
            "@pytest.fixture\n"
            "def client():\n"
            "    if _M:\n"
            "        pytest.skip('x')\n"
            "def test_x(client):\n    client.set_config_item('C', 'I', 'V')\n"
        )
        assert self._offence(src) == {"set_config_item"}

    def test_an_unevaluable_condition_is_not_a_gate(self) -> None:
        """Fail closed: a condition the scanner cannot evaluate proves nothing."""
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not helper(os.environ.get('U64_ALLOW_MUTATE')), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_bool_bound_gate_and_a_compound_condition_are_gates(self) -> None:
        src = (
            "import os, pytest\n"
            "_HOST = os.environ.get('U64_HOST')\n"
            "_A = bool(os.environ.get('U64_ALLOW_MUTATE'))\n"
            "pytestmark = pytest.mark.skipif(not (_HOST and _A), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    def test_code_outside_the_grammar_is_never_evaluated(self) -> None:
        """A lambda would evaluate cleanly to a skip; it must be refused unrun."""
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not (lambda: os.environ.get('U64_ALLOW_MUTATE'))(), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_attribute_access_is_never_evaluated(self) -> None:
        """``().__class__`` would evaluate to a truthy skip; the grammar refuses it.

        Attribute access is how an ``eval`` with empty builtins is escaped, so
        the grammar check is a sandbox, not only a shape filter.
        """
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not os.environ.get('U64_ALLOW_MUTATE') or ().__class__, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_other_requirements_are_assumed_met(self) -> None:
        """``_HOST and not _A`` skips on an unset gate once a host is configured."""
        src = (
            "import os, pytest\n"
            "_HOST = os.environ.get('U64_HOST')\n"
            "_A = os.environ.get('U64_ALLOW_MUTATE')\n"
            "pytestmark = pytest.mark.skipif(_HOST and not _A, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    @pytest.mark.parametrize("condition", [
        "not os.environ.get('U64_ALLOW_MUTATE', '1')",
        "not os.getenv('U64_ALLOW_MUTATE', 'yes')",
        "not os.environ.get('U64_ALLOW_MUTATE', '0')",
        "not os.getenv('U64_ALLOW_MUTATE', default='1')",
        "not os.environ.get('U64_ALLOW_MUTATE', FALLBACK)",
    ])
    def test_a_read_with_a_default_skips_only_if_the_default_does(self, condition) -> None:
        """The default is what an unset gate reads as; an unknown default fails closed."""
        src = (
            "import os, pytest\n"
            f"pytestmark = pytest.mark.skipif({condition}, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_default_that_still_skips_is_a_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    os.environ.get('U64_ALLOW_MUTATE', '0') != '1', reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    def test_a_name_bound_to_a_false_literal_is_not_assumed_set(self) -> None:
        """P6: ``_OFF = False`` makes ``_OFF and not <gate>`` never skip."""
        src = (
            "import os, pytest\n"
            "_OFF = False\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    _OFF and not os.environ.get('U64_ALLOW_MUTATE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    @pytest.mark.parametrize("rebinding", ["_M = '1'", "_M = compute()"])
    def test_a_rebound_gate_name_is_no_longer_the_gate(self, rebinding) -> None:
        """P7: a later rebinding replaces the gate read (literal) or fails closed."""
        src = (
            "import os, pytest\n"
            "_M = os.environ.get('U64_ALLOW_MUTATE')\n"
            f"{rebinding}\n"
            "pytestmark = pytest.mark.skipif(not _M, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_name_rebound_to_a_literal_no_longer_reads_the_gate(self) -> None:
        """``_M = ''`` after the gate read: ``not _M`` always skips, the gate decides nothing."""
        src = (
            "import os, pytest\n"
            "_M = os.environ.get('U64_ALLOW_MUTATE')\n"
            "_M = ''\n"
            "pytestmark = pytest.mark.skipif(not _M, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_another_env_requirement_is_assumed_unset(self) -> None:
        """P8: ``not <gate> and os.environ.get('CI')`` does not skip outside CI."""
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not os.environ.get('U64_ALLOW_MUTATE') and os.environ.get('CI'),\n"
            "    reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_multi_gate_or_condition_is_a_gate(self) -> None:
        """The legitimate multi-gate form: skip if any requirement is missing."""
        src = (
            "import os, pytest\n"
            "_LIVE = os.environ.get('RRNET_LIVE')\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not _LIVE or not os.environ.get('U64_ALLOW_MUTATE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    def test_an_error_inside_the_grammar_is_not_a_gate(self) -> None:
        """Q5: ``int(None)`` raises; an evaluation that errors proves nothing."""
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not int(os.environ.get('U64_ALLOW_MUTATE')), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_an_unbound_name_fails_closed(self) -> None:
        """An imported or undefined flag might be False: it is not assumed set."""
        src = (
            "import os, pytest\n"
            "from somewhere import FLAG\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    FLAG and not os.environ.get('U64_ALLOW_MUTATE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    @pytest.mark.parametrize("condition, why", [
        ("not helper(os.environ.get('U64_ALLOW_MUTATE'))", "cannot be evaluated"),
        ("os.environ.get('U64_ALLOW_MUTATE', '').lower() != '1'", "cannot be evaluated"),
        ("'U64_ALLOW_MUTATE' not in os.environ",
         "names the gate but does not read it with a literal key"),
        ("not os.environ.get(_VAR)",
         "names the gate but does not read it with a literal key"),
        ("os.environ.get('U64_ALLOW_MUTATE') == '1'", "does not skip when the gate is unset"),
    ])
    def test_a_rejected_condition_is_named_with_the_accepted_shapes(
        self, condition: str, why: str
    ) -> None:
        src = (
            "import os, pytest\n"
            "_VAR = 'U64_ALLOW_MUTATE'\n"
            f"pytestmark = pytest.mark.skipif({condition}, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}
        hint = gate_hint(ast.parse(src))
        assert "were not accepted as a gate" in hint
        assert f"{ast.unparse(ast.parse(condition, mode='eval').body)}   -- {why}" in hint
        assert "skipif(not os.environ.get('U64_ALLOW_MUTATE'), reason=...)" in hint

    def test_a_gated_module_has_no_rejected_conditions(self) -> None:
        src = (
            "import os, pytest\n"
            "_HOST = os.environ.get('U64_HOST')\n"
            "_MUTATE = os.environ.get('U64_ALLOW_MUTATE')\n"
            "pytestmark = [pytest.mark.skipif(not _HOST, reason='x'),\n"
            "              pytest.mark.skipif(not _MUTATE, reason='y')]\n"
            "def test_x(client):\n"
            "    if _MUTATE:\n        print('guard, not a skip')\n"
            "    enable_uci(client)\n"
        )
        hint = gate_hint(ast.parse(src))
        assert "not accepted" not in hint
        assert "Accepted gate shapes" in hint

    @pytest.mark.parametrize("condition", [
        "not _LIVE or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "not os.environ.get('X_LIVE') or os.environ.get('U64_ALLOW_MUTATE')",
    ], ids=["R4-bound", "R4b-inline"])
    def test_an_inverted_gate_inside_a_multi_gate_or_is_not_a_gate(self, condition) -> None:
        """With X_LIVE set and the gate unset these do not skip, so the module writes."""
        src = (
            "import os, pytest\n"
            "_LIVE = os.environ.get('X_LIVE')\n"
            f"pytestmark = pytest.mark.skipif({condition}, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    @pytest.mark.parametrize("count, offence", [(6, set()), (7, {"enable_uci"})])
    def test_more_other_env_vars_than_the_cap_fail_closed(self, count, offence) -> None:
        """Six other variables are enumerated (64 combinations); a seventh fails closed."""
        others = " or ".join(f"not os.environ.get('X{i}')" for i in range(count))
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            f"    not os.environ.get('U64_ALLOW_MUTATE') or {others}, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == offence

    @pytest.mark.parametrize("binding", [
        "_M = os.environ.get('U64_ALLOW_MUTATE')\n_M, _Z = '1', 0\n",
        "_M = '1'\ndef helper():\n    _M = os.environ.get('U64_ALLOW_MUTATE')\n",
        "_M = os.environ.get('U64_ALLOW_MUTATE')\nfrom string import digits as _M\n",
        "_M = os.environ.get('U64_ALLOW_MUTATE')\n_Y = (_M := '1')\n",
        "_M = os.environ.get('U64_ALLOW_MUTATE')\n_M += '1'\n",
        "_M = os.environ.get('U64_ALLOW_MUTATE')\nfor _M in ['1']:\n    pass\n",
        "_M = os.environ.get('U64_ALLOW_MUTATE')\ndef _M():\n    pass\n",
        "_M = os.environ.get('U64_ALLOW_MUTATE')\ndef helper(_M='1'):\n    pass\n",
    ], ids=["R5-tuple", "R6-function-local", "R7-import-alias", "R8-walrus",
            "R9-augassign", "R10-for-target", "def-name", "parameter"])
    def test_a_name_stored_any_other_way_fails_closed(self, binding) -> None:
        """Only a single-Name module-level assignment binds; any other store of the name fails closed."""
        src = (
            "import os, pytest\n"
            f"{binding}"
            "pytestmark = pytest.mark.skipif(not _M, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_a_falsy_flag_rebound_to_something_undecidable_fails_closed(self) -> None:
        """S6: after ``_OFF = compute()`` nothing is known about ``_OFF``."""
        src = (
            "import os, pytest\n"
            "_OFF = False\n"
            "_OFF = compute()\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    _OFF or not os.environ.get('U64_ALLOW_MUTATE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    @pytest.mark.parametrize("store", [
        "os.environ['U64_ALLOW_MUTATE'] = '1'\n",
        "os.environ.setdefault('U64_ALLOW_MUTATE', '1')\n",
        "os.environ.update({'U64_ALLOW_MUTATE': '1'})\n",
        "os.putenv('U64_ALLOW_MUTATE', '1')\n",
        "@pytest.fixture(autouse=True)\ndef _arm(monkeypatch):\n"
        "    monkeypatch.setenv('U64_ALLOW_MUTATE', '1')\n",
    ], ids=["C4-subscript", "C5-setdefault", "update", "putenv", "monkeypatch-setenv"])
    def test_a_module_that_sets_the_gate_itself_is_an_offence(self, store) -> None:
        """It skips on the gate but arms it first, so it writes on every run."""
        src = (
            "import os, pytest\n"
            f"{store}"
            "pytestmark = pytest.mark.skipif(\n"
            "    not os.environ.get('U64_ALLOW_MUTATE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci", "sets U64_ALLOW_MUTATE"}

    def test_setting_the_gate_is_an_offence_even_without_a_config_write(self) -> None:
        """Arming the gate at import leaks into every later module in the run."""
        src = (
            "import os\n"
            "os.environ['U64_ALLOW_MUTATE'] = '1'\n"
            "def test_x(client):\n    client.get_config_item('C', 'I')\n"
        )
        assert self._offence(src) == {"sets U64_ALLOW_MUTATE"}

    def test_the_gate_name_as_a_value_is_not_setting_the_gate(self) -> None:
        src = (
            "import os, pytest\n"
            "os.environ['X_LIVE'] = 'U64_ALLOW_MUTATE'\n"
            "os.environ.setdefault('X_OTHER', 'U64_ALLOW_MUTATE')\n"
            "pytestmark = pytest.mark.skipif(\n"
            "    not os.environ.get('U64_ALLOW_MUTATE'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    @pytest.mark.xfail(
        strict=True,
        reason="#353: an unknown env var is tried only unset/default and as '1', "
        "so a comparison against another literal escapes",
    )
    @pytest.mark.parametrize("condition", [
        "os.environ.get('X_LIVE') != 'yes' or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "not os.environ.get('U64_ALLOW_MUTATE') and os.environ.get('X_LIVE') != 'yes'",
    ], ids=["353-or", "353-and"])
    def test_a_comparison_against_another_literal_is_not_a_gate(self, condition) -> None:
        """With X_LIVE='yes' and the gate unset neither skips, so the module writes."""
        src = (
            "import os, pytest\n"
            f"pytestmark = pytest.mark.skipif({condition}, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    def test_read_only_module_needs_no_gate(self) -> None:
        src = "def test_x(client):\n    client.get_config_item('C', 'I')\n"
        assert self._offence(src) == set()

    def test_primitive_derivation_sees_a_config_put_and_ignores_a_get(self) -> None:
        src = (
            "class Ultimate64Client:\n"
            "    def w(self, c):\n"
            "        self._put_no_body(f'/v1/configs/{c}', query={})\n"
            "    def r(self, c):\n"
            "        return self._get_json(f'/v1/configs/{c}')\n"
            "    def p(self):\n"
            "        self._request('POST', '/v1/configs', body=b'')\n"
            "    def other(self):\n"
            "        self._put_no_body('/v1/machine:reset')\n"
        )
        assert derive_config_primitives(src) == {"w", "p"}

    def test_exclusion_stops_propagation(self, tmp_path: Path) -> None:
        (tmp_path / "m.py").write_text(
            "def set_config_item(): pass\n"
            "def _hyg():\n    set_config_item()\n"
            "def request():\n    _hyg()\n"
            "def helper():\n    set_config_item()\n"
        )
        prims = frozenset({"set_config_item"})
        assert config_writer_vocabulary(tmp_path, prims, excluded={}) == {
            "set_config_item", "_hyg", "request", "helper",
        }
        assert config_writer_vocabulary(tmp_path, prims, excluded={"_hyg": ""}) == {
            "set_config_item", "helper",
        }


class TestTheTestLevelScannerCanFail:
    """The per-test rule reports a writing test that lost its own gate."""

    VOCAB = {"set_config_item", "set_speed"}
    HEAD = (
        "import os, pytest\n"
        "_M = os.environ.get('U64_ALLOW_MUTATE')\n"
        "needs = pytest.mark.skipif(not _M, reason='x')\n"
    )

    def _offenders(self, body: str) -> list[str]:
        return ungated_tests_that_write(ast.parse(self.HEAD + body), self.VOCAB)

    def test_a_writing_test_without_its_marker_is_flagged(self) -> None:
        body = (
            "@needs\n"
            "def test_a(t):\n    t.set_speed(4)\n"
            "def test_b(t):\n    t.set_config_item('C', 'I', 'V')\n"
        )
        assert self._offenders(body) == ["test_b (set_config_item)"]

    def test_a_method_of_an_unmarked_class_is_flagged(self) -> None:
        body = "class TestX:\n    def test_a(self, t):\n        t.set_speed(4)\n"
        assert self._offenders(body) == ["test_a (set_speed)"]

    def test_a_read_only_test_needs_no_marker(self) -> None:
        assert self._offenders("def test_a(t):\n    t.get_speed()\n") == []

    def test_class_decorator_covers_its_methods(self) -> None:
        body = "@needs\nclass TestX:\n    def test_a(self, t):\n        t.set_speed(4)\n"
        assert self._offenders(body) == []

    def test_class_pytestmark_covers_its_methods(self) -> None:
        body = (
            "class TestX:\n    pytestmark = needs\n"
            "    def test_a(self, t):\n        t.set_speed(4)\n"
        )
        assert self._offenders(body) == []

    def test_module_pytestmark_covers_every_test(self) -> None:
        body = "pytestmark = [needs]\ndef test_a(t):\n    t.set_speed(4)\n"
        assert self._offenders(body) == []

    def test_in_body_skip_covers_the_test(self) -> None:
        body = "def test_a(t):\n    if not _M:\n        pytest.skip('x')\n    t.set_speed(4)\n"
        assert self._offenders(body) == []

    def test_a_requested_gated_fixture_covers_the_test_transitively(self) -> None:
        body = (
            "@pytest.fixture\n"
            "def mutable():\n    if not _M:\n        pytest.skip('x')\n    yield 1\n"
            "@pytest.fixture\n"
            "def speed(mutable):\n    yield mutable\n"
            "def test_a(speed, t):\n    t.set_speed(4)\n"
            "def test_b(t):\n    t.set_speed(4)\n"
        )
        assert self._offenders(body) == ["test_b (set_speed)"]

    def test_an_inverted_marker_does_not_cover_the_test(self) -> None:
        body = (
            "inverted = pytest.mark.skipif(_M == '1', reason='y')\n"
            "@inverted\n"
            "def test_a(t):\n    t.set_speed(4)\n"
        )
        assert self._offenders(body) == ["test_a (set_speed)"]

    def test_an_inverted_in_body_skip_does_not_cover_the_test(self) -> None:
        body = "def test_a(t):\n    if _M:\n        pytest.skip('x')\n    t.set_speed(4)\n"
        assert self._offenders(body) == ["test_a (set_speed)"]

    def test_an_inverted_module_pytestmark_does_not_cover_the_tests(self) -> None:
        body = (
            "pytestmark = pytest.mark.skipif(bool(_M), reason='y')\n"
            "def test_a(t):\n    t.set_speed(4)\n"
        )
        assert self._offenders(body) == ["test_a (set_speed)"]

    def test_a_mark_name_rebound_to_a_non_gate_does_not_cover_the_test(self) -> None:
        """S9: ``needs`` rebound to ``skipif(False)`` before ``@needs`` gates nothing."""
        body = (
            "needs = pytest.mark.skipif(False, reason='y')\n"
            "@needs\n"
            "def test_a(t):\n    t.set_speed(4)\n"
        )
        assert self._offenders(body) == ["test_a (set_speed)"]

    def test_a_module_that_sets_the_gate_covers_no_test(self) -> None:
        body = (
            "os.environ['U64_ALLOW_MUTATE'] = '1'\n"
            "@needs\n"
            "def test_a(t):\n    t.set_speed(4)\n"
        )
        assert self._offenders(body) == ["test_a (set_speed)"]

    def test_an_ungated_fixture_does_not_cover_the_test(self) -> None:
        body = (
            "@pytest.fixture\n"
            "def plain():\n    yield 1\n"
            "def test_a(plain, t):\n    t.set_speed(4)\n"
        )
        assert self._offenders(body) == ["test_a (set_speed)"]


# --------------------------------------------------------------------------- #
# Command Interface restore in the UCI modules (#268)                         #
# --------------------------------------------------------------------------- #

UCI_MODULES = (
    "test_uci_udp_send_live.py",
    "test_uci_udp_send_large_live.py",
    "test_uci_tcp_echo_live.py",
)


def _load_live_module(filename: str):
    """Import a live module under a private name; its tests are not collected."""
    import importlib.util

    path = TESTS_DIR / filename
    spec = importlib.util.spec_from_file_location(
        f"_mutation_gate_probe_{path.stem}", path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _RecordingClient:
    def __init__(self, fail: bool = False) -> None:
        self.writes: list[tuple[str, dict]] = []
        self.fail = fail

    def set_config_items(self, category: str, updates: dict) -> None:
        self.writes.append((category, dict(updates)))
        if self.fail:
            raise RuntimeError("PUT refused")


@pytest.mark.parametrize("filename", UCI_MODULES)
class TestCommandInterfaceRestore:
    """Restore to the value read first, not to a fixed ``Disabled``."""

    @pytest.mark.parametrize("prior, value", [(True, "Enabled"), (False, "Disabled")])
    def test_restores_the_value_read_before_the_write(
        self, filename: str, prior: bool, value: str
    ) -> None:
        client = _RecordingClient()
        _load_live_module(filename)._restore_command_interface(client, prior)
        assert client.writes == [
            ("C64 and Cartridge Settings", {"Command Interface": value})
        ]

    def test_a_failed_restore_is_reported_not_raised(
        self, filename: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        client = _RecordingClient(fail=True)
        _load_live_module(filename)._restore_command_interface(client, False)
        assert client.writes, "the restore was never attempted"
        assert "NOT restored" in capsys.readouterr().out

    def test_prior_is_read_first_and_restored_before_the_lock_is_released(
        self, filename: str
    ) -> None:
        tree = ast.parse((TESTS_DIR / filename).read_text())
        (test_fn,) = [f for f in _functions(tree) if f.name.startswith("test_")]

        def first_call(name: str) -> int:
            lines = [
                n.lineno for n in ast.walk(test_fn)
                if isinstance(n, ast.Call) and _callee_name(n) == name
            ]
            assert lines, f"{filename}: {name}() not called in {test_fn.name}"
            return min(lines)

        assert first_call("get_uci_enabled") < first_call("enable_uci")

        restoring = [
            t for t in ast.walk(test_fn)
            if isinstance(t, ast.Try)
            and any("_restore_command_interface" in _called_names(s) for s in t.finalbody)
        ]
        assert restoring, f"{filename}: no finally restores the Command Interface"
        inner = [
            t for outer in restoring for s in outer.finalbody for t in ast.walk(s)
            if isinstance(t, ast.Try)
            and any("release" in _called_names(f) for f in t.finalbody)
        ]
        assert inner, (
            f"{filename}: the lock release is not in a finally beneath the "
            "restore, so a restore that raises would leak the DeviceLock"
        )

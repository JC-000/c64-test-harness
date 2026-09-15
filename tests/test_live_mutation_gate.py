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
  and the condition must skip for **every** combination of candidate values
  of those it reads.  A variable's candidates are: unset (its literal
  default), ``"1"``, the empty string when a read of it has a non-empty
  literal default (#374), every literal the condition compares a read of it
  against -- strings, integers as their string, and the elements of an
  ``in`` container -- and, when there is at least one such literal, one
  value equal to none of them (#353).  At most 64 combinations are
  enumerated (six set/unset variables); more fails closed.  So
  ``not <gate> and get('CI')``, ``not get('X_LIVE') or get(GATE)``,
  ``get('X_LIVE') != 'yes' or get(GATE) == '1'`` and
  ``not get('X_LIVE', '1') or get(GATE) == '1'`` are refused while the
  multi-gate ``not _LIVE or not <gate>`` stays a gate;
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

**Which modules are scanned** (#375): every ``test_*_live.py``, plus any
``tests/**/test_*.py`` that reads ``U64_HOST`` from the environment -- at
import time always, and anywhere else unless the module supplies its own
``U64_HOST`` (``monkeypatch.setenv``/``setitem``, ``mock.patch.dict``,
``putenv``, ``setdefault``, an ``os.environ`` store).  Removing the variable
(``delenv``, ``delitem``, ``unsetenv``, ``pop``, ``del``) supplies nothing --
it means "no host" and cannot make a read elsewhere in the file safe -- so it
does not exempt a module (#396); nor does storing a blank literal -- ``""``,
whitespace such as ``" "``, or an f-string of only such text (``f''``) --
which the harness strips and reads as "no host" too (#411).  Literals only:
a blank value the scanner would have to compute still counts as a supply --
``str()``, a name bound to ``""``, a for-loop target
(``for os.environ['U64_HOST'] in ['']``), a tuple unpack
(``os.environ['U64_HOST'], x = '', 1``), an augmented store (``+= ''``), and a
dict literal that repeats the key with a real value before a blank one.  The gate
protects the device the operator named, and a test reaches that device only
through the ``U64_HOST`` the operator exported: a module that sets the
variable itself is driving the harness against a host it chose
(``test_device_lock_advisory.py`` patches ``urlopen`` and sets a fake
``U64_HOST``), whereas an import-time read happens before any of its own
patches run, so it still counts.  Constructing an ``Ultimate64Client`` or
``Ultimate64Transport`` does not select a module by itself: two dozen unit
tests do that against documentation-range addresses with the network
mocked, and a real device address written into a test is refused on its own
by ``tests/test_u64_runner_script_gates.py``.

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
* Module selection is by name as well: a module outside ``test_*_live.py``
  that reads the host through an alias or a helper is not scanned (#375).
  The supply check is **file-wide**: a module that reads the operator's host
  inside one function while *setting* ``U64_HOST`` (not removing it) in
  another is not scanned either (#396 narrowed this to stores; scoping it to
  the reading function would newly select ``test_device_lock_advisory.py``,
  whose read and store sit in different tests).
* A rebinding through ``globals()['_M'] = '1'`` (or ``setattr`` on the
  module) is not seen by the binder.
"""
from __future__ import annotations

import ast
import copy
import itertools
import math
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

#: Most other environment variables a condition may combine with the gate,
#: when each is tried only unset and set -- the cap below is ``2 ** 6``.
_MAX_OTHER_ENV_VARS = 6

#: Most combinations of candidate values a condition is evaluated under
#: (:func:`_candidate_values`); above this it fails closed rather than
#: enumerate.  Literal candidates count against the same cap (#353).
_MAX_COMBINATIONS = 2 ** _MAX_OTHER_ENV_VARS

#: A variable's "unset" candidate: it reads as its default.
_UNSET = object()


class _FailClosed(Exception):
    """A condition, or a binding it depends on, cannot be decided."""


def _read_value(read: tuple[str, ast.AST | None], env: dict[str, object]) -> object:
    """What an environment read evaluates to for one combination *env*.

    ``U64_HOST`` reads as set, and a variable *env* gives a value reads as
    that value.  Otherwise -- always, for the gate -- it reads as its default:
    ``None`` when there is none, the literal when the default is one, and a
    non-literal default fails closed.
    """
    variable, default = read
    if variable in _ASSUMED_SET:
        return "set"
    value = env.get(variable, _UNSET)
    if value is not _UNSET:
        return value
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


def _literal_strings(node: ast.AST) -> list[str]:
    """The values an environment variable would need to equal *node*.

    A string literal is itself, an integer literal its string (``bool``
    excluded), and a tuple, list or set of literals each element.
    """
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, str):
            return [value]
        if isinstance(value, int) and not isinstance(value, bool):
            return [str(value)]
        return []
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return [s for element in node.elts for s in _literal_strings(element)]
    return []


#: Prefix of the candidate that equals none of a variable's literals.
_NONE_OF_THE_LITERALS = "\x00none-of-the-literals"


def _candidate_values(expr: ast.AST) -> dict[str, list[object]]:
    """Per other environment variable *expr* reads, the values it is tried as.

    Unset (:data:`_UNSET`, so it reads as its default), ``"1"``, the empty
    string when some read of the variable has a non-empty literal default --
    unset then reads truthy, and set-but-empty is the value that reads falsy
    without ever being compared, so ``not get('X', '1') or ...`` is tried
    false (#374) -- then every literal a ``Compare`` sets against an operand
    that reads the variable (:func:`_literal_strings`, so ``in`` containers
    count), and -- when there is at least one such literal -- one value equal
    to none of them, so ``get('X', 'a') in ('a', '1')`` is also tried false
    (#353).
    """
    candidates: dict[str, list[object]] = {
        variable: [_UNSET, "1"] for variable in _other_env_vars(expr)
    }
    for node in ast.walk(expr):
        read = _env_read(node)
        if (
            read is not None
            and read[0] in candidates
            and isinstance(read[1], ast.Constant)
            and read[1].value not in (None, "")
            and "" not in candidates[read[0]]
        ):
            candidates[read[0]].append("")
    literals: dict[str, list[str]] = {variable: [] for variable in candidates}
    for node in ast.walk(expr):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        for i, operand in enumerate(operands):
            read_here = {
                read[0] for sub in ast.walk(operand)
                if (read := _env_read(sub)) is not None
            }
            for variable in read_here & candidates.keys():
                for j, other in enumerate(operands):
                    if j != i:
                        literals[variable] += _literal_strings(other)
    for variable, found in literals.items():
        for literal in found:
            if literal not in candidates[variable]:
                candidates[variable].append(literal)
        if found:
            other = _NONE_OF_THE_LITERALS
            while other in found:
                other += "'"
            candidates[variable].append(other)
    return candidates


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
    "other environment variable except U64_HOST may be unset, '1', set empty "
    "(when its read has a non-empty default), or any "
    "value the condition compares it with, and the condition must skip in "
    "every combination; bind names with a plain "
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
        combination of candidate values of the other environment variables it
        reads (:func:`_candidate_values`, :func:`_read_value`).  An unbound
        name, more than :data:`_MAX_COMBINATIONS` combinations, or a
        combination that :func:`_evaluate_once` cannot decide makes it
        undecidable.
        """
        try:
            inlined = self._inline(expr)
        except _FailClosed:
            return None
        candidates = _candidate_values(inlined)
        if math.prod(len(values) for values in candidates.values()) > _MAX_COMBINATIONS:
            return None
        results: list[object] = []
        for values in itertools.product(*candidates.values()):
            ok, value = _evaluate_once(inlined, dict(zip(candidates, values)))
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


#: The environment variable that names the device a test drives.
HOST_VAR = "U64_HOST"

#: Calls that set an environment key given as the first argument.  Removals
#: (``delenv``, ``unsetenv``, ``pop``, ``__delitem__``) are deliberately absent:
#: they supply no host (#396).
_HOST_KEY_FIRST = frozenset({"setenv", "putenv", "setdefault", "__setitem__"})
#: Calls that set a mapping key given as the second argument (``monkeypatch.setitem``;
#: ``delitem`` is a removal, absent for the same reason).
_HOST_KEY_SECOND = frozenset({"setitem"})


def _is_host_key(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value == HOST_VAR


def _reads_the_host(node: ast.AST) -> bool:
    read = _env_read(node)
    return read is not None and read[0] == HOST_VAR


def reads_the_host_at_import(tree: ast.Module) -> bool:
    """A module-level assignment whose value reads ``U64_HOST``."""
    return any(
        isinstance(stmt, (ast.Assign, ast.AnnAssign))
        and stmt.value is not None
        and any(_reads_the_host(sub) for sub in ast.walk(stmt.value))
        for stmt in tree.body
    )


def _names_a_host(value: ast.AST | None) -> bool:
    """Whether a stored value can name a host: anything but a blank literal.

    The harness strips ``U64_HOST`` before deciding there is a host
    (``tests/conftest.py`` ``env.strip()``, ``backends/unified_manager.py``
    ``h.strip()``), so storing ``""`` or ``" "`` -- or an f-string made only of
    such literal text, ``f''`` included -- supplies nothing, exactly like a
    removal (#411).  A missing or non-literal value is assumed to name one.
    """
    if isinstance(value, ast.Constant):
        return not (isinstance(value.value, str) and not value.value.strip())
    if isinstance(value, ast.JoinedStr):
        parts = value.values
        literal = all(isinstance(p, ast.Constant) and isinstance(p.value, str) for p in parts)
        return not (literal and not "".join(p.value for p in parts).strip())
    return True


def _call_value(call: ast.Call, index: int, keywords: tuple[str, ...]) -> ast.AST | None:
    """The value argument of a store call: positional *index*, else a keyword."""
    if len(call.args) > index:
        return call.args[index]
    return next((kw.value for kw in call.keywords if kw.arg in keywords), None)


def supplies_its_own_host(tree: ast.AST) -> bool:
    """The module sets ``U64_HOST`` to a host itself, anywhere in the file.

    Only a store counts.  A removal (``delenv``, ``del os.environ[...]``) says
    "no host": it cannot make a read of the operator's host in another
    function of the same file safe, so it does not exempt the module (#396).
    A store of the literal empty string says the same and is treated alike
    (#411).
    """
    # Subscript stores whose assigned value is the literal "" (plain, chained
    # or annotated assignment); every other environ subscript store supplies.
    empty_targets: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and not _names_a_host(node.value):
            empty_targets.update(id(t) for t in node.targets)
        elif isinstance(node, ast.AnnAssign) and node.value is not None and not _names_a_host(node.value):
            empty_targets.add(id(node.target))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and _is_environ(node.value)
            and _is_host_key(node.slice)
            and id(node) not in empty_targets
        ):
            return True
        if isinstance(node, ast.Call):
            name = _callee_name(node)
            args = node.args
            if (
                name in _HOST_KEY_FIRST and args and _is_host_key(args[0])
                and _names_a_host(_call_value(node, 1, ("value", "default")))
            ):
                return True
            if (
                name in _HOST_KEY_SECOND and len(args) > 1 and _is_host_key(args[1])
                and _names_a_host(_call_value(node, 2, ("value",)))
            ):
                return True
            if name in ("dict", "update") and (
                any(
                    isinstance(arg, ast.Dict)
                    and any(
                        _is_host_key(k) and _names_a_host(v)
                        for k, v in zip(arg.keys, arg.values)
                    )
                    for arg in args
                )
                or any(kw.arg == HOST_VAR and _names_a_host(kw.value) for kw in node.keywords)
            ):
                return True
    return False


def drives_a_named_device(tree: ast.Module) -> bool:
    """Whether a test module reaches the device the operator named (#375).

    An import-time read of ``U64_HOST`` always counts: it happens before any
    of the module's own patches can run.  A read anywhere else counts unless
    the module supplies its own ``U64_HOST`` -- then it is exercising the
    harness against a host it chose, not the operator's device.
    """
    if reads_the_host_at_import(tree):
        return True
    return not supplies_its_own_host(tree) and any(
        _reads_the_host(node) for node in ast.walk(tree)
    )


def _scanned_modules(root: Path = TESTS_DIR) -> list[Path]:
    """Every ``test_*_live.py`` in *root*, then every other ``test_*.py`` under
    it, recursively, that :func:`drives_a_named_device` (#375)."""
    live = sorted(root.glob("test_*_live.py"))
    floor = set(live)
    others = sorted(
        (
            p for p in root.rglob("test_*.py")
            if "__pycache__" not in p.parts
            and p not in floor
            and drives_a_named_device(ast.parse(p.read_text(), filename=str(p)))
        ),
        key=lambda p: p.relative_to(root).parts,
    )
    return live + others


def _module_id(path: Path) -> str:
    return str(path.relative_to(TESTS_DIR))


@pytest.mark.parametrize("path", _scanned_modules(), ids=_module_id)
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


@pytest.mark.parametrize("path", _scanned_modules(), ids=_module_id)
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


def test_the_scan_reaches_device_modules_outside_the_live_glob() -> None:
    """#375: ``test_bridge_ping_tod.py`` drives the U64 and writes CPU speed
    but is not named ``*_live.py``; it is scanned, and the mocked
    ``test_device_lock_advisory.py`` (sets U64_HOST itself) is not."""
    names = {str(p.relative_to(TESTS_DIR)) for p in _scanned_modules()}
    live = {p.name for p in _live_modules()}
    assert live <= names, sorted(live - names)
    assert "test_bridge_ping_tod.py" in names
    assert "test_device_lock_advisory.py" not in names
    assert "conftest.py" not in names


class TestModuleSelection:
    """#375: which test modules the gate rules are applied to."""

    @pytest.mark.parametrize("source, drives", [
        ("import os\n_H = os.environ.get('U64_HOST')\n", True),
        ("import os\nHOST = os.getenv('U64_HOST', '')\n", True),
        ("import os\ndef test_a():\n    c = Client(os.environ['U64_HOST'])\n", True),
        ("import os\n", False),
        ("NAME = 'U64_HOST'\ndef test_a():\n    return 'needs U64_HOST'\n", False),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', '10.0.0.1')\n"
         "    Client(os.environ.get('U64_HOST'))\n", False),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setitem(os.environ, 'U64_HOST', 'x')\n"
         "    os.environ.get('U64_HOST')\n", False),
        ("import os\nfrom unittest import mock\n"
         "@mock.patch.dict(os.environ, {'U64_HOST': 'x'})\ndef test_a():\n    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a():\n    os.environ['U64_HOST'] = 'x'\n    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a():\n    os.environ.setdefault('U64_HOST', 'x')\n    os.environ.get('U64_HOST')\n", False),
        ("import os\n_H = os.environ.get('U64_HOST')\n"
         "def test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', 'x')\n", True),
        # #396: removing the variable says "no host"; it supplies nothing, so it
        # cannot make a read of the operator's host elsewhere in the file safe.
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.delenv('U64_HOST', raising=False)\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.delitem(os.environ, 'U64_HOST', raising=False)\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ.pop('U64_HOST', None)\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.unsetenv('U64_HOST')\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    del os.environ['U64_HOST']\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ.__delitem__('U64_HOST')\n    os.environ.get('U64_HOST')\n", True),
        # #411: storing the literal empty string is "no host" too -- the harness
        # treats ``U64_HOST=`` as unset -- so, like a removal, it exempts nothing.
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', '')\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', value='')\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setitem(os.environ, 'U64_HOST', '')\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ['U64_HOST'] = ''\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ['U64_HOST']: str = ''\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ.setdefault('U64_HOST', '')\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.putenv('U64_HOST', '')\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ.__setitem__('U64_HOST', '')\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ.update({'U64_HOST': ''})\n    os.environ.get('U64_HOST')\n", True),
        ("import os\nfrom unittest import mock\n"
         "@mock.patch.dict(os.environ, {'U64_HOST': ''})\ndef test_a():\n    os.environ.get('U64_HOST')\n", True),
        ("import os\nfrom unittest import mock\n"
         "@mock.patch.dict(os.environ, U64_HOST='')\ndef test_a():\n    os.environ.get('U64_HOST')\n", True),
        # #441 review: the remaining recognised store forms, one case each.
        ("import os\ndef test_a():\n    os.environ.setdefault('U64_HOST', default='')\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setitem(os.environ, 'U64_HOST', value='')\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ['U64_HOST'] = y = ''\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    y = os.environ['U64_HOST'] = ''\n    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a():\n    os.environ.update(U64_HOST='')\n    os.environ.get('U64_HOST')\n", True),
        # The harness strips the host, so blank text is "no host" as well.
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', ' ')\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', f'')\n"
         "    os.environ.get('U64_HOST')\n", True),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', f' \\t')\n"
         "    os.environ.get('U64_HOST')\n", True),
        # #411 controls: a non-blank or non-literal value still supplies a host,
        # and one real store anywhere keeps the file-wide exemption.
        ("import os\ndef test_a(monkeypatch, h):\n    monkeypatch.setenv('U64_HOST', f' {h}')\n"
         "    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', f' h')\n"
         "    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', 0)\n"
         "    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a(monkeypatch, fake):\n    monkeypatch.setenv('U64_HOST', fake)\n"
         "    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a():\n    os.environ['U64_HOST'] = x = 'h'\n    os.environ.get('U64_HOST')\n", False),
        ("import os\nfrom unittest import mock\n"
         "@mock.patch.dict(os.environ, U64_HOST='h')\ndef test_a():\n    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', '')\n"
         "def test_b(monkeypatch):\n    monkeypatch.setenv('U64_HOST', 'h')\n"
         "    os.environ.get('U64_HOST')\n", False),
        ("import os\ndef test_a():\n    os.environ.update({'U64_HOST': '', 'U64_HOST': 'h'})\n"
         "    os.environ.get('U64_HOST')\n", False),
    ], ids=[
        "import-time-get", "import-time-getenv", "in-test-subscript", "no-read", "name-only",
        "setenv-mocked", "setitem-mocked", "patch-dict-mocked", "environ-store-mocked",
        "setdefault-mocked", "import-time-read-beats-a-later-setenv",
        "396-delenv-is-not-a-host", "396-delitem-is-not-a-host", "396-pop-is-not-a-host",
        "396-unsetenv-is-not-a-host", "396-del-subscript-is-not-a-host", "396-dunder-delitem-is-not-a-host",
        "411-empty-setenv-is-not-a-host", "411-empty-setenv-value-kw-is-not-a-host",
        "411-empty-setitem-is-not-a-host", "411-empty-environ-store-is-not-a-host",
        "411-empty-annotated-store-is-not-a-host", "411-empty-setdefault-is-not-a-host",
        "411-empty-putenv-is-not-a-host", "411-empty-dunder-setitem-is-not-a-host",
        "411-empty-update-dict-is-not-a-host", "411-empty-patch-dict-is-not-a-host",
        "411-empty-patch-dict-kw-is-not-a-host",
        "411-empty-setdefault-default-kw-is-not-a-host", "411-empty-setitem-value-kw-is-not-a-host",
        "411-empty-chained-store-is-not-a-host", "411-empty-chained-store-second-target-is-not-a-host",
        "411-empty-update-kw-is-not-a-host",
        "411-whitespace-is-not-a-host", "411-empty-fstring-is-not-a-host",
        "411-whitespace-fstring-is-not-a-host",
        "411-fstring-with-a-field-still-supplies-control", "411-fstring-with-text-still-supplies-control",
        "411-non-str-constant-still-supplies-control",
        "411-name-value-still-supplies-control",
        "411-chained-store-still-supplies-control", "411-patch-dict-kw-still-supplies-control",
        "411-one-real-store-keeps-the-file-exempt-control", "411-dict-with-a-real-value-supplies-control",
    ])
    def test_drives_a_named_device(self, source, drives) -> None:
        assert drives_a_named_device(ast.parse(source)) is drives

    def test_selection_recurses_and_keeps_the_live_floor(self, tmp_path) -> None:
        (tmp_path / "sub").mkdir()
        (tmp_path / "test_plain_live.py").write_text("x = 1\n")
        (tmp_path / "sub" / "test_deep.py").write_text("import os\nH = os.environ.get('U64_HOST')\n")
        (tmp_path / "test_mocked.py").write_text(
            "import os\ndef test_a(monkeypatch):\n    monkeypatch.setenv('U64_HOST', 'x')\n"
            "    os.environ.get('U64_HOST')\n")
        (tmp_path / "conftest.py").write_text("import os\nH = os.environ.get('U64_HOST')\n")
        got = [str(p.relative_to(tmp_path)) for p in _scanned_modules(tmp_path)]
        assert got == ["test_plain_live.py", "sub/test_deep.py"]

    def test_an_ungated_write_in_a_selected_module_is_an_offence(self) -> None:
        src = (
            "import os, pytest\n"
            "_U64_HOST = os.environ.get('U64_HOST')\n"
            "@pytest.mark.skipif(not _U64_HOST, reason='x')\n"
            "class TestT:\n"
            "    def test_a(self, client):\n        set_turbo_mhz(client, 48)\n"
        )
        tree = ast.parse(src)
        assert drives_a_named_device(tree)
        assert gate_offence(src, "test_x.py", {"set_turbo_mhz"}) == {"set_turbo_mhz"}
        assert ungated_tests_that_write(tree, {"set_turbo_mhz"}) == ["test_a (set_turbo_mhz)"]

    #: #396, the reviewer's planted module: the fixture reads the operator's
    #: host inside a function, one test removes the variable, another writes
    #: config ungated.  On the base the ``delenv`` exempted the whole file.
    PLANTED = (
        "import os\n"
        "import pytest\n"
        "from c64_test_harness.backends.ultimate64_helpers import enable_uci\n"
        "\n"
        "@pytest.fixture\n"
        "def client():\n"
        "    host = os.environ.get('U64_HOST')\n"
        "    if not host:\n"
        "        pytest.skip('no host')\n"
        "    return host\n"
        "\n"
        "def test_skips_cleanly_without_a_host(monkeypatch):\n"
        "    monkeypatch.delenv('U64_HOST', raising=False)\n"
        "\n"
        "def test_enables_uci(client):\n"
        "    enable_uci(client)\n"
    )

    def test_a_delenv_does_not_exempt_the_planted_module(self, tmp_path) -> None:
        (tmp_path / "test_planted.py").write_text(self.PLANTED)
        assert [p.name for p in _scanned_modules(tmp_path)] == ["test_planted.py"]
        tree = ast.parse(self.PLANTED)
        assert gate_offence(self.PLANTED, "test_planted.py", {"enable_uci"}) == {"enable_uci"}
        assert ungated_tests_that_write(tree, {"enable_uci"}) == ["test_enables_uci (enable_uci)"]

    @pytest.mark.parametrize("empty", [
        "monkeypatch.setenv('U64_HOST', '')",
        "os.environ['U64_HOST'] = ''",
    ], ids=["setenv", "environ-store"])
    def test_an_empty_host_store_does_not_exempt_the_planted_module(self, tmp_path, empty) -> None:
        """#411: the reviewer's probe -- ``U64_HOST=''`` is "no host", like a removal."""
        src = self.PLANTED.replace("monkeypatch.delenv('U64_HOST', raising=False)", empty)
        assert src != self.PLANTED
        (tmp_path / "test_planted.py").write_text(src)
        assert [p.name for p in _scanned_modules(tmp_path)] == ["test_planted.py"]
        assert gate_offence(src, "test_planted.py", {"enable_uci"}) == {"enable_uci"}
        assert ungated_tests_that_write(ast.parse(src), {"enable_uci"}) == [
            "test_enables_uci (enable_uci)"
        ]

    def test_the_planted_module_with_a_setenv_is_still_exempt_control(self, tmp_path) -> None:
        """Control: the same module supplying a host (not removing it) stays out,
        so the negative above is the delete rule, not selection of every reader."""
        src = self.PLANTED.replace(
            "monkeypatch.delenv('U64_HOST', raising=False)", "monkeypatch.setenv('U64_HOST', 'x')"
        )
        assert src != self.PLANTED
        (tmp_path / "test_planted.py").write_text(src)
        assert _scanned_modules(tmp_path) == []

    @pytest.mark.parametrize("name", [
        "test_device_lock_advisory.py", "test_live_fixture_teardowns.py",
        "test_u64_runner_script_gates.py", "test_unified_manager.py", "test_wav_capture_paths.py",
    ])
    def test_the_corpus_modules_that_mock_the_host_stay_unselected(self, name) -> None:
        """Controls from #396: all mocked unit/scan tests that supply or remove
        ``U64_HOST``.  Each still exists, and none is selected."""
        path = TESTS_DIR / name
        assert path.is_file(), f"{name} is gone; drop it from the controls"
        assert "U64_HOST" in path.read_text()
        assert path not in _scanned_modules()


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

    #: #353: each skips when the other variable is unset or "1", and not at
    #: some literal the condition itself names (or at a value naming none).
    LITERAL_ESCAPES = [
        "os.environ.get('X_LIVE') != 'yes' or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "not os.environ.get('U64_ALLOW_MUTATE') and os.environ.get('X_LIVE') != 'yes'",
        "os.environ.get('X_LIVE', '1') != '0' or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "os.environ.get('X_LIVE') not in ('yes', 'on') or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "int(os.environ.get('X_LIVE', '0')) != 2 or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "os.environ.get('X_LIVE') != _WANT or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "os.environ.get('X_LIVE', 'a') in ('a', '1') or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "'yes' != os.environ.get('X_LIVE') or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        # #374: a truthy literal default, never compared: unset and "1" both skip,
        # and only an empty value (set, empty) reads falsy and does not.
        "os.environ.get('X_LIVE', '1') or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        "os.getenv('X_LIVE', 'yes') or os.environ.get('U64_ALLOW_MUTATE') == '1'",
        # #374's own example: already refused before the fix (unset reads '1',
        # so ``not`` is False and it does not skip); kept as a guard.
        "not os.environ.get('X_LIVE', '1') or os.environ.get('U64_ALLOW_MUTATE') == '1'",
    ]
    LITERAL_ESCAPE_IDS = [
        "353-or", "353-and", "353-default-literal", "in-container", "int-literal",
        "bound-literal", "none-of-the-literals", "read-on-the-right",
        "374-truthy-default", "374-getenv-default", "374-issue-example",
    ]

    @pytest.mark.parametrize("condition", LITERAL_ESCAPES, ids=LITERAL_ESCAPE_IDS)
    def test_a_comparison_against_another_literal_is_not_a_gate(self, condition) -> None:
        """At the literal (X_LIVE='yes', '0', ...) with the gate unset none skips."""
        src = (
            "import os, pytest\n"
            "_WANT = 'yes'\n"
            f"pytestmark = pytest.mark.skipif({condition}, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == {"enable_uci"}

    @pytest.mark.parametrize("condition", [
        "os.environ.get('X_LIVE') != 'yes' or not os.environ.get('U64_ALLOW_MUTATE')",
        "os.environ.get('X_LIVE') not in ('yes', 'on') or os.environ.get('U64_ALLOW_MUTATE') != '1'",
        "os.environ.get('X_LIVE', 'a') in ('a', '1') and not os.environ.get('U64_ALLOW_MUTATE')"
        " or os.environ.get('U64_ALLOW_MUTATE') != '1'",
        "not os.environ.get('X_LIVE', '1') or not os.environ.get('U64_ALLOW_MUTATE')",
    ], ids=["literal-or-gate", "container-or-gate", "none-of-the-literals-still-gated",
            "truthy-default-or-gate"])
    def test_literals_beside_a_real_gate_still_gate(self, condition) -> None:
        """Control for #353: extra candidates must not refuse a condition that skips."""
        src = (
            "import os, pytest\n"
            f"pytestmark = pytest.mark.skipif({condition}, reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == set()

    @pytest.mark.parametrize("plain, offence", [(4, set()), (5, {"enable_uci"})])
    def test_literal_candidates_count_against_the_cap(self, plain, offence) -> None:
        """One variable compared with 'yes' has four candidates: 4 * 2**4 = 64 is
        enumerated, 4 * 2**5 = 128 fails closed, although six variables pass."""
        others = " or ".join(f"not os.environ.get('X{i}')" for i in range(plain))
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            f"    not os.environ.get('U64_ALLOW_MUTATE') or {others}"
            " or os.environ.get('Y') != 'yes', reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == offence

    @pytest.mark.parametrize("plain, offence", [(4, set()), (5, {"enable_uci"})])
    def test_the_empty_candidate_counts_against_the_cap(self, plain, offence) -> None:
        """#374: a variable with a truthy literal default has three candidates, so
        3 * 2**4 = 48 is enumerated and 3 * 2**5 = 96 fails closed -- where the
        same condition without the empty value (2 * 2**5 = 64) was a gate."""
        others = " or ".join(f"not os.environ.get('X{i}')" for i in range(plain))
        src = (
            "import os, pytest\n"
            "pytestmark = pytest.mark.skipif(\n"
            f"    not os.environ.get('U64_ALLOW_MUTATE') or {others}"
            " or not os.environ.get('Y', '1'), reason='x')\n"
            "def test_x(client):\n    enable_uci(client)\n"
        )
        assert self._offence(src) == offence

    def test_candidate_values_are_what_the_rule_says(self) -> None:
        expr = ast.parse(
            "get(GATE) or os.environ.get('A', '0') not in ('yes', 2, True)"
            " or str(os.getenv('B')) == 'on' or os.environ.get('C')"
            " or ('q', os.environ.get('D')) == ('r', 's')"
            " or os.environ.get('E', '') or os.getenv('F', 'on') or os.environ.get('G', '1') != ''",
            mode="eval",
        ).body
        got = _candidate_values(expr)
        assert list(got) == ["A", "B", "C", "D", "E", "F", "G"]
        # Only the *other* operands supply literals: 'q' shares D's operand.
        assert got["D"] == [_UNSET, "1", "r", "s", _NONE_OF_THE_LITERALS]
        # #374: a non-empty literal default adds the empty value, right after "1".
        assert got["A"] == [_UNSET, "1", "", "yes", "2", _NONE_OF_THE_LITERALS]
        assert got["B"] == [_UNSET, "1", "on", _NONE_OF_THE_LITERALS]
        assert got["C"] == [_UNSET, "1"]
        # An empty default already reads "" when unset; nothing to add.
        assert got["E"] == [_UNSET, "1"]
        assert got["F"] == [_UNSET, "1", ""]
        # The compared literal '' is the empty value: listed once.
        assert got["G"] == [_UNSET, "1", "", _NONE_OF_THE_LITERALS]

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

    @pytest.mark.parametrize(
        "condition",
        TestTheScannerItselfCanFail.LITERAL_ESCAPES,
        ids=TestTheScannerItselfCanFail.LITERAL_ESCAPE_IDS,
    )
    def test_a_marker_comparing_another_literal_does_not_cover_the_test(
        self, condition
    ) -> None:
        """#353 at test level: the marker does not skip at the literal."""
        body = (
            "_WANT = 'yes'\n"
            f"literal = pytest.mark.skipif({condition}, reason='y')\n"
            "@literal\n"
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

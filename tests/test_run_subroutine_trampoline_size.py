"""``run_subroutine``'s U64 trampoline must never reach the POST writemem path.

Issue #254 (scope-corrected by the owner's comment on it: ``jsr()``'s 5-byte
trampoline is the *VICE* branch and never crosses a REST wire, so only the
U64 branch is in scope here).

What is being pinned
--------------------
``_run_subroutine_u64`` installs a 14-byte flag trampoline and clears two
flag bytes, both through ``transport.write_memory`` -> ``client.write_mem``.
``write_mem`` chooses its wire form by payload size:

* ``len(data) <= client.write_mem_query_threshold`` -> ``PUT ?data=<hex>``,
  which binds a NULL attachment writer in the firmware route table and
  leaves nothing in ``/Temp``;
* above it -> ``POST`` with a body, which leaves a managed ``/Temp``
  attachment on firmware without upstream #686 (the C64U on 1.1.0).

The threshold is 48 on fixed firmware and 128 on leak-prone firmware, so 48
is the strict case and the one the trampoline has to clear. Until now that
held by arithmetic a reader had to redo, pinned by no test.

Why these assertions can fail
-----------------------------
A bare ``assert len(trampoline) <= 48`` is the unfalsifiable shape this
repo's review standard warns about: it passes whether or not the code is
right, because the value it compares against is not derived from anything
the code does. So the load-bearing assertions here are taken from the
*wire*: the request log a full ``run_subroutine`` call produces against a
stubbed transport, and the client's own ``/Temp`` attachment counter
(``_creates_temp_attachment`` is the firmware route table's rule, not a
restatement of the threshold). Grow the trampoline past the threshold and a
``POST /v1/machine:writemem`` appears in the log and the counter goes to 1.

No device traffic: ``_request_uncounted`` is stubbed, and the client is
built with an explicit threshold (which skips the capability probe) and
``temp_hygiene=False`` (which keeps the hygiene path off the network).
"""

from __future__ import annotations

import ast
import textwrap
from types import SimpleNamespace

import pytest

from c64_test_harness.backends.u64_capabilities import (
    THRESHOLD_POST_RISKY,
    THRESHOLD_POST_SAFE,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.execute import _build_u64_trampoline, run_subroutine


_TEST_HOST = "198.51.100.7"  # TEST-NET-2; nothing is there, nothing is sent.

_TRAMPOLINE_ADDR = 0x0360
_RUNNING_FLAG = 0x03F0
_DONE_FLAG = 0x03F1
_KEYBUF_COUNT = 0x00C6
_TARGET_ADDR = 0x1509


class _FakeDevice:
    """Stands in for the wire under ``Ultimate64Client._request_uncounted``.

    Records every request and models just enough C64 memory for
    ``run_subroutine`` to complete: the keyboard buffer reads drained, and
    the done flag flips to ``$02`` once the ``SYS`` keystrokes land — the
    6510 having "run" the trampoline.

    Writes are applied from *both* wire forms, so a trampoline that grows
    past the threshold still completes the call and fails on the assertion
    about the wire rather than on a timeout.
    """

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self.mem: dict[int, int] = {}

    # -- wire ----------------------------------------------------------
    def __call__(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
        query: dict | None = None,
    ) -> tuple[int, bytes]:
        query = dict(query or {})
        self.requests.append((method, path, query, body))

        if path.endswith("writemem"):
            addr = int(query["address"], 16)
            if "data" in query:
                payload = bytes.fromhex(query["data"])
            else:
                payload = body or b""
            self._write(addr, payload)
            return 200, b""

        if path.endswith("readmem"):
            addr = int(query["address"], 16)
            length = int(query["length"])
            return 200, bytes(self.mem.get(addr + i, 0) for i in range(length))

        return 200, b""

    # -- memory model --------------------------------------------------
    def _write(self, addr: int, payload: bytes) -> None:
        for i, byte in enumerate(payload):
            self.mem[addr + i] = byte
        # Keyboard buffer count written non-zero == the SYS line was
        # submitted. Model the 6510 consuming it: buffer drains, the
        # trampoline runs, both flags end up set.
        if addr == _KEYBUF_COUNT and payload and payload[0] != 0:
            self.mem[_KEYBUF_COUNT] = 0
            self.mem[_RUNNING_FLAG] = 0x01
            self.mem[_DONE_FLAG] = 0x02

    # -- queries -------------------------------------------------------
    def writemem_posts(self) -> list[tuple[str, str, dict, bytes | None]]:
        return [
            r for r in self.requests
            if r[0] == "POST" and r[1].endswith("writemem")
        ]

    def writemem_put_payload_sizes(self) -> list[int]:
        return [
            len(r[2]["data"]) // 2
            for r in self.requests
            if r[0] == "PUT" and r[1].endswith("writemem") and "data" in r[2]
        ]


def _make_target(threshold: int) -> tuple[SimpleNamespace, Ultimate64Client, _FakeDevice]:
    client = Ultimate64Client(
        host=_TEST_HOST,
        write_mem_query_threshold=threshold,  # explicit => no capability probe
        temp_hygiene=False,                   # => no hygiene path, no network
        warn_unlocked=False,
    )
    device = _FakeDevice()
    client._request_uncounted = device  # type: ignore[method-assign]

    transport = Ultimate64Transport(host=_TEST_HOST)
    transport._client.close()
    transport._client = client
    return SimpleNamespace(transport=transport), client, device


# ---------------------------------------------------------------------------
# The load-bearing test: drive the real call and read the wire.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "threshold",
    [
        pytest.param(THRESHOLD_POST_SAFE, id="fixed-firmware-48"),
        pytest.param(THRESHOLD_POST_RISKY, id="leak-prone-firmware-128"),
    ],
)
def test_run_subroutine_u64_never_takes_the_post_writemem_path(threshold: int) -> None:
    """Every byte ``run_subroutine`` writes on a U64 goes out as PUT ?data=.

    Both thresholds are exercised because the harness picks between them
    from the device's firmware grade; the trampoline has to clear the
    stricter one (48) for the fixed-firmware path to stay attachment-free
    too, and the C64U (128) is the device the attachments actually wedge.
    """
    target, client, device = _make_target(threshold)

    # Read the ceiling back off the client rather than trusting the value we
    # passed in. A test pinned to a literal 128 passes today and silently
    # stops protecting anything on fixed firmware, where the real ceiling is
    # 48 and the trampoline's margin is much thinner — so the number the
    # assertion uses has to be the one ``write_mem`` will actually branch on.
    live_threshold = client.write_mem_query_threshold
    assert live_threshold == threshold, (
        "the client did not take the threshold under test; this test would "
        f"otherwise assert against the wrong ceiling ({threshold} vs "
        f"{live_threshold})"
    )

    run_subroutine(target, _TARGET_ADDR, timeout=5.0, poll_cadence=0.0)

    assert device.writemem_posts() == [], (
        "run_subroutine took the POST writemem path, which leaves a /Temp "
        "attachment on firmware without upstream #686: "
        f"{[(r[0], r[2], len(r[3] or b'')) for r in device.writemem_posts()]}"
    )
    sizes = device.writemem_put_payload_sizes()
    assert sizes, "no writemem PUT was issued at all — the stub is not wired up"
    assert max(sizes) <= live_threshold, (
        f"largest PUT payload {max(sizes)} B exceeds the device's live "
        f"write_mem_query_threshold of {live_threshold} B; sizes={sizes}"
    )


@pytest.mark.parametrize(
    "threshold",
    [
        pytest.param(THRESHOLD_POST_SAFE, id="fixed-firmware-48"),
        pytest.param(THRESHOLD_POST_RISKY, id="leak-prone-firmware-128"),
    ],
)
def test_run_subroutine_u64_leaves_no_temp_attachment(threshold: int) -> None:
    """The client's own attachment counter stays at zero across the call.

    ``_creates_temp_attachment`` encodes the firmware route table's rule
    (a body plus POST), independently of the size threshold, so this is a
    second witness rather than a restatement of the test above.
    """
    target, client, device = _make_target(threshold)

    assert client._pending_temp_attachments == 0
    run_subroutine(target, _TARGET_ADDR, timeout=5.0, poll_cadence=0.0)

    assert client._pending_temp_attachments == 0, (
        "run_subroutine left "
        f"{client._pending_temp_attachments} /Temp attachment(s) behind"
    )


# ---------------------------------------------------------------------------
# The arithmetic itself, pinned so a reader does not have to redo it.
# ---------------------------------------------------------------------------

def test_both_thresholds_are_reachable_through_the_real_grading_path() -> None:
    """Both constants really are produced by grading actual firmware strings.

    This is a *reachability* check, not a completeness one — it proves the
    two ceilings the wire tests are parametrized over are both live, by
    walking the same ``from_info`` path a probe would. It deliberately makes
    no claim about what else the property might return; that is
    :func:`test_write_mem_query_threshold_cannot_return_a_third_value`,
    which is the only one of the pair that can see a new branch.
    """
    from c64_test_harness.backends.u64_capabilities import DeviceCapabilities

    payloads: list[dict | None] = [
        {"firmware_version": "3.15"},    # Ultimate line, carries #686
        {"firmware_version": "3.14d"},   # Ultimate line, does not
        {"firmware_version": "1.1.0"},   # CBM line (the C64U)
        None,                            # probe failed -> conservative
    ]
    thresholds = {
        DeviceCapabilities.from_info(p).write_mem_query_threshold
        for p in payloads
    }
    assert thresholds == {THRESHOLD_POST_SAFE, THRESHOLD_POST_RISKY}
    # The parametrized wire tests rely on 'safe' being the *tighter* ceiling,
    # not merely a different one.
    assert THRESHOLD_POST_SAFE < THRESHOLD_POST_RISKY


def _returned_value_leaves(node: ast.AST) -> list[ast.AST]:
    """Every expression a ``return`` can actually hand back.

    Descends the control-flow combinators that choose *between* values
    (conditional expressions, ``or``/``and`` chains) and stops at the leaves
    that produce one. Anything else — a call, a subscript, arithmetic — is
    returned as-is so the caller can reject it: this analysis can only
    certify what it understands, and silently accepting an unfamiliar node
    is how the previous version of this test passed a mutation.
    """
    if isinstance(node, ast.IfExp):
        return _returned_value_leaves(node.body) + _returned_value_leaves(node.orelse)
    if isinstance(node, ast.BoolOp):
        return [leaf for v in node.values for leaf in _returned_value_leaves(v)]
    return [node]


def test_the_codomain_analysis_accepts_and_rejects_the_right_shapes() -> None:
    """Fixtures for :func:`_returned_value_leaves`, which had none.

    Its docstring claims it descends the combinators that *choose between*
    values and rejects everything else. That claim was checked only by
    mutations run by hand against ``u64_capabilities.py`` — evidence that
    vanishes when the session ends. A claim with no fixture is one nobody
    will notice breaking, so here are the shapes, as examples that fail if
    the analysis stops behaving this way.
    """
    allowed = {"THRESHOLD_POST_SAFE", "THRESHOLD_POST_RISKY"}

    def offenders(body: str) -> list[str]:
        tree = ast.parse(textwrap.dedent(body))
        out = []
        for ret in [n for n in ast.walk(tree) if isinstance(n, ast.Return)]:
            assert ret.value is not None
            for leaf in _returned_value_leaves(ret.value):
                if not (isinstance(leaf, ast.Name) and leaf.id in allowed):
                    out.append(ast.dump(leaf)[:60])
        return out

    # Accepted: the two constants, and the combinators that pick between them.
    for ok in (
        "def f():\n    return THRESHOLD_POST_SAFE\n",
        "def f():\n    return A if c else B\n".replace("A", "THRESHOLD_POST_SAFE")
        .replace("B", "THRESHOLD_POST_RISKY"),
        "def f():\n    return THRESHOLD_POST_SAFE or THRESHOLD_POST_RISKY\n",
        # Nested conditionals still resolve to the two names.
        "def f():\n    return (THRESHOLD_POST_SAFE if a else\n"
        "            (THRESHOLD_POST_RISKY if b else THRESHOLD_POST_SAFE))\n",
    ):
        assert offenders(ok) == [], f"analysis rejected an allowed shape: {ok!r}"

    # Rejected: a literal, a third name, a call it cannot verify, arithmetic,
    # a subscript, and an attribute — the last three because "I do not
    # understand this node" must fail rather than pass.
    for bad in (
        "def f():\n    return 96\n",
        "def f():\n    return THRESHOLD_POST_MEDIUM\n",
        "def f():\n    return self._pick()\n",
        "def f():\n    return THRESHOLD_POST_SAFE * 2\n",
        "def f():\n    return _TABLE[self.generation]\n",
        "def f():\n    return self.threshold\n",
        "def f():\n    return THRESHOLD_POST_SAFE if c else 96\n",
    ):
        assert offenders(bad), f"analysis accepted a shape it must reject: {bad!r}"


def test_write_mem_query_threshold_cannot_return_a_third_value() -> None:
    """Constrain the codomain, rather than sampling four chosen inputs.

    The previous form of this test graded ``3.15``/``3.14d``/``1.1.0``/``None``
    and asserted the results were the two constants. That checks the cases
    the author had in mind and nothing else: a mutation adding

        if (self.firmware_version or "").startswith("2."):
            return 96

    left it green, because no payload in the list grades as ``2.x``. A
    sampled set can never establish "these are the only two" — no finite
    list of firmware strings can.

    So this reads the implementation instead: every value
    ``write_mem_query_threshold`` is capable of returning must be one of the
    two named constants. A numeric literal, a third constant, or an
    expression this analysis does not understand all fail — the last one
    deliberately, because "a new branch appeared and I cannot verify it" is
    the honest outcome, not a pass.

    What this does *not* claim: it says nothing about which firmware gets
    which ceiling. That is
    :func:`test_both_thresholds_are_reachable_through_the_real_grading_path`
    and the ``_writemem_post_safe`` tests elsewhere. This one answers
    exactly one question — can the wire tests' two parametrized cases miss a
    ceiling? — and it is the question the wire tests depend on.
    """
    import inspect

    from c64_test_harness.backends import u64_capabilities

    source = inspect.getsource(u64_capabilities.DeviceCapabilities)
    tree = ast.parse(textwrap.dedent(source))

    funcs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef)
        and n.name == "write_mem_query_threshold"
    ]
    assert len(funcs) == 1, (
        f"expected exactly one write_mem_query_threshold, found {len(funcs)} "
        "— the analysis below would be reading the wrong one"
    )

    returns = [n for n in ast.walk(funcs[0]) if isinstance(n, ast.Return)]
    assert returns, "write_mem_query_threshold has no return statement"

    allowed = {"THRESHOLD_POST_SAFE", "THRESHOLD_POST_RISKY"}
    offenders: list[str] = []
    for ret in returns:
        if ret.value is None:
            offenders.append("bare return (returns None)")
            continue
        for leaf in _returned_value_leaves(ret.value):
            if isinstance(leaf, ast.Name) and leaf.id in allowed:
                continue
            offenders.append(f"line {leaf.lineno}: {ast.dump(leaf)[:120]}")

    assert offenders == [], (
        "write_mem_query_threshold can return a value that is not one of "
        f"{sorted(allowed)}, so the two parametrized wire tests above no "
        f"longer cover every ceiling a device can present:\n  "
        + "\n  ".join(offenders)
    )


def test_u64_trampoline_is_14_bytes_and_clears_the_strict_threshold() -> None:
    """The documented size, checked against the builder and the constant.

    ``run_subroutine``'s docstring states "The trampoline is 14 bytes";
    ``memory_policy.HARNESS_SCRATCH`` spans ``$0360-$036D`` on that basis.
    This pins the number to the builder so the docstring and the scratch
    span cannot drift away from the code silently.
    """
    trampoline = _build_u64_trampoline(_TARGET_ADDR, _RUNNING_FLAG, _DONE_FLAG)

    assert len(trampoline) == 14
    # $0360..$036D inclusive is exactly 14 bytes.
    assert _TRAMPOLINE_ADDR + len(trampoline) - 1 == 0x036D
    assert len(trampoline) <= THRESHOLD_POST_SAFE < THRESHOLD_POST_RISKY

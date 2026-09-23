"""A flag is not a C64 address in the code builders either (#373).

#357 (PR #376) refused a ``bool``/``numpy.bool_`` on every entry point that
puts an address on a wire.  The code builders take addresses too and mask
them straight into operands (``result_addr & 0xFF``), so a ``True`` became
code aimed at ``$0001`` (the 6510 processor port) that a *guarded*
``write_memory`` then uploaded to a real int address.  Measured on the base
(3aaa53d): every address parameter of every builder below accepted ``True``
and emitted bytes identical to the int ``1``.

Every such function now carries ``_address.refuses_bool_address_args``,
which refuses a flag in any parameter named ``*addr``/``*address``/``*buf``
before the body runs -- no ``Asm`` is built, no byte emitted, no transport
touched.  The same holds for the orchestrators that take a transport plus
addresses (``run_ping_and_wait``, ``run_icmp_responder``,
``poll_until_ready``, ``play_sid``, ``play_sid_vice``), and
``BinaryViceTransport.set_registers`` / ``execute.set_register`` now refuse
a flag as any register value (``{"PC": True}`` sent the same monitor body
as ``{"PC": 1}``).

Byte identity: the int and ``IntEnum`` outputs are pinned to sha256
prefixes taken from the base, so the decorator provably changes nothing a
valid call emits.  No device, no VICE.
"""
from __future__ import annotations

import enum
import hashlib
import importlib
import inspect
import struct
from unittest.mock import patch

import pytest

from c64_test_harness import bridge_ping, execute, poll_until, sid, sid_player, tod_timer, uci_network
from c64_test_harness._address import ADDRESS_PARAM_RE, refuses_bool_address_args
from c64_test_harness.backends.vice_binary import CMD_REGISTERS_SET, BinaryViceTransport

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

FLAGS = [True, False]
if np is not None:
    FLAGS += [np.True_, np.False_]
_FLAG_IDS = [f"{type(f).__module__}.{f!r}" for f in FLAGS]


class A(enum.IntEnum):
    LOAD = 0xC000
    RESULT = 0xC100
    RX = 0x4000
    TX = 0x5000
    ARP = 0x5100
    DRAIN = 0xC1F0
    DEST = 0xC200
    PLAY = 0x1003
    STUB = 0xC300
    SOCK = 0xC1E0
    DATA = 0x4800
    DLEN = 0xC1E1
    ALEN = 0xC1E2
    STAT = 0xC1D0
    RESP = 0xC180
    RLEN = 0xC1D8
    SLEN = 0xC1D9
    ERR = 0xC1DA
    SENT = 0xC1DB
    CODE = 0xC400
    HOST = 0x4900
    PSID = 0x1000


IP = bytes([10, 0, 65, 2])
MAC = bytes([2, 0, 0, 0, 0, 1])
SNIP = bytes([0xAD, 0x08, 0xDE, 0x29, 0x01])
UCI = dict(status_addr=A.STAT, stat_len_addr=A.SLEN, error_addr=A.ERR, sentinel_addr=A.SENT, code_addr=A.CODE)

#: name -> (builder, IntEnum-addressed kwargs, sha256[:16] of the base's output).
#: Digests taken on 3aaa53d with scratch script i373_golden.py (int and IntEnum
#: agreed on the base for every row).  The seven UCI builders that push a command were
#: re-taken for #486, whose wait-for-reply change alters exactly two bytes of
#: each (the wait mask ``$01``->``$28`` and its branch opcode), sizes unchanged.
BUILDERS = {
    "build_cs8900a_reset_code": (bridge_ping.build_cs8900a_reset_code, dict(load_addr=A.LOAD, result_addr=A.RESULT), "4f96c03b59e1b071"),
    "build_icmp_responder_code": (bridge_ping.build_icmp_responder_code, dict(load_addr=A.LOAD, rx_buf=A.RX, my_ip=IP, result_addr=A.RESULT, my_mac=MAC), "7499c032a253503c"),
    "build_icmp_responder_tod_code": (bridge_ping.build_icmp_responder_tod_code, dict(load_addr=A.LOAD, rx_buf=A.RX, my_ip=IP, result_addr=A.RESULT, deadline_tenths=50, my_mac=MAC), "3a097608c848259b"),
    "build_ping_and_wait_code": (bridge_ping.build_ping_and_wait_code, dict(load_addr=A.LOAD, tx_frame_buf=A.TX, tx_frame_len=74, rx_buf=A.RX, result_addr=A.RESULT, identifier=0x1234, sequence=1, arp_frame_buf=A.ARP, arp_frame_len=42, drain_first=True, drain_status_addr=A.DRAIN), "22e5d725b816bc47"),
    "build_ping_and_wait_tod_code": (bridge_ping.build_ping_and_wait_tod_code, dict(load_addr=A.LOAD, tx_frame_buf=A.TX, tx_frame_len=74, rx_buf=A.RX, result_addr=A.RESULT, identifier=0x1234, sequence=1, deadline_tenths=50, arp_frame_buf=A.ARP, arp_frame_len=42, drain_first=True, drain_status_addr=A.DRAIN), "7e60dda9a8a34493"),
    "build_read_and_match_echo_reply_code": (bridge_ping.build_read_and_match_echo_reply_code, dict(load_addr=A.LOAD, rx_buf=A.RX, result_addr=A.RESULT, identifier=0x1234, sequence=1), "385c56ea9992e1dc"),
    "build_read_and_respond_echo_request_code": (bridge_ping.build_read_and_respond_echo_request_code, dict(load_addr=A.LOAD, rx_buf=A.RX, my_ip=IP, result_addr=A.RESULT, my_mac=MAC), "05e105273a4492e9"),
    "build_rx_echo_reply_code": (bridge_ping.build_rx_echo_reply_code, dict(load_addr=A.LOAD, rx_buf=A.RX, result_addr=A.RESULT, identifier=0x1234, sequence=1), "12fa259717a77888"),
    "build_rx_echo_reply_tod_code": (bridge_ping.build_rx_echo_reply_tod_code, dict(load_addr=A.LOAD, rx_buf=A.RX, result_addr=A.RESULT, expect_id=0x1234, expect_seq=1, deadline_tenths=50), "5f15a07d5eb3371d"),
    "build_rx_peek_code": (bridge_ping.build_rx_peek_code, dict(load_addr=A.LOAD, result_addr=A.RESULT), "97a73f08588a3d5d"),
    "build_tx_code": (bridge_ping.build_tx_code, dict(load_addr=A.LOAD, frame_buf=A.TX, frame_len=60, result_addr=A.RESULT), "e32ede6eadea2a2d"),
    "cs8900a_read_linectl_code": (bridge_ping.cs8900a_read_linectl_code, dict(dest_addr=A.DEST), "1de72344c8aa13ff"),
    "build_tod_start_code": (tod_timer.build_tod_start_code, dict(load_addr=A.LOAD), "87ee9b1052df2342"),
    "build_tod_read_tenths_code": (tod_timer.build_tod_read_tenths_code, dict(load_addr=A.LOAD, result_addr=A.RESULT), "69250fbf77344a83"),
    "build_poll_with_tod_deadline_code": (tod_timer.build_poll_with_tod_deadline_code, dict(load_addr=A.LOAD, peek_check_snippet=SNIP, result_addr=A.RESULT, deadline_tenths=50), "3a200cd12f7444b6"),
    "build_uci_probe": (uci_network.build_uci_probe, dict(result_addr=A.RESP, sentinel_addr=A.SENT, code_addr=A.CODE), "e5fe30ee49a863ca"),
    "build_uci_status_peek": (uci_network.build_uci_status_peek, dict(result_addr=A.RESP, sentinel_addr=A.SENT, code_addr=A.CODE), "4a77cf1d5a4952c2"),
    "build_uci_command": (uci_network.build_uci_command, dict(target=4, cmd=1, params=b"", resp_addr=A.RESP, resp_len_addr=A.RLEN, **UCI), "11601440abbda122"),
    "build_get_ip": (uci_network.build_get_ip, dict(result_addr=A.RESP, resp_len_addr=A.RLEN, **UCI), "603977ee61239910"),
    "build_tcp_connect": (uci_network.build_tcp_connect, dict(host_addr=A.HOST, port=80, result_addr=A.RESP, resp_len_addr=A.RLEN, **UCI), "ee0d84b3a835fcf0"),
    "build_udp_connect": (uci_network.build_udp_connect, dict(host_addr=A.HOST, port=80, result_addr=A.RESP, resp_len_addr=A.RLEN, **UCI), "89eaad3d224ea07f"),
    "build_socket_write": (uci_network.build_socket_write, dict(socket_id_addr=A.SOCK, data_addr=A.DATA, data_len_addr=A.DLEN, **UCI), "9e61438639226ae7"),
    "build_socket_read": (uci_network.build_socket_read, dict(socket_id_addr=A.SOCK, result_addr=A.RESP, max_len=100, actual_len_addr=A.ALEN, **UCI), "9b2efa649580f730"),
    "build_socket_close": (uci_network.build_socket_close, dict(socket_id_addr=A.SOCK, **UCI), "66f8bdd91748b3e4"),
    "build_vice_stub": (sid_player.build_vice_stub, dict(play_addr=A.PLAY, stub_addr=A.STUB), "ded740690d725b5c"),
    "build_test_psid": (sid.build_test_psid, dict(load_addr=A.PSID, init_code=b"\xea", play_code=b"\xea"), "e6504f4db256b843"),
}


def _plain(kw):
    return {k: (int(v) if isinstance(v, enum.IntEnum) else v) for k, v in kw.items()}


def _digest(fn, kw) -> str:
    return hashlib.sha256(bytes(fn(**kw))).hexdigest()[:16]


_ADDR_CASES = [
    (name, param)
    for name, (_, kw, _) in BUILDERS.items()
    for param, value in kw.items()
    if isinstance(value, enum.IntEnum)
]


# --------------------------------------------------------------------------- #
# Byte identity                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", list(BUILDERS))
def test_int_and_intenum_output_is_byte_identical_to_the_base(name):
    fn, kw, expected = BUILDERS[name]
    assert _digest(fn, _plain(kw)) == expected
    assert _digest(fn, kw) == expected


# --------------------------------------------------------------------------- #
# Refusal                                                                     #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("flag", FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("name, param", _ADDR_CASES, ids=[f"{n}-{p}" for n, p in _ADDR_CASES])
def test_every_builder_refuses_a_flag_on_every_address_parameter(name, param, flag):
    fn, kw, _ = BUILDERS[name]
    with patch.object(bridge_ping.Asm, "__init__", side_effect=AssertionError("Asm built")) as asm:
        with pytest.raises(ValueError, match=rf"^{name} {param} must be an int, not bool"):
            fn(**{**_plain(kw), param: flag})
    assert asm.call_count == 0


def test_positional_address_arguments_are_refused_too():
    with pytest.raises(ValueError, match="^build_tod_read_tenths_code result_addr"):
        tod_timer.build_tod_read_tenths_code(0xC000, True)
    with pytest.raises(ValueError, match="^build_vice_stub play_addr"):
        sid_player.build_vice_stub(True)


@pytest.mark.parametrize("name", ["build_ping_and_wait_code", "build_ping_and_wait_tod_code"])
def test_optional_buffers_left_out_still_build(name):
    """``None`` is the "not given" value of the optional ARP buffer."""
    fn, kw, _ = BUILDERS[name]
    kw = {k: v for k, v in _plain(kw).items() if k not in ("arp_frame_buf", "arp_frame_len", "drain_first", "drain_status_addr")}
    assert fn(**kw, arp_frame_buf=None)


def test_a_call_that_does_not_bind_raises_the_functions_own_error():
    with pytest.raises(TypeError):
        tod_timer.build_tod_start_code()  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# Orchestrators: recording fake, nothing touched                              #
# --------------------------------------------------------------------------- #

class _Rec:
    """Records every attribute use; answers reads with 0x01."""

    screen_cols = 40
    screen_rows = 25

    def __init__(self) -> None:
        self.log: list[tuple] = []

    def __getattr__(self, name):
        if name.startswith("__") or name in ("execution_policy", "memory_policy"):
            # Optional attributes read with getattr(..., None): absent.
            raise AttributeError(name)

        def _call(*a, **k):
            self.log.append((name, a, k))
            if name == "read_memory":
                return bytes([0x01]) * a[1]
            if name == "read_registers":
                return {"PC": 0, "A": 0, "X": 0, "Y": 0, "SP": 0xFF, "FL": 0x20}
            if name in ("set_checkpoint", "wait_for_stopped"):
                return 0
            return None

        return _call


def _psid():
    return sid.SidFile.from_bytes(sid.build_test_psid(load_addr=0x1000, init_code=b"\xea", play_code=b"\xea"))


_ORCH = {
    "run_ping_and_wait-rx_buf": lambda t, f: bridge_ping.run_ping_and_wait(t, tx_frame=bytes(74), arp=False, rx_buf=f, result_addr=0xC100, identifier=1, sequence=1, tx_frame_buf=0x5000, timeout_s=0.01),
    "run_ping_and_wait-result_addr": lambda t, f: bridge_ping.run_ping_and_wait(t, tx_frame=bytes(74), arp=False, rx_buf=0x4000, result_addr=f, identifier=1, sequence=1, tx_frame_buf=0x5000, timeout_s=0.01),
    "run_ping_and_wait-tx_frame_buf": lambda t, f: bridge_ping.run_ping_and_wait(t, tx_frame=bytes(74), arp=False, rx_buf=0x4000, result_addr=0xC100, identifier=1, sequence=1, tx_frame_buf=f, timeout_s=0.01),
    "run_ping_and_wait-peek_addr": lambda t, f: bridge_ping.run_ping_and_wait(t, tx_frame=bytes(74), arp=False, rx_buf=0x4000, result_addr=0xC100, identifier=1, sequence=1, tx_frame_buf=0x5000, timeout_s=0.01, peek_addr=f),
    "run_ping_and_wait-consume_addr": lambda t, f: bridge_ping.run_ping_and_wait(t, tx_frame=bytes(74), arp=False, rx_buf=0x4000, result_addr=0xC100, identifier=1, sequence=1, tx_frame_buf=0x5000, timeout_s=0.01, consume_addr=f),
    "run_icmp_responder-rx_buf": lambda t, f: bridge_ping.run_icmp_responder(t, rx_buf=f, my_ip=IP, result_addr=0xC100, timeout_s=0.01),
    "run_icmp_responder-result_addr": lambda t, f: bridge_ping.run_icmp_responder(t, rx_buf=0x4000, my_ip=IP, result_addr=f, timeout_s=0.01),
    "run_icmp_responder-peek_addr": lambda t, f: bridge_ping.run_icmp_responder(t, rx_buf=0x4000, my_ip=IP, result_addr=0xC100, timeout_s=0.01, peek_addr=f),
    "run_icmp_responder-consume_addr": lambda t, f: bridge_ping.run_icmp_responder(t, rx_buf=0x4000, my_ip=IP, result_addr=0xC100, timeout_s=0.01, consume_addr=f),
    "poll_until_ready-code_addr": lambda t, f: poll_until.poll_until_ready(t, f, 0xC100, timeout_s=0.01),
    "poll_until_ready-result_addr": lambda t, f: poll_until.poll_until_ready(t, 0xC000, f, timeout_s=0.01),
    "play_sid_vice-stub_addr": lambda t, f: sid_player.play_sid_vice(t, _psid(), stub_addr=f),
    "play_sid-stub_addr": lambda t, f: sid_player.play_sid(t, _psid(), stub_addr=f),
}


@pytest.mark.parametrize("flag", FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("case", list(_ORCH), ids=list(_ORCH))
def test_orchestrators_refuse_a_flag_before_touching_the_transport(case, flag):
    t = _Rec()
    fn_name, param = case.split("-")
    with pytest.raises(ValueError, match=rf"^{fn_name} {param} must be an int, not bool"):
        _ORCH[case](t, flag)
    assert t.log == []


@pytest.mark.parametrize("case", ["run_ping_and_wait-rx_buf", "run_icmp_responder-rx_buf", "poll_until_ready-code_addr", "play_sid_vice-stub_addr"])
def test_orchestrator_controls_an_int_reaches_the_transport(case):
    """The fake really is on the path: with an int address the call gets
    past the guard and uses the transport (whatever it does after that)."""
    t = _Rec()
    real = {"run_ping_and_wait-rx_buf": 0x4000, "run_icmp_responder-rx_buf": 0x4000,
            "poll_until_ready-code_addr": 0xC000, "play_sid_vice-stub_addr": 0xC300}[case]
    try:
        _ORCH[case](t, real)
    except Exception as exc:  # noqa: BLE001 - the fake is not a machine
        assert "not bool" not in str(exc)
    assert t.log != []


def test_play_sid_control_an_int_reaches_dispatch():
    """play_sid rejects the fake's class: that error, not the flag refusal."""
    with pytest.raises(sid_player.SidPlaybackError):
        sid_player.play_sid(_Rec(), _psid(), stub_addr=0xC300)


# --------------------------------------------------------------------------- #
# Register values                                                             #
# --------------------------------------------------------------------------- #

def _vice():
    with patch.object(BinaryViceTransport, "_connect"):
        t = BinaryViceTransport()
    t._reg_map = {"PC": (3, 16), "A": (0, 8), "SP": (4, 8), "FL": (5, 8)}
    sent: list[tuple[int, bytes]] = []

    class _Resp:
        body = b""

    def _send_and_recv(cmd, body=b""):
        sent.append((cmd, bytes(body)))
        return _Resp()

    t._send_and_recv = _send_and_recv  # type: ignore[method-assign]
    return t, sent


@pytest.mark.parametrize("flag", FLAGS, ids=_FLAG_IDS)
@pytest.mark.parametrize("reg", ["PC", "A", "SP", "FL"])
def test_set_registers_refuses_a_flag_value_before_any_command(reg, flag):
    t, sent = _vice()
    with pytest.raises(ValueError, match=rf"^register {reg} value must be an int, not bool"):
        t.set_registers({"A": 5, reg: flag})
    assert sent == []


@pytest.mark.parametrize("flag", FLAGS, ids=_FLAG_IDS)
def test_set_registers_refuses_the_flag_before_the_unknown_name_check(flag):
    t, sent = _vice()
    with pytest.raises(ValueError, match="not bool"):
        t.set_registers({"NOPE": flag})
    assert sent == []


@pytest.mark.parametrize("value, wire", [(1, "0100"), (0, "0000"), (A.PLAY, "0310")])
def test_set_registers_int_controls_still_send(value, wire):
    t, sent = _vice()
    t.set_registers({"PC": value})
    assert sent == [(CMD_REGISTERS_SET, bytes.fromhex("000100" + "0303" + wire))]


@pytest.mark.parametrize("flag", FLAGS, ids=_FLAG_IDS)
def test_execute_set_register_refuses_a_flag_value(flag):
    t = _Rec()
    with pytest.raises(ValueError, match="^register A value must be an int, not bool"):
        execute.set_register(t, "A", flag)
    assert t.log == []


def test_execute_set_register_control():
    t = _Rec()
    execute.set_register(t, "a", 1)
    assert t.log == [("set_registers", ({"A": 1},), {})]


# --------------------------------------------------------------------------- #
# The decorator and completeness                                              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name", ["addr", "address", "load_addr", "rx_buf", "socket_id_addr", "second_sid_address", "buf"])
def test_address_param_names_match(name):
    assert ADDRESS_PARAM_RE.search(name)


@pytest.mark.parametrize("name", ["frame_len", "my_ip", "mac", "peek_check_snippet", "buffer", "addresses", "ipaddr", "addr_count"])
def test_other_param_names_do_not_match(name):
    assert not ADDRESS_PARAM_RE.search(name)


def test_decorator_preserves_name_signature_and_result():
    def build(load_addr: int, frame_len: int = 3, arp_buf: int | None = None) -> bytes:
        return bytes([load_addr & 0xFF, frame_len])

    wrapped = refuses_bool_address_args(build)
    assert wrapped.__name__ == "build"
    assert inspect.signature(wrapped) == inspect.signature(build)
    assert wrapped(0xC0, frame_len=True) == build(0xC0, frame_len=True)  # not an address
    assert wrapped(0xC0, arp_buf=None) == b"\xc0\x03"
    assert wrapped.__refuses_bool_addresses__ == ("load_addr", "arp_buf")


#: Modules whose every public address-taking function must be decorated.
_GUARDED_MODULES = ("bridge_ping", "tod_timer", "uci_network", "sid_player", "sid", "poll_until")


def test_every_public_address_taking_function_in_the_builder_modules_is_guarded():
    """Completeness: a new builder with an ``*_addr`` parameter that forgets
    the decorator fails here, not on a machine."""
    missing = []
    seen = 0
    for modname in _GUARDED_MODULES:
        mod = importlib.import_module(f"c64_test_harness.{modname}")
        for name, obj in vars(mod).items():
            if name.startswith("_") or not inspect.isfunction(obj):
                continue
            if getattr(obj, "__module__", None) != mod.__name__:
                continue
            params = [p for p in inspect.signature(obj).parameters if ADDRESS_PARAM_RE.search(p)]
            if not params:
                continue
            seen += 1
            if getattr(obj, "__refuses_bool_addresses__", None) != tuple(params):
                missing.append(f"{modname}.{name}{tuple(params)}")
    assert missing == []
    assert seen == 31  # vacuity guard: the enumeration found the whole surface


def test_decorator_carries_positional_and_keyword_defaults():
    """#390: ``functools.wraps`` copies neither ``__defaults__`` nor
    ``__kwdefaults__``, so the wrapper reported ``None`` for both."""
    def build(load_addr: int, frame_len: int = 3, *, arp_buf: int | None = None) -> bytes:
        return bytes([load_addr & 0xFF, frame_len])

    wrapped = refuses_bool_address_args(build)
    assert wrapped.__defaults__ == (3,)
    assert wrapped.__kwdefaults__ == {"arp_buf": None}
    # Call behaviour is unchanged: the defaults still come from the real body.
    assert wrapped(0xC0) == b"\xc0\x03"
    with pytest.raises(ValueError, match="build arp_buf must be an int, not bool"):
        wrapped(0xC0, arp_buf=True)


def test_decorator_leaves_defaultless_functions_defaultless():
    def build(load_addr, frame_len):
        return bytes([load_addr & 0xFF, frame_len])

    wrapped = refuses_bool_address_args(build)
    assert wrapped.__defaults__ is None and wrapped.__kwdefaults__ is None


def test_every_decorated_builder_reports_its_real_defaults():
    """Over the whole guarded surface: the wrapper's defaults are the body's."""
    with_defaults = 0
    seen = 0
    for modname in _GUARDED_MODULES:
        mod = importlib.import_module(f"c64_test_harness.{modname}")
        for name, obj in vars(mod).items():
            if not hasattr(obj, "__refuses_bool_addresses__"):
                continue
            if getattr(obj, "__module__", None) != mod.__name__:
                continue
            seen += 1
            inner = obj.__wrapped__
            assert obj.__defaults__ == inner.__defaults__, f"{modname}.{name}"
            assert obj.__kwdefaults__ == inner.__kwdefaults__, f"{modname}.{name}"
            with_defaults += bool(inner.__defaults__ or inner.__kwdefaults__)
    assert seen == 31
    assert with_defaults >= 16  # vacuity guard: the defaults exist to be lost

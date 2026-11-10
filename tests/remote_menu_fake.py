"""An offline stand-in for an Ultimate's Telnet Remote Menu (issue #543).

Serves, on 127.0.0.1, a small model of the firmware's telnet UI that the
driver in ``backends/ultimate64_remote_menu.py`` is tested against. Nothing
here touches a device.

What it models, and where each behaviour comes from:

* **Wire format** (``Screen_VT100``, ``socket_gui.cc``, read at 7b628eb1 /
  3a1ff9ff / 7b15448a7): ``IAC DONT LINEMODE, IAC WILL ECHO`` then ``ESC c``
  on connect; ``ESC[r;cH`` positioning; ``ESC[<sgr>m ESC[27m`` colour;
  ``ESC(0``/``ESC(B`` around line-drawing glyphs; 60x24.
* **Screens** follow the 2026-11-09 transcripts of the 1541ultimate lane
  (``tests/fixtures/remote_menu/*.json``): the root listing, a volume
  listing with a name field of ``width - 11`` columns, the status row with
  the path and ``-F3=HELP-``, the selected row in its own colour, and the
  context menu box drawn at the right edge with a hook on the selected row.
* **A fresh connection is a fresh UI** (``socket_gui_task`` builds a new
  ``UserInterface`` and ``TreeBrowser`` per connection) and starts at the
  root with the cursor on the first entry. Device state (the WiFi state,
  queued messages, whether the application is still running) is shared.
* **Diffs:** after a key only the rows that changed are re-sent.
* **Cursors clamp** at the first and last item (``TreeBrowserState::up/down``,
  ``ContextMenu::down``); nothing wraps.
* **Input is bytes, not telnet.** Every byte the client sends is a key,
  ``0xFF`` included: on the u64ii target ``char`` is unsigned, so
  ``SocketStream::get_char``'s ``buffer[0] == -1`` never matches and an
  ``IAC`` reply reaches the UI as keys (``IAC DO ECHO`` ends in ``$01``,
  ``KEY_CTRL_A``: select all). Any reply therefore shows up here as a
  disallowed key.
* **Queued messages** (``UserInterface::postMessage`` / ``getMessage``) pop up
  once, on whichever session's browser polls first, as an ``Ok`` popup
  (``UIPopup``) answered by the key ``o``.
* **Run Update** (``FileTypeUpdate::execute``): an image the firmware
  rejects shows an ``Ok`` popup with the reason; an accepted one stops the
  application, so the session goes silent without closing and new
  connections get no answer (modelled by :meth:`FakeUltimate.connector`).

* **Task menu** (``TaskMenu``/``TaskSubMenu``, 584250385): F5 opens it on
  the Ultimate line and F1 on the framed ``COMMODORE`` layout; the other key
  pages the listing. A window 30 columns wide, at least 8 rows, centred; a
  category opened with RIGHT or ENTER draws its submenu inside that window,
  except a category with exactly one enabled action, whose action runs at
  once. Disabled items are drawn in their own colour and skipped by the
  cursor. Printer's items follow :attr:`FakeUltimate.printer_on` the way
  ``IecPrinter::update_task_items`` does.

Knobs on :class:`FakeUltimate` reproduce the bench facts in #543: the mirror
that does not redraw after ENTER, the modal "SID changed" dialog on a fresh
session, the password prompt, the session cap.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field

WIDTH = 60
HEIGHT = 24

ESC = "\x1b"
PREAMBLE = b"\xff\xfe\x22\xff\xfb\x01"   # IAC DONT LINEMODE, IAC WILL ECHO

# Colours as the transcripts show them. U64E and the C64U dev image mark the
# cursor row 0;32;1 against 0;37;2; the official C64U 1.1.0 (COMMODORE build)
# marks it 0;37;1 against 0;31;2 (1541ultimate tests/e2e/lib/telnet_backend.py).
SGR_SEL = "0;32;1"
SGR_FG = "0;37;2"
SGR_DIR = "0;31;1"
SGR_FILE = "0;33"
SGR_PATH = "0;36"
SGR_STATUS = "0;32;1"
SGR_INACTIVE = "0;30;1"

UPDATE_EXTENSIONS = ("U64", "UE2", "CFW")
FILE_ACTIONS = ["Copy to...", "Move to...", "Rename", "Delete"]


@dataclass
class Entry:
    name: str
    kind: str          # "DIR" or a file extension such as "UE2"
    size: str = ""     # "8830K"


@dataclass
class Cell:
    ch: str = " "
    sgr: str = SGR_FG
    reverse: bool = False
    glyph: bool = False    # drawn in the VT100 line-drawing set


def _blank_screen() -> list[list[Cell]]:
    return [[Cell() for _ in range(WIDTH)] for _ in range(HEIGHT)]


def _put(scr, row, col, text, sgr=SGR_FG, *, reverse=False, glyph=False):
    for i, ch in enumerate(text):
        c = col + i
        if 0 <= c < WIDTH and 0 <= row < HEIGHT:
            scr[row][c] = Cell(ch, sgr, reverse, glyph)


def render_row(row: int, cells: list[Cell]) -> bytes:
    """One full row in the firmware's style: position, then runs of colour."""
    out = [f"{ESC}[{row + 1};1H"]
    cur = None
    alt = False
    for cell in cells:
        key = (cell.sgr, cell.reverse)
        if key != cur:
            out.append(f"{ESC}[{cell.sgr}m{ESC}[{7 if cell.reverse else 27}m")
            cur = key
        if cell.glyph and not alt:
            out.append(f"{ESC}(0")
            alt = True
        elif not cell.glyph and alt:
            out.append(f"{ESC}(B")
            alt = False
        out.append(cell.ch)
    if alt:
        out.append(f"{ESC}(B")
    return "".join(out).encode("latin-1")


@dataclass
class SessionLog:
    """What one connection received from the driver."""

    index: int
    raw: bytearray = field(default_factory=bytearray)
    keys: list[str] = field(default_factory=list)
    #: ("label", "path") of every ENTER that executed a context-menu action.
    commits: list[tuple[str, str]] = field(default_factory=list)
    commit_times: list[float] = field(default_factory=list)
    connected_at: float = 0.0
    closed_by_client: bool = False


class FakeUltimate:
    """A scripted Ultimate telnet menu on 127.0.0.1.

    :param title: the header text between ``***`` markers.
    :param root: root entries as ``(name, description, status)``; the WiFi
        row is drawn from :attr:`wifi` and must be listed as ``("WiFi", "", "")``.
    :param tree: directory listings keyed by path (``"/USB1/"``).
    :param wifi: ``dict(state=..., ident=..., ip=..., mac=...)``; ``state`` is
        one of ``link-up``, ``link-down``, ``disabled``, ``off``, ``scanning``,
        ``busy``, ``firmware?``, ``no-module``.
    :param wifi_after_disable / wifi_after_enable: the states the WiFi row
        walks through after the action, one step per later connection.
    :param redraw_after_commit: ``False`` reproduces the mirror that sends
        nothing after the action's ENTER (bench fact, #543).
    :param commit_takes_effect: ``False`` models an ENTER that is lost.
    :param update: what Run Update does: ``"start"`` (the application stops),
        ``"ignore"`` (nothing happens), or ``"reject:<popup text>"``.
    :param update_load_s: seconds between the ENTER and the application
        stopping. How long a real load takes is not established: on the
        C64U on 2026-11-09 the first redraw came 9.6 s after the first ENTER,
        either a slow load of 8.8 MB or a lost first ENTER.
    :param messages: queued ``postMessage`` texts; an entry may also be
        ``(text, buttons)`` to draw a popup with other buttons.
    :param start_path: where a fresh session opens ("Enter Home on Startup").
    :param enter_cursor_at_end: entering a directory puts the cursor on its
        last entry instead of the first.
    :param menu_initial: the context menu's first highlighted item.
    :param menu_keys_swapped: UP and DOWN are swapped inside a context menu.
    :param flood_after_commit: after the action's ENTER the session keeps
        sending and never goes quiet.
    :param framed: the official C64U 1.1.0 layout (``COMMODORE``): the browser
        inside a border, ``F7=HELP`` and its own colours. Source-read only.
    :param password: send ``Password: `` first (``socket_ensure_authenticated``).
    :param refuse_sessions: answer like a device at its session cap.
    :param printer_on: the printer emulation's state (``printer_enable``).
    :param task_categories: other categories as ``{name: [labels]}``, after
        Printer; a one-item category runs its action when opened.
    :param task_progress_s: Flush/Eject draws a progress box for this long.
    :param task_right_ignored: RIGHT and ENTER on a category open nothing.
    :param task_key_ignored: the task key opens nothing (a build that maps it
        to something else).
    :param refuse_after_commit: once a task action has run, every new
        connection is refused like a device at its session cap.
    """

    def __init__(
        self,
        *,
        title: str = "*** Ultimate 64 Elite (V1.50) 3.15a *** Remote ***",
        root: list[tuple[str, str, str]] | None = None,
        tree: dict[str, list[Entry]] | None = None,
        wifi: dict | None = None,
        wifi_after_disable: list[dict] | None = None,
        wifi_after_enable: list[dict] | None = None,
        redraw_after_commit: bool = True,
        commit_takes_effect: bool = True,
        update: str = "start",
        update_load_s: float = 0.0,
        messages: list[str] | None = None,
        framed: bool = False,
        password: str | None = None,
        refuse_sessions: bool = False,
        start_path: str = "/",
        enter_cursor_at_end: bool = False,
        menu_initial: int = 0,
        menu_keys_swapped: bool = False,
        flood_after_commit: bool = False,
        printer_on: bool = True,
        task_categories: dict[str, list[str]] | None = None,
        task_progress_s: float = 0.0,
        task_right_ignored: bool = False,
        task_key_ignored: bool = False,
        refuse_after_commit: bool = False,
    ) -> None:
        self.title = title
        self.root = root if root is not None else [
            ("Temp", "RAM Disk", "Ready"),
            ("Flash", "Flash Disk", "Ready"),
            ("USB1", "Ultra Fit", "Ready"),
            ("Ftp", "Remote FTP Servers", "Ready"),
            ("Net0", "IP: 127.0.0.1", "Link Up"),
            ("WiFi", "", ""),
        ]
        self.tree = tree if tree is not None else {
            "/USB1/": [
                Entry("Backups", "DIR"),
                Entry("firmware", "DIR"),
                Entry("update_m3.u64", "U64", "5609K"),
                Entry("vf_ue.u64", "U64", "5503K"),
                Entry("wraptor.d64", "D64", "171K"),
            ],
            "/USB1/firmware/": [Entry("cu_vf.ue2", "UE2", "8830K")],
            "/USB1/Backups/": [],
        }
        self.wifi = dict(wifi or {"state": "link-up", "ip": "10.0.0.82"})
        self.wifi_after_disable = list(wifi_after_disable or [
            {"state": "disabled", "ident": "ESP32 WiFi Bridge V0.903 bench"}])
        self.wifi_after_enable = list(wifi_after_enable or [
            {"state": "link-down", "mac": "3C:DC:75:4A:38:60"},
            {"state": "link-up", "ip": "10.0.0.82"}])
        self._pending_wifi: list[dict] = []
        self.redraw_after_commit = redraw_after_commit
        self.commit_takes_effect = commit_takes_effect
        self.update = update
        self.update_load_s = update_load_s
        self.messages = list(messages or [])
        self.framed = framed
        self.password = password
        self.refuse_sessions = refuse_sessions
        self.start_path = start_path
        self.enter_cursor_at_end = enter_cursor_at_end
        self.menu_initial = menu_initial
        self.menu_keys_swapped = menu_keys_swapped
        self.flood_after_commit = flood_after_commit
        self.printer_on = printer_on
        self.task_categories = dict(task_categories if task_categories is not None else {
            "Software IEC": ["Turn On", "Reset"],
            "Power": ["Reboot"],
        })
        self.task_progress_s = task_progress_s
        self.task_right_ignored = task_right_ignored
        self.task_key_ignored = task_key_ignored
        self.refuse_after_commit = refuse_after_commit
        self.sgr_sel = "0;37;1" if framed else SGR_SEL
        self.sgr_fg = "0;31;2" if framed else SGR_FG
        #: True once Run Update stopped the application: no new connection
        #: is answered (see :meth:`connector`).
        self.app_stopped = False
        self.sessions: list[SessionLog] = []
        self.connect_attempts = 0
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._threads: list[threading.Thread] = []
        self._conns: list[socket.socket] = []
        self._stop = threading.Event()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> tuple[str, int]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(8)
        s.settimeout(0.05)
        self._sock = s
        t = threading.Thread(target=self._accept_loop, daemon=True)
        t.start()
        self._threads.append(t)
        return "127.0.0.1", s.getsockname()[1]

    def stop(self) -> None:
        self._stop.set()
        for c in list(self._conns):
            try:
                c.close()
            except OSError:
                pass
        if self._sock is not None:
            self._sock.close()
        for t in self._threads:
            t.join(timeout=2)

    def connector(self, address, timeout=None):
        """``socket.create_connection`` as a device that stopped answering.

        Once the application has jumped to the updater there is no network
        stack, so a SYN gets no answer: the driver sees a timeout.
        """
        with self._lock:
            self.connect_attempts += 1
            stopped = self.app_stopped
        if stopped:
            raise TimeoutError("timed out (fake: application stopped)")
        return socket.create_connection(address, timeout=timeout)

    # -- derived views ---------------------------------------------------
    def all_commits(self) -> list[tuple[str, str]]:
        return [c for s in self.sessions for c in s.commits]

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            with self._lock:
                log = SessionLog(index=len(self.sessions), connected_at=time.monotonic())
                self.sessions.append(log)
            self._conns.append(conn)
            t = threading.Thread(target=_Session(self, conn, log).run, daemon=True)
            t.start()
            self._threads.append(t)

    # -- device state shared across sessions ------------------------------
    def wifi_row(self, inner_width: int) -> str:
        w = self.wifi
        st = w["state"]
        if st == "link-up":
            return f"WiFi    IP: {w.get('ip', ''):<{inner_width - 21}}Link Up"
        if st == "link-down":
            return f"WiFi    MAC {w.get('mac', '')}{'':<{inner_width - 38}}Link Down"
        word = {"disabled": "Disabled", "off": "Disabled", "scanning": "Scanning",
                "busy": "Busy", "firmware?": "Firmware?", "no-module": "No Module"}[st]
        detail = {"busy": "Programming ESP32", "off": "", "no-module": ""}.get(
            st, w.get("ident", ""))
        return f"WiFi    {detail:<{inner_width - 17}}{word}"

    def wifi_actions(self) -> list[str]:
        st = self.wifi["state"]
        if st == "link-up":
            return ["Forget APs", "Disconnect", "Disable"]
        if st == "link-down":
            return ["Show APs..", "Connect", "Connect to..", "Forget APs", "Disable"]
        if st in ("disabled", "off"):
            return ["Enable"]
        if st == "no-module":
            return ["Disable"]
        return []

    def advance_wifi(self) -> None:
        """One step along a pending WiFi transition (called per connection)."""
        with self._lock:
            if self._pending_wifi:
                self.wifi = dict(self._pending_wifi.pop(0))

    def tasks(self) -> list[tuple[str, list[tuple[str, bool]]]]:
        """Categories as ``(name, [(label, enabled)])``, shown items only."""
        if self.printer_on:
            printer = [("Flush/Eject", True), ("Reset", True), ("Turn Off", True)]
        else:
            printer = [("Flush/Eject", False), ("Reset", False), ("Turn On", True)]
        cats = [("Printer", printer)]
        cats += [(n, [(a, True) for a in acts]) for n, acts in self.task_categories.items()]
        return cats

    def pop_message(self):
        with self._lock:
            return self.messages.pop(0) if self.messages else None

    def post_message(self, msg) -> None:
        with self._lock:
            self.messages.append(msg)


class _Session:
    """One connection: a fresh UserInterface with a TreeBrowser at the root."""

    def __init__(self, dev: FakeUltimate, conn: socket.socket, log: SessionLog) -> None:
        self.dev = dev
        self.conn = conn
        self.log = log
        self.path = dev.start_path
        self.cursor = 0
        self.first = 0
        self.menu: list[str] | None = None
        self.menu_index = 0
        self.popup: tuple[str, list[str]] | None = None
        self.tasks: list | None = None     # the task menu, when open
        self.task_index = 0
        self.sub: list[tuple[str, bool]] | None = None   # the open category
        self.sub_name = ""
        self.sub_index = 0
        self.progress: str | None = None
        self.sent = _blank_screen()
        self.silent = False   # the application has stopped: never send again
        self.inner_width = WIDTH - 2 if dev.framed else WIDTH
        self.list_top = 3 if dev.framed else 2
        self.list_rows = 19 if dev.framed else 21
        self.col0 = 1 if dev.framed else 0

    # -- wire ------------------------------------------------------------
    def send(self, data: bytes) -> None:
        if self.silent:
            return
        try:
            self.conn.sendall(data)
        except OSError:
            pass

    def run(self) -> None:
        dev = self.dev
        if dev.refuse_sessions:
            self.send(b"Too many connections, try again later.\r\n")
            self.conn.close()
            return
        dev.advance_wifi()
        if dev.password is not None:
            self.send(b"Password: \xff\xfb\x01")
            self.conn.settimeout(5)
            try:
                self.conn.recv(64)
            except OSError:
                pass
            self.conn.close()
            return
        self.send(PREAMBLE + b"\x1bc")
        self.sent = _blank_screen()
        self.redraw(full=True)
        msg = dev.pop_message()
        if msg is not None:
            self.popup = (msg, ["Ok"]) if isinstance(msg, str) else (msg[0], list(msg[1]))
            self.redraw()
        self.conn.settimeout(0.05)
        buf = b""
        while not dev._stop.is_set():
            try:
                chunk = self.conn.recv(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                self.log.closed_by_client = True
                break
            # Every byte on the wire is recorded, also once the application
            # has stopped: a key sent then sits unread in the device's TCP
            # buffer, but it was still sent (the kit's 2026-11-09 resend).
            self.log.raw.extend(chunk)
            if self.silent:
                continue  # the application is gone; nothing reads the socket
            buf += chunk
            buf = self._consume(buf)
        try:
            self.conn.close()
        except OSError:
            pass

    def _consume(self, buf: bytes) -> bytes:
        keys = {b"\x1b[A": "UP", b"\x1b[B": "DOWN", b"\x1b[C": "RIGHT",
                b"\x1b[D": "LEFT", b"\x1b[5~": "PAGEUP", b"\x1b[6~": "PAGEDOWN",
                b"\x1b[11~": "F1", b"\x1b[15~": "F5"}
        while buf:
            if buf[0] == 0x1B:
                for seq, name in keys.items():
                    if buf.startswith(seq):
                        self.key(name)
                        buf = buf[len(seq):]
                        break
                else:
                    if len(buf) < 5:
                        return buf
                    self.key("ESC?" + buf[:5].hex())
                    buf = buf[1:]
                continue
            b = buf[0]
            buf = buf[1:]
            name = {0x0D: "ENTER", 0x11: "RUNSTOP", 0x0A: "LF", 0x01: "CTRL_A",
                    0xFF: "0xFF"}.get(b, chr(b))
            self.key(name)
        return buf

    # -- UI model ----------------------------------------------------------
    def entries(self) -> list:
        if self.path == "/":
            return self.dev.root
        return self.dev.tree.get(self.path, [])

    def key(self, name: str) -> None:
        if self.silent:
            return
        self.log.keys.append(name)
        if self.popup is not None:
            if name == "o" and "Ok" in self.popup[1]:
                self.popup = None
                self.redraw()
            return
        if self.menu is not None:
            self.menu_key(name)
            return
        if self.tasks is not None:
            self.task_key(name)
            return
        task_key, page_key = ("F1", "F5") if self.dev.framed else ("F5", "F1")
        if name == task_key and not self.dev.task_key_ignored:
            self.tasks = self.dev.tasks()
            self.task_index = 0
            self.redraw()
            return
        if name == page_key:
            name = "PAGEDOWN" if name == "F5" else "PAGEUP"
        n = len(self.entries())
        if name == "DOWN" and n:
            self.cursor = min(n - 1, self.cursor + 1)
        elif name == "UP" and n:
            self.cursor = max(0, self.cursor - 1)
        elif name == "PAGEDOWN" and n:
            self.cursor = min(n - 1, self.cursor + self.list_rows // 2)
        elif name == "PAGEUP" and n:
            self.cursor = max(0, self.cursor - self.list_rows // 2)
        elif name == "RIGHT" and n:
            e = self.entries()[self.cursor]
            if self.path == "/" and e[0] not in ("Net0", "WiFi", "Ftp"):
                self.path = f"/{e[0]}/"
                self.enter_dir()
            elif self.path != "/" and e.kind == "DIR":
                self.path = f"{self.path}{e.name}/"
                self.enter_dir()
        elif name == "LEFT" and self.path != "/":
            parts = self.path.strip("/").split("/")
            self.path = "/" + "/".join(parts[:-1]) + ("/" if len(parts) > 1 else "")
            self.cursor = 0
            self.first = 0
        elif name == "ENTER" and n:
            self.menu = self.context_items()
            self.menu_index = min(self.dev.menu_initial, max(0, len(self.menu) - 1))
            if not self.menu:
                self.menu = None
        self.redraw()

    def enter_dir(self) -> None:
        self.first = 0
        n = len(self.entries())
        self.cursor = n - 1 if (self.dev.enter_cursor_at_end and n) else 0

    def context_items(self) -> list[str]:
        e = self.entries()[self.cursor]
        if self.path == "/":
            return self.dev.wifi_actions() if e[0] == "WiFi" else []
        if e.kind in UPDATE_EXTENSIONS:
            return ["Run Update"] + FILE_ACTIONS
        return list(FILE_ACTIONS)

    def menu_key(self, name: str) -> None:
        if self.dev.menu_keys_swapped and name in ("UP", "DOWN"):
            name = "UP" if name == "DOWN" else "DOWN"
        if name == "DOWN":
            self.menu_index = min(len(self.menu) - 1, self.menu_index + 1)
        elif name == "UP":
            self.menu_index = max(0, self.menu_index - 1)
        elif name in ("RUNSTOP", "LEFT"):
            self.menu = None
        elif name == "ENTER":
            label = self.menu[self.menu_index]
            self.menu = None
            self.commit(label)
            return
        self.redraw()

    def task_key(self, name: str) -> None:
        if self.sub is not None:
            enabled = [i for i, (_, en) in enumerate(self.sub) if en]
            if name == "DOWN":
                nxt = [i for i in enabled if i > self.sub_index]
                self.sub_index = nxt[0] if nxt else self.sub_index
            elif name == "UP":
                prv = [i for i in enabled if i < self.sub_index]
                self.sub_index = prv[-1] if prv else self.sub_index
            elif name == "LEFT":
                self.sub = None
            elif name == "ENTER" and self.sub[self.sub_index][1]:
                self.run_task_action(self.sub_name, self.sub[self.sub_index][0])
                return
            self.redraw()
            return
        if name == "DOWN":
            self.task_index = min(len(self.tasks) - 1, self.task_index + 1)
        elif name == "UP":
            self.task_index = max(0, self.task_index - 1)
        elif name == "LEFT":
            self.tasks = None
        elif name in ("RIGHT", "ENTER") and not self.dev.task_right_ignored:
            cat, acts = self.tasks[self.task_index]
            if len(acts) == 1 and acts[0][1]:   # TaskMenu::select_item
                self.run_task_action(cat, acts[0][0])
                return
            self.sub, self.sub_name = acts, cat
            self.sub_index = next((i for i, (_, en) in enumerate(acts) if en), 0)
        self.redraw()

    def run_task_action(self, cat: str, label: str) -> None:
        dev = self.dev
        self.tasks = None
        self.sub = None
        self.log.commits.append((f"{cat}/{label}", "tasks"))
        self.log.commit_times.append(time.monotonic())
        if dev.flood_after_commit:
            threading.Thread(target=self._flood, daemon=True).start()
        if dev.refuse_after_commit:
            dev.refuse_sessions = True
        if dev.commit_takes_effect and cat == "Printer":
            if label == "Turn Off":
                dev.printer_on = False
            elif label == "Turn On":
                dev.printer_on = True
            elif label == "Flush/Eject" and dev.task_progress_s:
                self.progress = "Flushing/Ejecting page..."
                self.redraw()
                time.sleep(dev.task_progress_s)
                self.progress = None
        if dev.redraw_after_commit:
            self.redraw()

    def commit(self, label: str) -> None:
        dev = self.dev
        self.log.commits.append((label, self.path))
        self.log.commit_times.append(time.monotonic())
        if dev.flood_after_commit:
            threading.Thread(target=self._flood, daemon=True).start()
        if label == "Run Update":
            self.run_update()
            return
        if dev.commit_takes_effect and label in ("Disable", "Enable"):
            seq = dev.wifi_after_disable if label == "Disable" else dev.wifi_after_enable
            with dev._lock:
                dev.wifi = dict(seq[0])
                dev._pending_wifi = [dict(s) for s in seq[1:]]
        if dev.redraw_after_commit:
            self.redraw()

    def _flood(self) -> None:
        end = time.monotonic() + 3.0
        while time.monotonic() < end and not self.dev._stop.is_set():
            self.send(b"\x1b[24;1H\x1b[0;36m\x1b[27m/ ")
            time.sleep(0.01)

    def run_update(self) -> None:
        dev = self.dev
        if dev.update.startswith("reject:"):
            self.popup = (dev.update.split(":", 1)[1], ["Ok"])
            self.redraw()
            return
        if dev.update == "ignore":
            if dev.redraw_after_commit:
                self.redraw()
            return
        # Accepted: the menu closes and the listing is redrawn into the
        # socket buffer, the file loads, then the application jumps to the
        # updater. Only the first segment of that redraw ever left the
        # device in the transcripts (1460 B on the C64U).
        if dev.update_load_s:
            time.sleep(dev.update_load_s)
        data = self.redraw(full=True, emit=False)
        self.send(data[:1460])
        with dev._lock:
            dev.app_stopped = True
        self.silent = True

    # -- drawing -----------------------------------------------------------
    def compose(self) -> list[list[Cell]]:
        dev = self.dev
        scr = _blank_screen()
        hpos = (WIDTH - len(dev.title)) // 2
        _put(scr, 0, hpos, dev.title, "0;37;1")
        _put(scr, 1, 0, "q" * WIDTH, SGR_FG, glyph=True)
        if dev.framed:
            _put(scr, 2, 0, "l" + "q" * (WIDTH - 2) + "k", SGR_FG, glyph=True)
            for r in range(3, 22):
                _put(scr, r, 0, "x", SGR_FG, glyph=True)
                _put(scr, r, WIDTH - 1, "x", SGR_FG, glyph=True)
            _put(scr, 22, 0, "m" + "q" * (WIDTH - 2) + "j", SGR_FG, glyph=True)
        entries = self.entries()
        if self.cursor < self.first:
            self.first = self.cursor
        if self.cursor >= self.first + self.list_rows:
            self.first = self.cursor - self.list_rows + 1
        iw = self.inner_width
        if not entries:
            _put(scr, self.list_top, self.col0, "< No Items >".ljust(iw), SGR_FG)
        for i in range(self.list_rows):
            idx = self.first + i
            row = self.list_top + i
            if idx >= len(entries):
                break
            sel = idx == self.cursor
            name_sgr = dev.sgr_sel if sel else dev.sgr_fg
            e = entries[idx]
            if self.path == "/":
                if e[0] == "WiFi":
                    text = dev.wifi_row(iw)
                    _put(scr, row, self.col0, text[:51 - (60 - iw)].ljust(51 - (60 - iw)), name_sgr)
                    _put(scr, row, self.col0 + 51 - (60 - iw), text[51 - (60 - iw):].ljust(9), SGR_STATUS)
                else:
                    _put(scr, row, self.col0, f"{e[0]:<8}{e[1]:<{iw - 17}}", name_sgr)
                    _put(scr, row, self.col0 + iw - 9, f"{e[2]:<9}", SGR_STATUS)
            else:
                field_w = iw - 11
                _put(scr, row, self.col0, e.name[:field_w].ljust(field_w), name_sgr)
                if e.kind == "DIR":
                    _put(scr, row, self.col0 + field_w, " DIR".ljust(11), SGR_DIR)
                else:
                    _put(scr, row, self.col0 + field_w, f" {e.kind:>3} {e.size}".ljust(11), SGR_FILE)
        _put(scr, 23, 0, self.path.ljust(WIDTH - 9), SGR_PATH)
        if dev.framed:
            _put(scr, 23, WIDTH - 8, "F7=HELP", "0;37;1")
        else:
            _put(scr, 23, WIDTH - 9, "-F3=HELP-", "0;37;1")
        if self.menu is not None:
            self.compose_menu(scr)
        if self.tasks is not None:
            self.compose_tasks(scr)
        if self.progress is not None:
            self.compose_box(scr, 8, 15, 30, 5, [self.progress, "", "####"])
        if self.popup is not None:
            self.compose_popup(scr)
        return scr

    def compose_menu(self, scr) -> None:
        items = self.menu
        max_len = min(25, max(len(a) for a in items))
        rows = len(items) + 2
        sel_line = self.list_top + (self.cursor - self.first)
        top = max(self.list_top, min(sel_line - 1, self.list_top + self.list_rows - rows))
        left = self.col0 + self.inner_width - 2 - max_len
        _put(scr, top, left, "l" + "q" * max_len + "k", SGR_FG, glyph=True)
        _put(scr, top + rows - 1, left, "m" + "q" * max_len + "j", SGR_FG, glyph=True)
        for i in range(1, rows - 1):
            _put(scr, top + i, left, "x", SGR_FG, glyph=True)
            _put(scr, top + i, left + max_len + 1, "x", SGR_FG, glyph=True)
            label = items[i - 1]
            sgr = self.dev.sgr_sel if i - 1 == self.menu_index else self.dev.sgr_fg
            _put(scr, top + i, left + 1, label.ljust(max_len), sgr)
        hook = "q" if sel_line in (top, top + rows - 1) else "k"
        _put(scr, sel_line, left, hook, SGR_FG, glyph=True)

    def compose_box(self, scr, top, left, width, rows, labels, sel=None, sgrs=None):
        _put(scr, top, left, "l" + "q" * (width - 2) + "k", SGR_FG, glyph=True)
        _put(scr, top + rows - 1, left, "m" + "q" * (width - 2) + "j", SGR_FG, glyph=True)
        for i in range(1, rows - 1):
            _put(scr, top + i, left, "x", SGR_FG, glyph=True)
            _put(scr, top + i, left + width - 1, "x", SGR_FG, glyph=True)
            label = labels[i - 1] if i - 1 < len(labels) else ""
            sgr = (sgrs[i - 1] if sgrs and i - 1 < len(sgrs) else self.dev.sgr_fg)
            if sel is not None and i - 1 == sel:
                sgr = self.dev.sgr_sel
            _put(scr, top + i, left + 1, label.ljust(width - 2), sgr)

    def compose_tasks(self, scr) -> None:
        """TaskMenu::init: 28 + 2 wide, at least 8 rows, centred. The
        submenu (ContextMenu::init) sits inside it at its right edge, its top
        border one row above the chosen category (``y_offs = y - 1``) and a
        hook on its left edge at that category's row, as on the U64E
        (178517dec, 2026-11-10)."""
        names = [c for c, _ in self.tasks]
        rows = max(8, min(len(names) + 2, HEIGHT - 4))
        top = (HEIGHT - rows) // 2
        left = (WIDTH - 30) // 2
        self.compose_box(scr, top, left, 30, rows, names, sel=self.task_index)
        if self.sub is not None:
            labels = [a for a, _ in self.sub]
            w = max(len(a) for a in labels) + 2
            srows = len(labels) + 2
            stop = max(top + 1, min(top + self.task_index, top + rows - 1 - srows))
            sleft = left + 30 - 1 - w
            sgrs = [self.dev.sgr_fg if en else SGR_INACTIVE for _, en in self.sub]
            self.compose_box(scr, stop, sleft, w, srows, labels, sel=self.sub_index, sgrs=sgrs)
            hook_row = top + 1 + self.task_index
            hook = "q" if hook_row in (stop, stop + srows - 1) else "k"
            _put(scr, hook_row, sleft, hook, SGR_FG, glyph=True)

    def compose_popup(self, scr) -> None:
        msg, buttons = self.popup
        names = [f" {b} " for b in buttons]
        bw = sum(len(n) for n in names)
        ww = max(len(msg), bw)
        x1 = (WIDTH - ww) // 2
        y1 = (HEIGHT - 5) // 2
        _put(scr, y1, x1, "l" + "q" * ww + "k", SGR_FG, glyph=True)
        _put(scr, y1 + 4, x1, "m" + "q" * ww + "j", SGR_FG, glyph=True)
        for r in range(1, 4):
            _put(scr, y1 + r, x1, "x", SGR_FG, glyph=True)
            _put(scr, y1 + r, x1 + 1, " " * ww, SGR_FG)
            _put(scr, y1 + r, x1 + ww + 1, "x", SGR_FG, glyph=True)
        _put(scr, y1 + 1, x1 + 1 + (ww - len(msg)) // 2, msg, SGR_FG)
        x = x1 + 1 + (ww - bw) // 2
        for i, n in enumerate(names):
            _put(scr, y1 + 3, x, n, SGR_FG, reverse=(i == 0))
            x += len(n)

    def redraw(self, *, full: bool = False, emit: bool = True) -> bytes:
        scr = self.compose()
        out = bytearray()
        for r in range(HEIGHT):
            if full or scr[r] != self.sent[r]:
                out += render_row(r, scr[r])
        self.sent = scr
        if emit and out:
            self.send(bytes(out))
        return bytes(out)

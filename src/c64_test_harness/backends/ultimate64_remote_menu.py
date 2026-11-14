"""The Ultimate's Telnet Remote Menu, driven from Python (issue #543).

The firmware serves its file browser over telnet (``socket_gui.cc``, the
"Telnet Remote Menu" service in Network Settings). This module is a
harness-owned client for it: a minimal telnet session, a VT100 screen
model, and a driver that browses to a path, opens an entry's context
menu, and chooses an action by its exact label. It reads and toggles the
WiFi row on the browser root, starts "Run Update" on an update image, and
runs a task-menu action (Tasks > Printer > Flush/Eject, #566).

Python 3.13 removed ``telnetlib`` and the harness has no runtime
dependencies, so the protocol is written here. 1541ultimate's
``tests/e2e/lib/telnet_backend.py`` was the reference (owner decision Q8,
2026-11-09); nothing is imported from it.

**Rules, all enforced before anything is sent:**

* **The caller must hold the device's** ``DeviceLock`` (any lock dir, keyed
  the way ``DeviceLock`` keys it, so addresses listed together in
  ``devices.toml`` fold to one key). Every call checks it before every
  connection, reads included: a telnet session is a UI session on a shared
  device. From 3a1ff9ff on the firmware also caps them at four
  (``TELNET_MAX_SESSIONS``); 1.1.0 (7b628eb1) has no cap.
* **Nothing is sent that the UI would not read as an intended key, not
  even telnet negotiation.** The server's ``IAC`` options are parsed and
  discarded, never answered (1541ultimate's reference does the same). On
  the u64ii target ``char`` is unsigned (riscv32, no ``-fsigned-char`` in
  ``target/u64ii/riscv/ultimate/Makefile``), so ``SocketStream::get_char``'s
  ``buffer[0] == -1`` never matches, and a reply such as ``IAC DO ECHO``
  would reach the menu as three keys, the last ``$01`` = ``KEY_CTRL_A``,
  select all (``tree_browser.cc``).
* **A state-changing ENTER is sent exactly once.** It is never resent, not
  when the screen does not change, not when verification fails. The
  verdict comes from a **fresh connection**: every connection gets a fresh
  ``UserInterface`` (``socket_gui_task``), and the mirror on the
  committing session sends only the rows that changed, and may send
  nothing at all after the ENTER (bench fact, #543).
* **The highlight is read, never assumed.** The browser and the context
  menu mark the cursor row by colour (``color_sel``); the driver learns
  that colour on the root screen of each session, moves one key at a time
  and reads the highlight back after every key. A key with no visible
  effect stops the call; cursors clamp at the ends and do not wrap
  (``TreeBrowserState::up/down``, ``ContextMenu::down``).
* **Only allow-listed keys reach the wire**: the cursor keys, VT100
  PageUp/PageDown (``ESC[5~``/``ESC[6~``, which every build maps the same
  way), ENTER as a bare CR, ``o`` for an ``Ok`` popup, and one F-key, only
  in the browser, to open the task menu. The F-keys mean different things
  per build (``UserInterface::keymapper``): the task menu is F5 on the
  Ultimate line and F1 on the ``COMMODORE`` build (1.1.0), where F5 is
  PageDown, so the driver picks the key from the layout it reads (framed =
  ``COMMODORE``). Either key, sent to the other build, only pages the
  listing. A menu is never closed with a key; the session is closed instead. Never LF (``$0A`` is ``KEY_CTRL_J``, swap
  joysticks), never ``ESC[2~`` (``KEY_INSERT``: delete selected), never a
  letter in the browser (on the official C64U, WASD navigation is the
  default and turns letters into cursor keys).
* **Modal screens stop the call**, except an ``Ok``-only popup whose text
  is in :data:`DISMISSABLE_MESSAGES` ("SID changed. Please review
  settings", which a fresh session may open on): that is answered with one
  ``o``, logged at WARNING and recorded in :attr:`dismissed_messages`.
  ``RETURN`` is never used on a popup: it picks the active button, which on
  a Yes/No popup is Yes. A popup that a task action draws after its ENTER
  ends :meth:`Ultimate64RemoteMenu.run_task` without an error: its text
  and buttons go into the result and it is left unanswered.
* **Every new session can take queued device messages**, read-only
  calls, verification polls and probes included: ``UserInterface::postMessage``
  queues device-wide, and the browser of whichever session polls next pops
  one per poll (``TreeBrowser::poll``, ``getMessage``) and shows it as a
  popup. When each is dismissable, a session answers it and the next one
  can follow, up to four (the queue's size); a probe never answers one, so
  it shows at most one. So a call can consume messages meant for someone at
  the device. Each result's
  ``messages`` lists what this call's sessions showed, dismissed or not;
  :attr:`dismissed_messages` keeps the ones this object answered.
* **Zero** ``/Temp`` **cost by construction:** this module makes no REST
  or FTP request. Telnet input creates no attachment.

**Dry runs** (``dry_run=True`` on the state-changing calls) still connect,
read, and navigate: they move the cursor, enter directories, and open the
context menu, then stop with the wanted action highlighted and disconnect
without the ENTER that would run it. Those keys only move UI state in the
connection's own ``UserInterface``, which the firmware discards when the
connection closes; they change no setting, file or radio state. That makes
a dry run proof that the path exists, the action is offered and the
highlight can be put on it, which a run that sent nothing could not show.
The one device-wide effect it can have is consuming queued messages (see
above).

**Run Update.** The updater's prompts ("Reformat Flash Disk?", "About to
update. Continue?", "Reset Configuration? (Recommended)", the ESP32
popups) are **not visible to this driver**: the application jumps to an
updater that links no network stack and draws on the C64 screen
(``FileTypeUpdate::execute``; ``update_common.h`` ``setup()`` takes the C64
as its host), and a bench test on 2026-11-09 confirmed that nothing of it
reaches telnet (see ``docs/firmware_flashing.md``). So the driver answers
no prompt; someone at the keyboard does. What it can see is a popup on
the committing session, or, once the application has jumped to the
updater, a session that goes silent without closing and fresh connections
that get no answer.
:meth:`Ultimate64RemoteMenu.run_update` reports which.

**Image validity is the caller's job.** The firmware checks almost nothing
before it jumps (``FileTypeUpdate``, ``filetype_u2p.cc``). A file is offered
"Run Update" when its extension is the product's update extension (``.u64``,
``.ue2``) or, from 3a1ff9ff on, ``.cfw``. A product image is loaded as-is
(``load_format_0``), with no header or product check. Only a ``.cfw``
(``load_format_1``, absent from 1.1.0) is checked, and refused with "Invalid
Update Image", "Wrong Product Family" or "Update for Wrong Product". The one
refusal a ``.u64``/``.ue2`` can get is a file-open error. So check the image
(product, hash) before uploading it; this driver cannot.

Evidence grades: the wire format and the screens are read from the
2026-11-09 transcripts of both devices (U64E and C64U, each on a 3.15a
bench image); the official C64U 1.1.0 layout (``COMMODORE`` build: the
browser inside a border, its own colours) is read from source at
7b628eb1 only and has not been seen on a wire.
"""

from __future__ import annotations

import errno
import logging
import re
import select
import socket
import time
from dataclasses import dataclass, field
from typing import Callable

from .ultimate64_client import Ultimate64Error, Ultimate64UnsafeOperationError
from .ultimate64_temp_gc import lock_held_for

__all__ = [
    "DISMISSABLE_MESSAGES",
    "RemoteMenuConnectError",
    "RemoteMenuLockError",
    "RemoteMenuNavigationError",
    "RemoteMenuNotFoundError",
    "RemoteMenuTiming",
    "RemoteMenuUnexpectedScreenError",
    "RemoteMenuVerifyError",
    "RunUpdateResult",
    "TASK_CATEGORIES",
    "TELNET_PORT",
    "TaskRunResult",
    "TelnetScreen",
    "Ultimate64RemoteMenu",
    "Ultimate64RemoteMenuError",
    "UpdateImageRejectedError",
    "WifiChangeResult",
    "WifiStatus",
    "parse_wifi_row",
    "read_view",
]

_log = logging.getLogger(__name__)

TELNET_PORT = 23

#: ``Screen_VT100::get_size_x/get_size_y`` at 7b628eb1 (C64U 1.1.0),
#: 3a1ff9ff and 7b15448a7. Both products' 2026-11-09 transcripts draw a
#: 60-column header rule.
WIDTH = 60
HEIGHT = 24

#: Width of the type/size column a directory listing draws after the name
#: (``BrowsableDirEntry::getDisplayString``: ``display_space = width - 11``).
_TYPE_COLUMN = 11
#: Root entries draw their name in an 8-column field ("SD      SD Card").
_ROOT_NAME_FIELD = 8
#: The longest unframed name field; a framed (COMMODORE) browser has 47.
_MAX_NAME_FIELD = WIDTH - _TYPE_COLUMN

#: Ok-only popups that may be answered with ``o``. Queued by
#: ``UserInterface::postMessage`` (``u64_config.cc``) and shown once, by
#: whichever session's browser polls first.
DISMISSABLE_MESSAGES = ("SID changed. Please review settings",)

#: Task-menu categories whose submenu this driver may open.
#: ``TaskMenu::select_item`` runs a category's action at once when the
#: category holds exactly one action and it is enabled, so opening any other
#: category could itself be a commit. A category goes on this list only once
#: the source shows it always holds more than one action. Printer: Flush/Eject,
#: Reset, Turn On, Turn Off (``IecPrinter::create_task_items``, 7b628eb1 and
#: 584250385).
TASK_CATEGORIES = ("Printer",)

#: Task actions whose effect a fresh connection can see: after the action,
#: the category's submenu offers this label (``IecPrinter::update_task_items``
#: shows Turn On or Turn Off, never both, by ``printer_enable`` on every
#: task-menu init). Flush/Eject and Reset draw nothing a telnet session can
#: read back.
_TASK_EFFECTS = {
    ("Printer", "Turn Off"): "Turn On",
    ("Printer", "Turn On"): "Turn Off",
}

#: Popup texts with which ``FileTypeUpdate::load_format_1`` refuses a ``.cfw``
#: (from 3a1ff9ff on). A ``.u64``/``.ue2`` is never checked; its one refusal
#: is a file-open error string.
_CFW_REFUSALS = ("Invalid Update Image", "Wrong Product Family", "Update for Wrong Product")

#: The only bytes this driver ever writes.
_KEYS: dict[str, bytes] = {
    "UP": b"\x1b[A",
    "DOWN": b"\x1b[B",
    "RIGHT": b"\x1b[C",
    "LEFT": b"\x1b[D",
    "PAGEUP": b"\x1b[5~",
    "PAGEDOWN": b"\x1b[6~",
    "ENTER": b"\r",
    "OK": b"o",
    # Only to open the task menu, and only in the browser: F5 on the
    # Ultimate line, F1 on the COMMODORE build (see :data:`TASK_CATEGORIES`).
    "F1": b"\x1b[11~",
    "F5": b"\x1b[15~",
}

# Telnet (RFC 854): parsed and discarded, never answered.
_IAC, _DONT, _DO, _WONT, _WILL, _SB, _SE = 255, 254, 253, 252, 251, 250, 240

#: The VT100 line-drawing glyphs ``Screen_VT100::output`` emits, as text.
_GLYPH_TEXT = {"l": "+", "k": "+", "m": "+", "j": "+", "q": "-", "x": "|",
               "t": "+", "u": "+", "v": "+", "w": "+", "n": "+"}
_BUTTONS = ("Ok", "Yes", "No", "All", "Cancel")

_NO_ANSWER_ERRNOS = {errno.EHOSTUNREACH, errno.EHOSTDOWN, errno.ENETUNREACH, errno.ETIMEDOUT}


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class Ultimate64RemoteMenuError(Ultimate64Error):
    """A Remote Menu call was refused or failed.

    ``enter_sent`` says whether the call's one state-changing ENTER had
    already been sent when it failed: if it is ``True`` the device may have
    acted, and the caller must not simply repeat the call. ``screen`` is the
    last screen read, when there was one.
    """

    def __init__(self, message: str, *, enter_sent: bool = False,
                 screen: tuple[str, ...] | None = None) -> None:
        super().__init__(message)
        self.enter_sent = enter_sent
        self.screen = screen


class RemoteMenuLockError(Ultimate64RemoteMenuError):
    """This process does not hold the device's ``DeviceLock``. Nothing was sent."""


class RemoteMenuConnectError(Ultimate64RemoteMenuError):
    """No usable session: unreachable, refused, password prompt, session cap,
    or no screen drawn."""


class RemoteMenuUnexpectedScreenError(Ultimate64RemoteMenuError):
    """The screen is not one the driver may act on: an unknown popup, a state
    with no action, a layout it cannot read. The call stopped there."""


class RemoteMenuNotFoundError(Ultimate64RemoteMenuError):
    """The path, entry or action label is not there (or cannot be matched
    unambiguously on a 60-column screen)."""


class RemoteMenuNavigationError(Ultimate64RemoteMenuError):
    """A navigation key had no visible effect, or the wrong one. Keys are not
    resent; the session was closed."""


class RemoteMenuVerifyError(Ultimate64RemoteMenuError):
    """The one ENTER was sent and fresh connections did not show its effect
    within the time allowed. It was not resent."""


class UpdateImageRejectedError(Ultimate64RemoteMenuError):
    """A popup appeared on the committing session after Run Update's ENTER
    (``popup`` is its text), so the application did not jump to the updater
    and nothing was flashed. The popup was left on that session.

    For a ``.u64``/``.ue2`` the firmware's only refusal is a file-open error:
    it does not check the image, so an image for the wrong product is *not*
    refused here (see the module docstring). Any other popup also stops the
    call and lands here.
    """

    def __init__(self, message: str, *, popup: str, screen: tuple[str, ...] | None = None) -> None:
        super().__init__(message, enter_sent=True, screen=screen)
        self.popup = popup


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RemoteMenuTiming:
    """How long the driver waits. Defaults are set from the 2026-11-09
    transcripts (first byte after a key within 15-50 ms, a full screen
    within 5 ms of its first segment) with a wide margin; tests shrink them.

    :param connect_timeout: TCP connect.
    :param first_byte_timeout: how long a key may take to draw something.
    :param idle_gap: silence that ends a redraw.
    :param max_drain: a redraw that never goes quiet within this is an error.
    :param poll_interval: between fresh-connection reads while verifying.
    :param first_probe_delay: after Run Update's ENTER, how long before the
        first fresh connection. While the application loads the image, every
        accepted connection still starts a UI task on the device; the delay
        keeps the first ones out of that window.
    """

    connect_timeout: float = 5.0
    first_byte_timeout: float = 2.0
    idle_gap: float = 0.2
    max_drain: float = 10.0
    poll_interval: float = 2.0
    first_probe_delay: float = 5.0


# ---------------------------------------------------------------------------
# The screen
# ---------------------------------------------------------------------------

class TelnetScreen:
    """A VT100 screen as the firmware's ``Screen_VT100`` drives it.

    Handles the telnet layer as well: ``IAC`` sequences are removed from the
    stream (also when split across reads) and discarded. Nothing is ever
    answered: on the u64ii target a reply would reach the menu as keys (see
    the module docstring).

    Each cell keeps its character, its attribute (the SGR parameter string in
    force and the reverse-video flag), and, for cells drawn in the
    line-drawing set, the raw glyph letter. :meth:`rows` shows glyphs as
    ``+``, ``-`` and ``|``, as 1541ultimate's renderer does.
    """

    def __init__(self, width: int = WIDTH, height: int = HEIGHT) -> None:
        self.width = width
        self.height = height
        self._iac: object = None
        self._esc: str | None = None
        self._csi = ""
        self.stream_text = ""
        self.reset()

    def reset(self) -> None:
        self.chars = [[" "] * self.width for _ in range(self.height)]
        self.attrs: list[list[tuple[str, bool]]] = [
            [("", False)] * self.width for _ in range(self.height)]
        self.glyphs: list[list[str | None]] = [[None] * self.width for _ in range(self.height)]
        self.x = 0
        self.y = 0
        self.sgr = ""
        self.reverse = False
        self.alt = False

    # -- input -----------------------------------------------------------
    def feed(self, data: bytes) -> None:
        for b in data:
            state = self._iac
            if state is None:
                if b == _IAC:
                    self._iac = "iac"
                else:
                    self._byte(b)
            elif state == "iac":
                if b == _IAC:
                    self._iac = None
                    self._byte(b)
                elif b in (_WILL, _WONT, _DO, _DONT):
                    self._iac = ("opt", b)
                elif b == _SB:
                    self._iac = "sb"
                else:
                    self._iac = None
            elif state == "sb":
                if b == _IAC:
                    self._iac = "sb-iac"
            elif state == "sb-iac":
                self._iac = None if b == _SE else "sb"
            else:
                self._iac = None  # the option byte of WILL/WONT/DO/DONT: discarded

    def _byte(self, byte: int) -> None:
        ch = chr(byte)
        if self._esc is None and byte != 0x1B and (byte >= 0x20 or ch in "\r\n"):
            self.stream_text = (self.stream_text + ch)[-80:]
        if self._esc == "csi":
            if 0x40 <= byte <= 0x7E:
                self._handle_csi(self._csi, ch)
                self._esc = None
            else:
                self._csi += ch
            return
        if self._esc == "charset":
            if ch == "0":
                self.alt = True
            elif ch == "B":
                self.alt = False
            self._esc = None
            return
        if self._esc == "esc":
            self._esc = None
            if ch == "[":
                self._esc = "csi"
                self._csi = ""
            elif ch == "(":
                self._esc = "charset"
            elif ch == "c":
                self.reset()
            return
        if byte == 0x1B:
            self._esc = "esc"
            return
        if ch == "\r":
            self.x = 0
            return
        if ch == "\n":
            self.x = 0
            self.y = min(self.height - 1, self.y + 1)
            return
        if ch == "\b":
            self.x = max(0, self.x - 1)
            return
        if byte < 0x20:
            return
        glyph = None
        if self.alt:
            glyph = ch
            ch = _GLYPH_TEXT.get(ch, ch)
        self._put(ch, glyph)

    def _handle_csi(self, params: str, final: str) -> None:
        parts = [p for p in params.split(";") if p]
        if final in "Hf":
            row = int(parts[0]) if parts else 1
            col = int(parts[1]) if len(parts) > 1 else 1
            self.y = max(0, min(self.height - 1, row - 1))
            self.x = max(0, min(self.width - 1, col - 1))
        elif final == "m":
            if parts and parts not in (["7"], ["27"]):
                self.sgr = params
            for p in parts or ["0"]:
                if p in ("0", "27"):
                    self.reverse = False
                elif p == "7":
                    self.reverse = True
        elif final == "J" and params in ("", "2"):
            keep = (self.sgr, self.reverse, self.alt)
            self.reset()
            self.sgr, self.reverse, self.alt = keep
        elif final in "ABCD":
            n = int(parts[0]) if parts else 1
            dy = {"A": -n, "B": n}.get(final, 0)
            dx = {"C": n, "D": -n}.get(final, 0)
            self.y = max(0, min(self.height - 1, self.y + dy))
            self.x = max(0, min(self.width - 1, self.x + dx))

    def _put(self, ch: str, glyph: str | None) -> None:
        self.chars[self.y][self.x] = ch
        self.attrs[self.y][self.x] = (self.sgr, self.reverse)
        self.glyphs[self.y][self.x] = glyph
        if self.x < self.width - 1:
            self.x += 1

    # -- output ----------------------------------------------------------
    def rows(self) -> list[str]:
        return ["".join(r) for r in self.chars]


# ---------------------------------------------------------------------------
# Reading a screen
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Box:
    top: int
    bottom: int
    left: int
    right: int

    def contains(self, other: "_Box") -> bool:
        return (other != self and self.top <= other.top and other.bottom <= self.bottom
                and self.left <= other.left and other.right <= self.right)


@dataclass(frozen=True)
class ListEntry:
    """One row of the browser listing."""

    row: int
    text: str        #: the row inside any frame, right-stripped
    name: str        #: the name field, right-stripped
    kind: str        #: "DIR", a file type such as "UE2", or "ROOT"
    attr: tuple[str, bool]
    truncated: bool  #: the name fills its field, so a longer name would look the same


@dataclass(frozen=True)
class MenuBox:
    labels: tuple[str, ...]
    highlighted: int | None


@dataclass(frozen=True)
class Popup:
    """A ``UIPopup``: its message (lines joined with ``\\n``, as the firmware
    passes it) and its buttons, left to right."""

    message: str
    buttons: tuple[str, ...]


@dataclass(frozen=True)
class View:
    """What one screen shows, as the driver needs it."""

    rows: tuple[str, ...]
    path: str | None
    framed: bool
    name_field: int
    entries: tuple[ListEntry, ...]
    selected: ListEntry | None
    menu: MenuBox | None
    popup: Popup | None


def _boxes(scr: TelnetScreen) -> list[_Box]:
    """Every box drawn with line-drawing glyphs, innermost ones only.

    Found from the top-right corner (``k`` with ``q`` to its left), down the
    right edge (``x``) to the bottom-right corner (``j``). The left edge is
    not required to be clean: a context menu puts its hook on the left edge
    at the selected row, and on the top or bottom row it replaces the corner
    with ``q`` (``ContextMenu::redraw``).
    """
    g = scr.glyphs
    found: list[_Box] = []
    for top in range(scr.height):
        for right in range(1, scr.width):
            if g[top][right] != "k" or g[top][right - 1] != "q":
                continue
            c = right - 1
            while c - 1 >= 0 and g[top][c - 1] == "q":
                c -= 1
            left = c - 1 if c - 1 >= 0 and g[top][c - 1] in ("l", "t", "w", "n") else c
            bottom = top + 1
            while bottom < scr.height and g[bottom][right] == "x":
                bottom += 1
            if bottom >= scr.height or bottom == top + 1 or g[bottom][right] != "j":
                continue
            if g[bottom][left] is None or any(g[bottom][x] != "q" for x in range(left + 1, right)):
                continue
            found.append(_Box(top, bottom, left, right))
    return [b for b in found if not any(b.contains(o) for o in found)]


def _first_cell_attr(scr: TelnetScreen, row: int, start: int, end: int) -> tuple[str, bool] | None:
    for x in range(start, end):
        if scr.chars[row][x] != " " and scr.glyphs[row][x] is None:
            return scr.attrs[row][x]
    return None


def _odd_one(attrs: list[tuple[str, bool] | None]) -> int | None:
    """Index of the one attribute that no other item shares, if exactly one."""
    tally: dict = {}
    for a in attrs:
        tally[a] = tally.get(a, 0) + 1
    odd = [i for i, a in enumerate(attrs) if a is not None and tally[a] == 1]
    return odd[0] if len(odd) == 1 and len(attrs) >= 3 else None


def read_view(scr: TelnetScreen, sel_attr: tuple[str, bool] | None = None) -> View:
    """Interpret a browser screen.

    With *sel_attr* (the selection colour learnt on this session) the
    selected listing row and the highlighted menu item are the ones drawn in
    it, and there must be exactly one. Without it, the one row drawn in an
    attribute no other row shares is taken, and only when there are at least
    three rows to compare (1541ultimate's ``_marked_row`` rule).
    """
    rows = scr.rows()
    status = rows[scr.height - 1]
    path_text = status[: scr.width - 9].rstrip()
    path = path_text if path_text.startswith("/") else None
    framed = scr.glyphs[2][0] == "l" and scr.glyphs[2][scr.width - 1] == "k"
    c0, c1 = (1, scr.width - 1) if framed else (0, scr.width)
    r0, r1 = (3, scr.height - 2) if framed else (2, scr.height - 1)
    name_field = (c1 - c0) - _TYPE_COLUMN

    boxes = [b for b in _boxes(scr)
             if not (framed and (b.top, b.left, b.right) == (2, 0, scr.width - 1))]
    popup = None
    menu = None
    menu_box = None
    for b in boxes:
        inner = [rows[r][b.left + 1:b.right] for r in range(b.top + 1, b.bottom)]
        # UIPopup::init: one row per message line, a blank row, the buttons
        # (window_height = message_lines + 4, button_y = message_lines + 1).
        if len(inner) >= 3 and not inner[-2].strip():
            words = tuple(inner[-1].split())
            if words and all(w in _BUTTONS for w in words):
                popup = Popup("\n".join(t.strip() for t in inner[:-2]), words)
                continue
        labels = tuple(t.strip() for t in inner)
        attrs = [_first_cell_attr(scr, r, b.left + 1, b.right) for r in range(b.top + 1, b.bottom)]
        if sel_attr is not None:
            hits = [i for i, a in enumerate(attrs) if a == sel_attr]
            hl = hits[0] if len(hits) == 1 else None
        else:
            hl = _odd_one(attrs)
        menu = MenuBox(labels, hl)
        menu_box = b

    entries: list[ListEntry] = []
    is_root = path == "/"
    for r in range(r0, r1):
        end = c1
        if menu_box is not None and menu_box.top <= r <= menu_box.bottom:
            end = min(end, menu_box.left)
        inner = rows[r][c0:end]
        if not inner.strip() or inner.strip() == "< No Items >":
            continue
        if is_root:
            name = inner[:_ROOT_NAME_FIELD].rstrip()
            kind = "ROOT"
            truncated = len(name) >= _ROOT_NAME_FIELD
        else:
            field_text = inner[:name_field]
            name = field_text.rstrip()
            tail = rows[r][c0 + name_field:c1].split()
            kind = tail[0] if tail and end == c1 else ""
            truncated = len(name) >= name_field or end < c0 + name_field
        attr = _first_cell_attr(scr, r, c0, end)
        entries.append(ListEntry(r, inner.rstrip(), name, kind, attr or ("", False), truncated))

    selected = None
    if sel_attr is not None:
        hits = [e for e in entries if e.attr == sel_attr]
        selected = hits[0] if len(hits) == 1 else None
    else:
        i = _odd_one([e.attr for e in entries])
        selected = entries[i] if i is not None else None
    return View(tuple(rows), path, framed, name_field, tuple(entries), selected, menu, popup)


# ---------------------------------------------------------------------------
# WiFi row
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WifiStatus:
    """The WiFi row on the browser root (``NetworkLWIP_WiFi::getDisplayString``).

    ``state`` is one of ``link-up``, ``link-down``, ``disabled``,
    ``scanning``, ``busy``, ``firmware?``, ``no-module``, ``unknown``.
    ``ident`` is the module's IDENT (e.g. ``ESP32 WiFi Bridge V1.494``) when
    the row says Disabled and names it; ``None`` when Disabled with a blank
    field (``eWifi_Off``) or in any other state. It is what fits the row's
    43-column field (41 in the framed 1.1.0 layout). ``detail`` is the
    middle field as drawn.
    """

    state: str
    ident: str | None
    ip: str | None
    mac: str | None
    detail: str
    row: str


_WIFI_WORDS = (("Disabled", "disabled"), ("Scanning", "scanning"), ("No Module", "no-module"),
               ("Firmware?", "firmware?"), ("Busy", "busy"), ("Unknown", "unknown"))


def parse_wifi_row(text: str) -> WifiStatus | None:
    """Parse one root row; ``None`` if it is not the WiFi row."""
    t = text.strip()
    if not t.startswith("WiFi"):
        return None
    body = t[4:].strip()
    m = re.fullmatch(r"IP:\s*(\S+)\s+Link Up", body)
    if m:
        return WifiStatus("link-up", None, m.group(1), None, f"IP: {m.group(1)}", t)
    m = re.fullmatch(r"MAC\s+((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})\s+Link Down", body)
    if m:
        return WifiStatus("link-down", None, None, m.group(1), f"MAC {m.group(1)}", t)
    for word, state in _WIFI_WORDS:
        if body.endswith(word):
            detail = body[: -len(word)].strip()
            ident = (detail or None) if state == "disabled" else None
            return WifiStatus(state, ident, None, None, detail, t)
    raise RemoteMenuUnexpectedScreenError(f"unrecognised WiFi row {t!r}")


#: WiFi state -> what the menu offers for it (``fetch_context_items``).
_DISABLE_FROM = ("link-up", "link-down", "no-module")
_ENABLED_STATES = ("link-up", "link-down", "scanning", "busy", "firmware?")


@dataclass(frozen=True)
class WifiChangeResult:
    """Outcome of :meth:`Ultimate64RemoteMenu.set_wifi_enabled`.

    ``changed`` is True when the ENTER was sent and a fresh connection then
    showed the row move (Disable: ``disabled``; Enable: out of
    ``disabled``). ``after`` is that fresh reading; ``None`` on a dry run or
    a no-op. ``menu`` is the context menu as read (empty if none opened).
    ``messages`` are the queued device messages this call's sessions showed,
    verification polls included.
    """

    action: str
    dry_run: bool
    before: WifiStatus
    after: WifiStatus | None
    enter_sent: bool
    changed: bool
    menu: tuple[str, ...]
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunUpdateResult:
    """Outcome of :meth:`Ultimate64RemoteMenu.run_update`.

    ``started`` means the application stopped answering after the one ENTER:
    two fresh connections in a row got no answer, while the committing
    session stayed open and silent (``evidence`` lists each observation).
    It does **not** mean the flash succeeded. The updater's prompts are on
    the C64 screen, not on telnet (``prompts_visible`` is always ``False``):
    someone at the keyboard answers them, and the new firmware is confirmed
    later from ``/v1/info``. ``messages`` are the queued device messages this
    call's sessions showed, probes included (a probe never answers one).
    """

    path: str
    dry_run: bool
    menu: tuple[str, ...]
    enter_sent: bool
    started: bool
    evidence: tuple[str, ...]
    prompts_visible: bool = False
    messages: tuple[str, ...] = ()


@dataclass(frozen=True)
class TaskRunResult:
    """Outcome of :meth:`Ultimate64RemoteMenu.run_task`.

    ``categories`` and ``items`` are the task menu and the category's submenu
    as read before the ENTER (blank rows dropped; a disabled item is listed
    too). ``enter_sent`` is False on a dry run. ``closed`` says whether the
    committing session then drew a screen with no menu and no popup within
    ``settle_timeout``: ``None`` when it drew nothing at all (the mirror may
    send nothing after an ENTER, #543), so it is evidence, not a verdict.
    ``verified`` is ``True`` when a fresh connection showed the action's
    effect (Turn On / Turn Off), ``None`` when the action has no effect a
    telnet session can see (Flush/Eject, Reset) or on a dry run.
    ``after_items`` is the submenu that fresh connection read.

    ``popup`` is the text of a popup the committing session drew after the
    ENTER (its lines joined with ``\\n``), and ``popup_buttons`` its buttons;
    ``None`` and ``()`` when there was none. A popup makes ``closed``
    ``False``. It is left open, unanswered: the call closes the session,
    which ends the firmware's popup loop without a key.
    """

    category: str
    item: str
    dry_run: bool
    categories: tuple[str, ...]
    items: tuple[str, ...]
    enter_sent: bool
    closed: bool | None
    verified: bool | None
    after_items: tuple[str, ...] | None
    screen: tuple[str, ...]
    messages: tuple[str, ...] = ()
    popup: str | None = None
    popup_buttons: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class _Session:
    """One telnet connection and its screen. Holds no policy."""

    def __init__(self, sock: socket.socket, timing: RemoteMenuTiming) -> None:
        self.sock = sock
        self.timing = timing
        self.screen = TelnetScreen()
        self.eof = False
        self.received = 0
        self.keys: list[str] = []
        self.sel_attr: tuple[str, bool] | None = None
        sock.setblocking(False)

    def drain(self, *, expect: bool, max_drain: float | None = None) -> int:
        """Read until the screen goes quiet; return the byte count.

        With *expect*, wait up to ``first_byte_timeout`` for the first byte;
        without, only one ``idle_gap``. A redraw that has started and does
        not go quiet within *max_drain* (default ``timing.max_drain``) of the
        call raises.
        """
        t = self.timing
        limit = t.max_drain if max_drain is None else max_drain
        start = time.monotonic()
        first_wait = t.first_byte_timeout if expect else t.idle_gap
        last: float | None = None
        got = 0
        while not self.eof:
            now = time.monotonic()
            if last is not None and now - start > limit:
                raise RemoteMenuUnexpectedScreenError(
                    f"the screen did not go quiet within {limit:.1f} s",
                    screen=tuple(self.screen.rows()))
            if last is None and now - start >= first_wait:
                break
            if last is not None and now - last >= t.idle_gap:
                break
            ready, _, _ = select.select([self.sock], [], [], t.idle_gap / 2)
            if not ready:
                continue
            try:
                chunk = self.sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                self.eof = True
                break
            if not chunk:
                self.eof = True
                break
            got += len(chunk)
            last = time.monotonic()
            self.screen.feed(chunk)
        self.received += got
        return got

    def _write(self, data: bytes) -> None:
        self.sock.setblocking(True)
        try:
            self.sock.sendall(data)
        except OSError as exc:
            raise RemoteMenuConnectError(
                f"sending {self.keys[-1] if self.keys else 'a key'} failed: {exc}") from exc
        finally:
            try:
                self.sock.setblocking(False)
            except OSError:
                pass

    def press(self, key: str) -> int:
        """Send one allow-listed key and read the redraw it causes."""
        self.send(key)
        return self.drain(expect=True)

    def send(self, key: str) -> None:
        payload = _KEYS[key]
        self.keys.append(key)
        self._write(payload)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class _Once:
    """The one state-changing ENTER of a call."""

    def __init__(self) -> None:
        self.sent = False

    def enter(self, session: _Session, what: str, host: str) -> None:
        if self.sent:
            raise Ultimate64RemoteMenuError(
                f"refusing a second ENTER for {what} on {host}", enter_sent=True)
        self.sent = True
        _log.info("Remote Menu on %s: %s, ENTER sent once", host, what)
        session.send("ENTER")


def _split_path(path: str) -> list[str]:
    if not path.startswith("/") or path.endswith("/") or "//" in path:
        raise RemoteMenuNotFoundError(f"path {path!r} must be absolute, like /SD/dir/file.ue2")
    parts = path[1:].split("/")
    if len(parts) < 2 or any(p in ("", ".", "..") for p in parts):
        raise RemoteMenuNotFoundError(f"path {path!r} needs a volume and a name, no . or ..")
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in path):
        raise RemoteMenuNotFoundError(f"path {path!r} has a character a listing cannot show")
    if len(parts[0]) >= _ROOT_NAME_FIELD:
        raise RemoteMenuNotFoundError(f"volume {parts[0]!r} is too long for the root's name field")
    for p in parts[1:]:
        if len(p) >= _MAX_NAME_FIELD:
            raise RemoteMenuNotFoundError(
                f"{p!r} is {len(p)} characters: the 60-column listing's name field is "
                f"{_MAX_NAME_FIELD} wide, and a name that fills it cannot be told from a "
                "longer one cut to fit")
    return parts


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------

class Ultimate64RemoteMenu:
    """Drive an Ultimate's Telnet Remote Menu.

    Each public call opens its own connections and closes them before it
    returns; no session outlives a call. The device's "Telnet Remote Menu"
    service must be enabled (it is on by default on 3.x and on the bench
    images, off by default on the official C64U 1.1.0).

    :param host: the device address, as the ``DeviceLock`` knows it.
    :param port: telnet port (23).
    :param timing: see :class:`RemoteMenuTiming`.
    :param connector: ``socket.create_connection``-compatible; for tests.
    """

    def __init__(self, host: str, *, port: int = TELNET_PORT,
                 timing: RemoteMenuTiming | None = None,
                 connector: Callable[..., socket.socket] | None = None) -> None:
        self.host = host
        self.port = port
        self.timing = timing or RemoteMenuTiming()
        self._connector = connector or socket.create_connection
        #: Ok-only messages this object answered, oldest first.
        self.dismissed_messages: list[str] = []
        #: Every popup text any session of this object showed, oldest first;
        #: each result's ``messages`` is the slice its call added.
        self._seen_messages: list[str] = []

    # -- gate and connection --------------------------------------------
    def _require_lock(self, operation: str) -> None:
        if not lock_held_for(self.host):
            raise RemoteMenuLockError(
                f"refusing {operation} on {self.host}: this process does not hold the "
                f"device's DeviceLock. Take it first, e.g. "
                f"DeviceLock({self.host!r}).acquire_or_raise(timeout=...).")

    def _connect(self) -> socket.socket:
        return self._connector((self.host, self.port), self.timing.connect_timeout)

    def _open(self, operation: str) -> _Session:
        """A fresh session at the browser root, its selection colour learnt."""
        self._require_lock(operation)
        try:
            sock = self._connect()
        except OSError as exc:
            raise RemoteMenuConnectError(
                f"{operation}: cannot connect to {self.host}:{self.port}: {exc}") from exc
        sess = _Session(sock, self.timing)
        try:
            sess.drain(expect=True)
            text = sess.screen.stream_text
            if "Password:" in text:
                raise RemoteMenuConnectError(
                    f"{operation}: {self.host} asks for a telnet password; this driver "
                    "does not send one")
            if "Too many connections" in text:
                raise RemoteMenuConnectError(
                    f"{operation}: {self.host} answered 'Too many connections' "
                    "(from 3a1ff9ff on the firmware allows four telnet sessions)")
            if sess.received == 0:
                raise RemoteMenuConnectError(
                    f"{operation}: {self.host} accepted the connection but drew nothing")
            view = self._settle_modals(sess)
            if view.path is None:
                raise RemoteMenuUnexpectedScreenError(
                    f"{operation}: not a browser screen (no path on the status row)",
                    screen=view.rows)
            view = self._to_root(sess, view)
            if view.selected is None:
                raise RemoteMenuUnexpectedScreenError(
                    f"{operation}: cannot tell which root row is selected", screen=view.rows)
            sess.sel_attr = view.selected.attr
            return sess
        except BaseException:
            sess.close()
            raise

    def _view(self, sess: _Session) -> View:
        return read_view(sess.screen, sess.sel_attr)

    def _settle_modals(self, sess: _Session) -> View:
        for _ in range(4):  # the firmware's message queue holds four
            view = self._view(sess)
            p = view.popup
            if p is None:
                return view
            self._seen_messages.append(p.message)
            if p.message in DISMISSABLE_MESSAGES and p.buttons == ("Ok",):
                _log.warning("Remote Menu on %s: answering %r with Ok", self.host, p.message)
                self.dismissed_messages.append(p.message)
                if sess.press("OK") == 0:
                    raise RemoteMenuUnexpectedScreenError(
                        f"popup {p.message!r} drew nothing after one 'o'; not pressing again",
                        screen=view.rows)
                continue
            raise RemoteMenuUnexpectedScreenError(
                f"modal popup on {self.host}: {p.message!r} with buttons {list(p.buttons)}; "
                "stopping without a key", screen=view.rows)
        raise RemoteMenuUnexpectedScreenError("popups kept appearing", screen=self._view(sess).rows)

    def _to_root(self, sess: _Session, view: View) -> View:
        for _ in range(16):
            if view.path == "/":
                return view
            before = view.path
            sess.press("LEFT")
            view = self._view(sess)
            if view.path == before:
                raise RemoteMenuNavigationError(
                    f"LEFT did not leave {before!r} on the way to the root", screen=view.rows)
        raise RemoteMenuNavigationError("too deep to return to the root", screen=view.rows)

    # -- navigation ---------------------------------------------------------
    def _select(self, sess: _Session, match: Callable[[ListEntry], bool], what: str) -> ListEntry:
        """Put the cursor on the one entry *match* accepts, one key at a time."""
        view = self._view(sess)
        seen_top = False
        for _ in range(400):
            if view.menu is not None or view.popup is not None:
                raise RemoteMenuUnexpectedScreenError(
                    f"a menu or popup is open while looking for {what}", screen=view.rows)
            sel = view.selected
            if sel is None:
                raise RemoteMenuUnexpectedScreenError(
                    f"cannot tell which row is selected while looking for {what}",
                    screen=view.rows)
            if match(sel):
                return sel
            visible = [e for e in view.entries if match(e)]
            if len(visible) > 1:
                raise RemoteMenuNotFoundError(f"{what} matches more than one row", screen=view.rows)
            if visible:
                key = "DOWN" if visible[0].row > sel.row else "UP"
            else:
                key = "PAGEDOWN" if seen_top else "PAGEUP"
            before = (view.rows, sel.row)
            sess.press(key)
            view = self._view(sess)
            # The cursor is marked by colour alone, so the text can be unchanged
            # while the selection moved.
            if (view.rows, view.selected.row if view.selected else None) == before:
                if key == "PAGEUP":
                    seen_top = True
                    continue
                if key == "PAGEDOWN":
                    raise RemoteMenuNotFoundError(f"{what} is not in {view.path}", screen=view.rows)
                raise RemoteMenuNavigationError(
                    f"{key} had no visible effect while moving to {what}; not resending",
                    screen=view.rows)
        raise RemoteMenuNotFoundError(f"gave up looking for {what}", screen=view.rows)

    def _enter(self, sess: _Session, expected_path: str) -> View:
        sess.press("RIGHT")
        view = self._view(sess)
        if view.path != expected_path:
            raise RemoteMenuNavigationError(
                f"RIGHT should have opened {expected_path!r}; the status row reads "
                f"{view.path!r}", screen=view.rows)
        return view

    def _navigate(self, sess: _Session, path: str) -> ListEntry:
        parts = _split_path(path)
        vol, dirs, name = parts[0], parts[1:-1], parts[-1]
        view = self._view(sess)
        root_names = [e.name for e in view.entries]
        hits = [n for n in root_names if n.casefold() == vol.casefold()]
        if len(hits) != 1:
            raise RemoteMenuNotFoundError(
                f"volume {vol!r} is not on {self.host}'s root (it shows {root_names})",
                screen=view.rows)
        dev_vol = hits[0]
        self._select(sess, lambda e: e.name == dev_vol, f"volume {dev_vol}")
        current = f"/{dev_vol}/"
        view = self._enter(sess, current)
        for d in dirs:
            self._check_fits(d, view)
            self._select(sess, lambda e, d=d: e.name == d and e.kind == "DIR" and not e.truncated,
                         f"directory {d!r}")
            current = f"{current}{d}/"
            view = self._enter(sess, current)
        self._check_fits(name, view)
        return self._select(
            sess, lambda e: e.name == name and e.kind != "DIR" and not e.truncated,
            f"file {name!r}")

    @staticmethod
    def _check_fits(name: str, view: View) -> None:
        if len(name) >= view.name_field:
            raise RemoteMenuNotFoundError(
                f"{name!r} is {len(name)} characters; this listing's name field is "
                f"{view.name_field} wide, so it cannot be matched exactly", screen=view.rows)

    def _open_menu(self, sess: _Session, what: str) -> MenuBox:
        sess.press("ENTER")
        view = self._view(sess)
        if view.popup is not None:
            raise RemoteMenuUnexpectedScreenError(
                f"a popup {view.popup.message!r} opened instead of {what}'s menu",
                screen=view.rows)
        if view.menu is None:
            raise RemoteMenuNotFoundError(f"no context menu opened for {what}", screen=view.rows)
        return view.menu

    def _highlight(self, sess: _Session, label: str, what: str) -> MenuBox:
        """Move the menu highlight onto *label*, reading it back after each key."""
        menu = self._view(sess).menu
        assert menu is not None
        if menu.labels.count(label) != 1:
            raise RemoteMenuNotFoundError(
                f"{what}'s menu {list(menu.labels)} does not offer {label!r} exactly once",
                screen=self._view(sess).rows)
        target = menu.labels.index(label)
        for _ in range(len(menu.labels) + 1):
            if menu.highlighted is None:
                raise RemoteMenuUnexpectedScreenError(
                    f"cannot tell which item of {what}'s menu is highlighted",
                    screen=self._view(sess).rows)
            if menu.highlighted == target:
                return menu
            key = "DOWN" if target > menu.highlighted else "UP"
            before = menu.highlighted
            sess.press(key)
            menu = self._view(sess).menu
            if menu is None or menu.highlighted is None or menu.highlighted == before or (
                    abs(target - menu.highlighted) >= abs(target - before)):
                raise RemoteMenuNavigationError(
                    f"{key} in {what}'s menu did not move the highlight one step toward "
                    f"{label!r}; not resending", screen=self._view(sess).rows)
        raise RemoteMenuNavigationError(f"could not reach {label!r}", screen=self._view(sess).rows)

    # -- public: reading ------------------------------------------------------
    def read_screen(self) -> tuple[str, ...]:
        """The root screen of a fresh session, as text. Sends no key unless a
        dismissable message is showing."""
        sess = self._open("read_screen")
        try:
            return self._view(sess).rows
        finally:
            sess.close()

    def wifi_status(self) -> WifiStatus:
        """The WiFi row on the browser root, from a fresh session."""
        sess = self._open("wifi_status")
        try:
            return self._wifi_from(self._view(sess))
        finally:
            sess.close()

    def _wifi_from(self, view: View) -> WifiStatus:
        for e in view.entries:
            st = parse_wifi_row(e.text)
            if st is not None:
                return st
        raise RemoteMenuNotFoundError(f"no WiFi row on {self.host}'s root", screen=view.rows)

    def context_actions(self, path: str) -> tuple[str, ...]:
        """The labels of *path*'s context menu. Opens the menu, chooses nothing."""
        _split_path(path)
        sess = self._open("context_actions")
        try:
            self._navigate(sess, path)
            return self._open_menu(sess, path).labels
        finally:
            sess.close()

    # -- public: WiFi -----------------------------------------------------------
    def set_wifi_enabled(self, enabled: bool, *, dry_run: bool = False,
                         verify_timeout: float = 15.0) -> WifiChangeResult:
        """Disable or enable the WiFi module through its row's context menu.

        Reads the row first. If it is already where *enabled* asks (Disable:
        ``disabled``; Enable: ``link-up``, ``link-down``, ``scanning``,
        ``busy`` or ``firmware?``), no menu is opened. Otherwise it refuses,
        without a key, from a state whose menu has no such action: Disable
        acts only from ``link-up``, ``link-down`` or ``no-module`` (so it
        refuses ``scanning``, ``busy``, ``firmware?`` and ``unknown``), and
        Enable only from ``disabled`` (so it refuses ``no-module`` and
        ``unknown``). It also refuses Disable when this session reached the device through the
        WiFi address. Otherwise selects the row, opens its menu, highlights
        the action, and sends ENTER once (not on a dry run). The result is
        read on fresh connections, polled until it shows or *verify_timeout*
        passes; on timeout :class:`RemoteMenuVerifyError` with
        ``enter_sent=True``.

        **What the action changes differs by product** (source-read):

        * U64E and the rest of the Ultimate line (``U64 != 2``, 7b15448a7
          ``network_esp32.cc`` ``set_enabled``): Disable/Enable sets the
          ``WiFi Enabled`` config item and, when the value changes, **writes
          the WiFi config store to flash** (``cfg->write()``). The choice
          survives a power cycle.
        * C64U (``U64 == 2``, 1.1.0 and 7b15448a7): ``wifi.RadioOff()`` /
          ``RadioOn()`` on the module, with no config write.
        """
        label = "Enable" if enabled else "Disable"
        once = _Once()
        start = len(self._seen_messages)
        sess = self._open(f"WiFi {label}")
        try:
            view = self._view(sess)
            before = self._wifi_from(view)
            reached = (before.state in _ENABLED_STATES) if enabled else (before.state == "disabled")
            if reached:
                return WifiChangeResult(label, dry_run, before, None, False, False, (),
                                        tuple(self._seen_messages[start:]))
            actionable = (before.state == "disabled") if enabled else (before.state in _DISABLE_FROM)
            if not actionable:
                raise RemoteMenuUnexpectedScreenError(
                    f"WiFi is {before.state!r}: its menu has no {label}; nothing sent",
                    screen=view.rows)
            if not enabled and before.ip is not None:
                peer = sess.sock.getpeername()[0]
                if peer == before.ip:
                    raise RemoteMenuUnexpectedScreenError(
                        f"refusing Disable: this session is connected through the WiFi "
                        f"address {peer}", screen=view.rows)
            self._select(sess, lambda e: parse_wifi_row(e.text) is not None, "the WiFi row")
            self._open_menu(sess, "the WiFi row")
            menu = self._highlight(sess, label, "the WiFi row")
            if dry_run:
                return WifiChangeResult(label, True, before, None, False, False, menu.labels,
                                        tuple(self._seen_messages[start:]))
            once.enter(sess, f"WiFi {label}", self.host)
            sess.drain(expect=False)
        except Ultimate64RemoteMenuError as exc:
            exc.enter_sent = once.sent
            raise
        finally:
            sess.close()

        moved = (lambda s: s.state != "disabled") if enabled else (lambda s: s.state == "disabled")
        after = self._poll_wifi(moved, verify_timeout, label)
        return WifiChangeResult(label, False, before, after, True, True, menu.labels,
                                tuple(self._seen_messages[start:]))

    def _poll_wifi(self, moved: Callable[[WifiStatus], bool], timeout: float,
                   label: str) -> WifiStatus:
        deadline = time.monotonic() + timeout
        last: WifiStatus | None = None
        while True:
            time.sleep(self.timing.poll_interval)
            try:
                sess = self._open(f"verify WiFi {label}")
            except Ultimate64RemoteMenuError as exc:
                raise RemoteMenuVerifyError(
                    f"WiFi {label}: ENTER sent once; a fresh connection then failed: {exc}",
                    enter_sent=True, screen=exc.screen) from exc
            try:
                last = self._wifi_from(self._view(sess))
            finally:
                sess.close()
            if moved(last):
                return last
            if time.monotonic() >= deadline:
                raise RemoteMenuVerifyError(
                    f"WiFi {label}: ENTER sent once; after {timeout:.0f} s fresh connections "
                    f"still show {last.row!r}. Not resent.", enter_sent=True)

    # -- public: Run Update -------------------------------------------------------
    def run_update(self, path: str, *, dry_run: bool = False,
                   confirm_device_powers_off: bool = False,
                   verify_timeout: float = 60.0) -> RunUpdateResult:
        """Choose "Run Update" on the update image at *path*.

        The image must already be on the device (``storage_put_file``).
        Every updater ends by switching the machine off; a U64E then needs
        its power button pressed (``docs/firmware_flashing.md``). So a real run
        requires ``confirm_device_powers_off=True``; a dry run does not.

        After the one ENTER the driver watches the committing session for a
        refusal popup (:class:`UpdateImageRejectedError`) and polls fresh
        connections. Two in a row that get no answer mean the application
        has stopped: ``started=True``. The first probe waits
        ``timing.first_probe_delay``. A fresh session that still shows the
        browser can be normal for a while: the application loads the whole
        image before it jumps. How long that takes is not measured.

        Evidence (C64U, 3.15a bench image, 2026-11-09, n=1): in the live
        ``flash_firmware`` run, one Run Update ENTER gave ``started=True``.
        Fresh sessions still answered at the root at about 5 s and 7 s, then
        the committing session received 1460 bytes after the ENTER, and the
        next two fresh connections got no answer. Earlier that day, in a
        bench test by hand, the first redraw after the ENTER came 9.6 s
        later and 2.3 s after a second ENTER; the live tests of #562 then
        sent 12 single ENTERs on WiFi toggles (6 per device) and all 12 took
        effect, so that delay was most likely the slow load of the 8.8 MB
        image, not a lost key.

        So polling continues until *verify_timeout*; then
        :class:`RemoteMenuVerifyError`. The ENTER is never resent. The
        prompts that follow are not visible here (see the module
        docstring), and a ``.u64``/``.ue2`` image is not checked by the
        firmware: check it before uploading.
        """
        if not dry_run and not confirm_device_powers_off:
            raise Ultimate64UnsafeOperationError(
                f"run_update({path!r}) starts the firmware updater on {self.host}, which "
                "ends by switching the machine off (a U64E needs its power button "
                "pressed). Pass confirm_device_powers_off=True, or dry_run=True.")
        _split_path(path)
        once = _Once()
        start = len(self._seen_messages)
        sess = self._open("Run Update")
        try:
            self._navigate(sess, path)
            self._open_menu(sess, path)
            menu = self._highlight(sess, "Run Update", path)
            if dry_run:
                sess.close()
                return RunUpdateResult(path, True, menu.labels, False, False, (),
                                       messages=tuple(self._seen_messages[start:]))
            once.enter(sess, f"Run Update on {path}", self.host)
            return self._await_update_start(sess, path, menu.labels, verify_timeout, start)
        except Ultimate64RemoteMenuError as exc:
            exc.enter_sent = once.sent
            raise
        finally:
            sess.close()

    def _await_update_start(self, sess: _Session, path: str, labels: tuple[str, ...],
                            timeout: float, start: int) -> RunUpdateResult:
        evidence: list[str] = []
        deadline = time.monotonic() + timeout
        no_answer = 0
        next_probe = time.monotonic() + self.timing.first_probe_delay
        while True:
            if not sess.eof:
                got = sess.drain(expect=False)
                if got:
                    evidence.append(f"committing session: {got} bytes after the ENTER")
                if sess.eof:
                    evidence.append("committing session: closed by the device")
            view = self._view(sess)
            if view.popup is not None:
                msg = view.popup.message
                self._seen_messages.append(msg)
                known = " (a .cfw refusal, FileTypeUpdate::load_format_1)" \
                    if msg in _CFW_REFUSALS else ""
                raise UpdateImageRejectedError(
                    f"Run Update on {path}: the application showed {msg!r}{known} instead "
                    "of jumping to the updater; nothing was flashed. The popup was left open.",
                    popup=msg, screen=view.rows)
            now = time.monotonic()
            if now >= next_probe:
                next_probe = now + self.timing.poll_interval
                outcome = self._probe()
                evidence.append(f"fresh connection: {outcome}")
                if outcome.startswith("no answer"):
                    no_answer += 1
                    if no_answer >= 2:
                        return RunUpdateResult(path, False, labels, True, True, tuple(evidence),
                                               messages=tuple(self._seen_messages[start:]))
                else:
                    no_answer = 0
            if time.monotonic() >= deadline:
                raise RemoteMenuVerifyError(
                    f"Run Update on {path}: ENTER sent once; after {timeout:.0f} s the "
                    f"application still answers or the result is unclear. Not resent. "
                    f"Evidence: {evidence}", enter_sent=True, screen=view.rows)
            time.sleep(min(self.timing.idle_gap, max(0.0, next_probe - time.monotonic())))

    def _probe(self) -> str:
        """One fresh connection, reading only. Sends no key.

        The new session may pop a queued device message; it is not answered,
        and its text is recorded for the result's ``messages``.
        """
        self._require_lock("verify Run Update")
        try:
            sock = self._connect()
        except ConnectionRefusedError:
            return "refused (a network stack answered; telnet is not listening)"
        except (TimeoutError, socket.timeout):
            return "no answer (connect timed out)"
        except OSError as exc:
            if exc.errno in _NO_ANSWER_ERRNOS:
                return f"no answer ({exc})"
            return f"error ({exc})"
        sess = _Session(sock, self.timing)
        try:
            got = sess.drain(expect=True)
            view = read_view(sess.screen)
            if got == 0:
                return "connected, nothing drawn"
            if view.popup is not None:
                self._seen_messages.append(view.popup.message)
                return f"answered, showing popup {view.popup.message!r}"
            return f"answered, path {view.path!r}"
        finally:
            sess.close()

    # -- public: task menu --------------------------------------------------------
    @staticmethod
    def _check_category(category: str) -> None:
        if category not in TASK_CATEGORIES:
            raise RemoteMenuNotFoundError(
                f"task category {category!r} is not one this driver opens (TASK_CATEGORIES = "
                f"{TASK_CATEGORIES}): the firmware runs a category's only action as soon as "
                "the category is opened, so a category is allowed only once the source shows "
                "that it always holds several")

    def _open_tasks(self, sess: _Session, what: str) -> MenuBox:
        """Open the task menu from the browser with the build's task key."""
        key = "F1" if self._view(sess).framed else "F5"
        sess.press(key)
        view = self._view(sess)
        if view.popup is not None:
            raise RemoteMenuUnexpectedScreenError(
                f"a popup {view.popup.message!r} opened instead of the task menu ({what})",
                screen=view.rows)
        if view.menu is None:
            raise RemoteMenuNavigationError(
                f"{key} opened no task menu on {self.host} ({what}); not resending",
                screen=view.rows)
        return view.menu

    def _open_category(self, sess: _Session, category: str) -> tuple[MenuBox, MenuBox]:
        """Open the task menu and *category*'s submenu; return both as read."""
        top = self._open_tasks(sess, category)
        self._highlight(sess, category, "the task menu")
        sess.press("RIGHT")
        view = self._view(sess)
        if view.popup is not None:
            raise RemoteMenuUnexpectedScreenError(
                f"a popup {view.popup.message!r} opened instead of the {category} submenu",
                screen=view.rows)
        sub = view.menu
        if sub is None or sub.labels == top.labels:
            raise RemoteMenuNavigationError(
                f"RIGHT on {category!r} opened no submenu; not resending", screen=view.rows)
        return top, sub

    def task_categories(self) -> tuple[str, ...]:
        """The task menu's categories. Opens the menu, opens no category."""
        sess = self._open("task_categories")
        try:
            return _labels(self._open_tasks(sess, "task_categories"))
        finally:
            sess.close()

    def task_items(self, category: str) -> tuple[str, ...]:
        """The items *category*'s submenu shows, disabled ones included.

        Opens the task menu and the category, chooses nothing. Only a
        category in :data:`TASK_CATEGORIES` is opened; any other is refused
        before connecting.
        """
        self._check_category(category)
        sess = self._open(f"task_items({category!r})")
        try:
            return _labels(self._open_category(sess, category)[1])
        finally:
            sess.close()

    def run_task(self, category: str, item: str, *, dry_run: bool = False,
                 settle_timeout: float = 10.0, verify_timeout: float = 15.0) -> TaskRunResult:
        """Choose *item* in the task menu's *category* (Tasks > Printer > Turn Off).

        Opens the task menu from the browser root, with F5 on the Ultimate line
        and F1 on the framed COMMODORE layout (official C64U 1.1.0, source-read
        only), highlights *category* and opens it with RIGHT, highlights *item*
        reading the highlight back after every key, and sends ENTER once (not on
        a dry run). Only a category in :data:`TASK_CATEGORIES` is opened; any
        other is refused before connecting.

        An item the submenu does not show (Turn Off while the printer is off)
        is :class:`RemoteMenuNotFoundError`; a disabled one (Flush/Eject while
        it is off) cannot be highlighted, the cursor skips it, and the call
        stops with :class:`RemoteMenuNavigationError`. Neither sends the ENTER.

        After the ENTER the committing session is read for up to
        *settle_timeout* (``closed`` in the result). A popup there ends the
        wait: its text and buttons are returned in ``popup`` and
        ``popup_buttons``, ``closed`` is ``False``, and no key is sent to it.
        Closing the session ends the firmware's popup loop
        (``UserInterface::popup`` runs while ``host->exists()``, which turns
        false once the telnet client is gone, ``SocketStream::is_alive``,
        700c7f2fa). The caller decides what the popup means. Turn On and Turn Off
        are then verified on fresh connections within *verify_timeout*
        (:class:`RemoteMenuVerifyError` if not); Flush/Eject and Reset have no
        effect telnet can show, so ``verified`` is ``None`` for them and the
        caller checks the outcome itself (the printer's output file, say).
        The ENTER is never resent.

        **Flush/Eject waits for the printer for up to 20 s**
        (``IEC_PRINTER_FLUSH_WAIT_MS``, behind a progress box). If the page is
        still rendering then, the firmware hides the box and shows an ``Ok``
        popup, "The page is still printing.\\nIt is saved when it is done."
        (``IecPrinter::executeCommand``, ``iec_printer.cc:345`` at
        700c7f2fa; the page is saved all the same). To see that popup in the
        result, pass a *settle_timeout* above 20 s; with the default the call
        returns ``closed=False`` while the progress box is still up.

        **Turn On / Turn Off change the device's config.** They set "IEC
        printer" (``CFG_PRINTER_ENABLE``) with ``set_value``, which marks the
        store as needing a flash write (``ConfigStore::setChanged``,
        584250385). Nothing here saves it, but a later save does: leaving the
        config menu with Auto Save Config, or a ``save_config_to_flash``. Its
        default is Disabled, so Turn On moves the device off the factory
        baseline. Put it back (Turn Off) before you release the lock.
        """
        self._check_category(category)
        what = f"task {category} > {item}"
        once = _Once()
        start = len(self._seen_messages)
        sess = self._open(what)
        try:
            top, _ = self._open_category(sess, category)
            menu = self._highlight(sess, item, f"the {category} task menu")
            cats, items = _labels(top), _labels(menu)
            if dry_run:
                return TaskRunResult(category, item, True, cats, items, False, None, None, None,
                                     self._view(sess).rows, tuple(self._seen_messages[start:]))
            once.enter(sess, what, self.host)
            closed, screen, popup = self._await_task_settled(sess, what, settle_timeout)
        except Ultimate64RemoteMenuError as exc:
            exc.enter_sent = once.sent
            raise
        finally:
            sess.close()
        effect = _TASK_EFFECTS.get((category, item))
        after = self._poll_task_effect(category, what, effect, verify_timeout) if effect else None
        return TaskRunResult(category, item, False, cats, items, True, closed,
                             True if effect else None, after, screen,
                             tuple(self._seen_messages[start:]),
                             popup.message if popup else None,
                             popup.buttons if popup else ())

    def _await_task_settled(self, sess: _Session, what: str, timeout: float
                            ) -> tuple[bool | None, tuple[str, ...], Popup | None]:
        deadline = time.monotonic() + timeout
        drew = False
        while True:
            # A progress bar may redraw for longer than timing.max_drain; that
            # is the action still running, not an error, so the drain may
            # last until the settle deadline.
            remaining = max(deadline - time.monotonic(), self.timing.idle_gap)
            try:
                drew = sess.drain(expect=True, max_drain=remaining) > 0 or drew
            except RemoteMenuUnexpectedScreenError:
                return False, self._view(sess).rows, None
            view = self._view(sess)
            if view.popup is not None:
                self._seen_messages.append(view.popup.message)
                _log.warning("Remote Menu on %s: %s: ENTER sent once; the session then "
                             "showed the popup %r %s. Left open, unanswered.", self.host,
                             what, view.popup.message, list(view.popup.buttons))
                return False, view.rows, view.popup
            if drew and view.menu is None:
                return True, view.rows, None
            if sess.eof or time.monotonic() >= deadline:
                return (False if drew else None), view.rows, None

    def _poll_task_effect(self, category: str, what: str, want: str,
                          timeout: float) -> tuple[str, ...]:
        deadline = time.monotonic() + timeout
        while True:
            time.sleep(self.timing.poll_interval)
            try:
                sess = self._open(f"verify {what}")
                try:
                    last = _labels(self._open_category(sess, category)[1])
                finally:
                    sess.close()
            except Ultimate64RemoteMenuError as exc:
                raise RemoteMenuVerifyError(
                    f"{what}: ENTER sent once; a fresh connection then failed: {exc}",
                    enter_sent=True, screen=exc.screen) from exc
            if want in last:
                return last
            if time.monotonic() >= deadline:
                raise RemoteMenuVerifyError(
                    f"{what}: ENTER sent once; after {timeout:.0f} s fresh connections still "
                    f"show {list(last)}, without {want!r}. Not resent.",
                    enter_sent=True)


def _labels(menu: MenuBox) -> tuple[str, ...]:
    """A menu's labels without the blank rows a tall window pads with."""
    return tuple(label for label in menu.labels if label)

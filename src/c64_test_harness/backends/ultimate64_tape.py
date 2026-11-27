"""The Ultimate-II+'s tape recorder and TAP player, driven through the Remote Menu (issue #584).

**Supported products, in a real C64.** The recorder and the player are two
separate FPGA options (``CAPAB_C2N_RECORDER``, ``CAPAB_C2N_STREAMER``;
generics ``g_c2n_recorder`` / ``g_c2n_streamer`` in the top levels):

* **Playback** (the player) and **capture** (the recorder): the
  Ultimate-II+ and II+L (``u2p_riscv_lattice.vhd``, ``u2p_riscv.vhd``,
  ``u2p_nios2_solo.vhd`` set both).
* The 1541 Ultimate-II (``ultimate_mb_700a.vhd``,
  ``ultimate_riscv_700a.vhd``): the player always, the recorder **only on
  dual-drive builds** (``g_c2n_recorder => g_dual_drive``, default false).
  A single-drive build can play a tape and cannot capture one.

**An Ultimate 64, Ultimate 64 Elite or C64 Ultimate has neither**: no top level for them sets
the generics (source-read, not traced exhaustively), and neither of this
project's bench devices lists a Tape task category (bench observation). On
those products every capture or playback call here raises
:class:`TapeUnsupportedError`; :func:`tape_require_supported` is the check to
run first. The harness has never been run against an Ultimate-II+ at all, so
its REST transport, ``create_manager(backend="u64")``, the FTP storage
helpers and the lock's address handling are unproven there too.

The firmware has no REST route for tape. The recorder and the player are
reached from its menus, so this module drives them over the Telnet Remote
Menu (:mod:`.ultimate64_remote_menu`) and checks files with ``storage_*``.

**What it does**

* :func:`tape_capture_start` / :func:`tape_capture_stop`: record the
  cassette **write** line to a named ``.tap`` file and report the file's
  path, size and status.
* :func:`tape_play` / :func:`tape_stop`: start a ``.tap`` playing and stop it.
* :func:`tape_status`: what the Tape task menu shows right now.
* :func:`tape_load_and_verify`: type ``LOAD``, start the tape, wait the load
  out, stop the tape, and check that the program arrived.

For a capture to hold data the C64 has to write pulses:
:func:`c64_test_harness.tape.build_tape_pulse_stimulus`, or a ``SAVE`` to
device 1 from your own program.

**Menu items, all chosen by name** (read from firmware source, not yet
seen on a wire; see "Evidence" below):

===========================  ==========================================  ==============================
Where                        Item                                        Source
===========================  ==========================================  ==============================
task menu, category          ``Tape``                                    ``tape_recorder.cc:108``,
                                                                         ``tape_controller.cc:76``
Tape, not recording          ``Sample tape to TAP`` (not driven here),   ``tape_recorder.cc:109-110``
                             ``Capture save to TAP``
Tape, recording              ``Finish Rec. to TAP``                      ``tape_recorder.cc:111``
Tape, a tape playing         ``Pause Tape Playback``                     ``tape_controller.cc:77``
Tape, a tape paused          ``Resume Tape Playback``,                   ``tape_controller.cc:78-79``
                             ``Stop Tape Playback``
a ``.tap`` file's menu       ``Enter (index)``, ``Run Tape``,            ``filetype_tap.cc:153-159``
                             ``Write to Tape``, ``Start Tape``
name prompt                  ``Give name for tap file..``                ``tape_recorder.cc:335``
after Finish                 popup ``Tape capture stopped.`` (Ok)        ``tape_recorder.cc:75``
===========================  ==========================================  ==============================

How the firmware behaves, as read:

* **The capture goes to the directory the browser is in** when the task
  menu is opened (``request_file`` opens the file under ``cmd->path``), so
  the driver browses into the target directory first. The two capture
  items are disabled where the path is not writable
  (``TapeRecorder::update_task_items``), and the root is not, so from the
  root the Tape category is listed only while something is recording or
  playing (``TaskMenu::init`` lists a category with at least one shown and
  enabled action).
* **"Capture save to TAP" times rising edges of the cassette write line**
  in phi2 ticks (the C64's 1 MHz clock; ``c2n_record.vhd`` counts
  ``phi2_tick``), which equal CPU cycles only while the CPU runs at 1 MHz,
  and pulls the sense line itself while it records
  (``TapeRecorder::start`` selects ``REC_SELECT_WRITE``; ``c2n_record.vhd``:
  ``pull_sense <= sel and enabled``). The motor line is not consulted (its
  term is commented out there). So a routine that toggles bit 3 of ``$01``
  is enough, and a KERNAL ``SAVE`` to device 1 sees the keys as pressed.
  Stopping the recorder adds one last interval of its own.
* **Stop is two steps.** The Tape menu offers ``Stop Tape Playback`` only
  while the tape is paused (``TapeController::update_task_items``), so
  :func:`tape_stop` chooses Pause and then Stop.
* **"Start Tape" presses play.** It resumes the C64 if the local menu had
  frozen it and starts the stream with the sense line asserted
  (``FileTypeTap::execute_st``, ``TapeController::start(1)``), after a
  lead-in of about 2.5 s. It types nothing: the C64 needs a ``LOAD``. ("Run
  Tape" resets the machine and types it; this module does not use it.)
* **Opening the Tape category runs nothing.** The task menu runs a
  category's action at once only when the category holds exactly one
  action, hidden ones included (``TaskMenu::select_item``); Tape holds
  three per subsystem.

**Rules, enforced before anything is sent**

* **The caller holds the device's** ``DeviceLock``. Every function checks it
  first, reads included, before any FTP or telnet connection.
* **A capture never targets** ``/Temp`` **or** ``/Flash``. The target path
  goes through the same check as ``storage_put_file`` before any menu
  action. A capture grows for as long as it runs: stop it, keep it short,
  and use a volume with ample free space. An existing file is never
  overwritten: the name must be free.
* **A name is 1..18 letters, digits,** ``_`` **or** ``-``; the firmware adds
  ``.tap``. Each character typed into the prompt is read back before the
  next, and the ENTER that creates the file is sent once.
* **No memory or screen reads while the C64 does cassette I/O** (#573):
  :func:`tape_load_and_verify` sleeps for the tape's playing time and reads
  afterwards.
* **The cassette buffer** ``$033C-$03FB`` **belongs to the KERNAL during a
  load.** :func:`tape_load_and_verify` reserves it in the transport's
  ``MemoryPolicy`` for the duration
  (:func:`c64_test_harness.tape.cassette_buffer_reserved`), uses no harness
  scratch there, and never passes ``override=``. Its own writes (clearing
  the load area and ``$AE/$AF``) go through the policy: if the policy
  refuses them the call stops before the ``LOAD`` is typed.

**Evidence.** Every menu name, prompt and popup above is read from the
1541ultimate source tree at 69a5f83eb (a working tree on a development
branch of the 3.x Ultimate line; the tape files were not compared with a
release tag). Nothing here has been tried on any device, and this project's
bench has no product with tape hardware, so the live module
``tests/test_tape_live.py`` has nowhere to run here: it skips with
:class:`TapeUnsupportedError`'s reason on the bench devices. A Tape menu
with a label outside the ones above stops the call
(``RemoteMenuUnexpectedScreenError``) rather than being read as a state.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..tape import PAL_CLOCK_HZ, TapFile, cassette_buffer_reserved, check_wait, parse_tap
from ..verify import LoadCheck, PrgFile
from . import ultimate64_remote_menu as _rm
from .ultimate64_remote_menu import (
    MenuBox,
    Popup,
    RemoteMenuLockError,
    RemoteMenuNavigationError,
    RemoteMenuNotFoundError,
    RemoteMenuUnexpectedScreenError,
    RemoteMenuVerifyError,
    TelnetScreen,
    Ultimate64RemoteMenu,
    Ultimate64RemoteMenuError,
)
from .ultimate64_storage import (
    _ascii_fold,
    _check_path,
    storage_get_file,
    storage_list_dir,
)
from .ultimate64_temp_gc import lock_held_for, lock_shell_refusal

__all__ = [
    "TAPE_NAME_MAX",
    "TapeCapture",
    "TapeCaptureNotStoppedError",
    "TapeCaptureResult",
    "TapeCaptureNotOfferedError",
    "TapeLoadResult",
    "TapeMenuRejectedError",
    "TapePlayResult",
    "TapeStatus",
    "TapeUnsupportedError",
    "Ultimate64TapeMenu",
    "tape_capture_start",
    "tape_capture_stop",
    "tape_load_and_verify",
    "tape_play",
    "tape_require_supported",
    "tape_status",
    "tape_stop",
]

_log = logging.getLogger(__name__)

TAPE_CATEGORY = "Tape"
ACTION_CAPTURE = "Capture save to TAP"
ACTION_FINISH = "Finish Rec. to TAP"
ACTION_PAUSE = "Pause Tape Playback"
ACTION_RESUME = "Resume Tape Playback"
ACTION_STOP = "Stop Tape Playback"
ACTION_START_TAPE = "Start Tape"
ACTION_SAMPLE = "Sample tape to TAP"
#: Every label the Tape category can hold, by the source. A submenu with any
#: other label is not read as a state: the call stops.
KNOWN_TAPE_ACTIONS = frozenset({
    ACTION_SAMPLE, ACTION_CAPTURE, ACTION_FINISH, ACTION_PAUSE, ACTION_RESUME, ACTION_STOP})
#: What the player adds to a ``.tap`` file's context menu.
TAP_FILE_ACTIONS = frozenset({"Enter (index)", "Run Tape", "Write to Tape", ACTION_START_TAPE})
NAME_PROMPT = "Give name for tap file.."
CAPTURE_STOPPED = "Tape capture stopped."

#: Longest capture name: the prompt takes 22 characters and the firmware
#: appends ``.tap`` (``TapeRecorder::request_file``).
TAPE_NAME_MAX = 18
_NAME_RE = re.compile(r"[A-Za-z0-9_-]{1,%d}\Z" % TAPE_NAME_MAX)

#: How long a fresh connection may take to show an action's effect, when a
#: call passes no ``verify_timeout``.
DEFAULT_VERIFY_TIMEOUT = 15.0

#: A capture larger than this is sized but not read back.
MAX_CAPTURE_READ = 8 * 1024 * 1024

#: ``TapeController::start`` queues this pause before the tape's own data.
_LEAD_IN_SECONDS = 2.5
#: KERNAL ST bits that mean a tape read went wrong: short block, long
#: block, unrecoverable read error, checksum error.
_TAPE_ST_ERRORS = 0x3C


class TapeMenuRejectedError(Ultimate64RemoteMenuError):
    """The firmware answered a tape action with a popup instead of doing it.

    ``popup`` is its text (a file-system error for a capture; "Can't open
    TAP file.", "Error reading TAP file header." or "TAP file: invalid
    signature." for a tape). The popup was left open; closing the session
    ends it.
    """

    def __init__(self, message: str, *, popup: str, screen: tuple[str, ...] | None = None) -> None:
        super().__init__(message, enter_sent=True, screen=screen)
        self.popup = popup


class TapeUnsupportedError(Ultimate64RemoteMenuError):
    """The device shows no tape recorder, or no TAP player.

    The recorder and the player are FPGA options of the Ultimate-II+ /
    II+L; a 1541 Ultimate-II has the player, and the recorder only on
    dual-drive builds. An Ultimate 64, Ultimate 64 Elite or C64 Ultimate
    has neither part, so its menus never list the items. ``product`` is
    the title the device's screen shows.
    """

    def __init__(self, message: str, *, product: str,
                 screen: tuple[str, ...] | None = None) -> None:
        super().__init__(message, screen=screen)
        self.product = product


class TapeCaptureNotOfferedError(Ultimate64RemoteMenuError):
    """The device has a TAP player, but the Tape menu offers no capture in
    the directory asked about. The menus cannot tell these two causes apart:

    * the firmware does not treat that directory as writable (a
      write-protected card, a volume mounted read-only), whatever FTP
      allows there; or
    * this build has the player and no recorder, as a single-drive 1541
      Ultimate-II does, so no directory will ever offer a capture.

    Playback works either way. Raised by :func:`tape_require_supported`
    when it was given a ``.tap`` to ask the player with. ``product`` is the
    title the device's screen shows."""

    def __init__(self, message: str, *, product: str,
                 screen: tuple[str, ...] | None = None) -> None:
        super().__init__(message, screen=screen)
        self.product = product


class TapeCaptureNotStoppedError(Ultimate64RemoteMenuError):
    """``tape_capture_stop`` found no "Finish Rec. to TAP" to choose, but the
    capture's file still has a zero length field: the recorder writes that
    field when it closes the file, so the file may still be open. Do not
    delete it; read :func:`tape_status` and the device's own menu first."""


@dataclass(frozen=True)
class TapeStatus:
    """What the Tape task menu shows from the browser root.

    ``items`` are its labels (empty when the task menu lists no Tape
    category: nothing recording, nothing playing). ``playing`` means a tape
    is loaded and not paused; the firmware keeps it loaded until its last
    pulse has been played or it is stopped, so it can stay ``True`` after
    the C64 has finished loading.

    **An empty** ``items`` **does not show that the device has tape
    hardware**: an idle device and a product without it look the same from
    the root. :func:`tape_require_supported` tells them apart.
    """

    items: tuple[str, ...]

    @property
    def recording(self) -> bool:
        return ACTION_FINISH in self.items

    @property
    def playing(self) -> bool:
        return ACTION_PAUSE in self.items

    @property
    def paused(self) -> bool:
        return ACTION_STOP in self.items or ACTION_RESUME in self.items


@dataclass(frozen=True)
class TapeCapture:
    """A capture started by :func:`tape_capture_start`; pass it to
    :func:`tape_capture_stop`. ``started`` is ``False`` for a dry run."""

    directory: str
    name: str
    path: str
    started: bool


@dataclass(frozen=True)
class TapeCaptureResult:
    """The capture file after :func:`tape_capture_stop`.

    ``status`` is one of:

    * ``"ok"``: a TAP header, a length field equal to the data present, and
      at least one data byte;
    * ``"empty"``: a valid file with no data (nothing toggled the write line);
    * ``"length-mismatch"``: the header's length field disagrees with the
      file (``declared_length`` against ``data_length``);
    * ``"bad-header"``: not a TAP file, or shorter than its header;
    * ``"missing"``: no such file in the directory (``size`` is ``None``);
    * ``"unread"``: listed, but not read back (``read_file=False`` or over
      :data:`MAX_CAPTURE_READ`); only ``size`` is known. The header was not
      read, so the still-open check (:class:`TapeCaptureNotStoppedError`)
      did not run either: ``"unread"`` does not show that the file is closed.

    ``was_recording`` says whether the menu offered "Finish Rec. to TAP";
    ``popup`` is the popup the firmware showed after it (answered with Ok
    when it is the expected "Tape capture stopped."). ``tap`` is the parsed
    file when it has a header.
    """

    path: str
    size: int | None
    status: str
    declared_length: int | None
    data_length: int | None
    was_recording: bool
    popup: str | None
    tap: TapFile | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass(frozen=True)
class TapePlayResult:
    """Outcome of :func:`tape_play`: the file's menu as read, whether the
    one ENTER was sent, and the Tape menu a fresh connection then showed."""

    path: str
    dry_run: bool
    menu: tuple[str, ...]
    enter_sent: bool
    status: TapeStatus | None


@dataclass(frozen=True)
class TapeLoadResult:
    """Outcome of :func:`tape_load_and_verify`.

    ``check`` is what ``PrgFile.verify_load`` read once the wait was over.
    For a tape the KERNAL sets its end pointer from the tape's header before
    it reads the data, so a matching pointer shows that the header was read
    and the load ran; ``loaded`` also needs every byte to match (the area is
    zeroed first) and no tape error in ST.
    """

    check: LoadCheck
    waited_seconds: float
    play: TapePlayResult
    after: TapeStatus | None

    @property
    def loaded(self) -> bool:
        return self.check.complete and not (self.check.status & _TAPE_ST_ERRORS)


def read_string_box(scr: TelnetScreen, prompt: str) -> str | None:
    """The text in the string box titled *prompt*, or ``None`` if none is drawn.

    ``UIStringBox::init`` draws a bordered window of three rows: the prompt,
    a blank row, the edit field.
    """
    rows = scr.rows()
    for b in _rm._boxes(scr):
        inner = [rows[r][b.left + 1:b.right] for r in range(b.top + 1, b.bottom)]
        if len(inner) == 3 and inner[0].strip() == prompt:
            return inner[2].strip()
    return None


def _require_lock(host: str, operation: str) -> None:
    """The same gate as the Remote Menu driver and the storage helpers: this
    process holds the lock, or is the child of a verified lock shell (#582)."""
    why = None if lock_held_for(host) else lock_shell_refusal(host)
    if why is not None:
        raise RemoteMenuLockError(
            f"refusing {operation} on {host}: this process does not hold the device's "
            f"DeviceLock ({why}). Take it first, e.g. "
            f"DeviceLock({host!r}).acquire_or_raise(timeout=...).")


def _product(rows: tuple[str, ...]) -> str:
    """The product title on the screen's first row, without its ``***`` marks."""
    parts = [p.strip() for p in rows[0].split("***") if p.strip()] if rows else []
    return parts[0] if parts else (rows[0].strip() if rows else "unknown product")


_UNSUPPORTED_HINT = (
    "The tape recorder and TAP player are FPGA options of the Ultimate-II+ / II+L (a "
    "1541 Ultimate-II has the player, and the recorder only on dual-drive builds); an "
    "Ultimate 64, Ultimate 64 Elite or C64 Ultimate has neither.")


def _capture_target(directory: str, name: str) -> tuple[list[str], str]:
    """Validate a capture target; return the directory's parts and the file's path.

    Raises ``ValueError`` for ``/Temp`` and ``/Flash`` and every path the
    storage helpers refuse to write, before anything is connected.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(
            f"capture name {name!r} must be 1..{TAPE_NAME_MAX} letters, digits, '_' or "
            "'-', without an extension (the firmware adds .tap)")
    if not isinstance(directory, str) or not directory.startswith("/"):
        raise ValueError(f"capture directory {directory!r} must be absolute, like /USB1/captures")
    path = f"{directory}/{name}.tap"
    parts = _check_path(path, write=True)
    # The browser's own limits (name-field widths, printable characters).
    _rm._split_path(path)
    return parts[:-1], path


class Ultimate64TapeMenu(Ultimate64RemoteMenu):
    """The Remote Menu driver with the tape actions.

    The module-level functions (:func:`tape_capture_start` and the rest)
    are the public way in: they add the path and storage checks. These
    methods only drive the menu. All of them keep the base driver's rules:
    the lock is checked before every connection, the highlight is read back
    after every key, and each state-changing ENTER is sent once and its
    effect read from a fresh connection.
    """

    # -- navigation ---------------------------------------------------------
    def _enter_directory(self, sess: Any, parts: list[str]) -> str:
        """Browse into ``/parts[0]/parts[1]/...`` and return the path shown."""
        vol, dirs = parts[0], parts[1:]
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
        return current

    def _open_tape(self, sess: Any, what: str) -> MenuBox | None:
        """Open the task menu and its Tape category; ``None`` if it lists none."""
        top = self._open_tasks(sess, what)
        if TAPE_CATEGORY not in _rm._labels(top):
            return None
        self._highlight(sess, TAPE_CATEGORY, "the task menu")
        sess.press("RIGHT")
        view = self._view(sess)
        if view.popup is not None:
            raise RemoteMenuUnexpectedScreenError(
                f"a popup {view.popup.message!r} opened instead of the Tape submenu",
                screen=view.rows)
        if read_string_box(sess.screen, NAME_PROMPT) is not None:
            raise RemoteMenuUnexpectedScreenError(
                "opening the Tape category opened the file-name prompt: this build runs "
                "a tape action as soon as the category is opened. Nothing was typed; "
                "closing the session cancels the prompt.", screen=view.rows)
        sub = view.menu
        if sub is None or sub.labels == top.labels:
            raise RemoteMenuNavigationError(
                "RIGHT on 'Tape' opened no submenu; not resending", screen=view.rows)
        unknown = [label for label in _rm._labels(sub) if label not in KNOWN_TAPE_ACTIONS]
        if unknown:
            raise RemoteMenuUnexpectedScreenError(
                f"the Tape menu on {self.host} holds {unknown}, which this driver does not "
                f"know (it knows {sorted(KNOWN_TAPE_ACTIONS)}). Its state cannot be read "
                "from labels it does not know, so nothing was chosen.", screen=view.rows)
        return sub

    def _overlay_popup(self, sess: Any) -> Popup | None:
        """A popup on the screen that is not the name prompt itself.

        ``read_view`` takes a three-row box whose last row holds only button
        words for a popup, and the name prompt is one while the text typed
        so far is ``No`` or ``Ok``.
        """
        popup = self._view(sess).popup
        if popup is not None and popup.message == NAME_PROMPT:
            return None
        return popup

    def _refuse_overlay(self, sess: Any, what: str, doing: str) -> None:
        popup = self._overlay_popup(sess)
        if popup is not None:
            self._seen_messages.append(popup.message)
            raise RemoteMenuUnexpectedScreenError(
                f"{what}: a popup {popup.message!r} {list(popup.buttons)} is on the screen "
                f"{doing}; a key now would answer it. Stopping; closing the session "
                "cancels the prompt.", screen=self._view(sess).rows)

    def _await_popup(self, sess: Any, timeout: float, *, expected: bool) -> Popup | None:
        """Read the committing session for a popup.

        With *expected* the wait lasts until one appears or *timeout*.
        Without, it ends as soon as the session has redrawn and gone quiet
        (or stayed silent for one ``first_byte_timeout``): an error popup
        comes with the redraw that follows the ENTER.
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(deadline - time.monotonic(), self.timing.idle_gap)
            try:
                sess.drain(expect=True, max_drain=remaining)
            except RemoteMenuUnexpectedScreenError:
                return None
            view = self._view(sess)
            if view.popup is not None:
                self._seen_messages.append(view.popup.message)
                return view.popup
            if not expected or sess.eof or time.monotonic() >= deadline:
                return None

    def _poll_status(self, want: Callable[[TapeStatus], bool], what: str,
                     timeout: float | None) -> TapeStatus:
        if timeout is None:
            timeout = DEFAULT_VERIFY_TIMEOUT
        deadline = time.monotonic() + timeout
        while True:
            time.sleep(self.timing.poll_interval)
            try:
                status = self.tape_status()
            except RemoteMenuLockError:
                raise
            except Ultimate64RemoteMenuError as exc:
                raise RemoteMenuVerifyError(
                    f"{what}: ENTER sent once; a fresh connection then failed: {exc}",
                    enter_sent=True, screen=exc.screen) from exc
            if want(status):
                return status
            if time.monotonic() >= deadline:
                raise RemoteMenuVerifyError(
                    f"{what}: ENTER sent once; after {timeout:.0f} s the Tape menu still "
                    f"shows {list(status.items)}. Not resent.", enter_sent=True)

    # -- reading --------------------------------------------------------------
    def tape_status(self) -> TapeStatus:
        """The Tape task menu as a fresh session at the root shows it."""
        sess = self._open("tape_status")
        try:
            sub = self._open_tape(sess, "tape_status")
            return TapeStatus(_rm._labels(sub) if sub is not None else ())
        finally:
            sess.close()

    def has_player(self, path: str) -> bool:
        """Whether the ``.tap`` at *path* gets the player's context items.

        They do not depend on where the file is or whether the directory is
        writable (``FileTypeTap::fetch_context_items``). Opens the file's
        menu and chooses nothing.
        """
        _rm._split_path(path)
        sess = self._open(f"TAP player check on {path}")
        try:
            self._navigate(sess, path)
            return bool(TAP_FILE_ACTIONS & set(self._open_menu(sess, path).labels))
        finally:
            sess.close()

    def require_recorder(self, parts: list[str], tap_path: str | None = None) -> str:
        """Check that the Tape menu inside directory *parts* offers a capture.

        With a recorder, a writable directory always lists the Tape
        category with "Capture save to TAP" (or "Finish Rec. to TAP" while
        one runs). Returns the product title; chooses nothing.

        No capture item has two possible causes that look the same in this
        menu: no recorder, or a directory the firmware does not treat as
        writable. With *tap_path* (a ``.tap`` on the device) the player is
        asked as well, and it does not depend on writability.

        :raises TapeCaptureNotOfferedError: no capture item, but the
            ``.tap`` at *tap_path* has the player's items: a directory that
            is not writable, or a build with a player and no recorder.
        :raises TapeUnsupportedError: no capture item, and no *tap_path* or
            no player either.
        """
        what = f"tape support check in /{'/'.join(parts)}"
        sess = self._open(what)
        try:
            shown = self._enter_directory(sess, parts)
            sub = self._open_tape(sess, what)
            view = self._view(sess)
            product = _product(view.rows)
            labels = _rm._labels(sub) if sub is not None else ()
        finally:
            sess.close()
        if ACTION_CAPTURE in labels or ACTION_FINISH in labels:
            return product
        seen = f"Tape menu: {list(labels) or 'not listed'}"
        if tap_path is not None and self.has_player(tap_path):
            raise TapeCaptureNotOfferedError(
                f"{product} at {self.host} has a TAP player ({tap_path} gets its menu "
                f"items) but offers no tape capture in {shown} ({seen}). Either the "
                "directory is not writable to the firmware (check the medium's write "
                "protection, or try another directory), or this build has a player and no "
                "recorder, as a single-drive 1541 Ultimate-II does. Playback is available "
                "either way.",
                product=product, screen=view.rows)
        asked = ("no TAP player either" if tap_path is not None
                 else "on a product that has a recorder this means the directory is not "
                      "writable; pass tap_path= to tell the two apart")
        raise TapeUnsupportedError(
            f"{product} at {self.host} offers no tape capture in {shown} ({seen}); {asked}. "
            f"{_UNSUPPORTED_HINT}", product=product, screen=view.rows)

    # -- capture --------------------------------------------------------------
    def start_capture(self, parts: list[str], name: str, *, dry_run: bool = False,
                      verify_timeout: float | None = None) -> tuple[str, ...]:
        """Tape > "Capture save to TAP" in directory *parts*, named *name*.

        Returns the Tape submenu's labels as read. The ENTER on the menu
        item only opens the name prompt; the file is created by the ENTER
        that ends the name, which is the one sent once. A dry run stops
        with the menu item highlighted.
        """
        what = f"tape capture to /{'/'.join(parts)}/{name}.tap"
        once = _rm._Once()
        sess = self._open(what)
        try:
            self._enter_directory(sess, parts)
            sub = self._open_tape(sess, what)
            if sub is None:
                view = self._view(sess)
                product = _product(view.rows)
                raise TapeUnsupportedError(
                    f"{what}: {product} lists no 'Tape' category in this directory. "
                    f"{_UNSUPPORTED_HINT} On a product that has a recorder this means the "
                    "directory is not writable.", product=product, screen=view.rows)
            menu = self._highlight(sess, ACTION_CAPTURE, "the Tape task menu")
            labels = _rm._labels(menu)
            if dry_run:
                return labels
            sess.press("ENTER")
            view = self._view(sess)
            if view.popup is not None:
                self._seen_messages.append(view.popup.message)
                raise TapeMenuRejectedError(
                    f"{what}: the firmware showed {view.popup.message!r} instead of the "
                    "name prompt", popup=view.popup.message, screen=view.rows)
            if read_string_box(sess.screen, NAME_PROMPT) != "":
                raise RemoteMenuUnexpectedScreenError(
                    f"{what}: no empty {NAME_PROMPT!r} prompt after choosing "
                    f"{ACTION_CAPTURE!r}; nothing typed, not resending", screen=view.rows)
            for i, ch in enumerate(name):
                sess.type_char(ch)
                # Checked after every character, so also before the next one
                # and before the ENTER below.
                self._refuse_overlay(sess, what, f"after typing {name[:i + 1]!r}")
                shown = read_string_box(sess.screen, NAME_PROMPT)
                if shown != name[:i + 1]:
                    raise RemoteMenuNavigationError(
                        f"{what}: after typing {name[:i + 1]!r} the prompt shows {shown!r}; "
                        "stopping. Closing the session cancels the prompt, so no file is "
                        "created.", screen=self._view(sess).rows)
            once.enter(sess, what, self.host)
            popup = self._await_popup(sess, self.timing.first_byte_timeout, expected=False)
            if popup is not None:
                raise TapeMenuRejectedError(
                    f"{what}: the firmware answered {popup.message!r}; no capture is "
                    "running. The popup was left open.", popup=popup.message,
                    screen=self._view(sess).rows)
        except Ultimate64RemoteMenuError as exc:
            exc.enter_sent = once.sent
            raise
        finally:
            sess.close()
        self._poll_status(lambda s: s.recording, what, verify_timeout)
        return labels

    def finish_capture(self, *, settle_timeout: float = 30.0,
                       verify_timeout: float | None = None) -> tuple[bool, str | None]:
        """Tape > "Finish Rec. to TAP". Returns ``(was_recording, popup)``.

        Nothing is sent when the menu does not offer the item. The expected
        "Tape capture stopped." popup is answered with one ``o``; any other
        popup is returned and left open.
        """
        what = "finish tape capture"
        once = _rm._Once()
        sess = self._open(what)
        try:
            sub = self._open_tape(sess, what)
            if sub is None or ACTION_FINISH not in _rm._labels(sub):
                return False, None
            self._highlight(sess, ACTION_FINISH, "the Tape task menu")
            once.enter(sess, what, self.host)
            popup = self._await_popup(sess, settle_timeout, expected=True)
            if popup is not None and popup.message == CAPTURE_STOPPED and popup.buttons == ("Ok",):
                sess.press("OK")
            elif popup is not None:
                _log.warning("Remote Menu on %s: %s: unexpected popup %r %s left open",
                             self.host, what, popup.message, list(popup.buttons))
        except Ultimate64RemoteMenuError as exc:
            exc.enter_sent = once.sent
            raise
        finally:
            sess.close()
        self._poll_status(lambda s: not s.recording, what, verify_timeout)
        return True, popup.message if popup is not None else None

    # -- playback -------------------------------------------------------------
    def start_tape(self, path: str, *, dry_run: bool = False,
                   verify_timeout: float | None = None) -> TapePlayResult:
        """Choose "Start Tape" on the ``.tap`` at *path*."""
        _rm._split_path(path)
        what = f"Start Tape on {path}"
        once = _rm._Once()
        sess = self._open(what)
        try:
            entry = self._navigate(sess, path)
            offered = self._open_menu(sess, path)
            if entry.kind.upper() == "TAP" and not TAP_FILE_ACTIONS & set(offered.labels):
                view = self._view(sess)
                product = _product(view.rows)
                raise TapeUnsupportedError(
                    f"{what}: {product} offers no tape action for a .tap file (its menu: "
                    f"{list(offered.labels)}). {_UNSUPPORTED_HINT}",
                    product=product, screen=view.rows)
            menu = self._highlight(sess, ACTION_START_TAPE, path)
            if dry_run:
                return TapePlayResult(path, True, menu.labels, False, None)
            once.enter(sess, what, self.host)
            popup = self._await_popup(sess, self.timing.first_byte_timeout, expected=False)
            if popup is not None:
                raise TapeMenuRejectedError(
                    f"{what}: the firmware answered {popup.message!r}; no tape is "
                    "playing. The popup was left open.", popup=popup.message,
                    screen=self._view(sess).rows)
        except Ultimate64RemoteMenuError as exc:
            exc.enter_sent = once.sent
            raise
        finally:
            sess.close()
        status = self._poll_status(lambda s: s.playing, what, verify_timeout)
        return TapePlayResult(path, False, menu.labels, True, status)

    def _tape_action(self, label: str, want: Callable[[TapeStatus], bool],
                     verify_timeout: float | None) -> TapeStatus:
        what = f"Tape > {label}"
        once = _rm._Once()
        sess = self._open(what)
        try:
            sub = self._open_tape(sess, what)
            if sub is None:
                raise RemoteMenuNotFoundError(
                    f"{what}: the task menu lists no 'Tape' category",
                    screen=self._view(sess).rows)
            self._highlight(sess, label, "the Tape task menu")
            once.enter(sess, what, self.host)
            self._await_popup(sess, self.timing.first_byte_timeout, expected=False)
        except Ultimate64RemoteMenuError as exc:
            exc.enter_sent = once.sent
            raise
        finally:
            sess.close()
        return self._poll_status(want, what, verify_timeout)

    def stop_tape(self, *, verify_timeout: float | None = None) -> TapeStatus:
        """Stop playback: Pause if a tape is playing, then Stop.

        Sends nothing when no tape is loaded. Returns the Tape menu as last
        read.
        """
        status = self.tape_status()
        if status.playing:
            status = self._tape_action(ACTION_PAUSE, lambda s: s.paused, verify_timeout)
        if status.paused:
            status = self._tape_action(
                ACTION_STOP, lambda s: not s.playing and not s.paused, verify_timeout)
        return status


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------

def _menu(host: str, menu: Ultimate64TapeMenu | None) -> Ultimate64TapeMenu:
    return menu if menu is not None else Ultimate64TapeMenu(host)


def tape_status(host: str, *, menu: Ultimate64TapeMenu | None = None) -> TapeStatus:
    """What the Tape task menu shows: recording, playing, paused.

    A device with no tape hardware reads the same as an idle one (no Tape
    category at the root); see :func:`tape_require_supported`. A Tape menu
    with a label this driver does not know raises
    ``RemoteMenuUnexpectedScreenError`` rather than being read as a state.
    """
    _require_lock(host, "tape_status")
    return _menu(host, menu).tape_status()


def tape_require_supported(host: str, directory: str, *, tap_path: str | None = None,
                           menu: Ultimate64TapeMenu | None = None) -> str:
    """Check that the device has a tape recorder; return its product title.

    Browses into *directory* (a writable one: not ``/Temp``, not ``/Flash``)
    and reads the Tape task menu there, where a product with a recorder
    always lists a capture item. Chooses nothing and creates nothing.

    **A directory the firmware does not treat as writable looks the same
    as a product without a recorder**: the capture items are disabled, so
    the menu lists no Tape category. That FTP can write there proves
    nothing about it. Pass *tap_path*, a ``.tap`` already on the device
    (``storage_put_file``), and the TAP player is asked too: its menu items
    do not depend on writability.

    :raises TapeCaptureNotOfferedError: no capture is offered in
        *directory*, but the file at *tap_path* gets the player's items.
        Either the directory is not writable to the firmware, or the build
        has a player and no recorder (a single-drive 1541 Ultimate-II); the
        menus cannot tell which. Playback works in both cases.
    :raises TapeUnsupportedError: no capture is offered and, with
        *tap_path*, no player either. Without *tap_path* this is also what
        a not-writable directory raises; the message says so. On an
        Ultimate 64 / 64 Elite / C64 Ultimate this is always the answer.
    """
    _require_lock(host, "tape_require_supported")
    parts, _ = _capture_target(directory, "probe")
    if tap_path is not None:
        # has_player checks it too, but only after the Tape-menu session has
        # run: here a malformed path is refused before any connection.
        _rm._split_path(tap_path)
    return _menu(host, menu).require_recorder(parts, tap_path)


def tape_capture_start(host: str, directory: str, name: str, *, dry_run: bool = False,
                       verify_timeout: float | None = None,
                       menu: Ultimate64TapeMenu | None = None) -> TapeCapture:
    """Start recording the cassette write line to ``<directory>/<name>.tap``.

    Checks, in this order and before any menu key: the lock; the target
    (``ValueError`` for ``/Temp``, ``/Flash`` and any path the storage
    helpers refuse to write, or a bad name); that *directory* exists and
    holds no file of that name (``FileNotFoundError``, ``FileExistsError``;
    names compare without case); that no capture is already running. Then
    it browses into *directory*, chooses Tape > "Capture save to TAP" by
    name, types *name* into the prompt and confirms that a fresh connection
    shows "Finish Rec. to TAP".

    The capture runs until :func:`tape_capture_stop`. Always stop it, in a
    ``finally``: the file grows with every pulse.

    :raises TapeMenuRejectedError: the firmware showed an error popup.
    :raises RemoteMenuVerifyError: the ENTER was sent but no capture shows.
    """
    _require_lock(host, "tape_capture_start")
    parts, path = _capture_target(directory, name)
    wanted = _ascii_fold(f"{name}.tap")
    for entry in storage_list_dir(host, directory):
        if _ascii_fold(entry.name) == wanted:
            raise FileExistsError(
                f"{directory}/{entry.name} already exists on {host}; a capture never "
                "overwrites. Delete it or choose another name.")
    driver = _menu(host, menu)
    if driver.tape_status().recording:
        raise Ultimate64RemoteMenuError(
            f"a tape capture is already running on {host}; stop it first "
            "(tape_capture_stop)")
    driver.start_capture(parts, name, dry_run=dry_run, verify_timeout=verify_timeout)
    return TapeCapture(directory, name, path, not dry_run)


def tape_capture_stop(host: str, capture: TapeCapture, *, read_file: bool = True,
                      settle_timeout: float = 30.0, verify_timeout: float | None = None,
                      menu: Ultimate64TapeMenu | None = None) -> TapeCaptureResult:
    """Finish the running capture and report *capture*'s file.

    Chooses Tape > "Finish Rec. to TAP" by name (nothing is sent if the
    menu does not offer it), confirms on a fresh connection that recording
    ended, then lists the directory for the file's size and, with
    *read_file*, reads it back and checks its TAP header. See
    :class:`TapeCaptureResult` for the statuses.

    :raises TapeCaptureNotStoppedError: Finish was not offered, yet the file
        read back has a zero length field. This needs the read: with
        ``read_file=False`` (or a file over :data:`MAX_CAPTURE_READ`) the
        check is skipped and the status is ``"unread"``, so read
        :func:`tape_status` before deleting such a file.
    """
    _require_lock(host, "tape_capture_stop")
    _capture_target(capture.directory, capture.name)
    was_recording, popup = _menu(host, menu).finish_capture(
        settle_timeout=settle_timeout, verify_timeout=verify_timeout)
    wanted = _ascii_fold(f"{capture.name}.tap")
    found = [e for e in storage_list_dir(host, capture.directory)
             if e.kind == "file" and _ascii_fold(e.name) == wanted]
    if not found:
        return TapeCaptureResult(capture.path, None, "missing", None, None, was_recording, popup)
    path = f"{capture.directory}/{found[0].name}"
    size = found[0].size
    if not read_file or size > MAX_CAPTURE_READ:
        return TapeCaptureResult(path, size, "unread", None, None, was_recording, popup)
    raw = storage_get_file(host, path)
    try:
        tap = parse_tap(raw)
    except ValueError:
        return TapeCaptureResult(path, len(raw), "bad-header", None, None, was_recording, popup)
    if not was_recording and tap.declared_length == 0:
        raise TapeCaptureNotStoppedError(
            f"{path} on {host}: the Tape menu offered no {ACTION_FINISH!r}, so nothing was "
            f"stopped, yet the file's length field is 0 ({len(tap.data)} data bytes follow). "
            "The recorder writes that field when it closes the file, so the capture may "
            "still be open. Not reporting a status; do not delete the file until the "
            "device shows that it is not recording.")
    if not tap.length_matches:
        status = "length-mismatch"
    elif not tap.data:
        status = "empty"
    else:
        status = "ok"
    return TapeCaptureResult(path, len(raw), status, tap.declared_length, len(tap.data),
                             was_recording, popup, tap)


def tape_play(host: str, path: str, *, dry_run: bool = False, verify_timeout: float | None = None,
              menu: Ultimate64TapeMenu | None = None) -> TapePlayResult:
    """Start the ``.tap`` at *path* playing ("Start Tape", chosen by name).

    The file must already be on the device (``storage_put_file``). The
    firmware presses play; the C64 still needs its own ``LOAD``. A fresh
    connection must then show "Pause Tape Playback".

    :raises TapeMenuRejectedError: the firmware refused the file.
    """
    _require_lock(host, "tape_play")
    return _menu(host, menu).start_tape(path, dry_run=dry_run, verify_timeout=verify_timeout)


def tape_stop(host: str, *, verify_timeout: float | None = None,
              menu: Ultimate64TapeMenu | None = None) -> TapeStatus:
    """Stop tape playback (Pause, then Stop). Sends nothing if none is loaded."""
    _require_lock(host, "tape_stop")
    return _menu(host, menu).stop_tape(verify_timeout=verify_timeout)


def tape_load_and_verify(transport: Any, host: str, tap_path: str, prg: PrgFile, *,
                         start: int | None = None, clock_hz: int = PAL_CLOCK_HZ,
                         found_pause: float = 15.0, margin: float = 5.0,
                         stop_after: bool = True, sleep: Callable[[float], None] = time.sleep,
                         menu: Ultimate64TapeMenu | None = None) -> TapeLoadResult:
    """Load *prg* from the tape at *tap_path* and check that it arrived.

    The machine must be at a BASIC prompt. In order: the lock check; the
    TAP is read from the device for its playing time; the load area and the
    KERNAL's end pointer ``$AE/$AF`` are zeroed; ``LOAD`` is typed (the
    KERNAL then waits for play); the tape is started; the call sleeps for
    the lead-in, the tape's playing time, *found_pause* (the KERNAL's pause
    after ``FOUND``) and *margin*; the tape is stopped (*stop_after*); and
    only then ``prg.verify_load`` reads the result.

    #573: between the typed ``LOAD`` and the end of the wait nothing reads
    C64 memory or the screen. The Remote Menu calls in that window are
    telnet only.

    The cassette buffer is reserved in the transport's ``MemoryPolicy`` from
    before the first write until the tape has been stopped; a policy that
    refuses the two clearing writes stops the call before anything is typed.

    Before anything is written or typed, a dry run of "Start Tape" checks
    that the file is there and the device has a player
    (:class:`TapeUnsupportedError` otherwise), and a negative or non-finite
    *found_pause* / *margin* or a non-positive *clock_hz* is a ``ValueError``
    before any device contact: either would end the wait early.

    If starting the tape then fails, the exception propagates after the
    tape has been stopped (*stop_after*). The ``LOAD`` was typed, so the C64
    is either still at ``PRESS PLAY ON TAPE`` (a refusal popup) or was
    loading when the tape stopped (the ENTER was sent but could not be
    confirmed): reset it either way.

    *found_pause* is not measured: 15 s is a generous allowance for the
    KERNAL's wait after ``FOUND`` (it continues by itself). *start* is where
    the program lands (default: its own load address; a relocatable program
    loads at the start of BASIC).
    """
    from ..keyboard import send_text
    from ..memory import write_bytes

    check_wait(clock_hz=clock_hz, found_pause=found_pause, margin=margin)
    _require_lock(host, "tape_load_and_verify")
    _rm._split_path(tap_path)
    tap = parse_tap(storage_get_file(host, tap_path))
    where = prg.load_address if start is None else start
    driver = _menu(host, menu)
    # Before anything is written or typed: the file is there and the device
    # offers "Start Tape" for it (TapeUnsupportedError if it has no player).
    driver.start_tape(tap_path, dry_run=True)
    waited = _LEAD_IN_SECONDS + tap.duration_seconds(clock_hz) + found_pause + margin
    after: TapeStatus | None = None
    with cassette_buffer_reserved(transport):
        write_bytes(transport, where, bytes(len(prg.data)))
        write_bytes(transport, 0x00AE, b"\x00\x00")
        send_text(transport, "LOAD\r")
        try:
            play = driver.start_tape(tap_path)
            # #573: wait the cassette I/O out; no memory or screen read until it is over.
            sleep(waited)
        finally:
            if stop_after:
                after = driver.stop_tape()
    check = prg.verify_load(transport, start=start)
    return TapeLoadResult(check, waited, play, after)

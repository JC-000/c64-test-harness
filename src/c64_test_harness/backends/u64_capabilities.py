"""Firmware capability probe for Ultimate devices.

The harness supports two device lines whose firmware fixes land on separate
schedules:

* the **Ultimate** line (U64 / U64 Elite), versioned ``3.x`` — 3.14, 3.14d,
  3.14e, 3.15;
* the **CBM** line (C64 Ultimate), versioned ``1.x`` — currently 1.1.0.

Code used to ask ``firmware_version.startswith("3.14")`` and branch on the
answer. That conflates "is this device 3.14" with "does this device have fix
X", and it got both device lines wrong at once: the C64U reports ``1.1.0``, so
it failed the match and silently took the path meant for *fixed* firmware even
though tag 1.1.0 predates the fix; and when a U64E was flashed to 3.15 the
match stopped firing and the behaviour flipped with nothing asserting it.

:class:`DeviceCapabilities` replaces that with named capabilities, each
carrying its own version rule. Anything the version string genuinely cannot
settle reports ``None`` rather than guessing — see *Post-tag capabilities*.

Post-tag capabilities
---------------------
``ee005041 "Bump to 3.15"`` is the *start* of the 3.15 line, not its release.
Work merged after it — multi-block socket reads (upstream #802/#806), socket
lifetime bounds (#808), readmem argument bounds (#760) — ships in builds that
all report the same ``"3.15"`` string. Those capabilities are ``None`` on
``3.x`` until a behavioural probe pins them, and ``False`` on the older lines
where the answer is knowable. Pass ``overrides=`` to record a probe result.
"""
from __future__ import annotations

import logging
import os
import re
import sys
import warnings
from dataclasses import dataclass, replace
from typing import Any, Mapping

_log = logging.getLogger(__name__)

__all__ = [
    "DeviceCapabilities",
    "CAPABILITY_NAMES",
    "CbmFixConstantStaleWarning",
    "THRESHOLD_POST_SAFE",
    "THRESHOLD_POST_RISKY",
]

#: ``write_mem`` POST cutoff on firmware that carries the Temp-folder fix.
THRESHOLD_POST_SAFE = 48

#: ``write_mem`` POST cutoff on firmware that does not. Pushes everything
#: below the device's 128-byte ``data=`` query cap onto the PUT path, keeping
#: the 48..127 range — the band that wedged the runner — off POST entirely.
THRESHOLD_POST_RISKY = 128

#: First Ultimate-line release containing the Temp-folder GC fix
#: (GideonZ/1541ultimate#686), which is what makes the POST ``writemem`` path
#: safe to use for small payloads. The merge is an ancestor of the 3.15 bump.
#:
#: The comparison in :meth:`DeviceCapabilities._writemem_post_safe` is ``>=``,
#: so **a future major grades as fixed**: ``4.0`` is post-safe by a rule
#: written before 4.x existed. That is deliberate, and it is the opposite
#: disposition to the unreadable-version branch beside it, which grades
#: conservatively — so both are argued here rather than one being silent
#: (#258). A merged fix normally stays merged, and grading 4.x as unfixed
#: would forgo the 48-byte threshold forever on firmware that almost
#: certainly carries the collector; the cost of being wrong is a small write
#: back on the leaking POST path, on a device generation that does not exist
#: yet and can be re-graded the moment it does. The unreadable-version branch
#: is conservative for the opposite reason: there, nothing is known at all.
_ULTIMATE_WRITEMEM_FIXED_FROM = (3, 15)

#: The CBM line has no release carrying that fix yet: tag ``1.1.0`` is not a
#: descendant of the merge, and no later CBM build has been verified. Set this
#: to the first fixed version when one ships — either ``(1, 2)`` or
#: ``(1, 2, 0)`` works; the comparison pads both sides.
#:
#: **Nothing changes on its own while this is ``None``** (#248). The C64U
#: firmware is the ``u64ii`` build of the same 1541ultimate tree, so a later
#: release will carry #686 — but it still grades ``writemem_post_safe=False``
#: here, keeping the device on the 128 threshold and the CLAUDE.md
#: hardware-safety clause in force after it stopped being true. Staying on
#: 128 is safe, so the grade does not guess; instead a CBM device reporting a
#: version above :data:`_CBM_LAST_KNOWN_UNFIXED` while this is still ``None``
#: emits :class:`CbmFixConstantStaleWarning` (a log line alone is invisible in
#: a green pytest run) and logs a WARNING, both naming this constant. When you
#: see it, establish whether that release descends from the #686 merge and
#: edit this line.
_CBM_WRITEMEM_FIXED_FROM: tuple[int, ...] | None = None

#: The newest CBM release known to lack #686 (tag ``1.1.0``). Anything newer
#: is *unverified*, not known-leaky — which is what the #248 notice reports.
_CBM_LAST_KNOWN_UNFIXED: tuple[int, ...] = (1, 1, 0)

#: Versions the #248 notice has already been logged for in this process. Every
#: client probes capabilities, so the notice is once per version, not per call.
_CBM_STALE_NOTICE_ISSUED: set[tuple[int, ...]] = set()


def _padded(version: tuple[int, ...]) -> tuple[int, ...]:
    """``(1, 2)`` -> ``(1, 2, 0)``, so two- and three-part versions order."""
    return tuple(version) + (0,) * max(0, 3 - len(version))


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


class CbmFixConstantStaleWarning(UserWarning):
    """A C64U reports firmware newer than the last release known to lack
    GideonZ/1541ultimate#686, but ``_CBM_WRITEMEM_FIXED_FROM`` is still
    ``None``, so it is graded leak-prone without anyone having checked (#248).

    A dedicated category so a suite can escalate or silence exactly this:
    ``-W error::c64_test_harness.CbmFixConstantStaleWarning``.

    Under an error filter (that one, a generic ``-W error::UserWarning``, or
    ``filterwarnings = error``) it raises **once per firmware version per
    process**, from the first ``DeviceCapabilities.from_info``, which in
    practice is ``Ultimate64Client`` construction. Later probes of the same
    version in that process do not raise, because the version is recorded
    as noticed before the warning is issued.
    """


_PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stacklevel_outside_package() -> int:
    """``stacklevel`` for a ``warnings.warn`` in the *caller* of this helper
    that names the first frame outside ``c64_test_harness``: the test or
    script that built the client, not the client's own property.
    (``skip_file_prefixes`` would do this, but it needs Python 3.12.)"""
    frame = sys._getframe(1)
    level = 1
    prefix = _PACKAGE_DIR + os.sep
    while frame is not None and os.path.abspath(frame.f_code.co_filename).startswith(prefix):
        frame = frame.f_back
        level += 1
    return level if frame is not None else 1


def _notice_cbm_constant_stale(version: tuple[int, ...]) -> None:
    """Warn and log, once per version per process, that a newer CBM release
    is graded unfixed only because :data:`_CBM_WRITEMEM_FIXED_FROM` was never
    set (#248)."""
    if version in _CBM_STALE_NOTICE_ISSUED:
        return
    _CBM_STALE_NOTICE_ISSUED.add(version)
    message = (
        f"C64U firmware {_dotted(version)} is newer than "
        f"{_dotted(_CBM_LAST_KNOWN_UNFIXED)}, the last CBM release known to lack "
        "the /Temp fix (GideonZ/1541ultimate#686), but _CBM_WRITEMEM_FIXED_FROM "
        "in backends/u64_capabilities.py is still None, so it is graded "
        f"writemem_post_safe=False (threshold {THRESHOLD_POST_RISKY}) without "
        f"anyone having checked. Establish whether {_dotted(version)} carries "
        "#686 and set that constant (#248); until then the CLAUDE.md "
        "hardware-safety clause stays in force."
    )
    # Log first: under ``-W error`` the warning raises, and the log line
    # should survive that.
    _log.warning("%s", message)
    warnings.warn(
        message, CbmFixConstantStaleWarning, stacklevel=_stacklevel_outside_package()
    )

#: Capabilities that no version string can settle on the ``3.x`` line, because
#: they landed after the version was bumped. Knowable (and False) elsewhere.
_POST_TAG_CAPABILITIES = (
    "uci_socket_read_multiblock",
    "uci_sockets_close_on_reset",
    "readmem_rejects_zero_length",
)

CAPABILITY_NAMES = (
    "writemem_post_safe",
    "runner_wedge_possible",
) + _POST_TAG_CAPABILITIES

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def _parse_version(raw: Any) -> tuple[int, ...] | None:
    """``"V3.14d"`` -> ``(3, 14)``; ``"1.1.0"`` -> ``(1, 1, 0)``.

    A trailing letter is a point release within the same minor (3.14d is
    still 3.14), so it is dropped rather than ordered.
    """
    if not isinstance(raw, str):
        return None
    match = _VERSION_RE.match(raw.strip().lstrip("Vv"))
    if match is None:
        return None
    return tuple(int(g) for g in match.groups() if g is not None)


@dataclass(frozen=True)
class DeviceCapabilities:
    """What a device's firmware can and cannot do.

    Every capability is tri-state: ``True`` present, ``False`` absent,
    ``None`` not determinable from the version alone.

    **``writemem_post_safe`` is the exception, and its ``None`` is explicit
    (#247).**  :meth:`from_info` always grades it ``True`` or ``False`` --
    an unreadable version grades ``False`` -- so ``None`` never comes from
    probing.  It is still a legal value, because a caller may build or
    ``replace`` an instance to mean "unknown, do not assume" (the transport
    and hygiene tests do), and every consumer resolves it to the leak-prone
    side: :attr:`write_mem_query_threshold` gives 128, and
    ``Ultimate64Transport`` chunks onto PUT unless the grade ``is True``.
    Anything other than ``True``, ``False`` or ``None`` is rejected with
    ``TypeError`` on construction: a truthy non-bool such as ``"no"`` or
    ``1`` used to grade post-safe.
    """

    firmware_version: str | None
    product: str | None
    version_tuple: tuple[int, ...] | None
    generation: str

    #: POST ``/v1/machine:writemem`` does not accumulate Temp-folder entries
    #: (upstream #686). When False, small payloads must take the PUT path.
    #: ``None`` only when hand-built; see the class docstring (#247).
    writemem_post_safe: bool | None
    #: The runner subsystem can wedge under write load (the inverse of above).
    runner_wedge_possible: bool | None
    #: ``READ_SOCKET`` accepts up to 1472 bytes and spans reply blocks.
    uci_socket_read_multiblock: bool | None
    #: UCI sockets are bounded and closed on C64 reset.
    uci_sockets_close_on_reset: bool | None
    #: ``GET /v1/machine:readmem?length=0`` answers 400 rather than 200.
    readmem_rejects_zero_length: bool | None

    def __post_init__(self) -> None:
        grade = self.writemem_post_safe
        if grade is not None and type(grade) is not bool:
            raise TypeError(
                "writemem_post_safe must be True, False or None (unknown, "
                f"treated as leak-prone); got {grade!r} (#247)"
            )

    @classmethod
    def from_info(
        cls,
        info: Mapping[str, Any] | None,
        *,
        overrides: Mapping[str, bool | None] | None = None,
    ) -> "DeviceCapabilities":
        """Derive capabilities from a ``GET /v1/info`` payload.

        :param info: the decoded payload, or ``None`` when the probe failed.
            A failed probe is treated as unknown firmware, which resolves
            conservatively — every fix assumed absent.
        :param overrides: capability name -> value, for pinning something a
            behavioural probe established. Unknown names raise ``ValueError``.
        """
        payload: Mapping[str, Any] = info if isinstance(info, Mapping) else {}
        raw_version = payload.get("firmware_version")
        version = _parse_version(raw_version)
        generation = cls._generation_for(version)

        writemem_post_safe = cls._writemem_post_safe(generation, version)
        # Post-tag work sits on the 3.15 line only. Below it the answer is
        # knowable and negative; on it the version string cannot tell.
        post_tag = None if (
            generation == "ultimate"
            and version is not None
            and version[:2] >= _ULTIMATE_WRITEMEM_FIXED_FROM
        ) else False

        caps = cls(
            firmware_version=raw_version if isinstance(raw_version, str) else None,
            product=payload.get("product") if isinstance(payload.get("product"), str) else None,
            version_tuple=version,
            generation=generation,
            writemem_post_safe=writemem_post_safe,
            runner_wedge_possible=(
                None if writemem_post_safe is None else not writemem_post_safe
            ),
            uci_socket_read_multiblock=post_tag,
            uci_sockets_close_on_reset=post_tag,
            readmem_rejects_zero_length=post_tag,
        )

        if overrides:
            unknown = set(overrides) - set(CAPABILITY_NAMES)
            if unknown:
                raise ValueError(
                    f"unknown capability name(s): {sorted(unknown)}; "
                    f"known names are {list(CAPABILITY_NAMES)}"
                )
            caps = replace(caps, **dict(overrides))

        # #248: only once overrides are applied, and only if the final grade
        # is still False. A probe that pinned writemem_post_safe has checked.
        if (
            generation == "cbm"
            and version is not None
            and _CBM_WRITEMEM_FIXED_FROM is None
            and caps.writemem_post_safe is False
            and _padded(version) > _padded(_CBM_LAST_KNOWN_UNFIXED)
        ):
            _notice_cbm_constant_stale(version)
        return caps

    @staticmethod
    def _generation_for(version: tuple[int, ...] | None) -> str:
        """The device line, or ``"unknown"``.

        ``"unknown"`` covers **two different situations, and only one of
        them is transient** (#258).  A caller that re-reads ``/v1/info``
        hoping the grade improves must tell them apart:

        * ``version is None`` — nothing parsed.  A genuine transient: the
          probe timed out, the device was mid-boot, the payload was
          malformed.  Re-reading may help.
        * a version parsed, but its major is neither ``>= 3`` nor ``1`` —
          a ``2.x`` string parses perfectly and still grades ``unknown``.
          Re-reading **cannot** help: the same string parses identically
          every time.  A consumer that keys a retry loop on "generation is
          unknown" burns its reads and then refuses with "no usable
          version", two lines below a line printing the version it read.

        Both are graded off for safety, which is right.  Only the
        diagnosis differs, and ``firmware_version`` is what distinguishes
        them: ``None`` for the first, a string for the second.
        """
        if version is None:
            return "unknown"
        if version[0] >= 3:
            return "ultimate"
        if version[0] == 1:
            return "cbm"
        return "unknown"

    @staticmethod
    def _writemem_post_safe(
        generation: str, version: tuple[int, ...] | None
    ) -> bool:
        """Conservative: an unreadable version assumes the fix is absent.

        Guessing "present" would put small writes back on the leaking POST
        path; guessing "absent" only costs a slightly higher PUT threshold.
        """
        if version is None:
            return False
        if generation == "ultimate":
            return version[:2] >= _ULTIMATE_WRITEMEM_FIXED_FROM
        if generation == "cbm":
            if _CBM_WRITEMEM_FIXED_FROM is None:
                # The #248 notice is emitted by from_info, after overrides.
                return False
            return _padded(version) >= _padded(_CBM_WRITEMEM_FIXED_FROM)
        return False

    @property
    def write_mem_query_threshold(self) -> int:
        """Payload size at which ``write_mem`` switches from PUT to POST."""
        # ``is True``, not truthiness: only an explicit True reaches 48, the
        # same test ``Ultimate64Transport`` applies before its single-request
        # path (#247).
        return (
            THRESHOLD_POST_SAFE
            if self.writemem_post_safe is True
            else THRESHOLD_POST_RISKY
        )

    def describe(self) -> str:
        """One-line summary for logs and skip messages."""
        version = self.firmware_version or "unknown"
        flags = ", ".join(
            f"{name}={getattr(self, name)}" for name in CAPABILITY_NAMES
        )
        return f"fw={version} generation={self.generation} {flags}"

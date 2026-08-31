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

import re
from dataclasses import dataclass, replace
from typing import Any, Mapping

__all__ = [
    "DeviceCapabilities",
    "CAPABILITY_NAMES",
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
_ULTIMATE_WRITEMEM_FIXED_FROM = (3, 15)

#: The CBM line has no release carrying that fix yet: tag ``1.1.0`` is not a
#: descendant of the merge, and no later CBM build has been verified. Set this
#: to the first fixed version when one ships.
_CBM_WRITEMEM_FIXED_FROM: tuple[int, ...] | None = None

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
    """

    firmware_version: str | None
    product: str | None
    version_tuple: tuple[int, ...] | None
    generation: str

    #: POST ``/v1/machine:writemem`` does not accumulate Temp-folder entries
    #: (upstream #686). When False, small payloads must take the PUT path.
    writemem_post_safe: bool | None
    #: The runner subsystem can wedge under write load (the inverse of above).
    runner_wedge_possible: bool | None
    #: ``READ_SOCKET`` accepts up to 1472 bytes and spans reply blocks.
    uci_socket_read_multiblock: bool | None
    #: UCI sockets are bounded and closed on C64 reset.
    uci_sockets_close_on_reset: bool | None
    #: ``GET /v1/machine:readmem?length=0`` answers 400 rather than 200.
    readmem_rejects_zero_length: bool | None

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
        return caps

    @staticmethod
    def _generation_for(version: tuple[int, ...] | None) -> str:
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
                return False
            return version[:2] >= _CBM_WRITEMEM_FIXED_FROM
        return False

    @property
    def write_mem_query_threshold(self) -> int:
        """Payload size at which ``write_mem`` switches from PUT to POST."""
        return (
            THRESHOLD_POST_SAFE
            if self.writemem_post_safe
            else THRESHOLD_POST_RISKY
        )

    def describe(self) -> str:
        """One-line summary for logs and skip messages."""
        version = self.firmware_version or "unknown"
        flags = ", ".join(
            f"{name}={getattr(self, name)}" for name in CAPABILITY_NAMES
        )
        return f"fw={version} generation={self.generation} {flags}"

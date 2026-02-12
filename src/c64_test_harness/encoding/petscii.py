"""Unicode → PETSCII conversion table with special key constants.

Merges the base table from vicemon.py with the extended mappings from
test_csr.py's EXTRA_PETSCII, producing one complete table.

Use ``char_to_petscii(ch)`` for conversion, or ``register_petscii(ch, code)``
to add project-specific mappings at runtime.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Special key constants (PETSCII codes for non-printable keys)
# ---------------------------------------------------------------------------

PETSCII_RETURN = 0x0D
PETSCII_HOME = 0x13
PETSCII_CLR = 0x93
PETSCII_DEL = 0x14
PETSCII_INSERT = 0x94

PETSCII_CRSR_DOWN = 0x11
PETSCII_CRSR_UP = 0x91
PETSCII_CRSR_RIGHT = 0x1D
PETSCII_CRSR_LEFT = 0x9D

PETSCII_F1 = 0x85
PETSCII_F3 = 0x86
PETSCII_F5 = 0x87
PETSCII_F7 = 0x88
PETSCII_F2 = 0x89
PETSCII_F4 = 0x8A
PETSCII_F6 = 0x8B
PETSCII_F8 = 0x8C

PETSCII_RUN_STOP = 0x03

# ---------------------------------------------------------------------------
# Unified ASCII/Unicode → PETSCII table
# ---------------------------------------------------------------------------

_PETSCII_MAP: dict[str, int] = {}

# Uppercase letters (PETSCII same code as ASCII)
for _i in range(ord("A"), ord("Z") + 1):
    _PETSCII_MAP[chr(_i)] = _i

# Lowercase → uppercase PETSCII
for _i in range(ord("a"), ord("z") + 1):
    _PETSCII_MAP[chr(_i)] = _i - 32

# Digits
for _i in range(ord("0"), ord("9") + 1):
    _PETSCII_MAP[chr(_i)] = _i

# Common punctuation (from vicemon.py)
_PETSCII_MAP[" "] = 0x20
_PETSCII_MAP["\r"] = 0x0D
_PETSCII_MAP["\n"] = 0x0D
_PETSCII_MAP["!"] = 0x21
_PETSCII_MAP['"'] = 0x22
_PETSCII_MAP["#"] = 0x23
_PETSCII_MAP["$"] = 0x24
_PETSCII_MAP["%"] = 0x25
_PETSCII_MAP["&"] = 0x26
_PETSCII_MAP["'"] = 0x27
_PETSCII_MAP["("] = 0x28
_PETSCII_MAP[")"] = 0x29
_PETSCII_MAP["*"] = 0x2A
_PETSCII_MAP["+"] = 0x2B
_PETSCII_MAP[","] = 0x2C
_PETSCII_MAP["-"] = 0x2D
_PETSCII_MAP["."] = 0x2E
_PETSCII_MAP["/"] = 0x2F
_PETSCII_MAP[":"] = 0x3A
_PETSCII_MAP[";"] = 0x3B
_PETSCII_MAP["="] = 0x3D
_PETSCII_MAP["?"] = 0x3F

# Extended mappings (from test_csr.py's EXTRA_PETSCII — bug fix #3)
_PETSCII_MAP["@"] = 0x40
_PETSCII_MAP["<"] = 0x3C
_PETSCII_MAP[">"] = 0x3E
_PETSCII_MAP["["] = 0x5B
_PETSCII_MAP["]"] = 0x5D
_PETSCII_MAP["_"] = 0xA4


def char_to_petscii(ch: str) -> int:
    """Convert a single character to its PETSCII code.

    Raises ``ValueError`` if no mapping exists.
    """
    try:
        return _PETSCII_MAP[ch]
    except KeyError:
        raise ValueError(f"No PETSCII mapping for {ch!r}") from None


def register_petscii(ch: str, code: int) -> None:
    """Register a custom character → PETSCII mapping.

    Useful for project-specific extensions (e.g., custom PETSCII graphics).
    """
    if not (0 <= code <= 255):
        raise ValueError(f"PETSCII code must be 0-255, got {code}")
    _PETSCII_MAP[ch] = code

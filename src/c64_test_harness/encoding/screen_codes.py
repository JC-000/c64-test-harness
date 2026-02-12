"""Full 256-entry C64 screen code to Unicode mapping.

Screen codes map the byte values in C64 screen memory ($0400-$07E7) to
displayable characters.  The table covers all 256 codes:

  0x00-0x1F: @, A-Z, [, \\, ], ^, _
  0x20:      space
  0x21-0x3F: !"#$%&'()*+,-./0-9:;<=>?
  0x40-0x5F: repeat of 0x00-0x1F
  0x60-0x7F: graphics characters (Unicode block element U+2592)
  0x80-0xFF: reverse-video versions of 0x00-0x7F
"""

# Graphics placeholder — distinguishable from period (screen code 0x2E)
GRAPHICS_PLACEHOLDER = "\u2592"  # ▒

# Build the table as a list first, then freeze as tuple
_table: list[str] = ["?"] * 256

# 0x00 = @
_table[0] = "@"
# 0x01-0x1A = A-Z
for _i in range(1, 27):
    _table[_i] = chr(ord("A") + _i - 1)
# 0x1B-0x1F
_table[27] = "["
_table[28] = "\\"
_table[29] = "]"
_table[30] = "^"
_table[31] = "_"
# 0x20 = space
_table[32] = " "
# 0x21-0x3F = printable ASCII !"#..?
for _i in range(33, 64):
    _table[_i] = chr(_i)
# 0x40-0x5F = repeat of 0x00-0x1F
for _i in range(64, 96):
    _table[_i] = _table[_i - 64]
# 0x60-0x7F = graphics characters
for _i in range(96, 128):
    _table[_i] = GRAPHICS_PLACEHOLDER
# 0x80-0xFF = reverse video (same visible chars as 0x00-0x7F)
for _i in range(128, 256):
    _table[_i] = _table[_i - 128]

SCREEN_CODE_TABLE: tuple[str, ...] = tuple(_table)
"""Immutable 256-entry table: ``SCREEN_CODE_TABLE[code]`` → Unicode char."""


def screen_code_to_char(code: int) -> str:
    """Convert a single screen code byte to its Unicode character."""
    return SCREEN_CODE_TABLE[code & 0xFF]

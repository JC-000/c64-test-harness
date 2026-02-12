"""C64 character encoding tables — screen codes and PETSCII."""

from .screen_codes import SCREEN_CODE_TABLE, screen_code_to_char
from .petscii import (
    char_to_petscii,
    register_petscii,
    PETSCII_RETURN,
    PETSCII_HOME,
    PETSCII_CLR,
    PETSCII_DEL,
    PETSCII_F1,
    PETSCII_F3,
    PETSCII_F5,
    PETSCII_F7,
    PETSCII_CRSR_DOWN,
    PETSCII_CRSR_RIGHT,
    PETSCII_CRSR_UP,
    PETSCII_CRSR_LEFT,
    PETSCII_RUN_STOP,
)

__all__ = [
    "SCREEN_CODE_TABLE",
    "screen_code_to_char",
    "char_to_petscii",
    "register_petscii",
    "PETSCII_RETURN",
    "PETSCII_HOME",
    "PETSCII_CLR",
    "PETSCII_DEL",
    "PETSCII_F1",
    "PETSCII_F3",
    "PETSCII_F5",
    "PETSCII_F7",
    "PETSCII_CRSR_DOWN",
    "PETSCII_CRSR_RIGHT",
    "PETSCII_CRSR_UP",
    "PETSCII_CRSR_LEFT",
    "PETSCII_RUN_STOP",
]

"""Log line emission for the simulator (docs/sentinel-plan.md section 5).

The 46-byte fixed-width header is a cross-component contract. Both the generator and the
reference parser depend on these exact offsets, so they are declared once here and the
tests assert against them. Changing any width or separator position requires approval.

    0         1         2         3         4
    0123456789012345678901234567890123456789012345
    1790603412345|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIMEOUT table=claims after_ms=3000
    ^0        ^13 ^14   ^16  ^19 ^20  ^23 ^24     ^29 ^30       ^36 ^37      ^45 ^46
"""

from __future__ import annotations

from dataclasses import dataclass

HEADER_BYTES: int = 46
TS_SLICE = slice(0, 13)
LEVEL_INDEX = 14
SERVICE_SLICE = slice(16, 19)
COMPONENT_SLICE = slice(20, 23)
CODE_SLICE = slice(24, 29)
LATENCY_SLICE = slice(30, 36)
TRACE_SLICE = slice(37, 45)
MESSAGE_SLICE = slice(46, None)

# Byte offsets of the seven separators (section 5, last table row).
SEPARATOR_INDEXES: tuple[int, ...] = (13, 15, 19, 23, 29, 36, 45)
SEPARATOR_BYTE = ord("|")
LEVELS: frozenset[str] = frozenset({"I", "W", "E", "F"})
LEVEL_CLASS: dict[str, int] = {"I": 0, "W": 1, "E": 2, "F": 3}
INVALID_LEVEL = 255
# Bytes above this are counted as oversized_line by the detector (section 8.9).
OVERSIZED_BYTES: int = 8192
# A line longer than this is almost certainly two interleaved writes or a stray binary
# blob, so the reference parser stops treating the tail as a message.
ABSOLUTE_MAX_BYTES: int = 1 << 20

ASCII_ERRORS = "strict"


class LineFormatError(ValueError):
    """Raised when a field cannot be rendered into the fixed-width header."""


@dataclass(frozen=True, slots=True)
class LogFields:
    """The seven header fields of one line, in log order."""

    ts_ms: int
    level: str
    service: str
    component: str
    code: str
    latency_ms: int
    trace_id: str
    message: str


def format_line(fields: LogFields) -> str:
    """Render one line, header included, without the trailing newline.

    Raises LineFormatError on any width violation, which means a bad field is a loud
    failure in the generator rather than a silent format drift in the log.
    """
    if not isinstance(fields.ts_ms, int):
        raise LineFormatError("ts_ms must be an int")
    # 13 digits is the field width, so the value must be in [10^12, 10^13).
    if not 1_000_000_000_000 <= fields.ts_ms <= 9_999_999_999_999:
        raise LineFormatError(f"ts_ms must be 13 digits, got {fields.ts_ms}")
    if fields.level not in LEVELS:
        raise LineFormatError(f"level must be one of IWE F, got {fields.level!r}")
    if len(fields.service) != 3 or not fields.service.isascii():
        raise LineFormatError(f"service must be 3 ASCII chars, got {fields.service!r}")
    if len(fields.component) != 3 or not fields.component.isascii():
        raise LineFormatError(f"component must be 3 ASCII chars, got {fields.component!r}")
    if len(fields.code) != 5 or not fields.code.isascii():
        raise LineFormatError(f"code must be 5 ASCII chars, got {fields.code!r}")
    if not 0 <= fields.latency_ms <= 999_999:
        raise LineFormatError(f"latency_ms must fit 6 digits, got {fields.latency_ms}")
    if len(fields.trace_id) != 8 or not _is_hex8(fields.trace_id):
        raise LineFormatError(f"trace_id must be 8 hex chars, got {fields.trace_id!r}")
    if not fields.message.isascii():
        raise LineFormatError("message must be ASCII (no PHI, no unicode)")

    header = (
        f"{fields.ts_ms:013d}|{fields.level}|{fields.service}|{fields.component}"
        f"|{fields.code}|{fields.latency_ms:06d}|{fields.trace_id}"
    )
    line = f"{header}|{fields.message}"
    if len(line.encode("ascii")) != HEADER_BYTES + len(fields.message):
        raise LineFormatError("header is not exactly 46 bytes")
    return line


def _is_hex8(value: str) -> bool:
    return all(c in "0123456789abcdefABCDEF" for c in value)

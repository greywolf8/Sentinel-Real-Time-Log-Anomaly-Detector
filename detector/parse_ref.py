"""Reference per-line parser for platform.log (docs/sentinel-plan.md sections 8.3 and 8.4).

This is the correctness oracle. The numpy batch parser in ``parse_batch.py`` is the fast
path; the two must produce identical counts, including on malformed lines (section 8.4,
property test). So this module is deliberately plain: no numpy, no regex on the hot path,
one line in and one verdict out, and it never raises on bad input.

Section 8.9 sets the contract. Every line is classified as either valid or exactly one of
these classes, and nothing is ever silently dropped:

    malformed_header   shorter than 46 bytes, or a separator byte is wrong
    bad_timestamp      non-digit in bytes 0-12
    bad_level          level byte outside I W E F
    unknown_component  the 7-byte SVC|CMP key is not in the catalog
    unknown_code       known component, code not in the catalog
    oversized_line     more than ~8 KB
    non_utf8_message   the message bytes do not decode (still counted as valid otherwise)

A line can trip more than one check. The class is the first failure in the order above,
which is the order a reader would notice them in, and ``flags`` keeps the full set so the
unidentified panel can show every reason.

    Timing checks (late, future, clock skew) are left to the caller: they need a watermark,
    and the watermark belongs to the ring, not the parser. ``parse_line`` returns the raw
    timestamp so the caller can do that.

The header offsets are redeclared here rather than imported. The detector does not import
simulator code (section 3), so the format is a contract written down twice, and
tests/test_log_format.py asserts the two declarations agree. That test is the reason this
duplication is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Iterable, Iterator, Mapping, Sequence

# --- the 46-byte header contract, docs/sentinel-plan.md section 5 ---
HEADER_BYTES: int = 46
TS_SLICE = slice(0, 13)
LEVEL_INDEX: int = 14
SERVICE_SLICE = slice(16, 19)
COMPONENT_SLICE = slice(20, 23)
CODE_SLICE = slice(24, 29)
LATENCY_SLICE = slice(30, 36)
TRACE_SLICE = slice(37, 45)
MESSAGE_SLICE = slice(46, None)
SEPARATOR_INDEXES: tuple[int, ...] = (13, 15, 19, 23, 29, 36, 45)
SEPARATOR_BYTE: int = ord("|")
LEVELS: frozenset[str] = frozenset({"I", "W", "E", "F"})
INVALID_LEVEL: int = 255
SUCCESS_CODE: str = "00000"
# Section 8.9: more than about 8 KB is oversized. The hard stop guards a stray binary
# blob, which is still counted but never parsed.
OVERSIZED_BYTES: int = 8192
ABSOLUTE_MAX_BYTES: int = 1 << 20

# The level byte to class mapping the numpy path uses as a 256-entry LUT (section 8.1).
LEVEL_LUT: list[int] = [INVALID_LEVEL] * 256
for _idx, _lvl in enumerate(sorted(LEVELS)):
    LEVEL_LUT[ord(_lvl)] = {ord("I"): 0, ord("W"): 1, ord("E"): 2, ord("F"): 3}[ord(_lvl)]


class LineClass(IntEnum):
    """Section 8.9 classification. VALID is 0 so a zero-initialised array means valid."""

    VALID = 0
    MALFORMED_HEADER = 1
    BAD_TIMESTAMP = 2
    BAD_LEVEL = 3
    UNKNOWN_COMPONENT = 4
    UNKNOWN_CODE = 5
    OVERSIZED_LINE = 6
    NON_UTF8_MESSAGE = 7


CLASS_NAMES: dict[LineClass, str] = {
    cls: name
    for cls, name in (
        (LineClass.VALID, "valid"),
        (LineClass.MALFORMED_HEADER, "malformed_header"),
        (LineClass.BAD_TIMESTAMP, "bad_timestamp"),
        (LineClass.BAD_LEVEL, "bad_level"),
        (LineClass.UNKNOWN_COMPONENT, "unknown_component"),
        (LineClass.UNKNOWN_CODE, "unknown_code"),
        (LineClass.OVERSIZED_LINE, "oversized_line"),
        (LineClass.NON_UTF8_MESSAGE, "non_utf8_message"),
    )
}

# Classes that must not touch any component ring. Everything else is attributed and
# counted, however odd it looks (section 8.9).
REJECTING_CLASSES: frozenset[LineClass] = frozenset(
    {
        LineClass.MALFORMED_HEADER,
        LineClass.BAD_TIMESTAMP,
        LineClass.BAD_LEVEL,
        LineClass.UNKNOWN_COMPONENT,
        LineClass.OVERSIZED_LINE,
    }
)


@dataclass(frozen=True, slots=True)
class ParsedLine:
    """One classified line. Fields are filled in only as far as the classification allows."""

    ok: bool
    cls: LineClass
    ts_ms: int = 0
    level: str = ""
    level_class: int = INVALID_LEVEL
    service: str = ""
    component: str = ""
    key: str = ""
    code: str = ""
    latency_ms: int = -1
    trace: str = ""
    message: str = ""
    raw_len: int = 0
    flags: frozenset[LineClass] = field(default_factory=frozenset)

    @property
    def class_name(self) -> str:
        return CLASS_NAMES[self.cls]

    @property
    def is_error(self) -> bool:
        """True for level E or F on a line that counts toward a component."""
        return self.counts_toward_component and self.level_class >= 2

    @property
    def counts_toward_component(self) -> bool:
        """Whether this line may update a component ring (section 8.9).

        unknown_code and non_utf8_message lines do: they are attributed to a known
        component, so their total counts, and an E or F unmapped code still counts toward
        the error rate. malformed_header, bad_timestamp, bad_level and unknown_component
        lines do not: they cannot be attributed, so they are recorded in ``unk`` only.
        """
        return self.cls not in REJECTING_CLASSES


class Vocabulary:
    """The known 7-byte component keys and 5-byte codes, loaded from catalog.yaml.

    The detector reads the catalog as a data file (section 5) and never imports sim code.
    ``build_vocabulary`` accepts a plain mapping so tests can build a tiny catalog without
    touching the YAML.
    """

    __slots__ = ("keys", "key_set", "codes", "code_set", "services")

    def __init__(
        self,
        keys: Iterable[str],
        codes: Iterable[str],
        services: Iterable[str] = (),
    ) -> None:
        self.keys: tuple[str, ...] = tuple(keys)
        self.key_set: frozenset[str] = frozenset(self.keys)
        self.codes: tuple[str, ...] = tuple(codes)
        self.code_set: frozenset[str] = frozenset(self.codes)
        self.services: frozenset[str] = frozenset(services)

    def __len__(self) -> int:
        return len(self.keys)


def build_vocabulary(components: Mapping[str, Mapping[str, str]], codes: Mapping[str, object]) -> Vocabulary:
    """Build a Vocabulary from catalog shapes: components as code -> {service, ...}."""
    keys = [f"{spec['service']}|{code}" for code, spec in components.items()]
    code_ids = [SUCCESS_CODE, *codes.keys()]
    services = [str(spec["service"]) for spec in components.values()]
    return Vocabulary(keys, code_ids, services)


def load_vocabulary(catalog_path: str | None = None) -> Vocabulary:
    """Load the vocabulary from sim/catalog.yaml.

    This is a data read, not a code dependency: the detector is allowed to read the
    catalog, never to import the simulator (section 3, key rules).
    """
    import yaml

    from pathlib import Path

    path = Path(catalog_path) if catalog_path else Path(__file__).resolve().parent.parent / "sim" / "catalog.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return build_vocabulary(raw["components"], raw["codes"])


def parse_timestamp(chunk: bytes) -> int | None:
    """Bytes 0-12 as epoch ms, or None if any byte is not a digit (section 8.9)."""
    if len(chunk) != 13:
        return None
    value = 0
    for byte in chunk:
        if byte < 0x30 or byte > 0x39:
            return None
        value = value * 10 + (byte - 0x30)
    return value


def parse_latency(chunk: bytes) -> int:
    """Bytes 30-35 as ms, or -1 if not six digits. A bad latency never rejects the line;
    the latency histogram skips it, the counts still stand."""
    if len(chunk) != 6:
        return -1
    value = 0
    for byte in chunk:
        if byte < 0x30 or byte > 0x39:
            return -1
        value = value * 10 + (byte - 0x30)
    return value


def split_lines(data: bytes) -> Iterator[bytes]:
    """Split a chunk into lines, dropping a trailing partial line (section 8.9).

    The tailer normally handles the carry buffer; this is here so the reference parser can
    be pointed straight at a file in a test.
    """
    start = 0
    while True:
        nl = data.find(b"\n", start)
        if nl < 0:
            break
        yield data[start:nl]
        start = nl + 1


def parse_line(line: bytes, vocab: Vocabulary | None = None) -> ParsedLine:
    """Classify one line. Never raises, whatever the bytes are."""
    raw_len = len(line)
    # Tolerate CRLF endings (section 8.9, other robustness).
    if line.endswith(b"\r"):
        line = line[:-1]
        raw_len -= 1

    if raw_len > OVERSIZED_BYTES:
        # Counted, truncated for sampling, never parsed: a 10 KB "line" is not a log line.
        return ParsedLine(ok=False, cls=LineClass.OVERSIZED_LINE, raw_len=raw_len,
                          flags=frozenset({LineClass.OVERSIZED_LINE}))

    if raw_len < HEADER_BYTES:
        return ParsedLine(ok=False, cls=LineClass.MALFORMED_HEADER, raw_len=raw_len,
                          flags=frozenset({LineClass.MALFORMED_HEADER}))

    if raw_len > ABSOLUTE_MAX_BYTES:  # pragma: no cover - covered by the oversized branch
        return ParsedLine(ok=False, cls=LineClass.OVERSIZED_LINE, raw_len=raw_len)

    header = line[:HEADER_BYTES]
    for index in SEPARATOR_INDEXES:
        if header[index] != SEPARATOR_BYTE:
            return ParsedLine(ok=False, cls=LineClass.MALFORMED_HEADER, raw_len=raw_len,
                              flags=frozenset({LineClass.MALFORMED_HEADER}))

    flags: set[LineClass] = set()

    ts_ms = parse_timestamp(header[TS_SLICE])
    if ts_ms is None:
        flags.add(LineClass.BAD_TIMESTAMP)

    level_byte = header[LEVEL_INDEX : LEVEL_INDEX + 1].decode("latin-1")
    level_class = LEVEL_LUT[header[LEVEL_INDEX]]
    if level_class == INVALID_LEVEL:
        flags.add(LineClass.BAD_LEVEL)

    service = header[SERVICE_SLICE].decode("latin-1")
    component = header[COMPONENT_SLICE].decode("latin-1")
    key = f"{service}|{component}"
    known_key = vocab is None or key in vocab.key_set
    if not known_key:
        flags.add(LineClass.UNKNOWN_COMPONENT)

    code = header[CODE_SLICE].decode("latin-1")
    known_code = code == SUCCESS_CODE or (vocab is not None and code in vocab.code_set)
    if not known_code:
        flags.add(LineClass.UNKNOWN_CODE)

    latency_ms = parse_latency(header[LATENCY_SLICE])
    trace = header[TRACE_SLICE].decode("latin-1")
    message_bytes = line[46:]

    # Rejecting classes: the line cannot be attributed to a component or a second, so it
    # must not touch any count. Reported class is the first rejection in section 8.9 order.
    for candidate in (
        LineClass.MALFORMED_HEADER,
        LineClass.BAD_TIMESTAMP,
        LineClass.BAD_LEVEL,
        LineClass.UNKNOWN_COMPONENT,
    ):
        if candidate in flags:
            return ParsedLine(
                ok=False,
                cls=candidate,
                ts_ms=ts_ms or 0,
                level=level_byte,
                level_class=level_class,
                service=service,
                component=component,
                key=key,
                code=code,
                latency_ms=latency_ms,
                trace=trace,
                raw_len=raw_len,
                flags=frozenset(flags),
            )

    # Counting classes: unknown_code and non_utf8_message are recorded but the line still
    # counts, and an E/F unmapped code still counts toward the error rate (section 8.9).
    try:
        message = message_bytes.decode("utf-8")
    except UnicodeDecodeError:
        # The hot path never decodes, so this is only visible when evidence is requested.
        flags.add(LineClass.NON_UTF8_MESSAGE)
        message = message_bytes.decode("utf-8", errors="replace")

    assert ts_ms is not None
    cls = LineClass.VALID
    if LineClass.NON_UTF8_MESSAGE in flags:
        cls = LineClass.NON_UTF8_MESSAGE
    elif LineClass.UNKNOWN_CODE in flags:
        cls = LineClass.UNKNOWN_CODE
    return ParsedLine(
        ok=not flags,
        cls=cls,
        ts_ms=ts_ms,
        level=level_byte,
        level_class=level_class,
        service=service,
        component=component,
        key=key,
        code=code,
        latency_ms=latency_ms,
        trace=trace,
        message=message,
        raw_len=raw_len,
        flags=frozenset(flags),
    )


@dataclass(slots=True)
class ParseSummary:
    """Counts per class, plus the totals the batch parser has to match exactly."""

    total: int = 0
    valid: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    unknown_keys: dict[str, int] = field(default_factory=dict)
    samples: list[bytes] = field(default_factory=list)
    max_samples: int = 200

    def add(self, parsed: ParsedLine, line: bytes) -> None:
        self.total += 1
        name = parsed.class_name
        self.by_class[name] = self.by_class.get(name, 0) + 1
        if parsed.ok:
            self.valid += 1
        else:
            if LineClass.UNKNOWN_COMPONENT in parsed.flags:
                self.unknown_keys[parsed.key] = self.unknown_keys.get(parsed.key, 0) + 1
            if len(self.samples) < self.max_samples:
                self.samples.append(line)


def parse_lines(lines: Iterable[bytes], vocab: Vocabulary | None = None) -> ParseSummary:
    """Classify many lines and count them. The summary is what the two parsers must agree on."""
    summary = ParseSummary()
    for line in lines:
        summary.add(parse_line(line, vocab), line)
    return summary


@dataclass(slots=True)
class ComponentCounts:
    """Per component totals: the exact output the vectorised parser must reproduce."""

    total: int = 0
    err: int = 0
    warn: int = 0
    info: int = 0
    unmapped_err: int = 0
    by_code: dict[str, int] = field(default_factory=dict)
    latency_sum: int = 0
    latency_count: int = 0
    first_sec: int | None = None
    last_sec: int | None = None

    def merge(self, other: "ComponentCounts") -> None:
        self.total += other.total
        self.err += other.err
        self.warn += other.warn
        self.info += other.info
        self.unmapped_err += other.unmapped_err
        self.latency_sum += other.latency_sum
        self.latency_count += other.latency_count
        for code, count in other.by_code.items():
            self.by_code[code] = self.by_code.get(code, 0) + count
        if other.first_sec is not None:
            self.first_sec = other.first_sec if self.first_sec is None else min(self.first_sec, other.first_sec)
            self.last_sec = other.last_sec if self.last_sec is None else max(self.last_sec, other.last_sec)


def count_lines(
    lines: Iterable[bytes],
    vocab: Vocabulary,
    key_to_name: Mapping[str, str] | None = None,
) -> dict[str, ComponentCounts]:
    """Count per component, in the shape the batch parser must match (section 8.4).

    An unknown_code line still counts in the component's total, and counts toward the
    error rate when the level is E or F (section 8.9). Malformed, unknown-component and
    oversized lines never touch any component's counts.
    """
    counts: dict[str, ComponentCounts] = {}
    for line in lines:
        parsed = parse_line(line, vocab)
        if not parsed.counts_toward_component:
            continue
        name = (key_to_name or {}).get(parsed.key, parsed.key)
        entry = counts.get(name)
        if entry is None:
            entry = counts[name] = ComponentCounts()
        entry.total += 1
        if parsed.level_class >= 2:
            entry.err += 1
        elif parsed.level_class == 1:
            entry.warn += 1
        else:
            entry.info += 1
        # One entry per code seen, whether or not the catalog knows it. Counting the
        # unknown code twice would double its share of the code-mix baseline.
        if parsed.code != SUCCESS_CODE:
            entry.by_code[parsed.code] = entry.by_code.get(parsed.code, 0) + 1
        if LineClass.UNKNOWN_CODE in parsed.flags and parsed.level_class >= 2:
            entry.unmapped_err += 1
        if parsed.latency_ms >= 0:
            entry.latency_sum += parsed.latency_ms
            entry.latency_count += 1
        second = parsed.ts_ms // 1000
        entry.first_sec = second if entry.first_sec is None else min(entry.first_sec, second)
        entry.last_sec = second if entry.last_sec is None else max(entry.last_sec, second)
    return counts


def iter_classes(lines: Sequence[bytes], vocab: Vocabulary | None = None) -> list[LineClass]:
    """Convenience for tests: the class of each line, in order."""
    return [parse_line(line, vocab).cls for line in lines]

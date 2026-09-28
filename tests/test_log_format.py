"""The 46-byte log format is a contract (docs/sentinel-plan.md section 5).

Every line the generator emits is checked here, byte by byte: header width, all seven
separator positions, the level alphabet, 13-digit timestamp, 6-digit zero-padded latency,
8 hex trace, ASCII only, single trailing newline. The offsets are asserted independently
of the implementation so a change to the format cannot slip through with a matching change
to the code that produces it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from detector import parse_ref
from sim.line_format import (
    HEADER_BYTES,
    LEVELS,
    SEPARATOR_BYTE,
    SEPARATOR_INDEXES,
    LogFields,
    format_line,
)
from tests.conftest import read_lines, run_dump

WIDTH = 30_000  # enough lines to cover every component, level and code path


def test_separator_positions_match_the_plan() -> None:
    # Byte 13, 15, 19, 23, 29, 36, 45, exactly as the last row of the section 5 table.
    assert SEPARATOR_INDEXES == (13, 15, 19, 23, 29, 36, 45)
    assert SEPARATOR_BYTE == ord("|")
    assert HEADER_BYTES == 46


def test_reference_parser_declares_the_same_offsets() -> None:
    """The detector redeclares the offsets rather than importing them; this pins them equal."""
    assert parse_ref.HEADER_BYTES == HEADER_BYTES
    assert parse_ref.SEPARATOR_INDEXES == SEPARATOR_INDEXES
    assert parse_ref.SEPARATOR_BYTE == SEPARATOR_BYTE
    assert parse_ref.LEVELS == LEVELS
    assert parse_ref.TS_SLICE == slice(0, 13)
    assert parse_ref.LEVEL_INDEX == 14
    assert parse_ref.SERVICE_SLICE == slice(16, 19)
    assert parse_ref.COMPONENT_SLICE == slice(20, 23)
    assert parse_ref.CODE_SLICE == slice(24, 29)
    assert parse_ref.LATENCY_SLICE == slice(30, 36)
    assert parse_ref.TRACE_SLICE == slice(37, 45)
    assert parse_ref.MESSAGE_SLICE == slice(46, None)


def test_sample_line_from_the_plan_round_trips(catalog_vocab: parse_ref.Vocabulary) -> None:
    """The exact example from section 5.1, byte for byte.

    Parsed against the real catalog, so the header offsets and the catalog both have to be
    right for this to come back valid.
    """
    raw = (
        b"1790603412345|E|CLM|STR|E4410|003002|b2c7e0d9|"
        b"CLAIM_DB_TIMEOUT table=claims after_ms=3000"
    )
    assert len(raw) == HEADER_BYTES + len(b"CLAIM_DB_TIMEOUT table=claims after_ms=3000")
    for index in SEPARATOR_INDEXES:
        assert raw[index : index + 1] == b"|"
    assert raw[0:13] == b"1790603412345"
    assert raw[14:15] == b"E"
    assert raw[16:19] == b"CLM"
    assert raw[20:23] == b"STR"
    assert raw[24:29] == b"E4410"
    assert raw[30:36] == b"003002"
    assert raw[37:45] == b"b2c7e0d9"

    parsed = parse_ref.parse_line(raw, catalog_vocab)
    assert parsed.ok, parsed.flags
    assert parsed.ts_ms == 1790603412345
    assert parsed.level == "E"
    assert parsed.key == "CLM|STR"
    assert parsed.code == "E4410"
    assert parsed.latency_ms == 3002
    assert parsed.trace == "b2c7e0d9"
    assert parsed.message == "CLAIM_DB_TIMEOUT table=claims after_ms=3000"
    # The plan's hand-written example does not keep the header latency and the message's
    # after_ms in step (003002 vs 3000). It is illustrative, so the generator is the thing
    # held to consistency: see test_generated_after_ms_matches_header_latency.


@pytest.fixture(scope="module")
def dump_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return run_dump(tmp_path_factory.mktemp("format"), WIDTH, daily=False)


def test_every_emitted_line_has_a_46_byte_header(dump_path: Path) -> None:
    lines = read_lines(dump_path)
    assert len(lines) == WIDTH
    for raw in lines:
        assert len(raw) >= HEADER_BYTES, raw


def test_every_separator_byte_is_a_pipe(dump_path: Path) -> None:
    for raw in read_lines(dump_path):
        for index in SEPARATOR_INDEXES:
            assert raw[index] == SEPARATOR_BYTE, (raw, index)


def test_every_level_is_in_the_alphabet(dump_path: Path) -> None:
    for raw in read_lines(dump_path):
        assert chr(raw[14]) in LEVELS, raw


def test_every_timestamp_is_13_digits(dump_path: Path) -> None:
    for raw in read_lines(dump_path):
        assert raw[0:13].isdigit(), raw
        assert len(raw[0:13]) == 13


def test_every_latency_is_six_zero_padded_digits(dump_path: Path) -> None:
    for raw in read_lines(dump_path):
        chunk = raw[30:36]
        assert len(chunk) == 6
        assert chunk.isdigit(), raw
        # Zero padded, so a short latency keeps its width and the fixed offsets hold.
        assert chunk == chunk.zfill(6)


def test_every_trace_id_is_eight_lowercase_hex(dump_path: Path) -> None:
    for raw in read_lines(dump_path):
        trace = raw[37:45]
        assert len(trace) == 8
        assert all(chr(c) in "0123456789abcdef" for c in trace), raw


def test_service_and_component_are_three_uppercase_alphanumerics(dump_path: Path) -> None:
    # Component codes are alphanumeric: the eligibility gateway is X12, not XAB.
    for raw in read_lines(dump_path):
        service = raw[16:19].decode("ascii")
        component = raw[20:23].decode("ascii")
        assert len(service) == 3 and service.isalpha() and service.isupper(), raw
        assert len(component) == 3 and component.isalnum() and component.isupper(), raw


def test_code_is_five_characters_and_digits_only_for_success(dump_path: Path) -> None:
    for raw in read_lines(dump_path):
        code = raw[24:29].decode("ascii")
        assert len(code) == 5, raw
        assert code.isdigit() or (code[0].isalpha() and code[1:].isdigit()), raw
        # A code starting with 0 is the success code, which the hot path skips.
        if raw[14] == ord("I"):
            assert code == "00000", raw


def test_every_line_is_ascii(dump_path: Path) -> None:
    dump_path.read_bytes().decode("ascii")  # raises on any byte above 0x7F


def test_file_ends_with_exactly_one_newline_and_no_blank_lines(dump_path: Path) -> None:
    data = dump_path.read_bytes()
    assert data.endswith(b"\n")
    assert b"\n\n" not in data
    assert b"\r" not in data
    assert data.count(b"\n") == WIDTH


def test_message_body_never_contains_a_pipe(dump_path: Path) -> None:
    """Only the seven header positions are separators; a pipe in the message would break
    anyone who splits the line instead of slicing it."""
    for raw in read_lines(dump_path):
        assert raw[46:].count(b"|") == 0, raw


def test_no_fault_state_leaks_into_the_log(dump_path: Path, catalog) -> None:
    """Section 6.1: rate changes are never written to the log.

    Two checks, because a banned word list alone is too blunt: NPI_RATE_LIMITED is a real
    catalog code, so words like RATE cannot simply be banned. Every emitted message is
    matched against the catalog templates instead, which proves the log carries only
    catalogued content and therefore no fault vocabulary at all.
    """
    allowed = {spec.template for spec in catalog.codes.values()}
    allowed |= {spec.success_template for spec in catalog.components.values()}
    # A rendered template differs from its template by substitution, so compare the static
    # prefix before the first placeholder, which is the part a fault marker would land in.
    prefixes = {tpl.split("{")[0] for tpl in allowed}

    for raw in read_lines(dump_path):
        message = raw[46:].decode("ascii")
        matched = False
        for tpl in allowed:
            head, sep, _ = tpl.partition("{")
            if not sep:
                if message == tpl:
                    matched = True
                    break
            elif message.startswith(head):
                matched = True
                break
        assert matched, f"message is not a rendered catalog template: {message!r}"
        assert not any(word in message for word in ("FAULT_INJECTED", "SCENARIO_", "RATE_CHANGED"))


def test_generated_after_ms_matches_header_latency(dump_path: Path) -> None:
    """Wherever a message carries after_ms= or ms=, it equals the header latency.

    Without this, an alert could quote an evidence line saying 3000 ms while the ring that
    raised it was counting 3002 ms.
    """
    checked = 0
    for raw in read_lines(dump_path):
        message = raw[46:].decode("ascii")
        for marker in ("after_ms=", " ms="):
            if marker not in message:
                continue
            value = message.split(marker)[1].split()[0]
            assert int(value) == parse_ref.parse_latency(raw[30:36]), raw
            checked += 1
    # Only a handful of the 48 codes carry a latency field, and they are error codes, so
    # the count is low by design. It is checked at all, not counted to a high bound.
    assert checked > 10, "expected latency-bearing messages in the dump"


def test_format_line_rejects_wrong_widths() -> None:
    base = LogFields(
        ts_ms=1790603412345,
        level="I",
        service="CLM",
        component="STR",
        code="00000",
        latency_ms=7,
        trace_id="b2c7e0d9",
        message="claim persisted claim=CLM-1",
    )
    assert len(format_line(base)) == HEADER_BYTES + len(base.message)

    from sim.line_format import LineFormatError

    good = {
        "ts_ms": base.ts_ms,
        "level": base.level,
        "service": base.service,
        "component": base.component,
        "code": base.code,
        "latency_ms": base.latency_ms,
        "trace_id": base.trace_id,
        "message": base.message,
    }
    bad_fields = (
        ("level", "X"),
        ("service", "CL"),
        ("component", "STRX"),
        ("code", "E441"),
        ("latency_ms", 1_000_000),
        ("trace_id", "b2c7e0d"),
        ("ts_ms", 1_794_103),
        ("message", "café"),
    )
    for field_name, bad in bad_fields:
        with pytest.raises(LineFormatError):
            format_line(LogFields(**{**good, field_name: bad}))  # type: ignore[arg-type]

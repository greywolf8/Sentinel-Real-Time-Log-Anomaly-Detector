"""Reference parser correctness, including every malformed-line class (section 8.9).

The plan's rule is that the detector never crashes, never silently drops, and never lets a
bad line corrupt a count. These tests are organised by that rule:

- valid lines parse into every field
- each of the eight classes is produced by the bytes that should produce it
- a rejecting class touches no component count; a counting class does
- unknown_code with level E still counts toward the error rate
- timing, the reason a line lands in its own second, is the caller's job
"""

from __future__ import annotations

import pytest

from detector.parse_ref import (
    LEVEL_LUT,
    REJECTING_CLASSES,
    LineClass,
    Vocabulary,
    build_vocabulary,
    count_lines,
    iter_classes,
    parse_latency,
    parse_line,
    parse_lines,
    parse_timestamp,
    split_lines,
)

HEADER = b"1790603412345|I|CLM|STR|00000|000041|a91f03c2|claim persisted claim=CLM-88213"


def line(
    ts: bytes = b"1790603412345",
    level: bytes = b"I",
    service: bytes = b"CLM",
    component: bytes = b"STR",
    code: bytes = b"00000",
    latency: bytes = b"000041",
    trace: bytes = b"a91f03c2",
    message: bytes = b"claim persisted claim=CLM-88213",
) -> bytes:
    """Build a line by field, so each test changes exactly one thing."""
    return b"|".join((ts, level, service, component, code, latency, trace, message))


# --- the valid path ---------------------------------------------------------


def test_a_valid_line_parses_every_field(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(HEADER, tiny_vocab)
    assert parsed.ok
    assert parsed.cls is LineClass.VALID
    assert parsed.flags == frozenset()
    assert parsed.ts_ms == 1790603412345
    assert parsed.level == "I"
    assert parsed.level_class == 0
    assert parsed.service == "CLM"
    assert parsed.component == "STR"
    assert parsed.key == "CLM|STR"
    assert parsed.code == "00000"
    assert parsed.latency_ms == 41
    assert parsed.trace == "a91f03c2"
    assert parsed.message == "claim persisted claim=CLM-88213"
    assert parsed.counts_toward_component
    assert not parsed.is_error


def test_levels_map_to_the_right_classes(tiny_vocab: Vocabulary) -> None:
    expected = {b"I": 0, b"W": 1, b"E": 2, b"F": 3}
    for byte, klass in expected.items():
        parsed = parse_line(line(level=byte, code=b"E4410"), tiny_vocab)
        assert parsed.level_class == klass, byte
        assert parsed.is_error is (klass >= 2)


def test_level_lut_matches_the_alphabet() -> None:
    """The 256-entry LUT is what the batch parser indexes (section 8.1)."""
    assert LEVEL_LUT[ord("I")] == 0
    assert LEVEL_LUT[ord("W")] == 1
    assert LEVEL_LUT[ord("E")] == 2
    assert LEVEL_LUT[ord("F")] == 3
    for byte in b"XW?i e":
        if byte in b"IWE F":
            continue
        assert LEVEL_LUT[byte] == 255, chr(byte)


def test_an_empty_message_is_still_valid(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(line(message=b""), tiny_vocab)
    assert parsed.ok
    assert parsed.message == ""


def test_crlf_is_tolerated(tiny_vocab: Vocabulary) -> None:
    """Section 8.9 other robustness: strip a trailing carriage return."""
    parsed = parse_line(HEADER + b"\r", tiny_vocab)
    assert parsed.ok
    assert parsed.message == "claim persisted claim=CLM-88213"


def test_a_60_byte_level_error_code_line(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(line(level=b"E", code=b"E4410", latency=b"003002",
                             message=b"CLAIM_DB_TIMEOUT table=claims after_ms=3002"), tiny_vocab)
    assert parsed.ok
    assert parsed.code == "E4410"
    assert parsed.latency_ms == 3002
    assert parsed.is_error


# --- malformed_header -------------------------------------------------------


def test_a_short_line_is_malformed(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(b"garbage line without a header", tiny_vocab)
    assert not parsed.ok
    assert parsed.cls is LineClass.MALFORMED_HEADER
    assert not parsed.counts_toward_component
    assert parsed.raw_len == len(b"garbage line without a header")


def test_an_empty_line_is_malformed(tiny_vocab: Vocabulary) -> None:
    assert parse_line(b"", tiny_vocab).cls is LineClass.MALFORMED_HEADER


def test_a_45_byte_header_is_one_byte_short(tiny_vocab: Vocabulary) -> None:
    """Byte 45 is the last separator, so dropping it must be caught."""
    truncated = HEADER[:45]
    assert len(truncated) == 45
    assert parse_line(truncated, tiny_vocab).cls is LineClass.MALFORMED_HEADER


@pytest.mark.parametrize("index", [13, 15, 19, 23, 29, 36, 45])
def test_each_separator_position_is_checked(index: int, tiny_vocab: Vocabulary) -> None:
    """All seven separators, one at a time, so no position can be skipped in the parser."""
    broken = bytearray(HEADER)
    broken[index] = ord(",")
    parsed = parse_line(bytes(broken), tiny_vocab)
    assert parsed.cls is LineClass.MALFORMED_HEADER, index
    assert LineClass.MALFORMED_HEADER in parsed.flags


def test_a_separator_replaced_by_a_digit_is_caught(tiny_vocab: Vocabulary) -> None:
    broken = bytearray(HEADER)
    broken[23] = ord("7")
    assert parse_line(bytes(broken), tiny_vocab).cls is LineClass.MALFORMED_HEADER


# --- bad_timestamp ----------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        b"17906034123a5",  # one letter in the middle
        b"abcdefghijklm",  # all letters
        b"179060341234 ",  # trailing space
        b" 790603412345",  # leading space
        b"-790603412345",  # leading minus
    ],
)
def test_thirteen_byte_timestamps_must_be_all_digits(bad: bytes, tiny_vocab: Vocabulary) -> None:
    assert len(bad) == 13
    parsed = parse_line(line(ts=bad), tiny_vocab)
    assert parsed.cls is LineClass.BAD_TIMESTAMP
    assert not parsed.counts_toward_component


@pytest.mark.parametrize("bad", [b"179060341234", b"17", b"           "])
def test_a_wrong_width_timestamp_shifts_the_header(
    bad: bytes, tiny_vocab: Vocabulary
) -> None:
    """A short or long timestamp field moves every later field, so the separator check
    catches it first. It is still rejected, just as malformed_header rather than
    bad_timestamp: the header is what is wrong, not the digits."""
    parsed = parse_line(line(ts=bad), tiny_vocab)
    assert parsed.cls is LineClass.MALFORMED_HEADER
    assert not parsed.counts_toward_component


def test_a_leading_plus_is_not_a_digit(tiny_vocab: Vocabulary) -> None:
    assert parse_line(line(ts=b"+790603412345"), tiny_vocab).cls is LineClass.BAD_TIMESTAMP


def test_a_full_13_digit_timestamp_is_accepted(tiny_vocab: Vocabulary) -> None:
    assert parse_timestamp(b"9999999999999") == 9999999999999
    assert parse_timestamp(b"0000000000000") == 0


# --- bad_level --------------------------------------------------------------


@pytest.mark.parametrize("bad", [b"X", b"i", b" ", b"D", b"1"])
def test_bad_level_bytes_are_rejected(bad: bytes, tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(line(level=bad), tiny_vocab)
    assert parsed.cls is LineClass.BAD_LEVEL
    assert parsed.level_class == 255
    # A bad level is not counted as an error, whatever the code says (section 8.9).
    assert not parsed.is_error
    assert not parsed.counts_toward_component


def test_bad_level_and_bad_code_reports_the_level(tiny_vocab: Vocabulary) -> None:
    """The reported class is the first rejection in section 8.9 order, but flags keeps
    both so the unidentified panel can show every reason."""
    parsed = parse_line(line(level=b"X", code=b"E9999"), tiny_vocab)
    assert parsed.cls is LineClass.BAD_LEVEL
    assert LineClass.UNKNOWN_CODE in parsed.flags


# --- unknown_component ------------------------------------------------------


def test_an_unknown_service_is_an_unknown_component(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(line(service=b"ZZZ", component=b"QQQ"), tiny_vocab)
    assert parsed.cls is LineClass.UNKNOWN_COMPONENT
    assert parsed.key == "ZZZ|QQQ"
    assert not parsed.counts_toward_component


def test_a_known_service_with_an_unknown_component(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(line(service=b"CLM", component=b"ZZZ"), tiny_vocab)
    assert parsed.cls is LineClass.UNKNOWN_COMPONENT
    assert parsed.key == "CLM|ZZZ"


def test_a_service_mismatch_inside_a_known_code_is_unknown(tiny_vocab: Vocabulary) -> None:
    """CLM|EDT is known, but PAY|EDT is not: the 7-byte key is what identifies a
    component, and bytes 16-22 are the component key (section 5)."""
    parsed = parse_line(line(service=b"PAY", component=b"EDT"), tiny_vocab)
    assert parsed.cls is LineClass.UNKNOWN_COMPONENT
    assert parsed.key == "PAY|EDT"


def test_the_unidentified_examples_from_the_plan(tiny_vocab: Vocabulary) -> None:
    """Section 5.1 lists four unidentifiable lines. All four are handled, none raises."""
    examples = [
        b"1790603412399|I|ZZZ|QQQ|00000|000012|a1b2c3d4|hello from a new service",
        b"1790603412402|E|CLM|EDT|E9999|000020|a1b2c3d4|SOMETHING_NEW unmapped",
        b"1790603412405|X|CLM|PRC|00000|000018|a1b2c3d4|invalid level character",
        b"garbage line without a header",
    ]
    classes = iter_classes(examples, tiny_vocab)
    assert classes == [
        LineClass.UNKNOWN_COMPONENT,
        LineClass.UNKNOWN_CODE,
        LineClass.BAD_LEVEL,
        LineClass.MALFORMED_HEADER,
    ]


# --- unknown_code -----------------------------------------------------------


def test_an_unknown_code_on_a_known_component(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(line(code=b"E9999", level=b"E", message=b"SOMETHING_NEW unmapped"),
                        tiny_vocab)
    assert parsed.cls is LineClass.UNKNOWN_CODE
    assert parsed.code == "E9999"
    assert LineClass.UNKNOWN_CODE in parsed.flags
    # Section 8.9: counted in the total, and E counts toward the error rate.
    assert parsed.counts_toward_component
    assert parsed.is_error


def test_an_unknown_warn_code_is_counted_but_not_an_error(tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(line(level=b"W", code=b"W9999"), tiny_vocab)
    assert parsed.cls is LineClass.UNKNOWN_CODE
    assert parsed.counts_toward_component
    assert not parsed.is_error


def test_the_success_code_is_never_unknown(tiny_vocab: Vocabulary) -> None:
    """00000 is a digit-only placeholder, not a catalog entry, but it is never unknown."""
    assert parse_line(line(code=b"00000"), tiny_vocab).ok
    assert parse_line(line(level=b"E", code=b"00000"), tiny_vocab).ok


# --- oversized_line ---------------------------------------------------------


def test_an_oversized_line_is_counted_and_not_parsed(tiny_vocab: Vocabulary) -> None:
    payload = b"x" * 9000
    parsed = parse_line(line(message=payload), tiny_vocab)
    assert parsed.cls is LineClass.OVERSIZED_LINE
    assert not parsed.counts_toward_component
    assert parsed.raw_len == 46 + len(payload)


def test_the_oversized_boundary_is_8kb(tiny_vocab: Vocabulary) -> None:
    just_under = line(message=b"x" * (8192 - 46))
    assert len(just_under) == 8192
    assert parse_line(just_under, tiny_vocab).ok
    over = line(message=b"x" * (8193 - 46))
    assert parse_line(over, tiny_vocab).cls is LineClass.OVERSIZED_LINE


# --- non_utf8_message -------------------------------------------------------


def test_non_utf8_bytes_decode_with_replacement(tiny_vocab: Vocabulary) -> None:
    """The hot path never decodes, so this is only visible when evidence is requested.
    Section 8.9: decode with replacement, count the line, do not drop it."""
    parsed = parse_line(line(message=b"bad \xff\xfe bytes"), tiny_vocab)
    assert parsed.cls is LineClass.NON_UTF8_MESSAGE
    assert parsed.counts_toward_component
    assert "\ufffd" in parsed.message
    assert parsed.ok is False


def test_high_ascii_bytes_are_not_utf8_errors(tiny_vocab: Vocabulary) -> None:
    """Bytes 0x80-0xFF are invalid UTF-8, but a Latin-1 message is still attributed."""
    parsed = parse_line(line(message=b"caf\xe9"), tiny_vocab)
    assert parsed.cls is LineClass.NON_UTF8_MESSAGE


def test_utf8_multibyte_messages_are_valid(tiny_vocab: Vocabulary) -> None:
    """The generator only emits ASCII, so a valid multi-byte message means someone else
    is writing to the log. It parses, and that is all this test asserts."""
    parsed = parse_line(line(message="café".encode()), tiny_vocab)
    assert parsed.cls is LineClass.VALID
    assert parsed.ok


# --- latency ----------------------------------------------------------------


def test_a_non_numeric_latency_is_reported_but_does_not_reject(tiny_vocab: Vocabulary) -> None:
    """A bad latency is not in the section 8.9 rejection list: the counts stand, only the
    latency histogram skips the line."""
    parsed = parse_line(line(latency=b"00a041"), tiny_vocab)
    assert parsed.ok
    assert parsed.latency_ms == -1


def test_latency_is_six_digits_zero_padded(tiny_vocab: Vocabulary) -> None:
    assert parse_latency(b"000002") == 2
    assert parse_latency(b"000000") == 0
    assert parse_latency(b"999999") == 999999
    assert parse_latency(b"12345") == -1
    assert parse_latency(b"1234567") == -1
    assert parse_latency(b"12_456") == -1


# --- the parser must never raise -------------------------------------------

FUZZ_INPUTS = [
    b"",
    b"\n",
    b"\x00",
    b"\x00" * 200,
    b"|" * 60,
    b"1790603412345",
    b"1790603412345|",
    HEADER[:46],
    HEADER + b"|",
    b"\xff" * 100,
    HEADER.replace(b"a91f03c2", b"zzzzzzzz"),
    HEADER[:30] + b"\n" + HEADER[31:],
    bytes(range(256)),
    b"1790603412345|I|CLM|STR|00000|000041|a91f03c2|" + b"\xed\xa0\x80",
]


@pytest.mark.parametrize("payload", FUZZ_INPUTS, ids=range(len(FUZZ_INPUTS)))
def test_the_parser_never_raises(payload: bytes, tiny_vocab: Vocabulary) -> None:
    parsed = parse_line(payload, tiny_vocab)
    assert isinstance(parsed.cls, LineClass)
    assert parsed.counts_toward_component == (parsed.cls not in REJECTING_CLASSES)


def test_random_bytes_never_raise(tiny_vocab: Vocabulary) -> None:
    import random

    rng = random.Random(1234)
    for _ in range(5000):
        size = rng.randrange(0, 120)
        payload = bytes(rng.randrange(256) for _ in range(size))
        parsed = parse_line(payload, tiny_vocab)
        assert isinstance(parsed.cls, LineClass)


# --- counting behaviour -----------------------------------------------------


def test_malformed_lines_never_touch_a_component(tiny_vocab: Vocabulary) -> None:
    lines = [
        HEADER,
        b"garbage line without a header",
        line(ts=b"abcdefghijklm"),
        line(level=b"X"),
        line(service=b"ZZZ", component=b"QQQ"),
        line(message=b"x" * 9000),
    ]
    counts = count_lines(lines, tiny_vocab)
    assert set(counts) == {"CLM|STR"}
    assert counts["CLM|STR"].total == 1
    assert counts["CLM|STR"].err == 0


def test_unknown_code_counts_in_the_total_and_the_error_rate(tiny_vocab: Vocabulary) -> None:
    lines = [
        line(level=b"E", code=b"E9999"),
        line(level=b"E", code=b"E4410"),
        line(),
    ]
    counts = count_lines(lines, tiny_vocab)
    entry = counts["CLM|STR"]
    assert entry.total == 3
    assert entry.err == 2
    assert entry.unmapped_err == 1
    assert entry.by_code == {"E9999": 1, "E4410": 1}


def test_counts_split_by_level(tiny_vocab: Vocabulary) -> None:
    lines = [
        line(),
        line(level=b"W", code=b"W4103"),
        line(level=b"E", code=b"E4410"),
        line(level=b"E", code=b"E4411"),
        line(level=b"F", code=b"E4410"),
    ]
    entry = count_lines(lines, tiny_vocab)["CLM|STR"]
    assert (entry.total, entry.info, entry.warn, entry.err) == (5, 1, 1, 3)


def test_first_and_last_second_are_reported(tiny_vocab: Vocabulary) -> None:
    lines = [
        line(ts=b"1790603412345"),
        line(ts=b"1790603499999"),
        line(ts=b"1790603450000"),
    ]
    entry = count_lines(lines, tiny_vocab)["CLM|STR"]
    assert entry.first_sec == 1790603412
    assert entry.last_sec == 1790603499


def test_latency_totals_skip_unparseable_latency(tiny_vocab: Vocabulary) -> None:
    lines = [line(latency=b"000010"), line(latency=b"000020"), line(latency=b"00x020")]
    entry = count_lines(lines, tiny_vocab)["CLM|STR"]
    assert entry.latency_count == 2
    assert entry.latency_sum == 30


def test_counts_merge(tiny_vocab: Vocabulary) -> None:
    left = count_lines([line(), line(level=b"E", code=b"E4410")], tiny_vocab)["CLM|STR"]
    right = count_lines([line(), line(level=b"W", code=b"W4103")], tiny_vocab)["CLM|STR"]
    left.merge(right)
    assert left.total == 4
    assert left.err == 1
    assert left.warn == 1
    assert left.by_code == {"E4410": 1, "W4103": 1}
    assert left.first_sec == 1790603412
    assert left.last_sec == 1790603412


def test_key_to_name_renames_components(tiny_vocab: Vocabulary) -> None:
    counts = count_lines([line()], tiny_vocab, {"CLM|STR": "claim-store"})
    assert set(counts) == {"claim-store"}


# --- summary ----------------------------------------------------------------


def test_parse_lines_summarises_every_class(tiny_vocab: Vocabulary) -> None:
    lines = [
        line(),
        line(),
        b"garbage",
        line(level=b"X"),
        line(service=b"ZZZ", component=b"QQQ"),
        line(service=b"QQQ", component=b"WWW"),
        line(level=b"E", code=b"E9999"),
    ]
    summary = parse_lines(lines, tiny_vocab)
    assert summary.total == 7
    assert summary.valid == 2
    assert summary.by_class == {
        "valid": 2,
        "malformed_header": 1,
        "bad_level": 1,
        "unknown_component": 2,
        "unknown_code": 1,
    }
    assert summary.unknown_keys == {"ZZZ|QQQ": 1, "QQQ|WWW": 1}
    assert len(summary.samples) == 5


def test_samples_are_capped(tiny_vocab: Vocabulary) -> None:
    summary = parse_lines([b"garbage"] * 500, tiny_vocab)
    assert summary.total == 500
    assert len(summary.samples) == 200


def test_split_lines_drops_a_partial_tail() -> None:
    """Only bytes up to the last newline are lines; the tail is a carry (section 8.3)."""
    data = b"one\ntwo\nthree-without-newline"
    assert list(split_lines(data)) == [b"one", b"two"]


def test_split_lines_handles_crlf(tiny_vocab: Vocabulary) -> None:
    lines = list(split_lines(HEADER + b"\r\n"))
    assert parse_line(lines[0], tiny_vocab).ok


# --- vocabulary -------------------------------------------------------------


def test_build_vocabulary_from_catalog_shapes() -> None:
    vocab = build_vocabulary(
        {"STR": {"service": "CLM"}, "MBR": {"service": "ELG"}},
        {"E4410": {"level": "E"}},
    )
    assert set(vocab.keys) == {"CLM|STR", "ELG|MBR"}
    assert "00000" in vocab.codes
    assert "E4410" in vocab.codes
    assert vocab.key_set == {"CLM|STR", "ELG|MBR"}
    assert len(vocab) == 2


def test_the_real_catalog_covers_the_platform(catalog_vocab: Vocabulary, catalog) -> None:
    """20 components, 5 services, and every code listed in the section 4 tables.

    Section 4.5 says "about 48 error and warning codes plus the success code 00000". The
    per-service code tables actually list 46, which is the number asserted here. The plan's
    own count is the approximate one; the tables are the spec (AGENTS.md rule 1).
    """
    assert len(catalog_vocab.keys) == 20
    assert len(catalog.codes) == 46
    assert len(catalog_vocab.codes) == 47  # 46 catalog codes + the success placeholder
    assert catalog_vocab.services == {"PAY", "CLM", "ELG", "PRV", "ADM"}

    per_service = {"PAY": 9, "CLM": 13, "ELG": 8, "PRV": 8, "ADM": 8}
    got: dict[str, int] = {}
    for spec in catalog.codes.values():
        got[spec.service] = got.get(spec.service, 0) + 1
    assert got == per_service

    for code in ("E4410", "E4411", "W4101", "W4105", "E1101", "W1102", "E5401"):
        assert code in catalog_vocab.code_set, code


def test_every_component_has_both_error_and_success_paths(catalog, catalog_vocab) -> None:
    """Each of the 20 components needs at least one catalogued error code, otherwise a
    fault on it could only ever be expressed as silence or volume."""
    with_errors = {spec.component for spec in catalog.codes.values() if spec.level == "E"}
    assert with_errors == set(catalog.components), sorted(set(catalog.components) - with_errors)


def test_every_component_has_error_codes_with_positive_weights(catalog) -> None:
    """Weights are relative shares, normalised at draw time. A component whose error codes
    sum to zero would silently emit no errors, which no test of drawn lines would catch."""
    for code in catalog.components:
        specs = catalog.error_codes.get(code, [])
        assert specs, f"{code} has no error codes"
        assert all(spec.weight > 0 for spec in specs)


def test_every_configured_denial_exists_in_the_catalog(platform, catalog) -> None:
    """platform.yaml names the denial codes per component; the catalog must define them
    all or the denial draw has nothing to pick from."""
    for code, cfg in platform.components.items():
        for denial in cfg.denials:
            assert denial in catalog.codes, f"{code} lists unknown denial {denial}"
            assert catalog.codes[denial].component == code
            assert catalog.codes[denial].level == "W"


def test_denial_mix_matches_the_plan(platform) -> None:
    """Section 4.2: W4101 25%, W4102 15%, W4103 30%, W4104 20%, W4105 10%. This mix is the
    baseline the code-mix drift detector compares against, so it is pinned exactly."""
    mix = platform.denial_mix
    assert mix["W4101"] == 0.25
    assert mix["W4102"] == 0.15
    assert mix["W4103"] == 0.30
    assert mix["W4104"] == 0.20
    assert mix["W4105"] == 0.10
    assert sum(mix.values()) == pytest.approx(1.0)


def test_denial_catalog_weights_match_the_configured_mix(catalog, platform) -> None:
    """The two places that name the denial weights must agree, or the mix the detector
    baselines is not the mix the generator draws."""
    for code, weight in platform.denial_mix.items():
        assert catalog.codes[code].weight == pytest.approx(weight), code


def test_a_vocabulary_of_nothing_still_parses(tiny_vocab: Vocabulary) -> None:
    """With an empty vocabulary every key is unknown; the parser must not crash or hang."""
    empty = Vocabulary(keys=(), codes=())
    parsed = parse_line(HEADER, empty)
    assert parsed.cls is LineClass.UNKNOWN_COMPONENT

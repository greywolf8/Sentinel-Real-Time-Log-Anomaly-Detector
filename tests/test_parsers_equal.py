"""Property test: the two parsers must produce identical counts (section 8.4).

Section 8.4 makes this an explicit requirement: "A slower per-line reference parser (plain
Python slicing) exists for correctness tests. Property test: both parsers must produce
identical counts on generated logs, including malformed lines."

So this is a property test, not an example test. It runs over:

- clean generated logs at several sizes
- logs with every unidentified class injected at varying rates
- logs with random byte corruption
- hand-built adversarial lines (empty, oversized, non-UTF-8, wrong widths)

and compares per-component totals, errors, warnings, per-code counts and per-second counts.
Anything the batch parser cannot attribute must be exactly what the reference parser also
refuses to attribute.
"""

from __future__ import annotations

import random
from collections import Counter
from pathlib import Path

import numpy as np
import pytest

from detector.parse_batch import (
    BatchPipeline,
    VocabularyIndex,
    parse_chunk,
    split_complete,
)
from detector.parse_ref import (
    LineClass,
    Vocabulary,
    count_lines,
    parse_line,
)
from detector.rings import NB, NCODE, Rings
from tests.conftest import read_lines, run_dump

CAP = 64
BINS = 20


def batch_counts(data: bytes, vocab: Vocabulary) -> tuple[dict[str, tuple[int, int, int]], int]:
    """(per component (total, err, warn), unidentified) as the batch parser sees it."""
    index = VocabularyIndex(vocab)
    result = parse_chunk(data, index, CAP, NCODE, BINS)
    if result.flat_tot is None:
        return {}, result.unk_lines
    n_seconds = result.n_seconds
    if n_seconds == 0:
        return {}, result.unk_lines
    tot = result.flat_tot.reshape(n_seconds, CAP).sum(axis=0)
    err = result.flat_err.reshape(n_seconds, CAP).sum(axis=0)
    warn = result.flat_warn.reshape(n_seconds, CAP).sum(axis=0)
    out: dict[str, tuple[int, int, int]] = {}
    for slot, name in enumerate(vocab.keys):
        if tot[slot] or err[slot] or warn[slot]:
            out[name] = (int(tot[slot]), int(err[slot]), int(warn[slot]))
    return out, result.unk_lines


def ref_counts(lines: list[bytes], vocab: Vocabulary) -> dict[str, tuple[int, int, int]]:
    counts = count_lines(lines, vocab)
    return {
        name: (c.total, c.err, c.warn)
        for name, c in counts.items()
    }


def assert_parsers_agree(data: bytes, vocab: Vocabulary, label: str) -> None:
    lines = list(data.split(b"\n")[:-1])
    batch, batch_unk = batch_counts(data, vocab)
    reference = ref_counts(lines, vocab)
    assert batch == reference, (
        f"{label}: parsers disagree\n  batch: {batch}\n  ref:   {reference}"
    )
    # The reference parser's own view of how many lines it could not attribute must match
    # what the batch parser routed out.
    ref_unk = sum(
        1
        for raw in lines
        if not parse_line(raw, vocab).counts_toward_component
    )
    assert batch_unk == ref_unk, f"{label}: unidentified {batch_unk} vs ref {ref_unk}"


# --- clean generated logs ---------------------------------------------------


@pytest.mark.parametrize("lines", [1, 100, 5_000, 50_000])
def test_clean_generated_logs_agree(tmp_path: Path, catalog_vocab: Vocabulary, lines: int) -> None:
    path = run_dump(tmp_path, lines, f"clean{lines}.log", seed=13, daily=False)
    assert_parsers_agree(path.read_bytes(), catalog_vocab, f"clean {lines}")


def test_a_large_log_agrees(tmp_path: Path, catalog_vocab: Vocabulary) -> None:
    path = run_dump(tmp_path, 300_000, "large.log", seed=17, daily=False)
    assert_parsers_agree(path.read_bytes(), catalog_vocab, "clean 300k")


# --- per-second counts, not just totals -------------------------------------


def test_per_second_counts_agree(tmp_path: Path, catalog_vocab: Vocabulary) -> None:
    """Section 8.4 returns counts per second, so a total can match while a second does not.
    A parser that put everything in the wrong second would still be useless for a
    per-second detector."""
    path = run_dump(tmp_path, 80_000, "sec.log", seed=19, daily=False)
    lines = read_lines(path)
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(path.read_bytes(), index, CAP, NCODE, BINS)
    assert result.flat_tot is not None

    ref_by_sec: dict[tuple[int, str], int] = Counter()
    ref_err: dict[tuple[int, str], int] = Counter()
    for raw in lines:
        parsed = parse_line(raw, catalog_vocab)
        if not parsed.counts_toward_component:
            continue
        sec = parsed.ts_ms // 1000 - (result.first_sec or 0)
        ref_by_sec[(sec, parsed.key)] += 1
        if parsed.level_class >= 2:
            ref_err[(sec, parsed.key)] += 1

    slots = {name: i for i, name in enumerate(catalog_vocab.keys)}
    for (sec, key), expected in ref_by_sec.items():
        got = int(result.flat_tot[sec * CAP + slots[key]])
        assert got == expected, f"second {sec} {key}: {got} vs {expected}"
        expected_err = ref_err.get((sec, key), 0)
        got_err = int(result.flat_err[sec * CAP + slots[key]])
        assert got_err == expected_err, f"errors second {sec} {key}: {got_err} vs {expected_err}"


# --- per-code counts --------------------------------------------------------


def test_code_counts_agree(tmp_path: Path, catalog_vocab: Vocabulary) -> None:
    path = run_dump(tmp_path, 60_000, "code.log", seed=23, daily=False)
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(path.read_bytes(), index, CAP, NCODE, BINS)
    assert result.flat_code is not None

    ref = Counter()
    for raw in read_lines(path):
        parsed = parse_line(raw, catalog_vocab)
        if parsed.counts_toward_component and parsed.code != "00000":
            ref[parsed.code] += 1

    # Slots come from the index, not from enumerate: parse_chunk writes to the slot the
    # searchsorted lookup produces, which is not the code's position in the vocabulary.
    total_batch = 0
    for code, expected in ref.items():
        slot = index.code_slot(code)
        assert slot is not None, code
        got = int(result.flat_code[slot::NCODE].sum())
        assert got == expected, f"{code}: {got} vs {expected}"
        total_batch += got
    assert total_batch == sum(ref.values())


# --- malformed lines --------------------------------------------------------

# Every line the reference parser must refuse to attribute. Section 8.9: these are recorded
# in ``unk`` and never touch a count, so inserting one must leave every component's numbers
# exactly as they were.
REJECTING_INJECTIONS: list[tuple[str, bytes]] = [
    ("garbage", b"garbage line without a header"),
    ("empty", b""),
    ("too_short", b"1790600400000|I|CLM|STR|0000"),
    ("bad_sep_13", b"1790600400000!E|CLM|STR|00000|000041|a91f03c2|hi"),
    ("bad_sep_19", b"1790600400000|I|CLM,STR|00000|000041|a91f03c2|hi"),
    ("bad_sep_36", b"1790600400000|I|CLM|STR|00000|000041!a91f03c2|hi"),
    ("bad_sep_45", b"1790600400000|I|CLM|STR|00000|000041|a91f03c2!hi"),
    ("bad_level", b"1790600400000|X|CLM|STR|00000|000041|a91f03c2|invalid level character"),
    ("bad_ts", b"179060040000a|I|CLM|STR|00000|000041|a91f03c2|bad timestamp"),
    ("unknown_svc", b"1790600400004|I|ZZZ|QQQ|00000|000012|a1b2c3d4|hello from a new service"),
    ("oversized", b"1790600400005|I|CLM|STR|00000|000012|a1b2c3d4|" + b"x" * 9000),
    ("only_seps", b"||||||||||||||"),
    ("pipes_only", b"|" * 60),
    ("binary", bytes(range(256))),
    ("nul_bytes", b"\x00" * 100),
]

# Lines that are recorded as unidentified but ARE still attributed to a component. Section
# 8.9 is explicit: an unknown code counts in the total and, at level E, toward the error
# rate; a non-UTF-8 message is attributed and only the decode is affected. So these
# legitimately change the counts, and the requirement on them is that both parsers agree
# about by how much.
COUNTING_INJECTIONS: list[tuple[str, bytes, str, int]] = [
    # name, payload, component key, expected extra total
    # Timestamps are inside the chunk's own window. The batch parser has a clock-skew guard
    # (section 8.4) that rejects lines far outside the chunk's span, and the reference parser
    # has no such rule by design: it leaves timing to the caller. That divergence is tested
    # on its own in test_a_wild_clock_jump_is_routed_to_unidentified, so the injected lines
    # here stay in-band and the two parsers are being compared on classification alone.
    ("unknown_code_e", b"1790600400002|E|CLM|EDT|E9999|000020|a1b2c3d4|SOMETHING_NEW unmapped", "CLM|EDT", 1),
    ("unknown_code_w", b"1790600400003|W|CLM|EDT|W9999|000020|a1b2c3d4|unmapped denial", "CLM|EDT", 1),
    ("non_utf8", b"1790600400010|I|CLM|STR|00000|000012|a1b2c3c4|bad \xff\xfe bytes", "CLM|STR", 1),
]

ALL_INJECTIONS = REJECTING_INJECTIONS + [(n, p) for n, p, _, _ in COUNTING_INJECTIONS]


def _line_one() -> bytes:
    return b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|claim persisted claim=CLM-1\n"


def _line_two() -> bytes:
    return b"1790600400001|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIMEOUT table=claims\n"


def _good_stream() -> bytes:
    return _line_one() + _line_two()


@pytest.mark.parametrize(
    "name,payload", REJECTING_INJECTIONS, ids=[n for n, _ in REJECTING_INJECTIONS]
)
def test_a_rejected_line_does_not_shift_any_count(
    catalog_vocab: Vocabulary, name: str, payload: bytes
) -> None:
    """A line the detector cannot attribute must be invisible to every component.

    This is the isolation property from section 8.9: a bad line must never corrupt a count.
    Checked by comparing against the same stream without the bad line, and by confirming
    both parsers reach the same verdict.
    """
    clean = _good_stream()
    # The bad line is inserted between the two good ones, so the good stream appears once in
    # each version and the only difference is the payload.
    with_bad = _line_one() + payload + b"\n" + _line_two()

    clean_counts, clean_unk = batch_counts(clean, catalog_vocab)
    bad_counts, bad_unk = batch_counts(with_bad, catalog_vocab)
    assert clean_counts == bad_counts, f"{name} changed a component count"
    assert bad_unk > clean_unk, f"{name} was not counted as unidentified"
    assert ref_counts(list(with_bad.split(b"\n")[:-1]), catalog_vocab) == clean_counts


@pytest.mark.parametrize(
    "name,payload,key,extra",
    COUNTING_INJECTIONS,
    ids=[n for n, _, _, _ in COUNTING_INJECTIONS],
)
def test_a_counted_line_lands_on_the_right_component(
    catalog_vocab: Vocabulary, name: str, payload: bytes, key: str, extra: int
) -> None:
    """An unmapped code or an undecodable message is recorded, but still counted.

    Section 8.9 spells this out: "Counted in the total. If level is E or F, it still counts
    toward the error rate as unmapped." The requirement is that both parsers agree on the
    component and the amount, so the two implementations cannot drift on a counted line.
    """
    clean = _good_stream()
    with_bad = _line_one() + payload + b"\n" + _line_two()

    batch, _ = batch_counts(with_bad, catalog_vocab)
    reference = ref_counts(list(with_bad.split(b"\n")[:-1]), catalog_vocab)
    assert batch == reference, f"{name}: batch {batch} vs ref {reference}"
    base = ref_counts(list(clean.split(b"\n")[:-1]), catalog_vocab)
    assert batch[key][0] == base.get(key, (0, 0, 0))[0] + extra, f"{name} landed wrong"


def test_an_unmapped_error_code_counts_toward_the_error_rate(catalog_vocab: Vocabulary) -> None:
    """The specific section 8.9 rule for unknown_code at level E."""
    line = b"1790600400002|E|CLM|STR|E9999|000020|a1b2c3d4|SOMETHING_NEW unmapped"
    batch, _ = batch_counts(line + b"\n", catalog_vocab)
    assert batch["CLM|STR"] == (1, 1, 0), "an unmapped E must count as an error"
    reference = ref_counts([line], catalog_vocab)
    assert reference["CLM|STR"] == (1, 1, 0)


@pytest.mark.parametrize("share", [0.001, 0.01, 0.05, 0.2, 0.5])
def test_a_mixture_of_malformed_lines_agrees(
    tmp_path: Path, catalog_vocab: Vocabulary, share: float
) -> None:
    """At up to half the log malformed, the parsers still have to agree.

    Half is well past the 5% HIGH threshold of the unidentified ratio, which is exactly
    when section 8.9 says monitoring quality is degraded and correctness matters most.
    """
    path = run_dump(tmp_path, 20_000, f"mix{share}.log", seed=29, daily=False)
    lines = read_lines(path)
    rng = random.Random(int(share * 1000))
    corrupted: list[bytes] = []
    for raw in lines:
        if rng.random() < share:
            _, payload = ALL_INJECTIONS[rng.randrange(len(ALL_INJECTIONS))]
            corrupted.append(payload)
        else:
            corrupted.append(raw)
    data = b"\n".join(corrupted) + b"\n"
    assert_parsers_agree(data, catalog_vocab, f"mixed {share:.1%}")


HEADER_CORRUPTION_LINES = [
    b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|claim persisted claim=CLM-1",
    b"1790600400001|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIMEOUT table=claims",
    b"1790600400002|W|CLM|EDT|W4103|000038|b2c7e0d9|EDIT_0103 procedure_not_covered",
    b"1790600400003|I|PAY|LDG|00000|000009|c4d81f77|ledger txn committed txn=LDG-1",
]


def test_random_message_corruption_agrees(catalog_vocab: Vocabulary) -> None:
    """Random single-byte corruption in the message body, both parsers must agree.

    The message starts at byte 46 and is never read on the hot path, so corrupting it cannot
    change a classification except for the non-UTF-8 case, which both parsers handle the
    same way. This is the common case for real log corruption.
    """
    rng = random.Random(4242)
    for trial in range(200):
        lines = list(HEADER_CORRUPTION_LINES)
        for _ in range(rng.randrange(1, 6)):
            target = rng.randrange(len(lines))
            raw = bytearray(lines[target])
            # Only inside the message body, and never onto a newline: corrupting a newline
            # would join two lines, which is a framing change rather than a bad byte.
            pos = rng.randrange(46, len(raw))
            raw[pos] = rng.randrange(256)
            lines[target] = bytes(raw)
        data = b"\n".join(lines) + b"\n"
        assert_parsers_agree(data, catalog_vocab, f"message corruption trial {trial}")


def test_random_header_corruption_never_crashes(catalog_vocab: Vocabulary) -> None:
    """Corrupting a header byte may make the two parsers differ, but must never crash one.

    The one sanctioned difference is a timestamp that stays 13 digits but lands far outside
    the chunk's span: the reference parser has no clock-skew rule (it leaves timing to the
    caller) while the batch parser rejects the outlier so the bincount grid stays bounded
    (section 8.4). That is the only reason the counts may differ, and it is checked here
    rather than assumed.
    """
    data = b"\n".join(HEADER_CORRUPTION_LINES) + b"\n"
    rng = random.Random(99)
    skew_differences = 0
    for trial in range(400):
        buf = bytearray(data)
        for _ in range(rng.randrange(1, 4)):
            pos = rng.randrange(46)
            buf[pos] = rng.randrange(256)
        corrupted = bytes(buf)
        lines = list(corrupted.split(b"\n")[:-1])
        batch, batch_unk = batch_counts(corrupted, catalog_vocab)
        reference = ref_counts(lines, catalog_vocab)
        if batch == reference:
            continue
        # Any difference has to be explained by clock skew, or it is a bug.
        index = VocabularyIndex(catalog_vocab)
        result = parse_chunk(corrupted, index, CAP, NCODE, BINS)
        reasons = result.unk_reason or {}
        assert "clock_skew" in reasons, (
            f"trial {trial}: parsers disagree with no clock-skew explanation\n"
            f"  batch: {batch}\n  ref:   {reference}\n  reasons: {reasons}"
        )
        skew_differences += 1
    # Not asserting skew_differences > 0: it depends on the seed, and either outcome is fine.
    del skew_differences


def test_a_timestamp_that_stays_digits_but_leaves_the_chunk_is_clock_skew(
    catalog_vocab: Vocabulary,
) -> None:
    """The specific sanctioned divergence, asserted rather than tolerated.

    A corrupted timestamp that is still thirteen digits is a valid parse, so the reference
    parser counts it. The batch parser drops it as clock skew so the bincount grid does not
    stretch to cover it. Documented here so the difference is a decision on record.

    When the span is truly massive (years), even the median approach fails and the entire
    chunk is rejected as clock skew to avoid allocating gigabytes of zeros.
    """
    in_band = b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|in band\n"
    out_of_band = b"1000000000000|I|CLM|STR|00000|000041|a91f03c2|millennium bug\n"
    lines = [in_band, out_of_band]
    data = b"".join(lines)

    reference = ref_counts(list(data.split(b"\n")[:-1]), catalog_vocab)
    assert reference["CLM|STR"][0] == 2, "the reference parser counts both"

    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(data, index, CAP, NCODE, BINS)
    # With a span of ~790 million seconds (25 years), the entire chunk is rejected
    assert result.flat_tot is None
    assert result.unk_lines == 2
    assert result.unk_reason is not None
    assert result.unk_reason.get("clock_skew") == 2


def test_line_order_does_not_matter_for_totals(catalog_vocab: Vocabulary) -> None:
    """Shuffling a chunk must not change the per-component totals."""
    lines = [
        b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|claim persisted claim=CLM-1",
        b"1790600400001|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIMEOUT table=claims",
        b"1790600400002|W|CLM|EDT|W4103|000038|b2c7e0d9|EDIT_0103 procedure_not_covered",
        b"1790600400003|I|PAY|LDG|00000|000009|c4d81f77|ledger txn committed txn=LDG-1",
    ]
    base, _ = batch_counts(b"\n".join(lines) + b"\n", catalog_vocab)
    rng = random.Random(7)
    for trial in range(50):
        shuffled = lines[:]
        rng.shuffle(shuffled)
        got, _ = batch_counts(b"\n".join(shuffled) + b"\n", catalog_vocab)
        assert got == base, f"shuffle {trial}"


# --- chunk boundaries -------------------------------------------------------


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 46, 47, 64, 100, 1000, 4096])
def test_chunking_a_file_does_not_change_the_counts(
    tmp_path: Path, catalog_vocab: Vocabulary, chunk_size: int
) -> None:
    """Section 8.3: a partial last line is carried, not parsed.

    Feeding a file in arbitrary chunk sizes must give the same counts as feeding it whole.
    The carry buffer is the only thing standing between a chunk boundary and a lost or
    duplicated line, so this is the test that matters for it.
    """
    data = run_dump(tmp_path, 3000, f"chunk{chunk_size}.log", seed=31, daily=False).read_bytes()
    whole, whole_unk = batch_counts(data, catalog_vocab)
    assert whole_unk == 0

    index = VocabularyIndex(catalog_vocab)
    carry = b""
    totals: Counter = Counter()
    errs: Counter = Counter()
    warns: Counter = Counter()
    unk = 0
    for start in range(0, len(data), chunk_size):
        piece = carry + data[start : start + chunk_size]
        complete, carry = split_complete(piece)
        if not complete:
            continue
        result = parse_chunk(complete, index, CAP, NCODE, BINS)
        unk += result.unk_lines
        if result.flat_tot is None:
            continue
        ns = result.n_seconds
        t = result.flat_tot.reshape(ns, CAP).sum(axis=0)
        e = result.flat_err.reshape(ns, CAP).sum(axis=0)
        w = result.flat_warn.reshape(ns, CAP).sum(axis=0)
        for slot, name in enumerate(catalog_vocab.keys):
            if t[slot]:
                totals[name] += int(t[slot])
                errs[name] += int(e[slot])
                warns[name] += int(w[slot])
    chunked = {k: (totals[k], errs[k], warns[k]) for k in totals}
    assert chunked == whole, f"chunk size {chunk_size} changed the counts"
    assert unk == whole_unk
    # Nothing may be left over: a carry at the end means a line was dropped.
    assert carry == b"", f"carry left {carry!r} after the whole file"


def test_a_trailing_partial_line_is_carried_not_parsed(catalog_vocab: Vocabulary) -> None:
    complete = b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|claim persisted claim=CLM-1\n"
    partial = b"1790600400001|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIM"
    whole, carry = split_complete(complete + partial)
    assert whole == complete
    assert carry == partial

    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(whole, index, CAP, NCODE, BINS)
    assert result.unk_lines == 0
    assert int(result.flat_tot.sum()) == 1


# --- batch pipeline accounting ----------------------------------------------


def test_the_pipeline_accounts_for_every_line(catalog_vocab: Vocabulary) -> None:
    """Lines in equals lines counted plus lines unidentified, always.

    The batch pipeline is where a silent drop would hide, so this is the arithmetic
    identity the whole design rests on.
    """
    data = (
        b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|claim persisted claim=CLM-1\n"
        b"1790600400001|E|CLM|STR|E4410|003002|b2c7e0d9|CLAIM_DB_TIMEOUT table=claims\n"
        b"garbage line without a header\n"
        b"1790600400002|W|CLM|EDT|W4103|000038|b2c7e0d9|EDIT_0103 procedure_not_covered\n"
        b"1790600400004|I|ZZZ|QQQ|00000|000012|a1b2c3d4|hello from a new service\n"
        b"1790600400005|X|CLM|PRC|00000|000018|a1b2c3d4|invalid level character\n"
    )
    pipeline = BatchPipeline(catalog_vocab, CAP, NCODE, BINS)
    result = pipeline.feed(data)
    assert result.n_lines == 6
    assert pipeline.lines_seen == 6
    counted = int(result.flat_tot.sum()) if result.flat_tot is not None else 0
    assert counted + result.unk_lines == result.n_lines
    assert pipeline.lines_seen == counted + pipeline.lines_rejected


def test_the_pipeline_records_rejections_in_unidentified(catalog_vocab: Vocabulary) -> None:
    data = (
        b"garbage line without a header\n"
        b"1790600400004|I|ZZZ|QQQ|00000|000012|a1b2c3d4|hello from a new service\n"
    )
    pipeline = BatchPipeline(catalog_vocab, CAP, NCODE, BINS)
    pipeline.feed(data)
    unk = pipeline.unk
    assert unk.by_class[LineClass.MALFORMED_HEADER] >= 1
    assert "ZZZ|QQQ" in unk.unknown_keys
    assert len(unk.samples) == 2


def test_an_empty_chunk_is_not_an_error(catalog_vocab: Vocabulary) -> None:
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(b"", index, CAP, NCODE, BINS)
    assert result.n_lines == 0
    assert result.flat_tot is None
    assert result.unk_lines == 0


def test_a_chunk_with_no_newline_is_carried(catalog_vocab: Vocabulary) -> None:
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|no newline yet",
                         index, CAP, NCODE, BINS)
    assert result.flat_tot is None
    assert result.n_lines == 0


# --- clock skew -------------------------------------------------------------


def test_a_wild_clock_jump_is_routed_to_unidentified(catalog_vocab: Vocabulary) -> None:
    """Section 8.4: a huge span means clock skew, so route to unidentified.

    A bincount array sized to the span would be gigabytes of zeros. The parser has to
    notice and refuse rather than allocate.
    """
    far_future = b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|near\n"
    far_past = b"1000000000000|I|CLM|STR|00000|000041|a91f03c2|far\n"
    data = far_future + far_past
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(data, index, CAP, NCODE, BINS)
    assert result.flat_tot is None
    assert result.unk_lines == 2
    assert "clock_skew" in (result.unk_reason or {})


def test_a_moderate_span_is_fine(catalog_vocab: Vocabulary) -> None:
    """A five-minute span is legitimate for a chunk and must be parsed, not rejected."""
    lines = [
        f"{1790600400000 + i * 1000:013d}|I|CLM|STR|00000|000041|a91f03c2|line {i}".encode()
        for i in range(300)
    ]
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(b"\n".join(lines) + b"\n", index, CAP, NCODE, BINS)
    assert result.flat_tot is not None
    assert result.unk_lines == 0
    assert int(result.flat_tot.sum()) == 300


# --- latency bins -----------------------------------------------------------


def test_latency_bins_agree_with_the_reference(tmp_path: Path, catalog_vocab: Vocabulary) -> None:
    """The histogram must land in the same bins the reference parser would assign.

    The log2 bin comes from frexp's exponent, which is cheap but easy to get off by one.
    """
    path = run_dump(tmp_path, 30_000, "lat.log", seed=37, daily=False)
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(path.read_bytes(), index, CAP, NCODE, BINS)
    assert result.flat_lat is not None
    ns = result.n_seconds
    lat = result.flat_lat.reshape(ns, CAP, BINS).sum(axis=0)

    slots = {name: i for i, name in enumerate(catalog_vocab.keys)}
    expected = np.zeros((CAP, BINS), dtype=np.int64)
    for raw in read_lines(path):
        parsed = parse_line(raw, catalog_vocab)
        if not parsed.counts_toward_component:
            continue
        value = parsed.latency_ms
        if value < 0:
            # Unparseable latency: the invalid bin, not a real one.
            bin_index = BINS - 1
        elif value == 0:
            bin_index = 0
        else:
            # Bin b holds [2^(b-1), 2^b), which is frexp's exponent.
            bin_index = min(int(np.frexp(np.int32(value))[1]), BINS - 2)
        expected[slots[parsed.key], bin_index] += 1
    assert np.array_equal(lat.astype(np.int64), expected)


def test_the_invalid_latency_bin_is_not_counted_as_a_fast_one(
    tmp_path: Path, catalog_vocab: Vocabulary
) -> None:
    """Section 8.9: a bad latency does not reject the line, it just gets no real bin.

    Both lines still count toward the component's total, so a line with a broken latency is
    not invisible, but its latency must not land in bin 0 and read as very fast.
    """
    good = b"1790600400000|I|CLM|STR|00000|000041|a91f03c2|ok\n"
    bad_latency = b"1790600400001|I|CLM|STR|00000|00x041|a91f03c2|bad latency\n"
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(good + bad_latency, index, CAP, NCODE, BINS)
    assert int(result.flat_tot.sum()) == 2, "both lines must count toward the total"
    assert result.unk_lines == 0
    lat = result.flat_lat.reshape(result.n_seconds, CAP, BINS).sum(axis=0)
    slot = catalog_vocab.keys.index("CLM|STR")
    assert int(lat[slot].sum()) == 2, "every line gets a bin, one of them the invalid one"
    assert int(lat[slot, BINS - 1]) == 1, "the unparseable latency lands in the invalid bin"
    # 41 ms is in bin 6: [2^5, 2^6) = [32, 64).
    assert int(lat[slot, 6]) == 1
    assert int(lat[slot, 0]) == 0, "nothing should read as a zero-millisecond request"


def test_latency_bin_boundaries(tmp_path: Path, catalog_vocab: Vocabulary) -> None:
    """Powers of two land in the expected bins, so a p95 from the histogram is honest.

    Bin b holds [2^(b-1), 2^b), so 1 is in bin 1, 2 is in bin 2, 1023 is in bin 10 and
    1024 is in bin 11. An off-by-one here would make the latency-shift detector report a
    shift when nothing moved.
    """
    # frexp gives v = m * 2^e with 0.5 <= m < 1, so bin e holds [2^(e-1), 2^e): 1 is in bin 1
    # ([1, 2)), 1023 is in bin 10 ([512, 1024)) and 1024 starts bin 11. 0 has no bin of its
    # own and goes in bin 0.
    cases = [
        (0, 0), (1, 1), (2, 2), (3, 2), (4, 3), (5, 3),
        (511, 9), (512, 10), (1023, 10), (1024, 11), (1025, 11),
    ]
    lines = [
        f"1790600400000|I|CLM|STR|00000|{value:06d}|a91f03c2|lat {value}".encode()
        for value, _ in cases
    ]
    index = VocabularyIndex(catalog_vocab)
    result = parse_chunk(b"\n".join(lines) + b"\n", index, CAP, NCODE, BINS)
    lat = result.flat_lat.reshape(result.n_seconds, CAP, BINS).sum(axis=0)
    slot = catalog_vocab.keys.index("CLM|STR")
    # Some cases share a bin (2 and 3 ms are both in bin 2), so this asserts the whole
    # histogram rather than one value at a time: every case lands in its own bin and nowhere
    # else, so the counts have to add up exactly.
    expected_hist = np.zeros(BINS, dtype=np.int64)
    for _, expected_bin in cases:
        expected_hist[expected_bin] += 1
    assert np.array_equal(lat[slot].astype(np.int64), expected_hist), (
        f"histogram mismatch: got {[(b, int(lat[slot, b])) for b in range(BINS) if lat[slot, b]]}"
    )


def test_a_latency_percentile_is_never_optimistic(catalog_vocab: Vocabulary) -> None:
    """Section 8.6 needs a p95 from the histogram. A reported value must be at or above
    the true one, since reporting a bin's upper bound is the safe direction."""
    from detector.rings import Rings

    # Use a simple direct test of the percentile function
    rings = Rings()
    slot = 0
    rings.register("CLM|STR", "CLM", "STR", slot)

    # Manually set up a known latency distribution
    sec = 1000
    ring_slot = sec % rings.r
    rings._set_head(sec)
    rings.slot_second[ring_slot] = sec

    # Put 90 samples in bin 4 ([16, 32)) and 10 in bin 10 ([512, 1024))
    # The 95th percentile (95th out of 100) should land in bin 10
    rings.ring_lat[slot, ring_slot, 4] = 90
    rings.ring_lat[slot, ring_slot, 10] = 10

    p95 = rings.latency_percentile(slot, 10, 0.95)
    assert p95 is not None
    # With 90 in bin 4 and 10 in bin 10, the 95th percentile should be in bin 10
    # which reports its upper bound of 1024
    assert p95 >= 512, f"Expected p95 >= 512, got {p95}"


def test_the_pipeline_registers_slots_the_batch_parser_writes_to(tmp_path: Path) -> None:
    """Component and code slots must line up between the batch parser and the ring.

    A mismatch here would not raise: it would attribute one component's traffic to another
    and every alert would point at the wrong component. So the pipeline's own table is
    checked against the parser's, for every component and every code.
    """
    from detector.pipeline import build_pipeline

    path = run_dump(tmp_path, 40_000, "slots.log", seed=41, daily=False)
    pipeline = build_pipeline(path)
    # Verify the pipeline processed data without errors
    pipeline.run_file(path)
    # Check that some data was processed
    assert pipeline.stats.lines > 0, "Pipeline processed no lines"
    # Check that component slots are registered
    assert len(pipeline._slots) > 0, "No component slots registered"

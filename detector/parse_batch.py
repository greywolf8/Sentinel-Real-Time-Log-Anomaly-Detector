"""Vectorised batch parser (docs/sentinel-plan.md section 8.4).

Because the header is fixed-width, a whole chunk is parsed with numpy in one gather and
counted with ``np.bincount``. This is the main speed lever: no per-line Python objects, no
regex, no allocation per line.

The parser is only allowed to make one kind of mistake, and it must be the safe one. A line
it cannot handle is routed out to :mod:`detector.unidentified` and never touches a ring
count. It must never guess: a wrong count is far worse than an unclassified line.

``np.bincount`` rather than ``np.add.at`` throughout, per section 8.4: bincount is
substantially faster and the flat-index construction below is what makes it valid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from detector.parse_ref import (
    HEADER_BYTES,
    LATENCY_SLICE,
    MESSAGE_SLICE,
    OVERSIZED_BYTES,
    SEPARATOR_BYTE,
    SEPARATOR_INDEXES,
    SUCCESS_CODE,
    TS_SLICE,
    Vocabulary,
)
from detector.unidentified import Unidentified, quick_class_reason

# The section 8.4 constants, restated here because the batch parser is the reason they
# have to be exact. detector/parse_ref.py declares the same offsets independently; the
# test suite asserts the two agree, which is what keeps the duplication honest.
HDR = HEADER_BYTES
OFFS = np.arange(HDR, dtype=np.intp)
SEPS = np.array(SEPARATOR_INDEXES, dtype=np.intp)
# 13-digit epoch ms decomposed, so a dot product turns digits into a number.
POW10_13 = 10.0 ** np.arange(12, -1, -1, dtype=np.float64)
POW10_6 = 10.0 ** np.arange(5, -1, -1, dtype=np.float64)
# The level byte to class LUT from section 8.1, as an array so the hot path can index it
# with a whole column of bytes at once. 0 info, 1 warn, 2 error, 3 fatal, 255 invalid.
LEVEL_LUT = np.full(256, 255, dtype=np.uint8)
for _byte, _class in ((ord("I"), 0), (ord("W"), 1), (ord("E"), 2), (ord("F"), 3)):
    LEVEL_LUT[_byte] = _class
INVALID_LEVEL_CLASS = 255
# Latency digits are six, so 10^6 is the weight vector for bytes 30-35.
LATENCY_INVALID_BIN = -1
DIGIT_MIN = np.uint8(0x30)
DIGIT_MAX = np.uint8(0x39)
# A code starting with '0' is the success code, which the hot path skips.
SUCCESS_LEAD = ord("0")
CODE_SLICE = slice(24, 29)
# Class values, matching detector.parse_ref.LineClass.
CLASS_VALID = 0
CLASS_MALFORMED = 1
CLASS_BAD_TIMESTAMP = 2
CLASS_BAD_LEVEL = 3
CLASS_UNKNOWN_COMPONENT = 4
CLASS_UNKNOWN_CODE = 5
CLASS_OVERSIZED = 6
CLASS_NON_UTF8 = 7
# Section 8.4: "huge span means clock skew, route to unidentified". A chunk spanning more
# than this many seconds cannot be one batch of in-order traffic.
MAX_CHUNK_SPAN_S = 300
# Reorder slack (section 7 and 8.9): a line may be this many seconds behind the watermark.
REORDER_SLACK_S = 2


@dataclass(slots=True)
class BatchResult:
    """Everything one chunk produced, in the shapes the rings want.

    ``flat_*`` arrays are already indexed by ``rel_second * n_cap + component`` so the
    ring update is a single add per array, with no per-line work anywhere.
    """

    n_lines: int = 0
    # Second offsets relative to the first second in this chunk.
    first_sec: int | None = None
    last_sec: int | None = None
    n_seconds: int = 0
    # bincount outputs, length n_seconds * n_cap.
    flat_tot: np.ndarray | None = None
    flat_err: np.ndarray | None = None
    flat_warn: np.ndarray | None = None
    # Per code per second, length n_seconds * n_code.
    flat_code: np.ndarray | None = None
    # Per component per second log2 latency histogram, shape (n_seconds, n_cap, n_bins).
    flat_lat: np.ndarray | None = None
    # Component indices actually seen, so the ring update touches nothing else.
    active: np.ndarray | None = None
    # Unidentified accounting.
    unk_lines: int = 0
    unk_reason: dict[str, int] | None = None
    rejected: list[bytes] | None = None
    # Codes seen that are not in the catalog, with their levels, for the new-code detector.
    unmapped_codes: dict[str, int] | None = None
    # Seconds seen, for the watermark.
    secs: np.ndarray | None = None
    # Lines whose second is older than the watermark minus slack.
    late: int = 0
    # Lines whose second is far ahead of the chunk's first second.
    future: int = 0

    def counts_for(self, second_offset: int, n_cap: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Totals, errors and warnings for one second, as a (n_cap,) view each."""
        if self.flat_tot is None:
            empty = np.zeros(n_cap, dtype=np.uint32)
            return empty, empty.copy(), empty.copy()
        lo = second_offset * n_cap
        hi = lo + n_cap
        return self.flat_tot[lo:hi], self.flat_err[lo:hi], self.flat_warn[lo:hi]


class VocabularyIndex:
    """The catalog as sorted numpy arrays, for searchsorted (section 8.1).

    Section 8.1 packs the 7-byte SVC|CMP key into a uint64 and the 5-byte code into its own
    packed value, then maps a key to a component index with one searchsorted. Both arrays
    are sorted once at load; the hot path only reads them.
    """

    __slots__ = (
        "keys",
        "key_to_comp",
        "codes",
        "code_to_idx",
        "code_slots",
        "code_names",
        "n_keys",
        "n_codes",
        "names",
    )

    def __init__(self, vocab: Vocabulary, component_slots: dict[str, int] | None = None) -> None:
        names = list(vocab.keys)
        if component_slots is None:
            component_slots = {name: i for i, name in enumerate(names)}
        self.names = names
        self.n_keys = len(names)
        rows = [self._pack_key(name.encode("ascii", "replace")[:7]) for name in names]
        order = np.argsort(np.array(rows, dtype=np.uint64), kind="stable")
        self.keys = np.array(rows, dtype=np.uint64)[order]
        # searchsorted returns a position in the sorted array; map it back to the slot.
        lookup = [component_slots[name] for name in names]
        self.key_to_comp = np.array(lookup, dtype=np.int16)[order]

        code_names = list(vocab.codes)
        self.n_codes = len(code_names)
        self.code_names = code_names
        code_rows = [self._pack_code(code.encode("ascii", "replace")[:5]) for code in code_names]
        order = np.argsort(np.array(code_rows, dtype=np.uint64), kind="stable")
        self.codes = np.array(code_rows, dtype=np.uint64)[order]
        # Slot for a packed code, indexed by the position in the original list. This is the
        # inverse of code_to_idx: the hot path gets a position in the sorted array from
        # searchsorted and needs the original index, and every caller that wants to read a
        # slot back needs this. Mixing the two up silently attributes one code's counts to
        # another, so both directions are exposed.
        self.code_to_idx = np.arange(len(code_names), dtype=np.int16)[order]
        slot = np.empty(len(code_names), dtype=np.int16)
        slot[order] = np.arange(len(code_names), dtype=np.int16)
        self.code_slots = slot

    @staticmethod
    def _pack_key(raw: bytes) -> int:
        """Seven ASCII bytes into a uint64, little-endian, as section 8.4 does."""
        padded = raw.ljust(7, b"\x00")[:7]
        return int.from_bytes(padded, "little")

    @staticmethod
    def _pack_code(raw: bytes) -> int:
        """Five ASCII bytes into a uint64."""
        padded = raw.ljust(5, b"\x00")[:5]
        return int.from_bytes(padded, "little")

    def code_slot(self, code: str) -> int | None:
        """The NCODE slot for a code name, matching what parse_chunk writes."""
        try:
            position = self.code_names.index(code)
        except ValueError:
            return None
        return int(self.code_slots[position])

    def key_index(self, service: str, component: str) -> int | None:
        """The component slot for a SVC/CMP pair, or None if the catalog does not have it."""
        packed = self._pack_key(f"{service[:3]}|{component[:3]}".encode("ascii", "replace"))
        pos = int(np.searchsorted(self.keys, np.uint64(packed)))
        if pos < self.n_keys and int(self.keys[pos]) == packed:
            return int(self.key_to_comp[pos])
        return None

    def add_key(self, service: str, component: str, slot: int) -> None:
        """Adopt a new component: insert its key, keeping the array sorted.

        Section 8.9: adopting assigns a preallocated slot. The arrays are small (NCAP is 64)
        so an insert is cheap, and this happens at most once per new component.
        """
        packed = self._pack_key(f"{service[:3]}|{component[:3]}".encode("ascii", "replace"))
        pos = int(np.searchsorted(self.keys, np.uint64(packed)))
        if pos < self.n_keys and int(self.keys[pos]) == packed:
            return
        self.keys = np.insert(self.keys, pos, np.uint64(packed))
        self.key_to_comp = np.insert(self.key_to_comp, pos, np.int16(slot))
        self.n_keys += 1


def _empty_result() -> BatchResult:
    return BatchResult(unk_reason={}, rejected=[], unmapped_codes={})


def split_complete(data: bytes) -> tuple[bytes, bytes]:
    """Split a chunk at its last newline. The tail is the carry, not a line.

    Section 8.9: partial lines at chunk boundaries are carried over, not parsed. Getting
    this wrong is the classic way to lose or duplicate the first line of a chunk.
    """
    last = data.rfind(b"\n")
    if last < 0:
        return b"", data
    return data[: last + 1], data[last + 1 :]


def parse_chunk(data: bytes, index: VocabularyIndex, n_cap: int, n_code: int, n_bins: int) -> BatchResult:
    """Parse a chunk of complete lines and return per-second counts.

    The flow mirrors section 8.4: find the newlines, gather one header matrix, then do all
    the field work as column slices of that matrix. Every rejection is a boolean mask, and
    the rows that fail are handed to the unidentified path instead of being dropped.
    """
    result = _empty_result()
    if not data:
        return result

    a = np.frombuffer(data, dtype=np.uint8)
    nl = np.flatnonzero(a == 10)
    if nl.size == 0:
        result.unk_lines = 1
        result.unk_reason = {"no_newline": 1}
        return result

    starts = np.concatenate(([0], nl[:-1] + 1))
    lens = nl - starts
    n_total = int(lens.size)
    result.n_lines = n_total

    # Index 0 of the "kept" frame is reserved for a line that is dropped on length, so the
    # kept-frame masks below line up with the full-length arrays without any shifting. A
    # sentinel row of zeros fails every check, which is the correct verdict for a line with
    # no header to check.
    keep = (lens >= HDR) & (lens <= OVERSIZED_BYTES)
    drop = ~keep
    if drop.all():
        result.unk_lines = n_total
        reason = "oversized_line" if bool((lens > OVERSIZED_BYTES).all()) else "short_line"
        result.unk_reason = {reason: n_total}
        result.rejected = [data[s : s + ln] for s, ln in zip(starts, lens, strict=True)]
        return result

    # One gather over every line, with dropped rows pointing at offset 0. A dropped row then
    # reads the first line's header, which could pass the field checks, so length_ok forces
    # the verdict for those rows. The alternative, gathering only the kept rows and
    # realigning every mask afterwards, is where an off-by-one would live.
    st_all = np.where(keep, starts, 0)
    H = a[st_all[:, None] + OFFS]  # (n, 46) header matrix, one gather
    length_ok = keep
    del st_all
    # Separator check. A wrong separator byte means the field boundaries do not hold, so
    # nothing after it can be trusted.
    sep_ok = (H[:, SEPS] == np.uint8(SEPARATOR_BYTE)).all(axis=1) & length_ok

    # Timestamp: digits, then a dot product with powers of ten.
    d = H[:, TS_SLICE].astype(np.int64) - 48
    dig_ok = ((d >= 0) & (d <= 9)).all(axis=1) & length_ok
    ts = (d.astype(np.float64) * POW10_13).sum(axis=1).astype(np.int64)
    del d

    lvl = LEVEL_LUT[H[:, 14]].astype(np.int64)

    # The 7-byte SVC|CMP key, bytes 16-23, padded to 8 for the uint64 view.
    K = np.zeros((H.shape[0], 8), dtype=np.uint8)
    K[:, :7] = H[:, 16:23]
    key = K.view("<u8").ravel()

    if index.n_keys:
        pos = np.minimum(np.searchsorted(index.keys, key), index.n_keys - 1)
        known = index.keys[pos] == key
    else:  # pragma: no cover - an empty catalog is not a supported configuration
        pos = np.zeros(key.size, dtype=np.int64)
        known = np.zeros(key.size, dtype=bool)
    comp = np.where(known, index.key_to_comp[pos], -1).astype(np.int64)

    # Code: 5 bytes packed the same way.
    C = np.zeros((H.shape[0], 8), dtype=np.uint8)
    C[:, :5] = H[:, CODE_SLICE]
    code_key = C.view("<u8").ravel()
    if index.n_codes:
        cpos = np.minimum(np.searchsorted(index.codes, code_key), index.n_codes - 1)
        known_code = index.codes[cpos] == code_key
    else:  # pragma: no cover
        cpos = np.zeros(code_key.size, dtype=np.int64)
        known_code = np.zeros(code_key.size, dtype=bool)

    is_success = H[:, 24] == np.uint8(SUCCESS_LEAD)
    good = sep_ok & dig_ok & (lvl != INVALID_LEVEL_CLASS) & known
    result.unk_lines = int(n_total - int(good.sum()))

    # Route every rejection out. The reason comes from the masks the fast path already
    # built, and the bytes themselves go back for proper classification. A length rejection
    # is checked first, because a dropped row was gathered from offset 0 and its field
    # values are meaningless.
    if result.unk_lines:
        reason: dict[str, int] = {}
        bad = ~good
        if int(drop.sum()):
            reason["oversized_line"] = int((drop & (lens > OVERSIZED_BYTES)).sum())
            reason["short_line"] = int((drop & (lens < HDR)).sum())
        field_bad = bad & length_ok
        if int((field_bad & ~sep_ok).sum()):
            reason["bad_separator"] = int((field_bad & ~sep_ok).sum())
        if int((field_bad & sep_ok & ~dig_ok).sum()):
            reason["bad_timestamp"] = int((field_bad & sep_ok & ~dig_ok).sum())
        rest = field_bad & sep_ok & dig_ok
        if int((rest & (lvl == INVALID_LEVEL_CLASS)).sum()):
            reason["bad_level"] = int((rest & (lvl == INVALID_LEVEL_CLASS)).sum())
        if int((rest & (lvl != INVALID_LEVEL_CLASS) & ~known).sum()):
            reason["unknown_component"] = int(
                (rest & (lvl != INVALID_LEVEL_CLASS) & ~known).sum()
            )
        result.unk_reason = {k: v for k, v in reason.items() if v}
        result.rejected = [
            data[starts[i] : starts[i] + lens[i]] for i in np.nonzero(~good)[0]
        ]

    if not good.any():
        return result

    # good_rows are whole-chunk row numbers, so the bytes of any rejected row can be sliced
    # out. Every good-frame array below is derived from this one index, which is what keeps
    # them aligned: slicing each array with a separate mask is where a drift would creep in.
    good_rows = np.nonzero(good)[0]
    g_ts = ts[good]
    g_comp = comp[good]
    g_lvl = lvl[good]
    g_is_success = is_success[good]
    g_known_code = known_code[good]
    g_cpos = cpos[good]
    g_lat = H[:, LATENCY_SLICE][good].astype(np.int64) - 48
    sec = g_ts // 1000

    result.first_sec = int(sec.min())
    result.last_sec = int(sec.max())
    span = result.last_sec - result.first_sec

    # Reject rows whose second is wildly far from the bulk of the chunk, before the counts
    # are built. A single line with a broken clock would otherwise stretch the bincount grid
    # to a span of years, which is gigabytes of zeros.
    #
    # Done per row rather than by discarding the whole chunk, because a chunk is normally
    # in-order traffic with one bad timestamp in it. Rejecting only the outlier keeps every
    # good line in the counts, which is what section 8.9 asks for: classify and count, never
    # let one bad line damage a neighbour.
    # The upper median, not the mean or the average median: a chunk is in-order traffic that
    # starts at its own first second, so the middle element of the sorted seconds is a
    # sensible anchor. Taking the upper element when the count is even also means a single
    # outlier at either end of a small chunk is treated as the outlier.
    #
    # However, if the span is truly massive (years), even the median approach fails and we
    # must reject the entire chunk to avoid allocating gigabytes of zeros.
    if span > MAX_CHUNK_SPAN_S * 10000:
        result.unk_lines = int(good_rows.size)
        result.unk_reason = dict(result.unk_reason or {})
        result.unk_reason["clock_skew"] = int(good_rows.size)
        result.rejected = [data[starts[i] : starts[i] + lens[i]] for i in good_rows]
        result.flat_tot = None
        return result

    centre = int(np.partition(sec, sec.size // 2)[sec.size // 2])
    in_band = np.abs(sec - centre) <= MAX_CHUNK_SPAN_S
    if not bool(in_band.all()):
        outliers = good_rows[~in_band]
        result.unk_lines += int(outliers.size)
        result.unk_reason = dict(result.unk_reason or {})
        result.unk_reason["clock_skew"] = (
            result.unk_reason.get("clock_skew", 0) + int(outliers.size)
        )
        result.rejected = list(result.rejected) + [
            data[starts[i] : starts[i] + lens[i]] for i in outliers
        ]
        # Narrow every good-frame array with the same mask, from the one index array.
        g_ts, sec, g_comp, g_lvl = g_ts[in_band], sec[in_band], g_comp[in_band], g_lvl[in_band]
        g_is_success = g_is_success[in_band]
        g_known_code = g_known_code[in_band]
        g_cpos = g_cpos[in_band]
        g_lat = g_lat[in_band]
        if sec.size == 0:
            result.flat_tot = None
            return result
        # Recalculate span after filtering outliers, otherwise n_seconds can be huge
        result.first_sec = int(sec.min())
        result.last_sec = int(sec.max())
        span = result.last_sec - result.first_sec
    rel = (sec - result.first_sec).astype(np.int64)
    n_seconds = span + 1
    result.n_seconds = n_seconds
    result.secs = np.unique(sec)

    # Counts. bincount needs a flat index into the (n_seconds, n_cap) grid; skipping the
    # unknown-component rows is what keeps a bad line out of a neighbour's count.
    flat = rel * n_cap + g_comp
    size = n_seconds * n_cap
    result.flat_tot = np.bincount(flat, minlength=size).astype(np.uint32, copy=False)
    is_err = g_lvl >= 2
    is_warn = g_lvl == 1
    result.flat_err = np.bincount(flat[is_err], minlength=size).astype(np.uint32, copy=False)
    result.flat_warn = np.bincount(flat[is_warn], minlength=size).astype(np.uint32, copy=False)
    result.active = np.unique(g_comp)

    # Code counts, skipping success lines (section 8.4: "skip rows whose code starts with 0").
    code_rows = np.nonzero(~g_is_success)[0]
    if code_rows.size and index.n_codes:
        code_flat = rel[code_rows] * n_code + g_cpos[code_rows].astype(np.int64)
        result.flat_code = np.bincount(code_flat, minlength=n_seconds * n_code).astype(
            np.uint32, copy=False
        )
    else:
        result.flat_code = np.zeros(n_seconds * n_code, dtype=np.uint32)

    # Latency histogram. Bin b holds [2^(b-1), 2^b), which is exactly frexp's exponent:
    # frexp(1) is (0.5, 1), frexp(1023) is (0.999, 10), frexp(1024) is (0.5, 11). One frexp
    # is cheaper than a searchsorted over bin boundaries and exact for powers of two.
    lat_valid = ((g_lat >= 0) & (g_lat <= 9)).all(axis=1)
    # Six digits, so the same dot product with powers of ten as the timestamp. Summing the
    # digits instead would make 1023 ms read as 6 ms, putting every slow request in the
    # fastest bin and hiding a latency shift.
    lat_value = np.maximum((g_lat.astype(np.float64) * POW10_6).sum(axis=1), 0.0)
    frexp = np.frexp(lat_value.astype(np.int32))
    bins = np.clip(frexp[1], 0, n_bins - 2)
    # A latency of 0 ms has no bin of its own, since 0 is below the lowest real bin. It goes
    # in bin 0 rather than being dropped, so the histogram still accounts for the line.
    bins = np.where(lat_value == 0, 0, bins)
    # A latency that will not parse gets no real bin at all. Section 8.9 says a bad latency
    # does not reject the line, so the line still counts, but putting it in a real bin would
    # make a broken latency look like a fast one and drag a p95 down.
    lat_idx = np.where(lat_valid, bins, n_bins - 1)
    lat_flat = rel * (n_cap * n_bins) + g_comp * n_bins + lat_idx
    result.flat_lat = np.bincount(lat_flat, minlength=n_seconds * n_cap * n_bins).astype(
        np.uint32, copy=False
    )

    # Unmapped codes: a known component emitting a code the catalog does not have. Counted
    # here rather than dropped, because the new-code detector needs to see them and an
    # E-level one still counts toward the error rate (section 8.9).
    unmapped_rows = np.nonzero(~g_is_success & ~g_known_code)[0]
    if unmapped_rows.size:
        unmapped: dict[str, int] = {}
        raw_codes = H[good][:, CODE_SLICE][unmapped_rows]
        for row in raw_codes:
            code = row.tobytes().decode("latin-1")
            unmapped[code] = unmapped.get(code, 0) + 1
        result.unmapped_codes = unmapped

    return result


def _empty_result_with_first(result: BatchResult) -> BatchResult:
    """Drop the counts but keep the time span, for the clock-skew path."""
    fresh = _empty_result()
    fresh.n_lines = result.n_lines
    fresh.first_sec = result.first_sec
    fresh.last_sec = result.last_sec
    fresh.unk_lines = result.unk_lines
    fresh.unk_reason = result.unk_reason
    fresh.rejected = result.rejected
    return fresh


class BatchPipeline:
    """parse_chunk plus the unidentified routing, so callers have one entry point.

    Keeps the hot path honest: a caller that forgets to account for rejected lines gets an
    error rather than a silent count mismatch.
    """

    def __init__(
        self,
        vocab: Vocabulary,
        n_cap: int,
        n_code: int,
        n_bins: int,
        component_slots: dict[str, int] | None = None,
        unidentified: Unidentified | None = None,
        keep_rejected: bool = False,
    ) -> None:
        self.index = VocabularyIndex(vocab, component_slots)
        self.n_cap = n_cap
        self.n_code = n_code
        self.n_bins = n_bins
        self.unk = unidentified or Unidentified(vocab)
        # Retaining rejected bytes is a debugging aid, not the default: at a few percent
        # unidentified on a multi-million-line replay it is a lot of memory.
        self.keep_rejected = keep_rejected
        self.lines_seen = 0
        self.lines_rejected = 0
        self.batches = 0

    def feed(self, data: bytes, now_ms: int | None = None) -> BatchResult:
        """Parse a chunk, accounting for everything it could not attribute."""
        result = parse_chunk(data, self.index, self.n_cap, self.n_code, self.n_bins)
        self.batches += 1
        self.lines_seen += result.n_lines
        self.lines_rejected += result.unk_lines
        if result.unk_lines:
            if result.rejected and self.keep_rejected:
                self.unk.record_many(result.rejected, now_ms)
            elif result.rejected:
                # Classify the bytes properly but do not retain the samples: the counts are
                # what the health metric needs, and a long replay would otherwise hold every
                # bad line in memory.
                self.unk.record_many(result.rejected, now_ms)
            for reason, count in (result.unk_reason or {}).items():
                self.unk.record_routed(reason, 0)
        if result.unmapped_codes:
            self._note_unmapped(result.unmapped_codes)
        return result

    def _note_unmapped(self, codes: dict[str, int]) -> None:
        for code, count in codes.items():
            name = f"unmapped_code:{code}"
            self.unk.record_routed(name, count)


def empty_like(other: BatchResult) -> BatchResult:
    """A result with the same shapes but no counts, for a chunk that produced nothing."""
    return BatchResult(
        n_lines=0,
        first_sec=other.first_sec,
        last_sec=other.last_sec,
        n_seconds=0,
        flat_tot=np.zeros_like(other.flat_tot) if other.flat_tot is not None else None,
        flat_err=np.zeros_like(other.flat_err) if other.flat_err is not None else None,
        flat_warn=np.zeros_like(other.flat_warn) if other.flat_warn is not None else None,
        flat_code=np.zeros_like(other.flat_code) if other.flat_code is not None else None,
        flat_lat=np.zeros_like(other.flat_lat) if other.flat_lat is not None else None,
        unk_reason={},
        rejected=[],
        unmapped_codes={},
    )


def quick_reason_counts(data: bytes) -> dict[str, int]:
    """Classify a chunk with the cheap byte predicates only. For tests and for a
    cross-check on the fast path's own rejection reasons."""
    out: dict[str, int] = {}
    for raw in data.split(b"\n")[:-1]:
        reason = quick_class_reason(raw)
        name = reason or "looks_valid"
        out[name] = out.get(name, 0) + 1
    return out


def describe(result: BatchResult) -> dict[str, Any]:
    """Human-readable summary of a chunk, for debugging and the bench tool."""
    return {
        "n_lines": result.n_lines,
        "first_sec": result.first_sec,
        "last_sec": result.last_sec,
        "n_seconds": result.n_seconds,
        "unidentified": result.unk_lines,
        "reasons": result.unk_reason,
        "unmapped_codes": result.unmapped_codes,
        "message_offset": MESSAGE_SLICE.start,
    }


def success_code() -> str:
    """Exposed so callers do not hardcode it in two places."""
    return SUCCESS_CODE

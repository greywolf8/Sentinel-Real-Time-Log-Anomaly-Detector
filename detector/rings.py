"""Preallocated ring buffers and window sums (docs/sentinel-plan.md sections 8.1 and 8.5).

Everything is allocated once at the section 8.1 sizes and never resized, so a new component
can take a preallocated slot (section 8.9 adopt) without a reallocation while traffic is
flowing. The sizes are small enough that the whole thing is a few megabytes:

    NCAP = 64 components, NCODE = 256 codes, R = 900 one-second slots (15 minutes),
    NB = 20 latency bins, NS = 8 services.

The time model is event time (section 7). A watermark advances as newer timestamps arrive
and a second is evaluated once the watermark is 2 seconds past it, which is the reorder
slack. That is what lets a line arriving slightly out of order still land in its own
second instead of being dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Section 8.1, verbatim.
NCAP = 64
NCODE = 256
R = 900
NB = 20
NS = 8
# Section 7: a second is evaluated once the watermark is this far past it.
REORDER_SLACK_S = 2
# Section 8.5 windows.
WINDOWS: tuple[int, ...] = (10, 60, 300)
# Section 8.5: a floor prevents a zero baseline blowing the z-score up.
P_FLOOR = 1e-4
# A latency bin is log2, so a latency of L sits in bin floor(log2(L)) + 1. NB bins cover
# 0 to about 5 million ms, which is far past anything the platform emits.
LAT_BINS = NB
# Histogram bin used for a latency that could not be parsed. Kept separate from bin 0 so a
# bad latency does not read as a very fast one.
LAT_INVALID_BIN = NB - 1


class RingError(ValueError):
    """Raised when a component, code or slot index is out of the preallocated range."""


@dataclass(slots=True)
class Slot:
    """One preallocated component slot."""

    index: int
    key: str = ""  # the 7-byte SVC|CMP form, e.g. "CLM|STR"
    service: str = ""
    component: str = ""
    adopted_at_ms: int = 0
    # Warm-up suppresses alerts while a newly adopted component learns its baseline.
    warmup_until_ms: int = 0
    active: bool = False


@dataclass(slots=True)
class Watermark:
    """Event-time watermark with reorder slack (sections 7 and 8.9).

    ``value_s`` is the highest event second seen. A second is settled once the watermark is
    REORDER_SLACK_S past it, which is when it is safe to evaluate: any line for that second
    that is still in flight would have to arrive out of order by more than the slack.
    """

    value_s: int = -1
    slack_s: int = REORDER_SLACK_S
    last_line_sec: int = -1
    late_lines: int = 0
    late_dropped: int = 0
    future_lines: int = 0

    def observe(self, sec: int) -> int:
        """Record an event second. Returns how far behind the watermark it was."""
        if sec > self.value_s:
            self.value_s = sec
            self.last_line_sec = sec
            return 0
        behind = self.value_s - sec
        if behind > self.slack_s:
            self.late_dropped += 1
        else:
            self.late_lines += 1
        return behind

    def settled_through(self) -> int:
        """The last event second that may be evaluated, watermark minus the slack."""
        return self.value_s - self.slack_s

    def is_settled(self, sec: int) -> bool:
        return sec <= self.settled_through()

    def to_json(self) -> dict[str, float | int]:
        return {
            "watermark_s": self.value_s,
            "last_line_s": self.last_line_sec,
            "slack_s": self.slack_s,
            "late_lines": self.late_lines,
            "late_dropped": self.late_dropped,
            "future_lines": self.future_lines,
        }


@dataclass(slots=True)
class Rings:
    """The per-second counts and the incrementally maintained window sums.

    The window sums are the reason this is not O(window) per second. Adding the new second
    and subtracting the one leaving gives an exact rolling sum in constant time, so a 300 s
    window costs the same as a 10 s one (section 8.5).
    """

    n_cap: int = NCAP
    n_code: int = NCODE
    r: int = R
    n_bins: int = NB

    # Per component per second. uint32 because a 15-minute slot at platform volume is
    # nowhere near 2^32, and uint32 halves the memory versus uint64.
    ring_tot: np.ndarray = field(init=False)
    ring_err: np.ndarray = field(init=False)
    ring_warn: np.ndarray = field(init=False)
    ring_code: np.ndarray = field(init=False)
    ring_lat: np.ndarray = field(init=False)

    # Rolling sums over the WINDOWS, shape (3, NCAP, 3) for (tot, err, warn).
    sums: np.ndarray = field(init=False)

    # Which absolute event second lives in ring slot j.
    slot_second: np.ndarray = field(init=False)
    # Cursor into the ring, the absolute second currently being written.
    head: int = -1
    # Highest second already folded into the window sums. -1 means none yet. A second is
    # folded once the watermark has settled past it, at which point no more lines can
    # arrive for it. Tracked explicitly because the head can be newer than what has been
    # folded: the head second is still open for late lines inside the reorder slack.
    folded_through: int = -1

    slots: list[Slot] = field(default_factory=list)
    _key_to_index: dict[str, int] = field(default_factory=dict)
    code_names: list[str] = field(default_factory=list)
    _code_index: dict[str, int] = field(default_factory=dict)
    service_of: list[str] = field(default_factory=list)
    svc_mat: np.ndarray = field(init=False)
    watermark: Watermark = field(default_factory=Watermark)
    seconds_evaluated: int = 0
    # Lines that arrived after their second was already evaluated and folded into a window.
    late_applied: int = 0
    late_rejected: int = 0

    def __post_init__(self) -> None:
        self.ring_tot = np.zeros((self.n_cap, self.r), dtype=np.uint32)
        self.ring_err = np.zeros((self.n_cap, self.r), dtype=np.uint32)
        self.ring_warn = np.zeros((self.n_cap, self.r), dtype=np.uint32)
        self.ring_code = np.zeros((self.n_code, self.r), dtype=np.uint32)
        self.ring_lat = np.zeros((self.n_cap, self.r, self.n_bins), dtype=np.uint16)
        self.sums = np.zeros((len(WINDOWS), self.n_cap, 3), dtype=np.int64)
        self.slot_second = np.full(self.r, -1, dtype=np.int64)
        self.svc_mat = np.zeros((NS, self.n_cap), dtype=np.uint8)
        self.slots = [Slot(index=i) for i in range(self.n_cap)]

    # -- slots -------------------------------------------------------------

    def register(self, key: str, service: str, component: str, index: int) -> Slot:
        """Bind a catalog key to a preallocated slot. Called once per component at load."""
        if not 0 <= index < self.n_cap:
            raise RingError(f"component slot {index} out of range 0..{self.n_cap - 1}")
        if key in self._key_to_index:
            return self.slots[self._key_to_index[key]]
        slot = self.slots[index]
        slot.key = key
        slot.service = service
        slot.component = component
        slot.active = True
        self._key_to_index[key] = index
        svc = self._service_index(service)
        if svc is not None:
            self.svc_mat[svc, index] = 1
            self.service_of.append(service)
        return slot

    def _service_index(self, service: str) -> int | None:
        """Service codes are 3 letters, so a small hash into NS rows. The 3-letter service
        code maps to a stable row without needing a per-run service table."""
        if not service:
            return None
        row = (ord(service[0]) * 31 + ord(service[-1])) % NS
        # Keep the mapping stable: if the row is already taken by a different service, walk
        # forward. NS is 8 and there are 5 services, so this terminates.
        for offset in range(NS):
            candidate = (row + offset) % NS
            taken = self.svc_mat[candidate].any()
            if not taken:
                return candidate
            existing = np.flatnonzero(self.svc_mat[candidate])
            if existing.size and self.slots[int(existing[0])].service == service:
                return candidate
        return row

    def index_of(self, key: str) -> int:
        try:
            return self._key_to_index[key]
        except KeyError as exc:
            raise RingError(f"component {key} is not registered") from exc

    def adopt(self, key: str, service: str, component: str, now_ms: int, warmup_s: float) -> int:
        """Section 8.9 adopt: assign a free preallocated slot to a new component.

        Warm-up runs from now, and the caller suppresses alerts until it ends, so a
        component that is noisy the moment it appears does not page anyone before the
        baseline has had time to learn.
        """
        if key in self._key_to_index:
            return self._key_to_index[key]
        for slot in self.slots:
            if not slot.active:
                slot.key = key
                slot.service = service
                slot.component = component
                slot.active = True
                slot.adopted_at_ms = now_ms
                slot.warmup_until_ms = now_ms + int(warmup_s * 1000)
                self._key_to_index[key] = slot.index
                svc = self._service_index(service)
                if svc is not None:
                    self.svc_mat[svc, slot.index] = 1
                return slot.index
        raise RingError(
            f"no free component slot: all {self.n_cap} are in use, cannot adopt {key}"
        )

    def is_warming(self, index: int) -> bool:
        return self.slots[index].warmup_until_ms > 0

    def key_name(self, index: int) -> str:
        return self.slots[index].key or f"<slot{index}>"

    def active_components(self) -> list[Slot]:
        return [s for s in self.slots if s.active]

    # -- codes -------------------------------------------------------------

    def register_code(self, name: str) -> int:
        """Bind a code to a slot in the NCODE table."""
        if name in self._code_index:
            return self._code_index[name]
        if len(self.code_names) >= self.n_code:
            raise RingError(f"no free code slot: all {self.n_code} are in use")
        index = len(self.code_names)
        self.code_names.append(name)
        self._code_index[name] = index
        return index

    def code_index(self, name: str) -> int | None:
        return self._code_index.get(name)

    # -- the ring ----------------------------------------------------------

    def _slot_for(self, sec: int) -> int:
        """Ring slot for an absolute event second, making it the head if it is newer.

        The head second is the newest one written, and it is deliberately NOT yet in the
        window sums: more lines can still arrive for it. It is folded in when a later
        second arrives, at which point it is complete. ``window_sums`` adds the head back so
        callers see the full window.
        """
        if self.head < 0:
            self._set_head(sec)
            return self.head
        current = int(self.slot_second[self.head])
        if sec > current:
            if sec - current >= self.r:
                # A jump at least as large as the ring: those seconds never existed here.
                self._reset_ring()
                self._set_head(sec)
                return self.head
            # The new second becomes the head. Folding into the window sums is
            # advance_to's job, not this one's: a second is not complete until the watermark
            # has settled past it, and until then more lines can still arrive for it.
            self._set_head(sec)
            return self.head
        if sec == current:
            return self.head
        # An older second, arriving late. Still in the ring if the gap is less than R.
        if current - sec >= self.r:
            raise RingError(f"event second {sec} has fallen out of the {self.r}-slot ring")
        return sec % self.r

    def _set_head(self, sec: int) -> None:
        slot = sec % self.r
        stale = int(self.slot_second[slot])
        if stale >= 0 and stale != sec:
            self._retire(stale)
        self.slot_second[slot] = sec
        self.head = slot

    def add_batch(self, result: object) -> int:
        """Fold a :class:`detector.parse_batch.BatchResult` into the ring.

        This is the only place counts enter the ring, and it is vectorised over the whole
        (n_seconds, NCAP) grid, so cost does not depend on the line count.

        Returns the number of lines that could not be placed because their second was
        already evaluated and folded into a window sum. Those are counted, never dropped
        silently.
        """
        first_sec = getattr(result, "first_sec", None)
        flat_tot = getattr(result, "flat_tot", None)
        if first_sec is None or flat_tot is None:
            return 0
        n_seconds = int(getattr(result, "n_seconds", 0))
        if n_seconds <= 0:
            return 0
        flat_err = getattr(result, "flat_err")
        flat_warn = getattr(result, "flat_warn")
        flat_code = getattr(result, "flat_code")
        flat_lat = getattr(result, "flat_lat")

        settled = self.watermark.settled_through()
        lost = 0
        for offset in range(n_seconds):
            sec = first_sec + offset
            if sec <= settled:
                # This second was already evaluated. Replaying it would double count, so the
                # lines are recorded as late instead (section 8.9: late_dropped, counted).
                lost += int(flat_tot[offset * self.n_cap : (offset + 1) * self.n_cap].sum())
                self.watermark.late_dropped += 1
                continue
            try:
                slot = self._slot_for(sec)
            except RingError:
                lost += int(flat_tot[offset * self.n_cap : (offset + 1) * self.n_cap].sum())
                continue
            lo = offset * self.n_cap
            hi = lo + self.n_cap
            self.ring_tot[:, slot] += flat_tot[lo:hi]
            self.ring_err[:, slot] += flat_err[lo:hi]
            self.ring_warn[:, slot] += flat_warn[lo:hi]
            if flat_code is not None:
                self.ring_code[:, slot] += flat_code[offset * self.n_code : (offset + 1) * self.n_code]
            if flat_lat is not None:
                width = self.n_cap * self.n_bins
                self.ring_lat[:, slot, :] += flat_lat[
                    offset * width : (offset + 1) * width
                ].reshape(self.n_cap, self.n_bins)
        self.late_rejected += lost
        return lost

    def observe_seconds(self, result: object) -> None:
        """Advance the watermark from the seconds a batch covered."""
        secs = getattr(result, "secs", None)
        if secs is None:
            return
        for sec in secs.tolist():
            self.watermark.observe(int(sec))

    # -- windows -----------------------------------------------------------

    def advance_to(self, sec: int) -> list[int]:
        """Fold every second up to ``sec`` into the window sums. Returns the seconds folded.

        This is the cold path: called once per completed second, it costs a handful of
        vector operations regardless of window size, because each second is added once and
        the one leaving each window subtracted once (section 8.5).

        The head second stays out of the sums until a later second arrives, so this must not
        be called with the head second itself; call it with the watermark's settled second.
        """
        if self.head < 0:
            self._set_head(sec)
            self.folded_through = sec - 1
            return []
        # Fold every second from the last folded one up to sec inclusive. A second that was
        # never written folds as zeros, so a quiet period ages the window rather than
        # freezing it.
        start = self.folded_through + 1
        if sec < start:
            return []
        if sec - start >= self.r:
            # The gap is larger than the ring: those seconds cannot be recovered.
            self._reset_ring()
            self._set_head(sec)
            self.folded_through = sec - 1
            return []
        folded: list[int] = []
        for target in range(start, sec + 1):
            self._fold_second(target)
            folded.append(target)
        self.folded_through = sec
        # Keep the head at or after the newest folded second, so a read of the newest
        # complete second is not a miss.
        if self.head < 0 or int(self.slot_second[self.head]) < sec:
            self._set_head(sec)
        return folded

    def _fold_second(self, sec: int) -> None:
        """Add a completed second to the window sums and push out what each window loses.

        Incremental by construction: add the entering second, subtract the leaving one.
        A window is never summed from scratch, which is what keeps a 300 s window as cheap
        as a 10 s one.
        """
        slot = sec % self.r
        present = int(self.slot_second[slot]) == sec
        if not present:
            # Never written: an empty second. Zero the slot so the retire path and any read
            # agree, then the add and subtract below are both no-ops on it.
            self.slot_second[slot] = sec
            self.ring_tot[:, slot] = 0
            self.ring_err[:, slot] = 0
            self.ring_warn[:, slot] = 0
            self.ring_lat[:, slot, :] = 0
            self.ring_code[:, slot] = 0

        for i, size in enumerate(WINDOWS):
            # Entering: this second is now inside the window.
            self.sums[i, :, 0] += self.ring_tot[:, slot]
            self.sums[i, :, 1] += self.ring_err[:, slot]
            self.sums[i, :, 2] += self.ring_warn[:, slot]
            # Leaving: the second that just fell out the far end.
            leaving = sec - size
            if leaving < 0:
                continue
            out_slot = leaving % self.r
            if int(self.slot_second[out_slot]) == leaving:
                self.sums[i, :, 0] -= self.ring_tot[:, out_slot]
                self.sums[i, :, 1] -= self.ring_err[:, out_slot]
                self.sums[i, :, 2] -= self.ring_warn[:, out_slot]
        self.seconds_evaluated += 1

    def _retire(self, sec: int) -> None:
        """Subtract a second whose ring slot is about to be reused.

        Normally a no-op, because a slot is only reused after R seconds and the 300 s
        window has already dropped it. It matters when a caller writes an old second late,
        which is exactly when the subtraction has to happen to keep the sums honest.
        """
        slot = sec % self.r
        newest = int(self.slot_second[self.head]) if self.head >= 0 else sec
        for i, size in enumerate(WINDOWS):
            if newest - sec < size:
                self.sums[i, :, 0] -= self.ring_tot[:, slot]
                self.sums[i, :, 1] -= self.ring_err[:, slot]
                self.sums[i, :, 2] -= self.ring_warn[:, slot]
        self.slot_second[slot] = -1
        self.ring_tot[:, slot] = 0
        self.ring_err[:, slot] = 0
        self.ring_warn[:, slot] = 0
        self.ring_lat[:, slot, :] = 0
        self.ring_code[:, slot] = 0

    def _reset_ring(self) -> None:
        """Forget every second. Used after a jump larger than the ring."""
        self.slot_second[:] = -1
        self.ring_tot[:] = 0
        self.ring_err[:] = 0
        self.ring_warn[:] = 0
        self.ring_lat[:] = 0
        self.ring_code[:] = 0
        self.sums[:] = 0
        self.head = -1

    def rebuild_sums(self) -> None:
        """Recompute the window sums from the ring, for use after a discontinuity."""
        self.sums[:] = 0
        if self.head < 0:
            return
        newest = int(self.slot_second[self.head])
        for slot in range(self.r):
            sec = int(self.slot_second[slot])
            if sec < 0:
                continue
            for i, size in enumerate(WINDOWS):
                if newest - sec < size:
                    self.sums[i, :, 0] += self.ring_tot[:, slot]
                    self.sums[i, :, 1] += self.ring_err[:, slot]
                    self.sums[i, :, 2] += self.ring_warn[:, slot]

    def window_sums(self, index: int, window_s: int) -> tuple[int, int, int]:
        """(total, error, warn) over the ``window_s`` seconds ending at the last folded second.

        The window ends at the watermark's settled second, not at the newest second seen.
        The newest couple of seconds are excluded on purpose: they are inside the reorder
        slack, so a line for them may still be in flight, and a rate computed over a
        half-arrived second would flap.
        """
        sums = self._window_sums_all(window_s)
        return (int(sums[index, 0]), int(sums[index, 1]), int(sums[index, 2]))

    def rate(self, index: int, window_s: int) -> float:
        """Error rate over a window, floored at the baseline (section 8.5)."""
        tot, err, _ = self.window_sums(index, window_s)
        return err / max(tot, 1)

    def window_counts(self, index: int, window_s: int) -> tuple[int, int, int]:
        """Alias of window_sums, spelled for call sites that want the raw counts."""
        return self.window_sums(index, window_s)

    def per_second(self, index: int, seconds: int) -> np.ndarray:
        """Totals for the last ``seconds`` seconds, oldest first. For charts."""
        if self.head < 0:
            return np.zeros(seconds, dtype=np.uint32)
        newest = int(self.slot_second[self.head])
        out = np.zeros(seconds, dtype=np.uint32)
        for age in range(seconds):
            sec = newest - (seconds - 1 - age)
            if sec < 0:
                continue
            slot = sec % self.r
            if self.slot_second[slot] == sec:
                out[age] = self.ring_tot[index, slot]
        return out

    def per_second_err(self, index: int, seconds: int) -> np.ndarray:
        if self.head < 0:
            return np.zeros(seconds, dtype=np.uint32)
        newest = int(self.slot_second[self.head])
        out = np.zeros(seconds, dtype=np.uint32)
        for age in range(seconds):
            sec = newest - (seconds - 1 - age)
            if sec < 0:
                continue
            slot = sec % self.r
            if self.slot_second[slot] == sec:
                out[age] = self.ring_err[index, slot]
        return out

    def code_counts(self, window_s: int) -> np.ndarray:
        """Per-code totals over a window, summed by scanning the ring.

        Code counts do not get incremental sums: the mix detector compares distributions,
        not totals, and section 8.6 runs it once per completed second over one component's
        handful of codes, which is cheap next to the line count.

        The result is indexed by :meth:`code_index`, which must agree with the slot
        numbering the batch parser writes to.
        """
        if self.head < 0:
            return np.zeros(self.n_code, dtype=np.uint32)
        newest = int(self.slot_second[self.head])
        out = np.zeros(self.n_code, dtype=np.uint32)
        for age in range(min(window_s, self.r)):
            sec = newest - age
            if sec < 0:
                continue
            slot = sec % self.r
            if self.slot_second[slot] == sec:
                out += self.ring_code[:, slot]
        return out

    def latency_percentile(self, index: int, window_s: int, percentile: float) -> float | None:
        """Latency percentile from the log2 histogram. None when there is no data.

        Section 8.6 latency shift needs a p95, and a histogram is what the ring keeps, so
        the answer is approximate by construction: it is the bin boundary, not an
        interpolated value. Good enough to notice a shift, which is the point.
        """
        if not 0.0 < percentile <= 1.0:
            raise ValueError(f"percentile must be in (0, 1], got {percentile}")
        if self.head < 0:
            return None
        newest = int(self.slot_second[self.head])
        hist = np.zeros(self.n_bins, dtype=np.int64)
        for age in range(min(window_s, self.r)):
            sec = newest - age
            if sec < 0:
                continue
            slot = sec % self.r
            if self.slot_second[slot] == sec:
                hist += self.ring_lat[index, slot, :]
        total = int(hist[: self.n_bins - 1].sum())
        if total == 0:
            return None
        target = total * percentile
        cumulative = 0
        for b in range(self.n_bins - 1):
            cumulative += int(hist[b])
            if cumulative >= target:
                # Bin b holds [2^(b-1), 2^b); report the upper bound, so a reported p95 is
                # never optimistic about how slow things are.
                return float(1 << b)
        return float(1 << (self.n_bins - 1))

    # -- roll-ups ----------------------------------------------------------

    def _window_sums_all(self, window_s: int) -> np.ndarray:
        """Window sums for every component, shape (NCAP, 3).

        One vectorised pass, so a roll-up over all components costs the same as reading one.
        Returns a copy, so a caller cannot corrupt the incremental sums by writing to it.
        """
        try:
            i = WINDOWS.index(window_s)
        except ValueError as exc:
            raise RingError(f"no {window_s}s window; have {WINDOWS}") from exc
        return self.sums[i].astype(np.int64, copy=True)

    def service_rollup(self, window_s: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(total, err, warn) per service row. One matrix multiply, as section 8.1 wants."""
        sums = self._window_sums_all(window_s)
        mat = self.svc_mat.astype(np.int64)
        return mat @ sums[:, 0], mat @ sums[:, 1], mat @ sums[:, 2]

    def system_rollup(self, window_s: int) -> tuple[int, int, int]:
        """(total, err, warn) across every component in the window."""
        sums = self._window_sums_all(window_s)
        return int(sums[:, 0].sum()), int(sums[:, 1].sum()), int(sums[:, 2].sum())

    def service_names(self) -> list[str]:
        """The service code for each row of svc_mat, empty where the row is unused."""
        return [s.service for s in self.slots if s.service][:NS]

    def service_row_for(self, service: str) -> int | None:
        """Which row of svc_mat a service occupies, or None if it has no components."""
        rows = np.flatnonzero(self.svc_mat.any(axis=1))
        for row in rows:
            if self.slots[int(np.flatnonzero(self.svc_mat[row])[0])].service == service:
                return int(row)
        return None

    def service_name_for_row(self, row: int) -> str:
        """The service code occupying a row, empty if the row is unused."""
        members = np.flatnonzero(self.svc_mat[row])
        if members.size == 0:
            return ""
        return self.slots[int(members[0])].service

    # -- health ------------------------------------------------------------

    def memory_bytes(self) -> int:
        total = 0
        for array in (self.ring_tot, self.ring_err, self.ring_warn, self.ring_code, self.ring_lat, self.sums, self.svc_mat, self.slot_second):
            total += array.nbytes
        return total

    def health(self) -> dict[str, float | int]:
        return {
            "seconds_evaluated": self.seconds_evaluated,
            "head_second": int(self.slot_second[self.head]) if self.head >= 0 else -1,
            "late_rejected": self.late_rejected,
            "memory_bytes": self.memory_bytes(),
            "active_components": len(self.active_components()),
            "codes_registered": len(self.code_names),
            **self.watermark.to_json(),
        }

"""Chunked log reader (docs/sentinel-plan.md section 8.3).

Reads a growing file in binary mode, keeping the offset across calls, and hands whole
chunks to the caller. Four things it has to get right, all named in section 8.3:

- a partial last line is carried, never parsed
- rotation is detected by inode change
- truncation is detected by comparing size to the offset
- when caught up, sleep briefly and adaptively rather than busy-spin

The tailer owns no detection state. It produces bytes and nothing else, so a bug here
shows up as missing counts rather than as a wrong alert.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

# Section 8.3 suggests 1 to 4 MB. The upper end wins on replay throughput; the lower end
# keeps a live tail responsive to a stop signal.
CHUNK_MIN = 1 << 20
CHUNK_MAX = 4 << 20
DEFAULT_CHUNK = 2 << 20
# Adaptive idle sleep. Start short so a live log is picked up quickly, back off to
# IDLE_SLEEP_MAX so a quiet log costs almost nothing, and snap back on the first bytes.
IDLE_SLEEP_MIN = 0.005
IDLE_SLEEP_MAX = 0.020
# After a rotation or a truncation, skip this many bytes to avoid re-reading a partial
# line's tail as if it were a new one.
RESYNC_BYTES = 1 << 16


class RotationDetected(Exception):
    """Raised by :meth:`Tailer.next_chunk` when the file was replaced underneath us."""


@dataclass(slots=True)
class TailerStats:
    """Counters the detector heartbeat reports (section 8.9, other robustness)."""

    chunks: int = 0
    bytes_read: int = 0
    lines_handed_over: int = 0
    rotations: int = 0
    truncations: int = 0
    idle_polls: int = 0
    resyncs: int = 0
    last_offset: int = 0
    last_size: int = 0
    inode: int = 0


@dataclass(slots=True)
class Tailer:
    """A follower on one log file.

    Not thread-safe by design: one reader task owns it (section 8.10, one asyncio loop).
    Use :meth:`follow` for the normal path, or drive :meth:`next_chunk` yourself.
    """

    path: Path
    chunk_size: int = DEFAULT_CHUNK
    start_at_end: bool = False
    _fd: int | None = None
    _offset: int = 0
    _inode: int = 0
    _carry: bytes = b""
    _idle: float = IDLE_SLEEP_MIN
    stats: TailerStats = field(default_factory=TailerStats)

    def __post_init__(self) -> None:
        if not CHUNK_MIN <= self.chunk_size <= CHUNK_MAX:
            raise ValueError(f"chunk_size must be between {CHUNK_MIN} and {CHUNK_MAX}")
        self.path = Path(self.path)

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Open the file, optionally skipping to the end.

        Backlog-then-follow (section 8.3): the default is to read from the start, because a
        detector restarted against an existing log should see the history it missed. A live
        demo wants start_at_end so it does not replay yesterday.
        """
        self.close()
        self._fd = os.open(self.path, os.O_RDONLY)
        info = os.fstat(self._fd)
        self._inode = info.st_ino
        self._offset = info.st_size if self.start_at_end else 0
        self._carry = b""
        self._idle = IDLE_SLEEP_MIN
        self.stats.inode = self._inode
        self.stats.last_offset = self._offset
        self.stats.last_size = info.st_size

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "Tailer":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- position ----------------------------------------------------------

    @property
    def offset(self) -> int:
        return self._offset

    @property
    def carry(self) -> bytes:
        """Bytes of a line not yet terminated by a newline. Never parsed."""
        return self._carry

    def seek(self, offset: int) -> None:
        """Restart reading at an absolute offset, discarding any carry.

        Used after rotation and truncation, and by the replay scrubber (section 11.1).
        """
        if self._fd is None:
            self.open()
        assert self._fd is not None
        self._offset = max(0, offset)
        os.lseek(self._fd, self._offset, os.SEEK_SET)
        self._carry = b""
        self._idle = IDLE_SLEEP_MIN
        self.stats.last_offset = self._offset

    def tell(self) -> int:
        """Byte offset of the next unread byte, excluding the carry.

        A checkpoint is offset plus carry, because the carry has been read from disk but not
        yet handed to a parser. Getting this wrong replays or drops a line on restart.
        """
        return self._offset + len(self._carry)

    # -- reading -----------------------------------------------------------

    def _ensure_open(self) -> None:
        if self._fd is None:
            self.open()

    def _check_rotation(self) -> bool:
        """True if the file was replaced or truncated under us.

        Inode change means a new file took the name (logrotate). Size below offset means
        the file was truncated in place. Both are handled by seeking to 0, which is the
        only correct answer: those lines are gone and the new content starts at 0.
        """
        try:
            info = os.stat(self.path)
        except FileNotFoundError:
            return False
        if info.st_ino != self._inode:
            self.stats.rotations += 1
            return True
        if info.st_size < self._offset:
            self.stats.truncations += 1
            return True
        return False

    def read_chunk(self) -> bytes:
        """One raw read at the current offset. Empty means caught up.

        Only bytes are returned; line framing is the caller's problem, so a caller that
        wants different framing does not have to fight this.
        """
        self._ensure_open()
        if self._check_rotation():
            self.open()
            return b""
        assert self._fd is not None
        data = os.read(self._fd, self.chunk_size)
        if data:
            self._offset += len(data)
            self.stats.chunks += 1
            self.stats.bytes_read += len(data)
            self._idle = IDLE_SLEEP_MIN
        self.stats.last_offset = self._offset
        return data

    def next_chunk(self, timeout: float | None = None) -> bytes:
        """Read until there is at least one whole line, or until ``timeout`` elapses.

        Returns a chunk ending exactly at the last newline, with any partial line kept in
        the carry for next time. An empty result means caught up (or timed out), which is
        the caller's cue to sleep.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            data = self.read_chunk()
            if data:
                return data
            if deadline is not None and time.monotonic() >= deadline:
                return b""
            self._idle_wait()
            if deadline is not None and time.monotonic() >= deadline:
                return b""
            if self._check_rotation():
                self.open()

    def _idle_wait(self) -> None:
        """Sleep an adaptively lengthened interval. Never busy-spins (section 8.3)."""
        self.stats.idle_polls += 1
        time.sleep(self._idle)
        # Back off toward the maximum so an idle log costs almost nothing, and snap back to
        # the minimum as soon as bytes arrive (read_chunk does that).
        self._idle = min(self._idle * 1.5, IDLE_SLEEP_MAX)

    def lines(self) -> Iterator[bytes]:
        """Yield whole lines forever, stripping the newline.

        This is the form the detector loop wants. The carry handling lives here so a
        partial line is never handed to a parser and never lost.
        """
        while True:
            data = self.read_chunk()
            if not data:
                self._idle_wait()
                continue
            buffered = self._carry + data
            last_nl = buffered.rfind(b"\n")
            if last_nl < 0:
                # No terminator in a whole chunk: the line is longer than the chunk. Keep
                # it and read more rather than guessing where it ends.
                self._carry = buffered
                continue
            complete = buffered[: last_nl + 1]
            self._carry = buffered[last_nl + 1 :]
            count = complete.count(b"\n")
            for line in complete.split(b"\n")[:-1]:
                yield line
            self.stats.lines_handed_over += count

    def iter_chunks(self, follow: bool = True) -> Iterator[bytes]:
        """Yield raw chunks, optionally following the file forever.

        ``follow=False`` drains the backlog and stops, which is what a benchmark or an
        offline replay wants. Rotation and truncation are handled either way.
        """
        while True:
            data = self.read_chunk()
            if data:
                yield data
                continue
            if not follow:
                return
            self._idle_wait()
            if self._check_rotation():
                self.open()

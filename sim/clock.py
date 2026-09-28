"""Virtual clock for the generator (docs/sentinel-plan.md section 6.1).

Event time in ``platform.log`` is epoch milliseconds. With ``--speed N`` the generator
runs N times faster than real time, so the virtual clock advances N seconds per real
second while wall-clock sleeps stay real. The detector reads event time only, so nothing
downstream needs to know about the speed factor.
"""

from __future__ import annotations

import time

SPEED_MIN: float = 0.0
SPEED_MAX: float = 1000.0


class VirtualClock:
    """Maps real monotonic seconds to virtual epoch milliseconds."""

    __slots__ = ("_speed", "_origin_wall", "_origin_virtual_ms", "_start_epoch_ms")

    def __init__(self, start_epoch_ms: int, speed: float = 1.0) -> None:
        if not SPEED_MIN <= speed <= SPEED_MAX:
            raise ValueError(f"speed out of range: {speed}")
        self._speed = speed
        self._start_epoch_ms = start_epoch_ms
        self._origin_wall = time.monotonic()
        self._origin_virtual_ms = start_epoch_ms

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def start_epoch_ms(self) -> int:
        return self._start_epoch_ms

    def now_ms(self) -> int:
        """Current virtual time in epoch milliseconds."""
        if self._speed == 0.0:
            return self._origin_virtual_ms
        elapsed_real = time.monotonic() - self._origin_wall
        return self._origin_virtual_ms + int(elapsed_real * 1000.0 * self._speed)

    def sleep(self, seconds: float) -> None:
        """Sleep for ``seconds`` of REAL time, so a higher speed means more events."""
        if seconds <= 0.0:
            return
        time.sleep(seconds)

    def virtual_elapsed_s(self) -> float:
        return (self.now_ms() - self._origin_virtual_ms) / 1000.0

    def daily_curve_slot(self, epoch_ms: int) -> int:
        """Hour of day in UTC, 0-23. Used for the daily traffic multiplier."""
        seconds_of_day = (epoch_ms // 1000) % 86400
        return int(seconds_of_day // 3600)


class ManualClock(VirtualClock):
    """A clock the caller advances by hand.

    Offline dump mode (``--dump``) needs event time to be a pure function of the tick
    index so that ``--seed`` reproduces byte-identical output without depending on how
    fast the machine happened to run. The real-time factor is irrelevant for a file that
    is written in one pass, so this clock reports exactly what it is told.
    """

    __slots__ = ("_manual_ms",)

    def __init__(self, start_epoch_ms: int) -> None:
        super().__init__(start_epoch_ms, speed=1.0)
        self._manual_ms = start_epoch_ms

    def advance_to(self, epoch_ms: int) -> None:
        if epoch_ms < self._manual_ms:
            raise ValueError("manual clock cannot go backwards")
        self._manual_ms = epoch_ms

    def now_ms(self) -> int:
        return self._manual_ms

    def sleep(self, seconds: float) -> None:
        return

"""SQLite outbox for alert delivery (docs/sentinel-plan.md section 10).

Alerts are written to an SQLite outbox with idempotency key, attempts, exponential backoff.
A worker runs separately so the reader is never blocked. This ensures failures never lose
alerts and the detector's hot path remains fast.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class OutboxStatus(str, Enum):
    """Status of an outbox entry."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DELIVERED = "delivered"
    FAILED = "failed"


@dataclass(slots=True)
class OutboxEntry:
    """One entry in the outbox."""

    id: int
    payload: dict[str, Any]
    idempotency_key: str
    attempts: int = 0
    max_attempts: int = 5
    next_attempt_at: float = 0.0
    status: OutboxStatus = OutboxStatus.PENDING
    created_at: float = 0.0
    delivered_at: float = 0.0
    error_message: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "payload": self.payload,
            "idempotency_key": self.idempotency_key,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "next_attempt_at": self.next_attempt_at,
            "status": self.status.value,
            "created_at": self.created_at,
            "delivered_at": self.delivered_at,
            "error_message": self.error_message,
        }


class Outbox:
    """SQLite outbox for reliable alert delivery."""

    def __init__(self, db_path: Path = Path("outbox.db")) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _init_db(self) -> None:
        """Initialize the SQLite database schema."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payload TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    attempts INTEGER DEFAULT 0,
                    max_attempts INTEGER DEFAULT 5,
                    next_attempt_at REAL DEFAULT 0.0,
                    status TEXT DEFAULT 'pending',
                    created_at REAL DEFAULT 0.0,
                    delivered_at REAL DEFAULT 0.0,
                    error_message TEXT DEFAULT ''
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_status ON outbox(status)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_next_attempt ON outbox(next_attempt_at)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_idempotency ON outbox(idempotency_key)"
            )
            conn.commit()
            conn.close()

    def add(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
        max_attempts: int = 5,
    ) -> int:
        """Add an entry to the outbox. Returns the entry ID."""
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO outbox
                    (payload, idempotency_key, attempts, max_attempts, next_attempt_at, status, created_at)
                    VALUES (?, ?, 0, ?, ?, 'pending', ?)
                    """,
                    (
                        json.dumps(payload),
                        idempotency_key,
                        max_attempts,
                        now,
                        now,
                    ),
                )
                conn.commit()
                entry_id = cursor.lastrowid
            except sqlite3.IntegrityError:
                # Duplicate idempotency key - return existing ID
                cursor.execute(
                    "SELECT id FROM outbox WHERE idempotency_key = ?",
                    (idempotency_key,),
                )
                row = cursor.fetchone()
                entry_id = row[0] if row else -1
            finally:
                conn.close()
        return entry_id

    def get_pending(self, limit: int = 100) -> list[OutboxEntry]:
        """Get pending entries that are ready for retry."""
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, payload, idempotency_key, attempts, max_attempts,
                       next_attempt_at, status, created_at, delivered_at, error_message
                FROM outbox
                WHERE status = 'pending' AND next_attempt_at <= ?
                ORDER BY next_attempt_at ASC
                LIMIT ?
                """,
                (now, limit),
            )
            rows = cursor.fetchall()
            conn.close()

        entries = []
        for row in rows:
            entries.append(
                OutboxEntry(
                    id=row[0],
                    payload=json.loads(row[1]),
                    idempotency_key=row[2],
                    attempts=row[3],
                    max_attempts=row[4],
                    next_attempt_at=row[5],
                    status=OutboxStatus(row[6]),
                    created_at=row[7],
                    delivered_at=row[8],
                    error_message=row[9],
                )
            )
        return entries

    def mark_in_progress(self, entry_id: int) -> None:
        """Mark an entry as in progress."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE outbox SET status = 'in_progress' WHERE id = ?",
                (entry_id,),
            )
            conn.commit()
            conn.close()

    def mark_delivered(self, entry_id: int) -> None:
        """Mark an entry as delivered."""
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE outbox
                SET status = 'delivered', delivered_at = ?
                WHERE id = ?
                """,
                (now, entry_id),
            )
            conn.commit()
            conn.close()

    def mark_failed(
        self, entry_id: int, error_message: str, backoff_s: float = 60.0
    ) -> None:
        """Mark an entry as failed and schedule retry with exponential backoff."""
        now = time.time()
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE outbox
                SET status = 'pending',
                    attempts = attempts + 1,
                    next_attempt_at = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (now + backoff_s, error_message, entry_id),
            )
            conn.commit()
            conn.close()

    def get_entry(self, entry_id: int) -> OutboxEntry | None:
        """Get an entry by ID."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, payload, idempotency_key, attempts, max_attempts,
                       next_attempt_at, status, created_at, delivered_at, error_message
                FROM outbox
                WHERE id = ?
                """,
                (entry_id,),
            )
            row = cursor.fetchone()
            conn.close()

        if not row:
            return None

        return OutboxEntry(
            id=row[0],
            payload=json.loads(row[1]),
            idempotency_key=row[2],
            attempts=row[3],
            max_attempts=row[4],
            next_attempt_at=row[5],
            status=OutboxStatus(row[6]),
            created_at=row[7],
            delivered_at=row[8],
            error_message=row[9],
        )

    def cleanup_old(self, days: int = 7) -> int:
        """Clean up old delivered entries."""
        cutoff = time.time() - (days * 86400)
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute(
                """
                DELETE FROM outbox
                WHERE status = 'delivered' AND delivered_at < ?
                """,
                (cutoff,),
            )
            deleted = cursor.rowcount
            conn.commit()
            conn.close()
        return deleted

    def stats(self) -> dict[str, int]:
        """Get outbox statistics."""
        with self._lock:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status")
            rows = cursor.fetchall()
            conn.close()

        stats = {status.value: 0 for status in OutboxStatus}
        for status, count in rows:
            stats[status] = count
        return stats


def exponential_backoff(attempt: int, base_s: float = 60.0, max_s: float = 3600.0) -> float:
    """Calculate exponential backoff with jitter."""
    backoff = base_s * (2 ** (attempt - 1))
    backoff = min(backoff, max_s)
    # Add jitter: +/- 25%
    jitter = backoff * 0.25 * (2 * (hash(str(attempt)) % 100) / 100 - 1)
    return backoff + jitter


class OutboxWorker:
    """Worker that processes outbox entries and delivers them.

    Runs in a separate thread or asyncio task so the reader is never blocked.
    """

    def __init__(
        self,
        outbox: Outbox,
        delivery_func: callable[[dict[str, Any]], bool],
        poll_interval_s: float = 5.0,
    ) -> None:
        self.outbox = outbox
        self.delivery_func = delivery_func
        self.poll_interval_s = poll_interval_s
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the worker."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop the worker."""
        self._running = False
        if self._task:
            await self._task
            self._task = None

    async def _run(self) -> None:
        """Main worker loop."""
        logger.info("Outbox worker started")
        while self._running:
            try:
                # Get pending entries
                entries = self.outbox.get_pending(limit=10)
                if not entries:
                    await asyncio.sleep(self.poll_interval_s)
                    continue

                # Process each entry
                for entry in entries:
                    if not self._running:
                        break

                    # Check if max attempts reached
                    if entry.attempts >= entry.max_attempts:
                        logger.error(
                            f"Outbox entry {entry.id} failed after {entry.attempts} attempts"
                        )
                        self.outbox.mark_failed(
                            entry.id,
                            f"Max attempts ({entry.max_attempts}) exceeded",
                            backoff_s=86400,  # Wait a day before retry
                        )
                        continue

                    # Mark as in progress
                    self.outbox.mark_in_progress(entry.id)

                    # Try to deliver
                    try:
                        success = await asyncio.to_thread(
                            self.delivery_func, entry.payload
                        )
                        if success:
                            self.outbox.mark_delivered(entry.id)
                            logger.info(f"Outbox entry {entry.id} delivered")
                        else:
                            backoff = exponential_backoff(entry.attempts + 1)
                            self.outbox.mark_failed(
                                entry.id,
                                "Delivery function returned False",
                                backoff_s=backoff,
                            )
                            logger.warning(
                                f"Outbox entry {entry.id} delivery failed, retry in {backoff:.1f}s"
                            )
                    except Exception as e:
                        backoff = exponential_backoff(entry.attempts + 1)
                        self.outbox.mark_failed(
                            entry.id,
                            str(e),
                            backoff_s=backoff,
                        )
                        logger.error(
                            f"Outbox entry {entry.id} delivery error: {e}, retry in {backoff:.1f}s"
                        )

            except Exception as e:
                logger.error(f"Outbox worker error: {e}")
                await asyncio.sleep(self.poll_interval_s)

        logger.info("Outbox worker stopped")

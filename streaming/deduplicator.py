"""Idempotency layer for at-least-once delivery.

JetStream (and any at-least-once transport) can redeliver a message: after a
consumer crash between processing and ack, the same bytes arrive again. To make
consumers idempotent we remember which event *contents* have already produced a
state change, keyed by :attr:`EventEnvelope.content_hash`, and drop repeats.

Two stores are provided:

* :class:`InMemoryDedupStore` -- fast, non-durable; fine for a single process
  that never needs to survive a restart.
* :class:`SqliteDedupStore` -- durable across restarts using stdlib ``sqlite3``.
  This is what makes the "consumer restart" guarantee hold without pulling in
  Redis or any external cache (see rule 13 in ``CLAUDE.md``).
"""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from typing import Optional, Protocol

from .event_envelope import EventEnvelope


class DedupStore(Protocol):
    def contains(self, key: str) -> bool: ...

    def add(self, key: str) -> bool:
        """Record ``key``. Returns ``True`` if newly added, ``False`` if it was
        already present (lets callers add-and-check atomically)."""
        ...

    def close(self) -> None: ...


class InMemoryDedupStore:
    """Bounded LRU set. Non-durable."""

    def __init__(self, capacity: int = 1_000_000) -> None:
        self._capacity = capacity
        self._keys: "OrderedDict[str, None]" = OrderedDict()
        self._lock = threading.Lock()

    def contains(self, key: str) -> bool:
        with self._lock:
            return key in self._keys

    def add(self, key: str) -> bool:
        with self._lock:
            if key in self._keys:
                self._keys.move_to_end(key)
                return False
            self._keys[key] = None
            if len(self._keys) > self._capacity:
                self._keys.popitem(last=False)
            return True

    def close(self) -> None:  # pragma: no cover - nothing to release
        pass


class SqliteDedupStore:
    """Durable dedup store backed by a single sqlite table.

    Durability is what lets a restarted consumer recognise a redelivered event
    it already processed before the crash. The ``INSERT OR IGNORE`` + row-count
    check is our atomic add-and-test.
    """

    def __init__(self, path: str) -> None:
        # check_same_thread=False so an async consumer callable on a worker
        # thread can share the connection; all access is serialized by _lock.
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS processed (content_hash TEXT PRIMARY KEY)"
        )
        self._conn.commit()

    def contains(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM processed WHERE content_hash = ? LIMIT 1", (key,)
            )
            return cur.fetchone() is not None

    def add(self, key: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO processed (content_hash) VALUES (?)", (key,)
            )
            self._conn.commit()
            return cur.rowcount > 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Deduplicator:
    """Content-hash based idempotency guard."""

    def __init__(self, store: Optional[DedupStore] = None) -> None:
        self._store: DedupStore = store or InMemoryDedupStore()

    def _key(self, envelope: EventEnvelope) -> str:
        # content_hash is populated in model_post_init, but guard anyway.
        return envelope.content_hash or envelope.compute_content_hash()

    def is_duplicate(self, envelope: EventEnvelope) -> bool:
        return self._store.contains(self._key(envelope))

    def mark_processed(self, envelope: EventEnvelope) -> bool:
        """Record the event as processed. Returns ``True`` if newly recorded."""

        return self._store.add(self._key(envelope))

    def check_and_mark(self, envelope: EventEnvelope) -> bool:
        """Atomically test-and-set.

        Returns ``True`` if this is the first time we've seen the content (the
        caller should process it) and ``False`` if it is a duplicate (skip).
        """

        return self._store.add(self._key(envelope))

    def close(self) -> None:
        self._store.close()

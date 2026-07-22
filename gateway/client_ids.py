"""Unique client order IDs and an idempotency registry.

Each order intent gets a unique client order ID. Resubmitting the *same* client
order ID is idempotent: the cached acknowledgement is returned and the exchange
is not called again -- protecting against duplicate orders on retries.
"""

from __future__ import annotations

import itertools
import threading
import uuid
from typing import Dict, Optional

from .orders import OrderAck


class ClientOrderIdFactory:
    def __init__(self, prefix: str = "mm") -> None:
        self._prefix = prefix
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def new(self) -> str:
        with self._lock:
            n = next(self._counter)
        # Counter guarantees local uniqueness; uuid guards against restarts /
        # multiple processes sharing a prefix.
        return f"{self._prefix}-{n}-{uuid.uuid4().hex[:12]}"


class IdempotencyRegistry:
    """Maps client_order_id -> the ack we already got for it."""

    def __init__(self) -> None:
        self._acks: Dict[str, OrderAck] = {}
        self._lock = threading.Lock()

    def get(self, client_order_id: str) -> Optional[OrderAck]:
        with self._lock:
            return self._acks.get(client_order_id)

    def seen(self, client_order_id: str) -> bool:
        with self._lock:
            return client_order_id in self._acks

    def record(self, client_order_id: str, ack: OrderAck) -> None:
        with self._lock:
            self._acks[client_order_id] = ack

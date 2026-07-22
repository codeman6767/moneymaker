"""Low-latency streaming event backbone (Module 1).

An event-driven, at-least-once, integrity-checked transport layer for the
low-latency lane. Everything here is transport-agnostic except
:mod:`streaming.nats_bus`, the NATS JetStream implementation used for the first
build.

See ``CLAUDE.md`` for the permanent rules this module upholds (timestamp
completeness, sequence-gap safety, no hot-path DB/model loads, monotonic
latency measurement).
"""

from .correction_handler import CorrectionHandler, CorrectionResult, CorrectionStatus
from .deduplicator import (
    Deduplicator,
    InMemoryDedupStore,
    SqliteDedupStore,
)
from .event_bus import (
    SUBJECTS,
    DeadLetter,
    DeadLetterQueue,
    Delivery,
    EventBus,
    EventHandler,
    EventProcessor,
    InMemoryEventBus,
    MessageContext,
    ProcessAction,
    ProcessResult,
    ProcessStatus,
)
from .event_envelope import (
    SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    EventEnvelope,
)
from .latency import LatencyHistogram, LatencyRegistry, LatencySnapshot, monotonic_ns
from .replay import (
    InMemoryRawEventStore,
    JsonlRawEventStore,
    RawEventStore,
    Replayer,
    replay_sort_key,
)
from .sequence_tracker import (
    ObserveResult,
    SequenceStatus,
    SequenceTracker,
    StreamHealth,
)

__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_SCHEMA_VERSIONS",
    "EventEnvelope",
    "SUBJECTS",
    "EventBus",
    "InMemoryEventBus",
    "EventProcessor",
    "EventHandler",
    "Delivery",
    "MessageContext",
    "ProcessAction",
    "ProcessResult",
    "ProcessStatus",
    "DeadLetter",
    "DeadLetterQueue",
    "SequenceTracker",
    "SequenceStatus",
    "StreamHealth",
    "ObserveResult",
    "Deduplicator",
    "InMemoryDedupStore",
    "SqliteDedupStore",
    "CorrectionHandler",
    "CorrectionResult",
    "CorrectionStatus",
    "LatencyRegistry",
    "LatencyHistogram",
    "LatencySnapshot",
    "monotonic_ns",
    "RawEventStore",
    "InMemoryRawEventStore",
    "JsonlRawEventStore",
    "Replayer",
    "replay_sort_key",
]

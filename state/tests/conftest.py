"""Fixture loading for live-state tests.

Each JSON fixture describes a subject/provider and an ordered list of events.
The loader rehydrates them into :class:`EventEnvelope` objects so tests drive
the state models exactly as the streaming backbone would.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Tuple

import pytest

from streaming.event_envelope import EventEnvelope

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_events(filename: str) -> Tuple[dict, List[EventEnvelope]]:
    data = json.loads((FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    subject = data["subject"]
    provider = data["provider"]
    events = [
        EventEnvelope(
            subject=subject,
            provider=provider,
            event_type=rec["event_type"],
            sequence=rec.get("sequence"),
            event_time=rec["event_time"],
            payload=rec.get("payload", {}),
            is_correction=rec.get("is_correction", False),
        )
        for rec in data["events"]
    ]
    return data, events


@pytest.fixture
def mlb_events() -> Tuple[dict, List[EventEnvelope]]:
    return load_events("mlb_half_inning.json")


@pytest.fixture
def nba_events() -> Tuple[dict, List[EventEnvelope]]:
    return load_events("nba_sequence.json")


@pytest.fixture
def kalshi_events() -> Tuple[dict, List[EventEnvelope]]:
    return load_events("kalshi_book.json")

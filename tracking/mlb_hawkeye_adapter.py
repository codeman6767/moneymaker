"""Optional MLB Hawk-Eye (frame-level) tracking adapter.

STATUS: interface only. Like the NBA optical adapter, this serves real
frame-level data from a licensed store when configured and is otherwise
unavailable. It never fabricates trajectories or fielder positions
(requirement 5).

Frame-level Hawk-Eye is distinct from event-level Statcast metrics (see
``mlb_statcast_adapter.py``): Hawk-Eye produces the underlying high-frequency
ball trajectory and player/skeletal positions, whereas Statcast exposes derived
per-event measures (release speed, launch angle, etc.).

LICENSING / HISTORICAL DATA
---------------------------
MLB Hawk-Eye optical tracking is proprietary to MLB Advanced Media. Raw
frame-level tracking is **not** part of the public Statcast/Baseball-Savant
feeds and is not publicly redistributable. Using it requires:

* an MLBAM data agreement granting access to raw tracking;
* an explicit historical-data entitlement for prior seasons;
* compliance with MLBAM redistribution and derived-data restrictions.

Do not commit raw Hawk-Eye data to this repository. Point the adapter at an
external, access-controlled store.
"""

from __future__ import annotations

from typing import Iterator, Optional

from .base import (
    FrameParquetStore,
    FrameSource,
    TrackingFrame,
)


class MLBHawkEyeAdapter(FrameSource):
    """Serves MLB frame-level tracking from a licensed Parquet store."""

    provider_name = "mlb_hawkeye"
    licensing = (
        "Proprietary MLB Hawk-Eye optical tracking (MLB Advanced Media). Not part "
        "of public Statcast feeds; requires an MLBAM agreement and a historical-"
        "data entitlement; not redistributable."
    )

    def __init__(
        self,
        frame_store: Optional[FrameParquetStore] = None,
        *,
        root_path: Optional[str] = None,
        enabled: bool = True,
    ) -> None:
        if frame_store is None and root_path is not None:
            frame_store = FrameParquetStore(root_path)
        self._store = frame_store
        self._enabled = enabled

    def is_available(self) -> bool:
        return self._enabled and self._store is not None

    def iter_frames(
        self, game_id: str, *, period: Optional[int] = None
    ) -> Iterator[TrackingFrame]:
        self.require_available()
        assert self._store is not None
        for frame in self._store.read_frames(game_id, sport="mlb"):
            # For MLB, "period" maps to inning when present.
            if period is None or frame.period == period:
                yield frame

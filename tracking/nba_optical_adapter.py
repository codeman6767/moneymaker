"""Optional NBA optical (frame-level) tracking adapter.

STATUS: interface only. This adapter is deliberately *not* backed by a bundled
data source and it never synthesizes frames. When it has been pointed at a real,
licensed frame store it serves those frames; otherwise it reports itself
unavailable so the rest of the system runs on event-level data alone
(requirement 5 and 11).

LICENSING / HISTORICAL DATA
---------------------------
NBA player-tracking (optical) data is proprietary. Historically produced by
STATS SportVU, then Second Spectrum, and more recently Hawk-Eye, it is licensed
through the NBA and is **not** freely redistributable. Using it requires:

* a commercial data-license agreement with the rights holder / the NBA;
* a separate historical-data entitlement for past seasons (live entitlements do
  not automatically grant history);
* adherence to the provider's redistribution and derived-data terms.

Do not commit raw optical data to this repository. Point the adapter at an
external, access-controlled store (see :class:`~tracking.base.FrameParquetStore`).
"""

from __future__ import annotations

from typing import Iterator, Optional

from .base import (
    FrameDataUnavailable,
    FrameParquetStore,
    FrameSource,
    TrackingFrame,
)


class NBAOpticalAdapter(FrameSource):
    """Serves NBA frame-level tracking from a licensed Parquet store.

    Pass a configured :class:`FrameParquetStore` (or a root path) that already
    contains licensed data. With no store configured the adapter is simply
    unavailable -- it will not, and cannot, invent frames.
    """

    provider_name = "nba_optical"
    licensing = (
        "Proprietary NBA optical tracking (SportVU / Second Spectrum / Hawk-Eye). "
        "Requires a commercial license and a historical-data entitlement; not "
        "redistributable."
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
        # Available only when explicitly enabled AND backed by a real store.
        return self._enabled and self._store is not None

    def iter_frames(
        self, game_id: str, *, period: Optional[int] = None
    ) -> Iterator[TrackingFrame]:
        self.require_available()
        assert self._store is not None  # for type-checkers; guarded above
        for frame in self._store.read_frames(game_id, sport="nba"):
            if period is None or frame.period == period:
                yield frame

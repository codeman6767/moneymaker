# Tracking & positional-data architecture (Module 3)

This module separates two fundamentally different kinds of spatial data and
guarantees the system keeps working when the harder-to-get kind is missing.

## The two data levels

| | Event-level | Frame-level (optical tracking) |
|---|---|---|
| What | One coordinate per discrete event (shot spot, batted-ball landing) | Per-player positions sampled many times/sec |
| Types | `EventCoordinate`, `NBAShotEvent`, `OnCourtLineup`, `StatcastPitch`, `StatcastBattedBall` | `TrackingFrame`, `PlayerFrameSample` |
| Source | Ordinary play-by-play / Statcast feeds | Optional, licensed `FrameSource` adapters |
| Availability | Assumed always available | **Assumed unavailable** unless configured |
| Storage | With the event stream / state | Partitioned Parquet + PostgreSQL manifests |

The two are **distinct, non-interchangeable types**. `EventCoordinate.level` is
permanently `EVENT` (a read-only property, never a settable field), there is no
function that converts an event coordinate into a frame, and every frame-level
aggregator calls `assert_frame_level(...)` first — so event-only data can never
be silently treated as player tracking. This is enforced by tests.

## Functioning without frame data

- Event-level features (`shot_distance`, `shot_zone`, `pitch_measures`,
  `batted_ball_measures`) never touch frames and always work.
- Frame-level features (`lineup_spacing`, `defender_distance`, `movement_speed`)
  require a configured adapter. When none is available, adapters report
  `is_available() == False` and raise `FrameDataUnavailable` — callers catch it
  and degrade gracefully.
- No adapter ever synthesizes frames, and no coordinate/velocity absent from a
  source is invented (missing axes stay `None`).

## Storage layout

- **Frame data → partitioned Parquet** via `FrameParquetStore`, partitioned as
  `sport=<>/game_id=<>/date=<>/` (Hive-style). Coordinate/motion columns are
  nullable and written only when the provider supplies them.
- **Metadata / manifests → PostgreSQL** via `PostgresManifestRepository`
  (`tracking_manifests` table). This is bulk/offline research metadata and is
  **not** on the hot decision path. An `InMemoryManifestRepository` is provided
  for tests.

## Licensing & historical-data requirements

Raw optical tracking is proprietary. **Do not commit raw frame data to this
repository.** Point adapters at external, access-controlled stores.

### NBA optical (`NBAOpticalAdapter`)
- Proprietary NBA player-tracking (historically STATS SportVU → Second Spectrum
  → Hawk-Eye), licensed through the NBA. Not freely redistributable.
- Requires: a commercial data-license agreement; a **separate historical-data
  entitlement** for past seasons; compliance with redistribution/derived-data
  terms.

### MLB Hawk-Eye (`MLBHawkEyeAdapter`)
- Proprietary MLB Advanced Media (MLBAM) optical tracking. Raw frame-level
  tracking is **not** part of the public Statcast feeds.
- Requires: an MLBAM data agreement for raw tracking; an explicit historical-data
  entitlement; compliance with MLBAM redistribution/derived-data restrictions.

### MLB Statcast (`MLBStatcastAdapter`) — event-level, not frames
- Publicly accessible via MLB's Baseball Savant (and tools like pybaseball) but
  subject to MLB's terms of use; intended for personal/non-commercial analysis
  unless a separate agreement exists.
- Historical coverage varies by metric/season (pitch tracking from ~2008 via
  PITCHf/x; full Statcast from 2015). Confirm entitlement before commercial use
  or redistribution.

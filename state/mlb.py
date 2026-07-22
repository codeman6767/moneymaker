"""Live MLB game state.

A single-writer, sequence-tracked model of one MLB game. Every handler is an
O(1) field update (dict/int/tuple assignment); nothing scans or does I/O.

Event types consumed (in ``envelope.event_type``):

* ``lineup``         -- set a team's batting order.
* ``at_bat``         -- set current batter and pitcher.
* ``pitch``          -- increment pitch counts (per-pitcher bullpen usage).
* ``out``            -- add outs.
* ``score``          -- add runs to a team.
* ``bases``          -- set base occupancy.
* ``inning``         -- advance inning/half; reset outs and bases.
* ``pitching_change``-- change pitcher (records bullpen usage).
* ``status``         -- set game status.
* ``snapshot``       -- full-state replacement (recovery).
* ``correction``     -- restate absolute field values.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from streaming.event_envelope import EventEnvelope

from .base import LiveState

BASES = ("1B", "2B", "3B")
GAME_STATUSES = ("scheduled", "warmup", "live", "suspended", "delayed", "final")


class MLBGameState(LiveState):
    kind = "mlb_game"
    require_snapshot_first = False

    def __init__(self, entity_id: str) -> None:
        super().__init__(entity_id)
        self.game_status: str = "scheduled"
        self.inning: int = 1
        self.half: str = "top"  # "top" or "bottom"
        self.outs: int = 0
        self.bases: Dict[str, Optional[str]] = {b: None for b in BASES}
        self.score: Dict[str, int] = {"home": 0, "away": 0}
        self.batter: Optional[str] = None
        self.pitcher: Optional[str] = None
        # Current pitcher's pitch count is derived from bullpen_usage; we also
        # keep a game-wide total for convenience.
        self.total_pitches: int = 0
        self.bullpen_usage: Dict[str, int] = {}
        self.batting_order: Dict[str, list[str]] = {"home": [], "away": []}

    # -- Handlers -------------------------------------------------------------
    def _apply_event(self, envelope: EventEnvelope) -> None:
        etype = envelope.event_type
        p = envelope.payload
        handler = self._HANDLERS.get(etype)
        if handler is None:
            # Unknown event type: flag data quality but do not crash the writer.
            from .base import DataQuality

            self.quality |= DataQuality.MISSING_FIELD
            return
        handler(self, p)

    def _h_lineup(self, p: Dict[str, Any]) -> None:
        team = p.get("team", "home")
        self.batting_order[team] = list(p.get("order", []))

    def _h_at_bat(self, p: Dict[str, Any]) -> None:
        if "batter" in p:
            self.batter = p["batter"]
        if "pitcher" in p:
            self.pitcher = p["pitcher"]
            self.bullpen_usage.setdefault(self.pitcher, 0)

    def _h_pitch(self, p: Dict[str, Any]) -> None:
        pitcher = p.get("pitcher", self.pitcher)
        n = int(p.get("count", 1))
        self.total_pitches += n
        if pitcher is not None:
            self.bullpen_usage[pitcher] = self.bullpen_usage.get(pitcher, 0) + n

    def _h_out(self, p: Dict[str, Any]) -> None:
        self.outs += int(p.get("outs", 1))

    def _h_score(self, p: Dict[str, Any]) -> None:
        team = p.get("team", "home")
        self.score[team] = self.score.get(team, 0) + int(p.get("runs", 1))

    def _h_bases(self, p: Dict[str, Any]) -> None:
        # payload {"1B": "playerX" | null, ...}; absent bases left unchanged.
        for b in BASES:
            if b in p:
                self.bases[b] = p[b]

    def _h_inning(self, p: Dict[str, Any]) -> None:
        if "inning" in p:
            self.inning = int(p["inning"])
        if "half" in p:
            self.half = p["half"]
        # New half-inning: reset outs and clear the bases.
        self.outs = 0
        self.bases = {b: None for b in BASES}

    def _h_pitching_change(self, p: Dict[str, Any]) -> None:
        self.pitcher = p.get("pitcher")
        if self.pitcher is not None:
            self.bullpen_usage.setdefault(self.pitcher, 0)

    def _h_status(self, p: Dict[str, Any]) -> None:
        from .base import DataQuality

        status = p.get("status", self.game_status)
        if status not in GAME_STATUSES:
            self.quality |= DataQuality.OUT_OF_RANGE
        self.game_status = status

    _HANDLERS = {
        "lineup": _h_lineup,
        "at_bat": _h_at_bat,
        "pitch": _h_pitch,
        "out": _h_out,
        "score": _h_score,
        "bases": _h_bases,
        "inning": _h_inning,
        "pitching_change": _h_pitching_change,
        "status": _h_status,
    }

    def _apply_snapshot(self, envelope: EventEnvelope) -> None:
        s = envelope.payload
        self.game_status = s.get("game_status", self.game_status)
        self.inning = int(s.get("inning", self.inning))
        self.half = s.get("half", self.half)
        self.outs = int(s.get("outs", self.outs))
        self.bases = {b: s.get("bases", {}).get(b) for b in BASES} if "bases" in s else self.bases
        self.score = dict(s.get("score", self.score))
        self.batter = s.get("batter", self.batter)
        self.pitcher = s.get("pitcher", self.pitcher)
        self.total_pitches = int(s.get("total_pitches", self.total_pitches))
        self.bullpen_usage = dict(s.get("bullpen_usage", self.bullpen_usage))
        if "batting_order" in s:
            self.batting_order = {k: list(v) for k, v in s["batting_order"].items()}

    def _apply_correction(self, envelope: EventEnvelope) -> None:
        # Corrections carry absolute values for the fields they restate.
        s = envelope.payload
        if "outs" in s:
            self.outs = int(s["outs"])
        if "score" in s:
            self.score.update(s["score"])
        if "bases" in s:
            for b in BASES:
                if b in s["bases"]:
                    self.bases[b] = s["bases"][b]
        if "inning" in s:
            self.inning = int(s["inning"])
        if "half" in s:
            self.half = s["half"]
        if "batter" in s:
            self.batter = s["batter"]
        if "pitcher" in s:
            self.pitcher = s["pitcher"]
        if "game_status" in s:
            self.game_status = s["game_status"]

    def _content(self) -> dict[str, Any]:
        current_pitch_count = self.bullpen_usage.get(self.pitcher, 0) if self.pitcher else 0
        return {
            "game_status": self.game_status,
            "inning": self.inning,
            "half": self.half,
            "outs": self.outs,
            "bases": dict(self.bases),
            "score": dict(self.score),
            "batter": self.batter,
            "pitcher": self.pitcher,
            "pitch_count": current_pitch_count,
            "total_pitches": self.total_pitches,
            "bullpen_usage": dict(self.bullpen_usage),
            "batting_order": {k: list(v) for k, v in self.batting_order.items()},
        }

"""Live NBA game state.

Single-writer, sequence-tracked model of one NBA game. All handlers are O(1)
field updates; no scans, no I/O.

Event types (``envelope.event_type``):

* ``period``        -- advance to a period; reset clock and team fouls.
* ``clock``         -- set the precise game clock (seconds remaining).
* ``score``         -- add points to a team.
* ``possession``    -- set the team in possession.
* ``timeout``       -- decrement a team's remaining timeouts.
* ``team_foul``     -- increment a team's foul count.
* ``player_foul``   -- increment a player's fouls (and the team's).
* ``substitution``  -- update players on court for a team.
* ``player_minutes``-- set a player's minutes played.
* ``status``        -- set game status.
* ``snapshot`` / ``correction`` -- recovery / restatement.
"""

from __future__ import annotations

from typing import Any, Dict, List

from streaming.event_envelope import EventEnvelope

from .base import LiveState

GAME_STATUSES = ("scheduled", "live", "halftime", "overtime", "final", "suspended")


class NBAGameState(LiveState):
    kind = "nba_game"
    require_snapshot_first = False

    def __init__(self, entity_id: str) -> None:
        super().__init__(entity_id)
        self.game_status: str = "scheduled"
        self.period: int = 1
        self.clock_seconds: float = 720.0  # 12:00 in a standard NBA period
        self.score: Dict[str, int] = {"home": 0, "away": 0}
        self.possession: str = ""  # "home" / "away" / ""
        self.timeouts: Dict[str, int] = {"home": 7, "away": 7}
        self.team_fouls: Dict[str, int] = {"home": 0, "away": 0}
        self.players_on_court: Dict[str, List[str]] = {"home": [], "away": []}
        self.player_fouls: Dict[str, int] = {}
        self.player_minutes: Dict[str, float] = {}

    # -- Handlers -------------------------------------------------------------
    def _apply_event(self, envelope: EventEnvelope) -> None:
        handler = self._HANDLERS.get(envelope.event_type)
        if handler is None:
            from .base import DataQuality

            self.quality |= DataQuality.MISSING_FIELD
            return
        handler(self, envelope.payload)

    def _h_period(self, p: Dict[str, Any]) -> None:
        if "period" in p:
            self.period = int(p["period"])
        # Fresh period: reset clock and per-period team fouls.
        self.clock_seconds = float(p.get("clock_seconds", 720.0))
        self.team_fouls = {"home": 0, "away": 0}

    def _h_clock(self, p: Dict[str, Any]) -> None:
        self.clock_seconds = float(p["clock_seconds"])

    def _h_score(self, p: Dict[str, Any]) -> None:
        team = p.get("team", "home")
        self.score[team] = self.score.get(team, 0) + int(p.get("points", 0))

    def _h_possession(self, p: Dict[str, Any]) -> None:
        self.possession = p.get("team", self.possession)

    def _h_timeout(self, p: Dict[str, Any]) -> None:
        from .base import DataQuality

        team = p.get("team", "home")
        remaining = self.timeouts.get(team, 0) - 1
        if remaining < 0:
            remaining = 0
            self.quality |= DataQuality.OUT_OF_RANGE
        self.timeouts[team] = remaining

    def _h_team_foul(self, p: Dict[str, Any]) -> None:
        team = p.get("team", "home")
        self.team_fouls[team] = self.team_fouls.get(team, 0) + int(p.get("count", 1))

    def _h_player_foul(self, p: Dict[str, Any]) -> None:
        player = p["player"]
        self.player_fouls[player] = self.player_fouls.get(player, 0) + 1
        if "team" in p:
            self.team_fouls[p["team"]] = self.team_fouls.get(p["team"], 0) + 1

    def _h_substitution(self, p: Dict[str, Any]) -> None:
        team = p.get("team", "home")
        on_court = list(self.players_on_court.get(team, []))
        for out_player in p.get("out", []):
            if out_player in on_court:
                on_court.remove(out_player)
        for in_player in p.get("in", []):
            if in_player not in on_court:
                on_court.append(in_player)
        self.players_on_court[team] = on_court

    def _h_player_minutes(self, p: Dict[str, Any]) -> None:
        self.player_minutes[p["player"]] = float(p["minutes"])

    def _h_status(self, p: Dict[str, Any]) -> None:
        from .base import DataQuality

        status = p.get("status", self.game_status)
        if status not in GAME_STATUSES:
            self.quality |= DataQuality.OUT_OF_RANGE
        self.game_status = status

    _HANDLERS = {
        "period": _h_period,
        "clock": _h_clock,
        "score": _h_score,
        "possession": _h_possession,
        "timeout": _h_timeout,
        "team_foul": _h_team_foul,
        "player_foul": _h_player_foul,
        "substitution": _h_substitution,
        "player_minutes": _h_player_minutes,
        "status": _h_status,
    }

    def _apply_snapshot(self, envelope: EventEnvelope) -> None:
        s = envelope.payload
        self.game_status = s.get("game_status", self.game_status)
        self.period = int(s.get("period", self.period))
        self.clock_seconds = float(s.get("clock_seconds", self.clock_seconds))
        self.score = dict(s.get("score", self.score))
        self.possession = s.get("possession", self.possession)
        self.timeouts = dict(s.get("timeouts", self.timeouts))
        self.team_fouls = dict(s.get("team_fouls", self.team_fouls))
        if "players_on_court" in s:
            self.players_on_court = {k: list(v) for k, v in s["players_on_court"].items()}
        self.player_fouls = dict(s.get("player_fouls", self.player_fouls))
        self.player_minutes = {k: float(v) for k, v in s.get("player_minutes", self.player_minutes).items()}

    def _apply_correction(self, envelope: EventEnvelope) -> None:
        s = envelope.payload
        if "score" in s:
            self.score.update(s["score"])
        if "clock_seconds" in s:
            self.clock_seconds = float(s["clock_seconds"])
        if "period" in s:
            self.period = int(s["period"])
        if "possession" in s:
            self.possession = s["possession"]
        if "timeouts" in s:
            self.timeouts.update(s["timeouts"])
        if "team_fouls" in s:
            self.team_fouls.update(s["team_fouls"])
        if "player_fouls" in s:
            self.player_fouls.update(s["player_fouls"])
        if "game_status" in s:
            self.game_status = s["game_status"]

    def _content(self) -> dict[str, Any]:
        return {
            "game_status": self.game_status,
            "period": self.period,
            "clock_seconds": self.clock_seconds,
            "score": dict(self.score),
            "possession": self.possession,
            "timeouts": dict(self.timeouts),
            "team_fouls": dict(self.team_fouls),
            "players_on_court": {k: list(v) for k, v in self.players_on_court.items()},
            "player_fouls": dict(self.player_fouls),
            "player_minutes": dict(self.player_minutes),
        }

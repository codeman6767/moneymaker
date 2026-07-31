"""Preserved-shape pilot fixtures for e017 identity extraction.

The payload shapes here are the ones the F1B rich pilots actually received (field
names and nesting verified against the preserved raw responses). They exist so a
provider payload-shape change breaks a test rather than silently returning zero
identities and re-opening the 0%-coverage hole.

Offline only; no ProviderResponse, no client, no socket.
"""

from __future__ import annotations

import json

from sports_quant.ingest.identity_extract import (
    endpoint_is_supported,
    extract_identities,
)

MLB = "mlb_statsapi"
NBA = "balldontlie"

# --- MLB preserved shapes -------------------------------------------------- #
MLB_SCHEDULE = json.dumps({"dates": [{"games": [{
    "gamePk": 822788, "officialDate": "2026-07-20",
    "teams": {
        "home": {"team": {"id": 141, "name": "Toronto Blue Jays",
                          "link": "/api/v1/teams/141"},
                 "probablePitcher": {"id": 680700, "fullName": "Jose Berrios"}},
        "away": {"team": {"id": 139, "name": "Tampa Bay Rays",
                          "link": "/api/v1/teams/139"}},
    },
}]}]})

MLB_BOXSCORE = json.dumps({"teams": {
    "away": {
        "team": {"id": 139, "name": "Tampa Bay Rays", "abbreviation": "TB",
                 "locationName": "Tampa Bay", "teamName": "Rays",
                 "clubName": "Rays", "shortName": "Tampa Bay", "teamCode": "tba"},
        "players": {
            "ID518886": {"person": {"id": 518886, "fullName": "Craig Kimbrel",
                                    "boxscoreName": "Kimbrel"},
                         "position": {"code": "1", "name": "Pitcher",
                                      "type": "Pitcher", "abbreviation": "P"},
                         "jerseyNumber": "46", "parentTeamId": 139},
        },
    },
    "home": {
        "team": {"id": 141, "name": "Toronto Blue Jays", "abbreviation": "TOR",
                 "locationName": "Toronto", "teamName": "Blue Jays"},
        "players": {
            "ID670764": {"person": {"id": 670764, "fullName": "Bo Bichette"},
                         "position": {"code": "6", "name": "Shortstop",
                                      "type": "Infielder", "abbreviation": "SS"},
                         "jerseyNumber": "11", "parentTeamId": 141},
        },
    },
}})

MLB_LINESCORE = json.dumps({
    "currentInning": 9, "teams": {"home": {"runs": 4}, "away": {"runs": 2}},
    "defense": {"pitcher": {"id": 518886, "fullName": "Craig Kimbrel"},
                "catcher": {"id": 665489, "fullName": "Alejandro Kirk"}},
    "offense": {"batter": {"id": 670764, "fullName": "Bo Bichette"}},
})

MLB_ROSTER = json.dumps({"teamId": 141, "rosterType": "active", "roster": [
    {"person": {"id": 670764, "fullName": "Bo Bichette"},
     "position": {"code": "6", "name": "Shortstop", "type": "Infielder",
                  "abbreviation": "SS"},
     "jerseyNumber": "11", "parentTeamId": 141, "status": {"code": "A"}},
    # A genuinely nameless entry: must be reported, never invented.
    {"person": {"id": 999999}, "parentTeamId": 141},
]})

# --- NBA preserved shapes -------------------------------------------------- #
_NYK = {"id": 20, "abbreviation": "NYK", "city": "New York", "conference": "East",
        "division": "Atlantic", "full_name": "New York Knicks", "name": "Knicks"}
_DET = {"id": 9, "abbreviation": "DET", "city": "Detroit", "conference": "East",
        "division": "Central", "full_name": "Detroit Pistons", "name": "Pistons"}
_CLARKSON = {"id": 100, "first_name": "Jordan", "last_name": "Clarkson",
             "position": "G", "jersey_number": "00", "team_id": 20,
             "height": "6-5", "weight": "194", "college": "Missouri",
             "country": "USA", "draft_year": 2014, "draft_round": 2,
             "draft_number": 46}

NBA_GAMES = json.dumps({"data": [{
    "id": 18447316, "date": "2026-01-05", "datetime": "2026-01-06T00:00:00.000Z",
    "season": 2025, "status": "Final", "period": 4,
    "home_team": _DET, "visitor_team": _NYK,
}], "meta": {}})

NBA_GAME = json.dumps({"data": {
    "id": 18447316, "date": "2026-01-05", "datetime": "2026-01-06T00:00:00.000Z",
    "season": 2025, "status": "Final", "home_team": _DET, "visitor_team": _NYK,
}})

NBA_BOX_SCORES = json.dumps({"data": [{
    "id": 18447316, "date": "2026-01-05", "season": 2025, "status": "Final",
    "home_team": {**_DET, "players": [
        {"player": {"id": 17896033, "first_name": "Cade", "last_name": "Cunningham",
                    "position": "G", "jersey_number": "2"}, "pts": 25},
    ]},
    "visitor_team": {**_NYK, "players": [{"player": _CLARKSON, "pts": 12}]},
}]})

NBA_STATS = json.dumps({"data": [
    {"id": 1, "player": _CLARKSON, "team": _NYK, "game": {"id": 18447316}, "pts": 12},
]})

NBA_ADVANCED = json.dumps({"data": [
    {"id": 1, "player": _CLARKSON, "team": _NYK, "game": {"id": 18447316},
     "pace": 99.1, "pie": 0.1},
]})

NBA_PLAYS = json.dumps({"data": [
    {"game_id": 18447316, "order": 1, "period": 1, "team": _NYK,
     "participants": [100, 17896033], "type": "Jump Ball", "text": "Jump ball"},
]})

# The FLAT lineup shape the offline parser repair established: one row per
# player, with the player object as a sibling of position/starter.
NBA_LINEUPS = json.dumps({"data": [
    {"id": 7001, "game_id": 18447316, "player": _CLARKSON, "team": _NYK,
     "position": "G", "starter": True},
]})


# --------------------------------------------------------------------------- #
def test_every_pilot_endpoint_family_is_supported() -> None:
    for endpoint in ("/schedule", "/game/822788/boxscore", "/game/822788/linescore",
                     "/teams/141/roster"):
        assert endpoint_is_supported(MLB, endpoint), endpoint
    for endpoint in ("/v1/games", "/v1/games/18447316", "/v1/box_scores", "/v1/stats",
                     "/nba/v1/stats/advanced", "/v1/plays", "/v1/lineups"):
        assert endpoint_is_supported(NBA, endpoint), endpoint


def test_mlb_pilot_fixture_resolves_its_pilot_entities() -> None:
    schedule = extract_identities(provider=MLB, endpoint="/schedule", body=MLB_SCHEDULE)
    assert {t.provider_team_id for t in schedule.teams} == {"139", "141"}
    assert {t.full_name for t in schedule.teams} == {"Tampa Bay Rays", "Toronto Blue Jays"}
    # The probable pitcher is a genuine identity object on the schedule.
    assert [(p.provider_player_id, p.full_name) for p in schedule.players] == [
        ("680700", "Jose Berrios")]

    box = extract_identities(provider=MLB, endpoint="/game/822788/boxscore",
                             body=MLB_BOXSCORE)
    assert {t.provider_team_id for t in box.teams} == {"139", "141"}
    rich = next(t for t in box.teams if t.provider_team_id == "141")
    assert (rich.abbreviation, rich.city, rich.nickname) == ("TOR", "Toronto", "Blue Jays")
    assert [(p.provider_player_id, p.full_name, p.position, p.provider_team_id)
            for p in box.players] == [
        ("518886", "Craig Kimbrel", "P", "139"),
        ("670764", "Bo Bichette", "SS", "141")]
    # StatsAPI supplies no parts and no birth date; none are invented.
    assert all(p.first_name is None and p.last_name is None and p.birth_date is None
               for p in box.players)

    line = extract_identities(provider=MLB, endpoint="/game/822788/linescore",
                              body=MLB_LINESCORE)
    assert {p.provider_player_id for p in line.players} == {"518886", "665489", "670764"}
    # Linescore team blocks hold runs/hits/errors, not team objects.
    assert line.teams == []


def test_mlb_roster_reports_the_nameless_entry_individually() -> None:
    roster = extract_identities(provider=MLB, endpoint="/teams/141/roster",
                               body=MLB_ROSTER)
    assert [(p.provider_player_id, p.full_name, p.provider_team_id)
            for p in roster.players] == [("670764", "Bo Bichette", "141")]
    assert [(r.kind, r.provider_entity_id) for r in roster.rejected] == [
        ("player", "999999")]


def test_nba_pilot_fixture_resolves_its_pilot_entities() -> None:
    for endpoint, body in (("/v1/games", NBA_GAMES), ("/v1/games/18447316", NBA_GAME)):
        out = extract_identities(provider=NBA, endpoint=endpoint, body=body)
        assert {t.provider_team_id for t in out.teams} == {"9", "20"}
        assert {t.full_name for t in out.teams} == {"Detroit Pistons", "New York Knicks"}
        assert out.players == []  # the games listing names no players

    box = extract_identities(provider=NBA, endpoint="/v1/box_scores", body=NBA_BOX_SCORES)
    assert {t.provider_team_id for t in box.teams} == {"9", "20"}
    assert [(p.provider_player_id, p.full_name, p.first_name, p.last_name, p.position)
            for p in box.players] == [
        ("100", "Jordan Clarkson", "Jordan", "Clarkson", "G"),
        ("17896033", "Cade Cunningham", "Cade", "Cunningham", "G")]
    assert all(p.birth_date is None for p in box.players)


def test_nba_flat_lineup_identities_do_not_duplicate_statistics_identities() -> None:
    """One player named by lineups, stats and advanced stats is ONE identity."""

    lineups = extract_identities(provider=NBA, endpoint="/v1/lineups", body=NBA_LINEUPS)
    stats = extract_identities(provider=NBA, endpoint="/v1/stats", body=NBA_STATS)
    advanced = extract_identities(provider=NBA, endpoint="/nba/v1/stats/advanced",
                                 body=NBA_ADVANCED)
    assert lineups.players == stats.players == advanced.players
    assert lineups.teams == stats.teams == advanced.teams
    assert [p.provider_player_id for p in lineups.players] == ["100"]


def test_nba_plays_contribute_a_team_but_never_a_nameless_player() -> None:
    plays = extract_identities(provider=NBA, endpoint="/v1/plays", body=NBA_PLAYS)
    assert [t.provider_team_id for t in plays.teams] == ["20"]
    assert plays.players == []
    assert plays.rejected == []


def test_extraction_is_order_independent_across_the_pilot_corpus() -> None:
    """Replaying the same responses in any order yields the same identity set."""

    corpus = [
        (MLB, "/schedule", MLB_SCHEDULE),
        (MLB, "/game/822788/boxscore", MLB_BOXSCORE),
        (MLB, "/game/822788/linescore", MLB_LINESCORE),
        (MLB, "/teams/141/roster", MLB_ROSTER),
    ]

    def collect(items: list[tuple[str, str, str]]) -> tuple[set, set]:
        teams: set = set()
        players: set = set()
        for provider, endpoint, body in items:
            out = extract_identities(provider=provider, endpoint=endpoint, body=body)
            teams |= {(t.provider_team_id, t.full_name) for t in out.teams}
            players |= {(p.provider_player_id, p.full_name) for p in out.players}
        return teams, players

    assert collect(corpus) == collect(list(reversed(corpus)))


def test_extraction_output_is_sorted_by_a_total_provider_id_order() -> None:
    """'1' and '01' are equal as integers but must still order deterministically."""

    body = json.dumps({"data": [
        {"player": {"id": "01", "first_name": "Zed", "last_name": "Zulu"},
         "team": _NYK},
        {"player": {"id": "1", "first_name": "Amy", "last_name": "Alpha"},
         "team": _NYK},
        {"player": {"id": "abc", "first_name": "Bob", "last_name": "Bravo"},
         "team": _NYK},
    ]})
    first = extract_identities(provider=NBA, endpoint="/v1/stats", body=body)
    reversed_body = json.dumps({"data": list(reversed(json.loads(body)["data"]))})
    second = extract_identities(provider=NBA, endpoint="/v1/stats", body=reversed_body)
    assert [p.provider_player_id for p in first.players] == [
        p.provider_player_id for p in second.players]
    assert len({p.provider_player_id for p in first.players}) == 3


def test_no_fixture_body_contains_a_credential() -> None:
    """The fixtures are payload bodies, never request metadata."""

    for body in (MLB_SCHEDULE, MLB_BOXSCORE, MLB_LINESCORE, MLB_ROSTER, NBA_GAMES,
                 NBA_GAME, NBA_BOX_SCORES, NBA_STATS, NBA_ADVANCED, NBA_PLAYS,
                 NBA_LINEUPS):
        low = body.lower()
        for bad in ("api_key", "apikey", "authorization", "bearer", "x-api-key",
                    "secret", "password", "token"):
            assert bad not in low

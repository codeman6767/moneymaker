"""Tests for injury / lineup / material-news intelligence (Module 4).

Covers the seven required scenarios -- duplicated report, corrected report,
ambiguous player, late scratch, conflicting sources, official confirmation,
report published after prediction time -- plus the core invariants (append-only
history, deterministic matching, confidence gating, material-only triggering).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from intel import (
    ChangeType,
    LineupAdapter,
    MatchStatus,
    MaterialChangeDetector,
    MLBProbablePitcherAdapter,
    NBAInjuryReportAdapter,
    PlayerDirectory,
    PlayerStatus,
    Projection,
    ReportRegistry,
    Severity,
    SocialNewsAdapter,
    SourceMeta,
    SourceType,
    StatusHistory,
    TeamAnnouncementAdapter,
    UnauthorizedSourceError,
    active_probability,
    expected_minutes,
    is_actionable,
    score_source,
)

T = datetime(2026, 7, 1, 17, 0, tzinfo=timezone.utc)


def make_directory() -> PlayerDirectory:
    d = PlayerDirectory()
    d.add("Luka Dončić", "DAL", "p_luka")
    d.add("Jaylen Brown", "BOS", "p_jb")
    d.add("Jalen Williams", "OKC", "p_jw1")
    d.add("Jalen Williams", "OKC", "p_jw2")  # genuinely ambiguous
    d.add("Gerrit Cole", "NYY", "p_cole")
    return d


def luka_key(d: PlayerDirectory) -> str:
    return d.match("Luka Doncic", "DAL").player.key()


def injury_report(published_at, players):
    return {"published_at": published_at.isoformat(), "players": players}


# --------------------------------------------------------------------------- #
# Deterministic matching + ambiguous player
# --------------------------------------------------------------------------- #
def test_deterministic_matching_and_ambiguity():
    d = make_directory()
    # Accent/suffix-insensitive exact match.
    assert d.match("Luka Doncic", "DAL").status is MatchStatus.MATCHED
    assert d.match("luka  doncic jr", "DAL").player.player_id == "p_luka"
    # Two players share a normalized name on the same team -> ambiguous.
    amb = d.match("Jalen Williams", "OKC")
    assert amb.status is MatchStatus.AMBIGUOUS
    assert len(amb.candidates) == 2
    # Unknown player -> unmatched, never guessed.
    assert d.match("Nobody Here", "DAL").status is MatchStatus.UNMATCHED


def test_ambiguous_player_not_assigned():
    d = make_directory()
    adapter = NBAInjuryReportAdapter(d)
    result = adapter.parse(
        injury_report(T, [{"name": "Jalen Williams", "team": "OKC", "status": "Out", "reason": "knee"}]),
        retrieved_at=T,
    )
    # No snapshot fabricated; the row is surfaced as unresolved for review.
    assert result.snapshots == []
    assert len(result.unresolved) == 1
    assert result.unresolved[0].match_status is MatchStatus.AMBIGUOUS


# --------------------------------------------------------------------------- #
# Extraction + confidence
# --------------------------------------------------------------------------- #
def test_extraction_and_confidence():
    d = make_directory()
    adapter = NBAInjuryReportAdapter(d)
    result = adapter.parse(
        injury_report(T, [{"name": "Luka Doncic", "team": "DAL", "status": "Questionable", "reason": "ankle"}]),
        retrieved_at=T + timedelta(seconds=3),
    )
    snap = result.snapshots[0]
    assert snap.player.player_id == "p_luka"
    assert snap.status is PlayerStatus.QUESTIONABLE
    assert snap.reason == "ankle"
    # publication and retrieval timestamps are distinct and both stored.
    assert snap.source.published_at == T
    assert snap.source.retrieved_at == T + timedelta(seconds=3)
    # Official league scores above social.
    assert snap.confidence > 0.9


# --------------------------------------------------------------------------- #
# Duplicated report -> no new storage, no trigger
# --------------------------------------------------------------------------- #
def test_duplicated_report_detected():
    d = make_directory()
    adapter = NBAInjuryReportAdapter(d)
    registry = ReportRegistry()
    raw = injury_report(T, [{"name": "Luka Doncic", "team": "DAL", "status": "Out", "reason": "rest"}])

    first = adapter.poll(raw, now=T, registry=registry)
    second = adapter.poll(raw, now=T + timedelta(minutes=30), registry=registry)

    assert first.is_new is True and first.result.snapshots
    assert second.is_new is False and second.result is None  # same file, not re-stored

    # Even if the same snapshot reaches the detector twice, it is a no-op.
    detector = MaterialChangeDetector()
    snap = first.result.snapshots[0]
    assert detector.ingest(snap, now=T) is not None
    assert detector.ingest(snap, now=T) is None
    assert len(detector.history.history(snap.subject_key)) == 1


# --------------------------------------------------------------------------- #
# Corrected report -> both preserved (never overwritten)
# --------------------------------------------------------------------------- #
def test_corrected_report_preserves_history():
    d = make_directory()
    adapter = NBAInjuryReportAdapter(d)
    detector = MaterialChangeDetector()
    subject = luka_key(d)
    detector.set_projection(Projection(subject_key=subject, projected_status=PlayerStatus.AVAILABLE, projected_minutes=34))

    r1 = adapter.parse(injury_report(T, [{"name": "Luka Doncic", "team": "DAL", "status": "Questionable"}]), T)
    r2 = adapter.parse(injury_report(T + timedelta(hours=2), [{"name": "Luka Doncic", "team": "DAL", "status": "Out"}]), T + timedelta(hours=2))

    c1 = detector.ingest(r1.snapshots[0], now=T)
    c2 = detector.ingest(r2.snapshots[0], now=T + timedelta(hours=2))

    assert c1 is not None and c2 is not None
    assert c2.change_type is ChangeType.PLAYER_RULED_OUT
    # Both observations are retained; the earlier one is not overwritten.
    hist = detector.history.history(subject)
    assert [s.status for s in hist] == [PlayerStatus.QUESTIONABLE, PlayerStatus.OUT]


# --------------------------------------------------------------------------- #
# Material-only triggering + probability/minutes estimation
# --------------------------------------------------------------------------- #
def test_no_trigger_when_nothing_material_changes():
    d = make_directory()
    detector = MaterialChangeDetector()
    subject = luka_key(d)
    detector.set_projection(Projection(subject_key=subject, projected_status=PlayerStatus.AVAILABLE, projected_minutes=34))
    adapter = NBAInjuryReportAdapter(d)
    # Official says AVAILABLE, which matches the projection -> not material.
    snap = adapter.parse(injury_report(T, [{"name": "Luka Doncic", "team": "DAL", "status": "Available"}]), T).snapshots[0]
    assert detector.ingest(snap, now=T) is None


def test_estimates_active_probability_and_minutes():
    d = make_directory()
    detector = MaterialChangeDetector()
    subject = luka_key(d)
    detector.set_projection(Projection(subject_key=subject, projected_status=PlayerStatus.AVAILABLE, projected_minutes=34))
    adapter = NBAInjuryReportAdapter(d)
    snap = adapter.parse(injury_report(T, [{"name": "Luka Doncic", "team": "DAL", "status": "Out"}]), T).snapshots[0]

    change = detector.ingest(snap, now=T)
    assert change.active_probability_before == pytest.approx(0.99)
    assert change.active_probability_after == 0.0
    assert change.expected_minutes_after == 0.0
    # Before/after model inputs are both captured.
    assert change.model_inputs_before["status"] == "available"
    assert change.model_inputs_after["status"] == "out"

    # Direct estimator sanity.
    assert active_probability(PlayerStatus.OUT) == 0.0
    assert expected_minutes(PlayerStatus.AVAILABLE, projected_minutes=30, snapshot_minutes=None, minutes_restriction=None) == pytest.approx(29.7)
    assert expected_minutes(PlayerStatus.AVAILABLE, projected_minutes=30, snapshot_minutes=None, minutes_restriction=20) == pytest.approx(19.8)


# --------------------------------------------------------------------------- #
# Late scratch (starting pitcher)
# --------------------------------------------------------------------------- #
def test_late_scratch_starting_pitcher():
    d = make_directory()
    detector = MaterialChangeDetector()
    adapter = MLBProbablePitcherAdapter(d)
    game_time = T + timedelta(minutes=30)
    published = T
    raw = {
        "published_at": published.isoformat(),
        "game_id": "mlb-1",
        "probables": [{"pitcher": "Gerrit Cole", "team": "NYY", "status": "scratched", "reason": "elbow"}],
    }
    snap = adapter.parse(raw, retrieved_at=T).snapshots[0]
    detector.set_projection(Projection(subject_key=snap.subject_key, projected_status=PlayerStatus.PROBABLE_STARTER, role="starting_pitcher"))

    change = detector.ingest(snap, now=T)
    assert change.change_type is ChangeType.STARTING_PITCHER_SCRATCHED
    alert = detector.to_alert(change, now=T, game_time=game_time)
    assert alert.severity is Severity.CRITICAL
    assert "LATE" in alert.message
    assert alert.actionable is True  # official league feed


# --------------------------------------------------------------------------- #
# Conflicting sources
# --------------------------------------------------------------------------- #
def test_conflicting_sources():
    d = make_directory()
    detector = MaterialChangeDetector()
    subject = luka_key(d)
    detector.set_projection(Projection(subject_key=subject, projected_status=PlayerStatus.AVAILABLE, projected_minutes=34))

    official = NBAInjuryReportAdapter(d)
    social = SocialNewsAdapter(d, authorized_authors=["@insider"])

    off_snap = official.parse(injury_report(T, [{"name": "Luka Doncic", "team": "DAL", "status": "Out"}]), T).snapshots[0]
    detector.ingest(off_snap, now=T)

    soc_raw = {"author": "@insider", "player": "Luka Doncic", "team": "DAL", "status": "available", "published_at": (T + timedelta(minutes=5)).isoformat()}
    soc_snap = social.parse(soc_raw, retrieved_at=T + timedelta(minutes=5)).snapshots[0]
    change = detector.ingest(soc_snap, now=T + timedelta(minutes=5))

    assert change.conflict is True
    assert set(change.conflicting_sources) == {off_snap.source.source_id, soc_snap.source.source_id}
    alert = detector.to_alert(change, now=T + timedelta(minutes=5))
    # Conflicting + unconfirmed social => not auto-actionable.
    assert alert.actionable is False


# --------------------------------------------------------------------------- #
# Official confirmation
# --------------------------------------------------------------------------- #
def test_official_confirmation_upgrades_and_is_actionable():
    d = make_directory()
    detector = MaterialChangeDetector()
    subject = luka_key(d)
    detector.set_projection(Projection(subject_key=subject, projected_status=PlayerStatus.AVAILABLE, projected_minutes=34))

    social = SocialNewsAdapter(d, authorized_authors=["@insider"])
    team = TeamAnnouncementAdapter(d)

    soc = social.parse({"author": "@insider", "player": "Luka Doncic", "team": "DAL", "status": "out", "published_at": T.isoformat()}, T).snapshots[0]
    first = detector.ingest(soc, now=T)
    assert first.requires_confirmation is True  # social alone

    ann = team.parse({"player": "Luka Doncic", "team": "DAL", "status": "out", "published_at": (T + timedelta(minutes=10)).isoformat()}, T + timedelta(minutes=10)).snapshots[0]
    change = detector.ingest(ann, now=T + timedelta(minutes=10))

    assert change is not None
    assert change.official_confirmation is True
    alert = detector.to_alert(change, now=T + timedelta(minutes=10))
    assert alert.actionable is True


# --------------------------------------------------------------------------- #
# Report published after prediction time
# --------------------------------------------------------------------------- #
def test_report_published_after_prediction_time():
    d = make_directory()
    detector = MaterialChangeDetector()
    subject = luka_key(d)
    prediction_time = T
    detector.set_projection(
        Projection(subject_key=subject, projected_status=PlayerStatus.AVAILABLE, projected_minutes=34, prediction_time=prediction_time)
    )
    adapter = NBAInjuryReportAdapter(d)
    # Published an hour AFTER the prediction was computed.
    snap = adapter.parse(injury_report(T + timedelta(hours=1), [{"name": "Luka Doncic", "team": "DAL", "status": "Out"}]), T + timedelta(hours=1)).snapshots[0]
    change = detector.ingest(snap, now=T + timedelta(hours=1))
    assert change.published_after_prediction is True


# --------------------------------------------------------------------------- #
# Social: authorization + never auto-actionable
# --------------------------------------------------------------------------- #
def test_social_requires_authorization():
    d = make_directory()
    social = SocialNewsAdapter(d, authorized_authors=["@insider"])
    with pytest.raises(UnauthorizedSourceError):
        social.parse({"author": "@random_scraper", "player": "Luka Doncic", "team": "DAL", "status": "out"}, T)


def test_social_never_actionable_without_confirmation():
    d = make_directory()
    social = SocialNewsAdapter(d, authorized_authors=["@insider"])
    snap = social.parse({"author": "@insider", "player": "Luka Doncic", "team": "DAL", "status": "out", "published_at": T.isoformat()}, T).snapshots[0]
    assert is_actionable(snap.confidence, snap.source) is False
    # Confidence ordering: official beats social.
    assert score_source(snap.source) < 0.85


# --------------------------------------------------------------------------- #
# Lineup differs from projected + game postponed
# --------------------------------------------------------------------------- #
def test_confirmed_lineup_differs_from_projected():
    d = make_directory()
    detector = MaterialChangeDetector()
    adapter = LineupAdapter(d)
    # Project a lineup with Jaylen Brown; confirmed lineup swaps in Luka instead.
    projected = frozenset({d.match("Jaylen Brown", "BOS").player.key()})
    subject = "bos-1:lineup:BOS"
    detector.set_projection(Projection(subject_key=subject, projected_lineup_keys=projected))

    raw = {"published_at": T.isoformat(), "game_id": "bos-1", "team": "BOS", "confirmed": True,
           "lineup": [{"name": "Luka Doncic"}]}
    result = adapter.parse(raw, retrieved_at=T)
    change = detector.confirmed_lineup_change(result.lineup, now=T)
    assert change.change_type is ChangeType.CONFIRMED_DIFFERS_FROM_PROJECTED
    assert change.official_confirmation is True


def test_game_postponed():
    detector = MaterialChangeDetector()
    source = SourceMeta(
        source_id="nba_official", source_type=SourceType.OFFICIAL_LEAGUE,
        published_at=T, retrieved_at=T, confirmed=True,
    )
    change = detector.game_postponed("nba-1", source, now=T)
    assert change.change_type is ChangeType.GAME_POSTPONED
    alert = detector.to_alert(change, now=T)
    assert alert.severity is Severity.CRITICAL


# --------------------------------------------------------------------------- #
# Append-only history never overwrites
# --------------------------------------------------------------------------- #
def test_history_is_append_only():
    history = StatusHistory()
    d = make_directory()
    adapter = NBAInjuryReportAdapter(d)
    subject = luka_key(d)
    for i, status in enumerate(("Available", "Questionable", "Out", "Available")):
        # Distinct publication times => four genuinely distinct observations.
        snap = adapter.parse(injury_report(T + timedelta(minutes=i), [{"name": "Luka Doncic", "team": "DAL", "status": status}]), T).snapshots[0]
        history.append(snap)
    hist = history.history(subject)
    assert len(hist) == 4
    assert history.latest(subject).status is PlayerStatus.AVAILABLE


# --------------------------------------------------------------------------- #
# Polling cadence
# --------------------------------------------------------------------------- #
def test_poll_schedule_due_on_release():
    d = make_directory()
    adapter = NBAInjuryReportAdapter(d)
    # A release time is 17:00 UTC; due if we last polled before it.
    due = adapter.is_due(now=datetime(2026, 7, 1, 17, 1, tzinfo=timezone.utc),
                         last_polled=datetime(2026, 7, 1, 16, 30, tzinfo=timezone.utc))
    assert due is True
    not_due = adapter.is_due(now=datetime(2026, 7, 1, 17, 1, tzinfo=timezone.utc),
                            last_polled=datetime(2026, 7, 1, 17, 0, 30, tzinfo=timezone.utc))
    assert not_due is False

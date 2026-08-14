"""NBA Lane-R completion-evidence policy, derivation and materialization.

The load-bearing tests are the refusals. A derivation that accepts a truncated,
mis-ordered or mixed payload would silently hand F1-R a completion instant for a
game that did not end there -- and the real March corpus contains exactly that
shape, so this is not a hypothetical.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.models import RawResponse
from sports_quant.db.repositories.raw_responses import (
    SqliteRawResponseRepository,
    body_hash,
)
from sports_quant.retrospective.nba_completion import (
    NBA_COMPLETION_CLASSIFICATION,
    NBA_COMPLETION_ENDPOINT,
    NBA_COMPLETION_POLICY,
    NBA_COMPLETION_POLICY_VERSION,
    NBA_COMPLETION_PROVIDER,
    CompletionEvidenceError,
    MaterializationOutcome,
    derive_completion_evidence,
    find_completion_payload,
    materialize_completion_evidence,
)

GAME = "18447686"
TIP = "2026-03-01T18:11:07.000Z"
END = "2026-03-01T20:36:10.000Z"
#: END + 6h, the instant the conservative rule makes the fact knowable.
EFFECTIVE = "2026-03-02T02:36:10.000000Z"


def play(order: int, *, period: int = 1, wallclock: str = TIP,
         type_: str = "Jumpball", game_id: str = GAME,
         clock: str = "12:00", home: int = 0, away: int = 0) -> dict[str, Any]:
    # Scores are always present in the real feed and are what corroborates the
    # terminal play, so fixtures carry them too.
    return {"game_id": int(game_id), "order": order, "type": type_,
            "period": period, "clock": clock, "wallclock": wallclock,
            "home_score": home, "away_score": away}


def payload(plays: list[dict[str, Any]]) -> str:
    return json.dumps({"data": plays})


def good_plays() -> list[dict[str, Any]]:
    """A minimal but structurally valid completed NBA game."""

    return [
        play(1, period=1, wallclock=TIP, home=0, away=0),
        play(2, period=2, wallclock="2026-03-01T19:05:00.000Z", home=48, away=44),
        play(3, period=4, wallclock="2026-03-01T20:36:09.000Z", home=110, away=104),
        play(4, period=4, wallclock=END, type_="End Game", clock="0.0",
             home=112, away=104),
    ]


def raw(body: str, *, provider: str = NBA_COMPLETION_PROVIDER,
        endpoint: str = NBA_COMPLETION_ENDPOINT, game_id: str = GAME,
        http_status: int = 200,
        raw_id: str = "raw_01TESTTESTTESTTESTTESTTEST") -> RawResponse:
    return RawResponse(
        raw_response_id=raw_id, run_id="run_x", provider=provider,
        endpoint=endpoint,
        request_params_json=json.dumps({"game_id": game_id, "per_page": "100"}),
        http_status=http_status, response_headers_json="{}",
        requested_at="2026-08-04T22:12:13.473194Z",
        received_at="2026-08-04T22:12:13.658496Z", elapsed_ns=1,
        body=body, body_bytes=len(body.encode()), body_hash=body_hash(body),
        content_hash="c" * 64, created_at="2026-08-04T22:12:13.852547Z",
        http_method="GET", content_type="application/json")


@contextmanager
def db(path: Path) -> Iterator[sqlite3.Connection]:
    initialize_database(path)
    with Database(path).connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        yield conn


def store(conn: sqlite3.Connection, r: RawResponse) -> None:
    cols = ("raw_response_id, run_id, provider, endpoint, request_params_json, "
            "http_method, http_status, response_headers_json, content_type, "
            "requested_at, received_at, elapsed_ns, body, body_bytes, "
            "body_hash, content_hash, created_at")
    conn.execute(
        f"INSERT INTO raw_responses ({cols}) VALUES ({','.join('?' * 17)})",
        (r.raw_response_id, r.run_id, r.provider, r.endpoint,
         r.request_params_json, r.http_method, r.http_status,
         r.response_headers_json, r.content_type, r.requested_at, r.received_at,
         r.elapsed_ns, r.body, r.body_bytes, r.body_hash, r.content_hash,
         r.created_at))


# --------------------------------------------------------------------------- #
# 1-3. The happy path, and the strength claim it makes
# --------------------------------------------------------------------------- #
def test_valid_evidence_derives_the_final_play_instant() -> None:
    evidence = derive_completion_evidence(raw(payload(good_plays())))
    assert evidence.source_event_completed_at == "2026-03-01T20:36:10.000000Z"
    assert evidence.provider_game_id == GAME
    assert evidence.terminal_play_order == 4
    assert evidence.play_count == 4
    assert evidence.league_id == "lg_nba"


def test_the_derived_instant_is_the_maximum_valid_wallclock() -> None:
    evidence = derive_completion_evidence(raw(payload(good_plays())))
    stamps = [p["wallclock"] for p in good_plays()]
    assert evidence.source_event_completed_at.startswith(max(stamps)[:19])


def test_the_policy_reports_a_lower_bound_not_an_official_final() -> None:
    """The strength claim must survive in the artefact, not just the docstring."""

    evidence = derive_completion_evidence(raw(payload(good_plays())))
    assert evidence.classification == "defensible_derived_lower_bound"
    assert evidence.policy_version == NBA_COMPLETION_POLICY_VERSION
    assert NBA_COMPLETION_CLASSIFICATION == "defensible_derived_lower_bound"
    lowered = NBA_COMPLETION_POLICY.lower()
    assert "lower-bound" in lowered
    assert "not an official-final timestamp" in lowered
    # And it must never claim equivalence with the official final.
    for overclaim in ("official final time", "is the official",
                      "equals the official", "direct completion"):
        assert overclaim not in lowered, overclaim


# --------------------------------------------------------------------------- #
# 4-16. Fail-closed refusals
# --------------------------------------------------------------------------- #
def test_a_non_nba_provider_is_refused() -> None:
    with pytest.raises(CompletionEvidenceError, match="NBA"):
        derive_completion_evidence(
            raw(payload(good_plays()), provider="mlb_statsapi"))


def test_an_mlb_style_endpoint_is_refused() -> None:
    with pytest.raises(CompletionEvidenceError, match="endpoint"):
        derive_completion_evidence(
            raw(payload(good_plays()), endpoint="/game/1/boxscore"))


def test_a_non_success_response_is_not_evidence() -> None:
    with pytest.raises(CompletionEvidenceError, match="http_status"):
        derive_completion_evidence(raw(payload(good_plays()), http_status=404))


def test_a_payload_for_another_game_is_refused() -> None:
    with pytest.raises(CompletionEvidenceError, match="asked for"):
        derive_completion_evidence(
            raw(payload(good_plays()), game_id="99999999"))


def test_malformed_json_is_refused() -> None:
    with pytest.raises(CompletionEvidenceError, match="not valid JSON"):
        derive_completion_evidence(raw("{not json"))


def test_empty_plays_are_refused() -> None:
    with pytest.raises(CompletionEvidenceError, match="no plays"):
        derive_completion_evidence(raw(payload([])))


def test_a_missing_wallclock_is_refused_not_inferred() -> None:
    plays = good_plays()
    del plays[2]["wallclock"]
    with pytest.raises(CompletionEvidenceError, match="wallclock"):
        derive_completion_evidence(raw(payload(plays)))


def test_a_malformed_wallclock_is_refused() -> None:
    plays = good_plays()
    plays[1]["wallclock"] = "2026-13-45T99:99:99Z"
    with pytest.raises(CompletionEvidenceError, match="not a parseable"):
        derive_completion_evidence(raw(payload(plays)))


@pytest.mark.parametrize("naive", [
    "2026-03-01T20:36:10.000", "2026-03-01 20:36:10", "2026-03-01T20:36:10",
])
def test_a_naive_wallclock_is_refused(naive: str) -> None:
    """A timestamp with no zone has no defensible instant."""

    plays = good_plays()
    plays[-1]["wallclock"] = naive
    with pytest.raises(CompletionEvidenceError, match="no timezone"):
        derive_completion_evidence(raw(payload(plays)))


def test_non_monotonic_wallclock_is_refused() -> None:
    plays = good_plays()
    plays[2]["wallclock"] = "2026-03-01T17:00:00.000Z"   # before the tip
    with pytest.raises(CompletionEvidenceError, match="decreases"):
        derive_completion_evidence(raw(payload(plays)))


def test_a_truncated_payload_with_no_end_game_is_refused() -> None:
    plays = good_plays()[:-1]                             # drop the End Game
    with pytest.raises(CompletionEvidenceError, match="truncated"):
        derive_completion_evidence(raw(payload(plays)))


def test_contradictory_game_ids_inside_the_payload_are_refused() -> None:
    plays = good_plays()
    plays[1]["game_id"] = 42
    with pytest.raises(CompletionEvidenceError, match="mixes game ids"):
        derive_completion_evidence(raw(payload(plays)))


def test_duplicate_play_orders_are_refused() -> None:
    plays = good_plays()
    plays[1]["order"] = plays[0]["order"]
    with pytest.raises(CompletionEvidenceError, match="not unique"):
        derive_completion_evidence(raw(payload(plays)))


def test_two_end_game_plays_are_refused() -> None:
    plays = good_plays()
    plays[2]["type"] = "End Game"
    with pytest.raises(CompletionEvidenceError, match="cannot identify a single"):
        derive_completion_evidence(raw(payload(plays)))


def test_the_real_corpus_shape_of_misordered_evidence_is_refused() -> None:
    """The exact defect found in NBA 2026-03 games 18447741 and 18447742.

    `End Game` sits mid-sequence with later-ordered plays from an EARLIER period
    carrying later wallclocks. Wallclock is monotonic in order, so a monotonicity
    check alone passes it; the period regression is what exposes it.
    """

    plays = [
        play(1, period=1, wallclock="2026-03-01T18:11:00.000Z", home=2, away=0),
        play(2, period=4, wallclock="2026-03-01T20:36:27.000Z",
             type_="End Game", clock="0.0", home=121, away=110),
        play(3, period=3, wallclock="2026-03-01T20:59:22.000Z",
             type_="End Period", clock="0.0", home=103, away=80),
    ]
    with pytest.raises(CompletionEvidenceError):
        derive_completion_evidence(raw(payload(plays)))


def test_end_game_that_is_not_the_last_play_is_refused() -> None:
    plays = [
        play(1, period=1, wallclock="2026-03-01T18:11:00.000Z", home=2, away=0),
        play(2, period=4, wallclock="2026-03-01T20:36:00.000Z",
             type_="End Game", clock="0.0", home=110, away=104),
        play(3, period=4, wallclock="2026-03-01T20:40:00.000Z",
             home=110, away=104),
    ]
    with pytest.raises(CompletionEvidenceError, match="not the last play"):
        derive_completion_evidence(raw(payload(plays)))


def test_conflicting_duplicate_evidence_for_one_game_is_refused(
    tmp_path: Path,
) -> None:
    with db(tmp_path / "src.db") as conn:
        with transaction(conn):
            store(conn, raw(payload(good_plays()), raw_id="raw_01AAAAAAAAAAAAAAAAAAAAAAAA"))
            store(conn, raw(payload(good_plays()), raw_id="raw_01BBBBBBBBBBBBBBBBBBBBBBBB"))
        with pytest.raises(CompletionEvidenceError, match="refusing to choose"):
            find_completion_payload(conn, provider_game_id=GAME)


# --------------------------------------------------------------------------- #
# 17-23. Materialization
# --------------------------------------------------------------------------- #
def test_materialization_preserves_the_payload_and_its_timestamps(
    tmp_path: Path,
) -> None:
    original = raw(payload(good_plays()))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            result = materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        assert result.outcome is MaterializationOutcome.CREATED

        copied = SqliteRawResponseRepository(dst).get(original.raw_response_id)
        assert copied is not None
        # Body and hashes byte-identical.
        assert copied.body == original.body
        assert copied.body_hash == original.body_hash
        assert copied.content_hash == original.content_hash
        # Receipt/observation metadata is the AUGUST collection time, untouched.
        assert copied.received_at == "2026-08-04T22:12:13.658496Z"
        assert copied.requested_at == "2026-08-04T22:12:13.473194Z"
        assert copied.created_at == "2026-08-04T22:12:13.852547Z"
        # Identity survives, so the certification cites the same row.
        assert copied.raw_response_id == original.raw_response_id


def test_the_derived_instant_is_never_written_over_receipt_metadata(
    tmp_path: Path,
) -> None:
    """The central honesty property: two different concepts stay different."""

    original = raw(payload(good_plays()))
    evidence = derive_completion_evidence(original)
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        copied = SqliteRawResponseRepository(dst).get(original.raw_response_id)

    assert copied is not None
    assert evidence.source_event_completed_at.startswith("2026-03-01")
    assert copied.received_at.startswith("2026-08-04")
    assert copied.received_at != evidence.source_event_completed_at


def test_materialization_is_idempotent(tmp_path: Path) -> None:
    original = raw(payload(good_plays()))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            first = materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        with transaction(dst):
            second = materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        assert first.outcome is MaterializationOutcome.CREATED
        assert second.outcome is MaterializationOutcome.REUSED
        assert dst.execute(
            "SELECT COUNT(*) FROM raw_responses").fetchone()[0] == 1


def test_a_conflicting_destination_row_fails_rather_than_overwrites(
    tmp_path: Path,
) -> None:
    original = raw(payload(good_plays()))
    tampered = raw(payload(good_plays()[:2] + [
        play(3, period=4, wallclock="2026-03-01T21:00:00.000Z",
             type_="End Game", clock="0.0", home=112, away=104)]))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            store(dst, tampered)          # same id, different body
        with pytest.raises(CompletionEvidenceError, match="Refusing to overwrite"):
            with transaction(dst):
                materialize_completion_evidence(
                    src, dst, raw_response_id=original.raw_response_id)
        # The pre-existing row is untouched.
        survivor = SqliteRawResponseRepository(dst).get(original.raw_response_id)
        assert survivor is not None
        assert survivor.body == tampered.body


def test_materialization_never_synthesizes_a_game_status_history_row(
    tmp_path: Path,
) -> None:
    original = raw(payload(good_plays()))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        assert dst.execute(
            "SELECT COUNT(*) FROM game_status_history").fetchone()[0] == 0
        assert src.execute(
            "SELECT COUNT(*) FROM game_status_history").fetchone()[0] == 0


def test_materialization_never_writes_to_the_source(tmp_path: Path) -> None:
    """Traced at the SQL level on the source connection."""

    original = raw(payload(good_plays()))
    statements: list[str] = []
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        src.set_trace_callback(statements.append)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        src.set_trace_callback(None)
    executed = " ".join(statements).lower()
    for write in ("insert ", "update ", "delete ", "drop ", "alter "):
        assert write not in executed, f"source received a {write.strip()}"


def test_a_missing_source_row_is_refused(tmp_path: Path) -> None:
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with pytest.raises(CompletionEvidenceError, match="does not exist"):
            with transaction(dst):
                materialize_completion_evidence(
                    src, dst, raw_response_id="raw_01NOPENOPENOPENOPENOPENOPE")


# --------------------------------------------------------------------------- #
# 24-31. The full v19 certification path, end to end
# --------------------------------------------------------------------------- #
def _certified_reader_case(tmp_path: Path, cutoff: str) -> Any:
    """Build the whole reviewed chain on a disposable database and read it."""

    from sports_quant.db.repositories.retrospective import (
        SqliteRetrospectiveProvenanceRepository,
    )
    from sports_quant.retrospective.provenance import (
        AvailabilityBasis,
        EligibilityVerdict,
        EntityType,
        G1Variant,
        ProvenanceClass,
        ProviderNamespace,
    )
    from sports_quant.retrospective.reader import RetrospectiveResearchReader

    ns = ProviderNamespace("lg_nba", "balldontlie", EntityType.GAME, "v1")
    original = raw(payload(good_plays()))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        evidence = derive_completion_evidence(original)
        with transaction(dst):
            materialized = materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
            repo = SqliteRetrospectiveProvenanceRepository(dst)
            corpus = repo.record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id="lg_nba", reconstruction_policy_version="nba-comp-test",
                cutoff_policy_id="pregame", cutoff_policy_version="1",
                source_corpus_digest="src", target_set_digest="t",
                g1_variant=G1Variant.G1_B_CORE, code_version="test")
            repo.certify_input(
                corpus_version_id=corpus.corpus_version_id, namespace=ns,
                provider_game_id="18447999",         # a LATER target game
                feature_family="prior_results",
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                reconstruction_policy_version="nba-comp-test",
                eligibility=EligibilityVerdict.ELIGIBLE,
                availability_basis=AvailabilityBasis.EVENT_DERIVED,
                availability_rule_id="prior_event_completion_conservative_v1",
                availability_source=NBA_COMPLETION_POLICY,
                source_evidence_table="raw_responses",
                source_evidence_id=materialized.raw_response_id,
                source_event_completed_at=evidence.source_event_completed_at)
        reader = RetrospectiveResearchReader(
            dst, corpus_version_id=corpus.corpus_version_id, cutoff=cutoff)
        return reader.admit_feature(namespace=ns, provider_game_id="18447999",
                                    feature_family="prior_results")


def test_the_full_v19_path_admits_at_completion_plus_six_hours(
    tmp_path: Path,
) -> None:
    from sports_quant.retrospective.reader import AdmittedInput

    decision = _certified_reader_case(tmp_path, EFFECTIVE)
    assert isinstance(decision, AdmittedInput), decision
    assert decision.effective_at == EFFECTIVE
    assert decision.availability_rule_id == "prior_event_completion_conservative_v1"
    assert decision.certification.source_evidence_table == "raw_responses"
    # The stored availability source carries the versioned policy.
    assert NBA_COMPLETION_POLICY_VERSION in (
        decision.certification.availability_source or "")


@pytest.mark.parametrize("cutoff,admitted", [
    ("2026-03-02T02:36:09.999999Z", False),   # 1us before
    ("2026-03-02T02:36:10.000000Z", True),    # exactly at
    ("2026-03-02T02:36:10.000001Z", True),    # 1us after
])
def test_the_six_hour_boundary_is_exact(
    tmp_path: Path, cutoff: str, admitted: bool
) -> None:
    from sports_quant.retrospective.reader import AdmittedInput

    decision = _certified_reader_case(tmp_path, cutoff)
    assert isinstance(decision, AdmittedInput) is admitted, decision


def test_the_certification_resolves_to_the_exact_destination_row(
    tmp_path: Path,
) -> None:
    from sports_quant.retrospective.reader import AdmittedInput

    decision = _certified_reader_case(tmp_path, EFFECTIVE)
    assert isinstance(decision, AdmittedInput)
    cited = decision.certification.source_evidence_id
    assert cited == "raw_01TESTTESTTESTTESTTESTTEST"


def test_no_new_availability_rule_was_introduced() -> None:
    from sports_quant.retrospective.rules import AVAILABILITY_RULES

    assert set(AVAILABILITY_RULES) == {
        "prior_event_completion_conservative_v1",
        "prior_event_completion_immediate_v1"}
    assert AVAILABILITY_RULES[
        "prior_event_completion_conservative_v1"].lag_seconds == 6 * 3600


def test_an_altered_rule_digest_still_fails_closed(tmp_path: Path) -> None:
    # Rebuild the case, then tamper with the persisted digest.
    from sports_quant.db.repositories.retrospective import (
        SqliteRetrospectiveProvenanceRepository,
    )
    from sports_quant.retrospective.provenance import (
        AvailabilityBasis,
        EligibilityVerdict,
        EntityType,
        G1Variant,
        ProvenanceClass,
        ProviderNamespace,
        RetrospectiveProvenanceError,
    )
    from sports_quant.retrospective.reader import RetrospectiveResearchReader

    ns = ProviderNamespace("lg_nba", "balldontlie", EntityType.GAME, "v1")
    original = raw(payload(good_plays()))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        evidence = derive_completion_evidence(original)
        with transaction(dst):
            m = materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
            repo = SqliteRetrospectiveProvenanceRepository(dst)
            corpus = repo.record_corpus_version(
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                league_id="lg_nba", reconstruction_policy_version="t",
                cutoff_policy_id="pregame", cutoff_policy_version="1",
                source_corpus_digest="src", target_set_digest="t",
                g1_variant=G1Variant.G1_B_CORE, code_version="test")
            repo.certify_input(
                corpus_version_id=corpus.corpus_version_id, namespace=ns,
                provider_game_id="18447999", feature_family="prior_results",
                provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
                reconstruction_policy_version="t",
                eligibility=EligibilityVerdict.ELIGIBLE,
                availability_basis=AvailabilityBasis.EVENT_DERIVED,
                availability_rule_id="prior_event_completion_conservative_v1",
                availability_source=NBA_COMPLETION_POLICY,
                source_evidence_table="raw_responses",
                source_evidence_id=m.raw_response_id,
                source_event_completed_at=evidence.source_event_completed_at)
        with transaction(dst):
            dst.execute("DROP TRIGGER trg_rip_no_update")
            dst.execute("UPDATE reconstructed_input_provenance "
                        "SET availability_rule_digest = ?", ("0" * 64,))
        reader = RetrospectiveResearchReader(
            dst, corpus_version_id=corpus.corpus_version_id, cutoff=EFFECTIVE)
        with pytest.raises(RetrospectiveProvenanceError, match="has changed"):
            reader.admit_feature(namespace=ns, provider_game_id="18447999",
                                 feature_family="prior_results")


# --------------------------------------------------------------------------- #
# 29-32. Isolation: strict PIT and provider references untouched
# --------------------------------------------------------------------------- #
def test_strict_pit_is_untouched_by_this_module() -> None:
    import hashlib
    import inspect

    from sports_quant.pit.asof import AsOfReader
    from sports_quant.pit.dataset import _feature_cutoff

    digest = hashlib.sha256(
        inspect.getsource(_feature_cutoff).encode("utf-8")).hexdigest()[:32]
    assert digest == "5d55345b6e2d8836df83428de82462df"
    for banned in ("completion", "wallclock", "nba_completion"):
        assert not any(banned in n for n in dir(AsOfReader)), banned


def test_no_provider_reference_path_is_introduced() -> None:
    import inspect

    from sports_quant.retrospective import nba_completion

    source = "".join(inspect.getsource(nba_completion).split('"""')[::2])
    for banned in ("provider_team_references", "provider_game_references",
                   "provider_player_references", "entity_match_decisions",
                   "game_status_history"):
        assert banned not in source, banned


def test_the_module_opens_no_network_capable_surface() -> None:
    import inspect

    from sports_quant.retrospective import nba_completion

    source = inspect.getsource(nba_completion)
    for banned in ("httpx", "requests", "socket", "urllib", "BalldontlieClient",
                   "MlbStatsApiClient", "load_settings"):
        assert banned not in source, banned

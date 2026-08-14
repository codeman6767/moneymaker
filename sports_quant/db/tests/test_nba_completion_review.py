"""Independent adversarial review of the NBA completion policy at 30a8746.

Written against the preserved evidence and the reviewed architecture, not the
implementation report. The harness is deliberately independent of
`test_nba_completion_evidence.py`.

The organising question is not "does the derivation accept good payloads" but
"what does it wrongly accept, and what does it wrongly REJECT" -- a false
rejection silently shrinks a research corpus and is just as damaging to a
defensible result as a false admission.
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
    NBA_COMPLETION_ENDPOINT,
    NBA_COMPLETION_PROVIDER,
    CompletionEvidenceError,
    derive_completion_evidence,
    materialize_completion_evidence,
)

GAME = "18447686"


def play(order: int, *, period: int, wallclock: str, type_: str = "Shot",
         home: int = 0, away: int = 0, game_id: str = GAME,
         clock: str = "5:00") -> dict[str, Any]:
    return {"game_id": int(game_id), "order": order, "type": type_,
            "period": period, "clock": clock, "wallclock": wallclock,
            "home_score": home, "away_score": away}


def body_of(plays: list[dict[str, Any]]) -> str:
    return json.dumps({"data": plays})


def raw(body: str, *, provider: str = NBA_COMPLETION_PROVIDER,
        endpoint: str = NBA_COMPLETION_ENDPOINT, game_id: str = GAME,
        http_status: int = 200, raw_id: str = "raw_01REVIEWREVIEWREVIEWREVIEW",
        received_at: str = "2026-08-04T22:12:13.658496Z") -> RawResponse:
    return RawResponse(
        raw_response_id=raw_id, run_id="run_r", provider=provider,
        endpoint=endpoint,
        request_params_json=json.dumps({"game_id": game_id, "per_page": "100"}),
        http_status=http_status, response_headers_json="{}",
        requested_at="2026-08-04T22:12:13.473194Z", received_at=received_at,
        elapsed_ns=1, body=body, body_bytes=len(body.encode()),
        body_hash=body_hash(body), content_hash="c" * 64,
        created_at="2026-08-04T22:12:13.852547Z", http_method="GET",
        content_type="application/json")


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


# =========================================================================== #
# B. The period-monotonicity rule -- a CONFIRMED FALSE REJECTION
# =========================================================================== #
def test_a_mid_sequence_period_disorder_does_not_invalidate_a_terminal_play() -> None:
    """DEFECT R1: `order` is not guaranteed to be period-ordered.

    Reproduces real NBA 2026-03 game 18447743. Its `order` sequence visits
    periods out of order in the middle of the payload, but its terminal play is
    corroborated three independent ways: it carries the `End Game` marker, it
    holds the maximum wallclock, and its score equals the maximum score in the
    payload (and the official game score).

    The shipped implementation rejected it purely for the period disorder, which
    encodes an assumption about provider `order` semantics that the preserved
    evidence does not support. Rejecting genuine evidence shrinks the research
    corpus for no defensible reason.

    Contrast `test_a_truncated_sequence_is_still_refused`: there the terminal
    play is NOT corroborated, and refusal is correct.
    """

    plays = [
        play(1, period=2, wallclock="2026-03-01T18:30:00.000Z", home=10, away=8),
        play(2, period=1, wallclock="2026-03-01T18:31:00.000Z", home=12, away=8),
        play(3, period=2, wallclock="2026-03-01T18:32:00.000Z", home=14, away=9),
        play(4, period=4, wallclock="2026-03-01T20:36:10.000Z",
             type_="End Game", clock="0.0", home=138, away=118),
    ]
    evidence = derive_completion_evidence(raw(body_of(plays)))
    assert evidence.source_event_completed_at == "2026-03-01T20:36:10.000000Z"
    assert evidence.terminal_play_order == 4


def test_a_truncated_sequence_is_still_refused() -> None:
    """The real 18447741/18447742 shape must remain refused.

    `End Game` sits mid-sequence and the last-ordered play carries a LOWER score
    than the payload's maximum -- the sequence does not end where it claims to.
    """

    plays = [
        play(1, period=1, wallclock="2026-03-01T18:30:00.000Z", home=10, away=8),
        play(2, period=4, wallclock="2026-03-01T20:36:27.000Z",
             type_="End Game", clock="0.0", home=121, away=110),
        play(3, period=3, wallclock="2026-03-01T20:59:22.000Z",
             type_="End Period", clock="0.0", home=103, away=80),
    ]
    with pytest.raises(CompletionEvidenceError):
        derive_completion_evidence(raw(body_of(plays)))


def test_a_terminal_play_below_the_payload_maximum_score_is_refused() -> None:
    """The discriminator that actually separates truncated from disordered.

    The terminal play carries `End Game` at maximum order and maximum wallclock,
    but a score lower than plays it supposedly follows. That is a truncated or
    stitched feed, not a completed game.
    """

    plays = [
        play(1, period=1, wallclock="2026-03-01T18:30:00.000Z", home=99, away=90),
        play(2, period=4, wallclock="2026-03-01T20:36:10.000Z",
             type_="End Game", clock="0.0", home=50, away=40),
    ]
    with pytest.raises(CompletionEvidenceError, match="score"):
        derive_completion_evidence(raw(body_of(plays)))


def test_overtime_periods_are_accepted() -> None:
    plays = [
        play(1, period=1, wallclock="2026-03-01T18:30:00.000Z", home=2, away=0),
        play(2, period=4, wallclock="2026-03-01T20:30:00.000Z", home=110, away=110),
        play(3, period=6, wallclock="2026-03-01T21:05:00.000Z",
             type_="End Game", clock="0.0", home=130, away=127),
    ]
    evidence = derive_completion_evidence(raw(body_of(plays)))
    assert evidence.terminal_play_period == 6
    assert evidence.source_event_completed_at == "2026-03-01T21:05:00.000000Z"


# =========================================================================== #
# H. Input-boundary attacks the implementation's own suite did not cover
# =========================================================================== #
@pytest.mark.parametrize("provider", [
    "BALLDONTLIE", "BallDontLie", " balldontlie", "balldontlie ",
    "ball​dontlie",
])
def test_provider_case_and_unicode_variants_are_refused(provider: str) -> None:
    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    with pytest.raises(CompletionEvidenceError):
        derive_completion_evidence(raw(body_of(plays), provider=provider))


@pytest.mark.parametrize("endpoint", [
    "/v1/plays/", "/V1/PLAYS", "/v1/plays?x=1", " /v1/plays", "/v1/play",
])
def test_endpoint_variants_are_refused(endpoint: str) -> None:
    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    with pytest.raises(CompletionEvidenceError):
        derive_completion_evidence(raw(body_of(plays), endpoint=endpoint))


def test_a_boolean_order_is_refused() -> None:
    """`True` is an int in Python; an order of True must not sort as 1."""

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    plays[0]["order"] = True
    with pytest.raises(CompletionEvidenceError):
        derive_completion_evidence(raw(body_of(plays)))


@pytest.mark.parametrize("stamp", [
    "2026-03-01t20:36:10.000z",             # lowercase z
    "2026-03-01T20:36:10.000+00:00",        # explicit offset
    "2026-03-01T15:36:10.000-05:00",        # non-UTC offset, same instant
])
def test_alternative_valid_zoned_forms_are_handled_consistently(
    stamp: str,
) -> None:
    """Whatever the form, the result must be the same UTC instant or a refusal.

    A parser that silently accepted a lowercase `z` as naive, or dropped an
    offset, would shift the derived bound.
    """

    plays = [play(1, period=4, wallclock=stamp, type_="End Game", clock="0.0",
                  home=1, away=0)]
    try:
        evidence = derive_completion_evidence(raw(body_of(plays)))
    except CompletionEvidenceError:
        return                      # refusing an unusual form is acceptable
    assert evidence.source_event_completed_at == "2026-03-01T20:36:10.000000Z"


def test_equal_wallclocks_on_the_terminal_pair_are_accepted() -> None:
    """Ties must not be treated as regression."""

    stamp = "2026-03-01T20:36:10.000Z"
    plays = [
        play(1, period=4, wallclock=stamp, home=1, away=0),
        play(2, period=4, wallclock=stamp, type_="End Game", clock="0.0",
             home=1, away=0),
    ]
    evidence = derive_completion_evidence(raw(body_of(plays)))
    assert evidence.source_event_completed_at == "2026-03-01T20:36:10.000000Z"


def test_a_string_game_id_in_the_payload_still_matches() -> None:
    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    plays[0]["game_id"] = GAME              # string rather than int
    evidence = derive_completion_evidence(raw(body_of(plays)))
    assert evidence.provider_game_id == GAME


def test_a_non_object_payload_is_refused() -> None:
    with pytest.raises(CompletionEvidenceError):
        derive_completion_evidence(raw(json.dumps([1, 2, 3])))


# =========================================================================== #
# D. raw_response_id preservation across databases
# =========================================================================== #
def test_an_unrelated_destination_row_with_the_same_id_is_refused(
    tmp_path: Path,
) -> None:
    """Two corpora can mint the same id only by collision, but a destination may
    already hold an unrelated row under it. That must never be overwritten."""

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    original = raw(body_of(plays))
    unrelated = raw(json.dumps({"data": []}), raw_id=original.raw_response_id,
                    endpoint="/v1/box_scores")
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            store(dst, unrelated)
        with pytest.raises(CompletionEvidenceError, match="Refusing to overwrite"):
            with transaction(dst):
                materialize_completion_evidence(
                    src, dst, raw_response_id=original.raw_response_id)
        survivor = SqliteRawResponseRepository(dst).get(original.raw_response_id)
        assert survivor is not None
        assert survivor.endpoint == "/v1/box_scores"


def test_evidence_from_two_source_corpora_coexists_without_collision(
    tmp_path: Path,
) -> None:
    """A reconstruction DB must be able to hold evidence from several corpora."""

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    a = raw(body_of(plays), raw_id="raw_01CORPUSACORPUSACORPUSA", game_id="1")
    b = raw(body_of(plays), raw_id="raw_01CORPUSBCORPUSBCORPUSB", game_id="2")
    with db(tmp_path / "a.db") as sa, db(tmp_path / "b.db") as sb, \
            db(tmp_path / "dst.db") as dst:
        with transaction(sa):
            store(sa, a)
        with transaction(sb):
            store(sb, b)
        with transaction(dst):
            materialize_completion_evidence(sa, dst,
                                            raw_response_id=a.raw_response_id)
            materialize_completion_evidence(sb, dst,
                                            raw_response_id=b.raw_response_id)
        assert dst.execute(
            "SELECT COUNT(*) FROM raw_responses").fetchone()[0] == 2


def test_the_same_evidence_under_a_different_id_is_a_separate_row(
    tmp_path: Path,
) -> None:
    """Documents the actual contract: identity is the id, not the content.

    Two corpora that captured the SAME provider response independently will have
    different `raw_response_id` values, so materializing both yields two rows
    with identical bodies. That is not corruption -- each row remains a faithful
    copy of the evidence its own corpus preserved -- but a future reader must not
    assume one row per distinct payload.
    """

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    first = raw(body_of(plays), raw_id="raw_01FIRSTFIRSTFIRSTFIRSTFI")
    second = raw(body_of(plays), raw_id="raw_01SECONDSECONDSECONDSEC")
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, first)
            store(src, second)
        with transaction(dst):
            materialize_completion_evidence(src, dst,
                                            raw_response_id=first.raw_response_id)
            materialize_completion_evidence(src, dst,
                                            raw_response_id=second.raw_response_id)
        rows = dst.execute(
            "SELECT raw_response_id, body_hash FROM raw_responses "
            "ORDER BY raw_response_id").fetchall()
    assert len(rows) == 2
    assert rows[0][1] == rows[1][1]          # identical evidence
    assert rows[0][0] != rows[1][0]          # distinct destination rows


# =========================================================================== #
# E. The copy must be exact in all 17 columns, compared as stored
# =========================================================================== #
def test_every_column_survives_materialization_unchanged(tmp_path: Path) -> None:
    """Compared column by column at the SQL level, not via a parsed model."""

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    # Unicode, unusual params ordering, explicit NULL content_type.
    original = raw(body_of(plays))
    original = RawResponse(
        **{**original.__dict__,
           "response_headers_json": json.dumps({"x-note": "café — ünïcode"}),
           "content_type": None,
           "request_params_json": json.dumps({"per_page": "100",
                                              "game_id": GAME})})
    cols = ("raw_response_id, run_id, provider, endpoint, request_params_json, "
            "http_method, http_status, response_headers_json, content_type, "
            "requested_at, received_at, elapsed_ns, body, body_bytes, "
            "body_hash, content_hash, created_at")
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        a = src.execute(f"SELECT {cols} FROM raw_responses").fetchone()
        b = dst.execute(f"SELECT {cols} FROM raw_responses").fetchone()
    names = [c.strip() for c in cols.split(",")]
    differing = [n for n, x, y in zip(names, a, b, strict=True) if x != y]
    assert not differing, differing
    assert b[names.index("content_type")] is None       # NULL stayed NULL


def test_body_bytes_are_compared_as_bytes_not_parsed_json(
    tmp_path: Path,
) -> None:
    """Two JSON texts can parse equal yet differ byte-wise; the copy is bytes."""

    spaced = '{"data": [ {"game_id": 18447686, "order": 1, "type": "End Game", ' \
             '"period": 4, "clock": "0.0", "wallclock": ' \
             '"2026-03-01T20:36:10.000Z", "home_score": 1, "away_score": 0} ]}'
    original = raw(spaced)
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
        copied = dst.execute("SELECT body FROM raw_responses").fetchone()[0]
    assert copied.encode("utf-8") == spaced.encode("utf-8")
    assert derive_completion_evidence(
        raw(spaced)).source_event_completed_at == "2026-03-01T20:36:10.000000Z"


# =========================================================================== #
# G/J. Is the derived instant actually tied to the evidence it cites?
# =========================================================================== #
def _certify(dst: sqlite3.Connection, *, evidence_id: str, completed_at: str,
             availability_source: str, target: str = "18447999") -> Any:
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

    ns = ProviderNamespace("lg_nba", "balldontlie", EntityType.GAME, "v1")
    repo = SqliteRetrospectiveProvenanceRepository(dst)
    corpus = repo.record_corpus_version(
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        league_id="lg_nba", reconstruction_policy_version="rev",
        cutoff_policy_id="pregame", cutoff_policy_version="1",
        source_corpus_digest="src", target_set_digest="t",
        g1_variant=G1Variant.G1_B_CORE, code_version="rev")
    repo.certify_input(
        corpus_version_id=corpus.corpus_version_id, namespace=ns,
        provider_game_id=target, feature_family="prior_results",
        provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
        reconstruction_policy_version="rev",
        eligibility=EligibilityVerdict.ELIGIBLE,
        availability_basis=AvailabilityBasis.EVENT_DERIVED,
        availability_rule_id="prior_event_completion_conservative_v1",
        availability_source=availability_source,
        source_evidence_table="raw_responses", source_evidence_id=evidence_id,
        source_event_completed_at=completed_at)
    return corpus


def test_a_fabricated_completion_instant_is_detected_by_verification(
    tmp_path: Path,
) -> None:
    """DEFECT R4: nothing re-derived the stored instant from its own evidence.

    `availability_source` is a free-text LOCATOR by architecture (section 11:
    "pointer to the documenting evidence"; f018 calls it "a stable citation
    key"), so it is correctly NOT digest-bound the way `availability_rule_digest`
    is -- that one is checked by `verify_rule_digest`.

    But that leaves the derived value itself unchecked. A certification can name
    the right policy, cite real evidence, and still carry a
    `source_event_completed_at` the evidence does not produce. The reader admits
    it, because the reader's job is availability, not re-derivation.

    The repair is a verifier, not a schema change.
    """

    from sports_quant.retrospective.nba_completion import (
        NBA_COMPLETION_POLICY,
        verify_completion_certifications,
    )

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    original = raw(body_of(plays))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
            # A LIE: six hours earlier than the evidence supports.
            _certify(dst, evidence_id=original.raw_response_id,
                     completed_at="2026-03-01T14:36:10.000000Z",
                     availability_source=NBA_COMPLETION_POLICY)
        report = verify_completion_certifications(dst)

    assert not report.ok, "a fabricated completion instant went undetected"
    assert any("does not match" in p for p in report.problems), report.problems


def test_verification_accepts_an_honestly_derived_certification(
    tmp_path: Path,
) -> None:
    from sports_quant.retrospective.nba_completion import (
        NBA_COMPLETION_POLICY,
        verify_completion_certifications,
    )

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    original = raw(body_of(plays))
    evidence = derive_completion_evidence(original)
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
            _certify(dst, evidence_id=original.raw_response_id,
                     completed_at=evidence.source_event_completed_at,
                     availability_source=NBA_COMPLETION_POLICY)
        report = verify_completion_certifications(dst)

    assert report.ok, report.problems
    assert report.checked == 1


def test_verification_detects_evidence_altered_after_certification(
    tmp_path: Path,
) -> None:
    """The certification stays valid-looking while its evidence changes."""

    from sports_quant.retrospective.nba_completion import (
        NBA_COMPLETION_POLICY,
        verify_completion_certifications,
    )

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    original = raw(body_of(plays))
    evidence = derive_completion_evidence(original)
    later = [play(1, period=4, wallclock="2026-03-01T23:59:59.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
            _certify(dst, evidence_id=original.raw_response_id,
                     completed_at=evidence.source_event_completed_at,
                     availability_source=NBA_COMPLETION_POLICY)
        with transaction(dst):
            # raw_responses is append-only at the DB level, so a determined
            # direct-SQL adversary must drop the trigger first. That protection
            # is itself worth recording: casual mutation is already impossible.
            dst.execute("DROP TRIGGER trg_raw_responses_no_update")
            dst.execute("UPDATE raw_responses SET body = ? "
                        "WHERE raw_response_id = ?",
                        (body_of(later), original.raw_response_id))
        report = verify_completion_certifications(dst)

    assert not report.ok
    assert any("does not match" in p for p in report.problems), report.problems


def test_verification_detects_a_certification_citing_missing_evidence(
    tmp_path: Path,
) -> None:
    from sports_quant.retrospective.nba_completion import (
        NBA_COMPLETION_POLICY,
        verify_completion_certifications,
    )

    plays = [play(1, period=4, wallclock="2026-03-01T20:36:10.000Z",
                  type_="End Game", clock="0.0", home=1, away=0)]
    original = raw(body_of(plays))
    with db(tmp_path / "src.db") as src, db(tmp_path / "dst.db") as dst:
        with transaction(src):
            store(src, original)
        with transaction(dst):
            materialize_completion_evidence(
                src, dst, raw_response_id=original.raw_response_id)
            _certify(dst, evidence_id=original.raw_response_id,
                     completed_at="2026-03-01T20:36:10.000000Z",
                     availability_source=NBA_COMPLETION_POLICY)
        with transaction(dst):
            dst.execute("DROP TRIGGER trg_raw_responses_no_delete")
            dst.execute("DELETE FROM raw_responses")
        report = verify_completion_certifications(dst)

    assert not report.ok
    assert any("no longer exists" in p for p in report.problems), report.problems


def test_availability_source_is_a_locator_not_a_cryptographic_binding() -> None:
    """Documents the adjudication so the wording cannot drift back.

    The architecture asks for a pointer to the documenting evidence, and f018
    calls the column a stable citation key. It is deliberately NOT digest-bound
    the way `availability_rule_digest` is, and nothing here should claim
    otherwise. Reproducibility comes from re-derivation, which is what
    `verify_completion_certifications` provides.
    """

    import inspect

    from sports_quant.db.repositories import retrospective as repo_mod

    source = inspect.getsource(
        repo_mod.SqliteRetrospectiveProvenanceRepository.certify_input)
    # The repository resolves the RULE digest from code, but takes
    # availability_source verbatim -- confirming its locator role.
    assert "_resolve_rule_digest" in source
    assert "availability_source" in source

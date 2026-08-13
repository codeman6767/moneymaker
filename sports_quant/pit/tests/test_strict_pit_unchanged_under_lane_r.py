"""Strict point-in-time behaviour is identical under schema v18 (task §17, §26).

This is the most important file in the f018 change set. The whole justification
for adding a retrospective provenance lane is that it does not weaken the strict
one, and "it does not" is a claim that has to be tested rather than asserted.

Three properties, tested three different ways so a single mistaken assumption
cannot make all three pass:

1. **Structural.** The v18 tables are registered as ``unsupported`` joins, so a
   dataset builder that names one fails closed exactly as it would for any
   unregistered table. There is no new joinable surface.
2. **Behavioural.** ``AsOfReader`` returns the same answers as before -- in
   particular the cutoff comparison, ``_feature_cutoff``, and the refusal to see
   an observation recorded after the cutoff.
3. **The specific blocker.** An August-fetched March lineup is still invisible at
   a March cutoff. This is the exact case that made Lane R necessary, and if v18
   had quietly made it visible, the retrospective lane would be pointless.
"""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
from pathlib import Path

import pytest

from sports_quant.db.engine import Database, transaction
from sports_quant.db.init import initialize_database
from sports_quant.db.repositories.retrospective import (
    SqliteRetrospectiveProvenanceRepository,
)
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION
from sports_quant.pit.asof import AsOfReader
from sports_quant.pit.dataset import _feature_cutoff
from sports_quant.pit.models import Cutoff
from sports_quant.pit.registry import (
    ForbiddenJoinError,
    TableClass,
    assert_joinable,
    classify,
    registered_tables,
)
from sports_quant.retrospective import (
    AvailabilityBasis,
    EligibilityVerdict,
    EntityType,
    G1Variant,
    ProvenanceClass,
    ProviderNamespace,
)

from .conftest import CUTOFF, T1, Ctx, seed_lineup

LANE_R_TABLES = (
    "reconstruction_corpus_versions",
    "identity_audit_records",
    "identity_audit_findings",
    "static_crosswalk_provenance",
    "reconstructed_input_provenance",
)

#: SHA-256 of ``_feature_cutoff``'s source as it stood at f881916 (schema v17),
#: the commit immediately before this migration. Verified identical at v18.
_FEATURE_CUTOFF_V17_SHA256 = (
    "5d55345b6e2d8836df83428de82462df776d6f14de02571ac6e1f4e8fa0453d7"
)


# --------------------------------------------------------------------------- #
# 1. Structural: no new joinable surface
# --------------------------------------------------------------------------- #
def test_registry_still_exactly_covers_the_live_schema(db_path: Path) -> None:
    initialize_database(db_path)
    with Database(db_path).connection() as conn:
        actual = {
            str(r[0]) for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")
        }
    registered = set(registered_tables())
    assert registered == actual, {"missing": actual - registered,
                                  "extra": registered - actual}
    assert len(registered_tables()) == len(set(registered_tables()))


@pytest.mark.parametrize("table", LANE_R_TABLES)
def test_v18_provenance_tables_are_unsupported_joins(table: str) -> None:
    """Lane-R provenance is not reachable from a Lane-L dataset row."""

    assert classify(table).classification is TableClass.UNSUPPORTED
    with pytest.raises(ForbiddenJoinError):
        assert_joinable({table})
    # And it stays refused when declared alongside a legitimately joinable table,
    # so one safe join cannot carry an unsafe one in with it.
    with pytest.raises(ForbiddenJoinError):
        assert_joinable({"teams", table})


def test_no_v18_table_became_asof_or_immutable() -> None:
    """A v18 table classified as as-of would be a new feature-facing surface."""

    for table in LANE_R_TABLES:
        assert classify(table).classification not in (
            TableClass.ASOF_FILTERED, TableClass.IMMUTABLE, TableClass.SEASON_SCOPED)


def test_schema_is_at_the_current_version(db_path: Path) -> None:
    result = initialize_database(db_path)
    assert result.schema_version == CURRENT_SCHEMA_VERSION == 19


# --------------------------------------------------------------------------- #
# 2. Behavioural: AsOfReader is untouched
# --------------------------------------------------------------------------- #
def test_asof_reader_has_no_retrospective_mode() -> None:
    """§18: no boolean mode was bolted onto the strict reader.

    A mode flag is how two lanes become one by accident: every call site then has
    to remember which lane it is in, and the default eventually wins.
    """

    names = set(dir(AsOfReader))
    for banned in ("retrospective", "reconstructed", "lane_r", "research_mode",
                   "allow_reconstructed", "provenance_class", "ignore_pit"):
        assert not any(banned in n for n in names), banned
    init_params = set(AsOfReader.__init__.__code__.co_varnames)
    assert not {p for p in init_params if "retro" in p or "research" in p}


def test_feature_cutoff_source_is_byte_identical_to_v17() -> None:
    """``_feature_cutoff`` is the single gate that produced the 0-row result.

    Pinned by SHA-256 over its source rather than by behaviour alone: this is the
    function whose relaxation would silently "fix" Lane L's coverage, and the
    whole point of building Lane R separately was to avoid touching it. A
    legitimate future change to it must update this hash deliberately.
    """

    source = inspect.getsource(_feature_cutoff)
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert digest == _FEATURE_CUTOFF_V17_SHA256, (
        "_feature_cutoff changed under the f018 migration; strict PIT must be "
        "behaviourally identical at v18"
    )


def test_no_bypass_appeared_on_the_strict_reader() -> None:
    assert not [n for n in dir(AsOfReader)
                if "bypass" in n or "override" in n or "ignore" in n]


def test_reader_does_not_touch_any_v18_table(db_path: Path) -> None:
    """Every statement the reader issues, checked against the v18 table names.

    Uses SQLite's trace hook rather than inspecting source: what matters is the
    SQL actually executed, not what the code appears to say.
    """

    initialize_database(db_path)
    statements: list[str] = []
    with Database(db_path).connection() as conn:
        conn.set_trace_callback(statements.append)
        reader = AsOfReader(conn, Cutoff.parse(CUTOFF))
        reader.matched_entity(source_provider="balldontlie", source_ref="1",
                              entity_type="team")
        conn.set_trace_callback(None)
    executed = " ".join(statements).lower()
    for table in LANE_R_TABLES:
        assert table not in executed, f"AsOfReader touched {table}"


# --------------------------------------------------------------------------- #
# 3. The specific blocker: August-fetched March lineups stay invisible
# --------------------------------------------------------------------------- #
LATE = "2026-08-01T12:00:00.000000Z"


def test_late_fetched_lineup_is_invisible_at_the_pregame_cutoff(
    conn: sqlite3.Connection, ctx: Ctx
) -> None:
    """The blocker that made Lane R necessary is still a blocker under v18.

    Seeded through the real ``SqliteLineupRepository`` path, at an observation
    time after the cutoff -- the situation
    ``F1_HISTORICAL_PIT_FEASIBILITY_REVIEW.md`` found for 239/239 NBA and 400/400
    MLB games.
    """

    seed_lineup(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                observed_at=LATE, is_confirmed=True)
    reader = AsOfReader(conn, Cutoff.parse(CUTOFF))
    assert reader.observation(
        "lineup_snapshots",
        anchor_where="provider_game_id = ? AND provider_team_id = ?",
        anchor_params=("G1", "101"),
    ) is None
    # It exists; it is simply not knowable at the cutoff.
    assert conn.execute("SELECT COUNT(*) FROM lineup_snapshots").fetchone()[0] == 1


def test_an_on_time_lineup_is_still_visible(
    conn: sqlite3.Connection, ctx: Ctx
) -> None:
    """The counterpart: v18 did not accidentally hide a legitimate observation."""

    seed_lineup(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                observed_at=T1, is_confirmed=True)
    reader = AsOfReader(conn, Cutoff.parse(CUTOFF))
    assert reader.observation(
        "lineup_snapshots",
        anchor_where="provider_game_id = ? AND provider_team_id = ?",
        anchor_params=("G1", "101"),
    ) is not None


def test_recording_lane_r_provenance_does_not_change_the_strict_answer(
    conn: sqlite3.Connection, ctx: Ctx
) -> None:
    """Certifying an input in Lane R changes nothing about the Lane-L answer.

    Written as before/after rather than as an inspection, because the risk is not
    that the reader reads the new tables -- it is that writing to them perturbs
    something shared. It does not: the two lanes share no read path.
    """

    seed_lineup(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                observed_at=LATE, is_confirmed=True)

    def strict_answer() -> object:
        return AsOfReader(conn, Cutoff.parse(CUTOFF)).observation(
            "lineup_snapshots",
            anchor_where="provider_game_id = ? AND provider_team_id = ?",
            anchor_params=("G1", "101"))

    before = strict_answer()
    league = str(conn.execute(
        "SELECT league_id FROM leagues WHERE code='MLB'").fetchone()[0])
    with transaction(conn):
        repo = SqliteRetrospectiveProvenanceRepository(conn)
        corpus = repo.record_corpus_version(
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            league_id=league, reconstruction_policy_version="rp-1",
            cutoff_policy_id="pregame_lock", cutoff_policy_version="1",
            source_corpus_digest="src", target_set_digest="tgt",
            g1_variant=G1Variant.G1_B_CORE)
        evidence = str(conn.execute(
            "SELECT raw_response_id FROM raw_responses LIMIT 1").fetchone()[0])
        repo.certify_input(
            corpus_version_id=corpus.corpus_version_id,
            namespace=ProviderNamespace(league, "mlb_statsapi", EntityType.TEAM, "v1"),
            provider_game_id="G1", feature_family="team_rolling_core",
            provenance_class=ProvenanceClass.RECONSTRUCTED_RESEARCH,
            reconstruction_policy_version="rp-1",
            eligibility=EligibilityVerdict.ELIGIBLE,
            availability_basis=AvailabilityBasis.EVENT_DERIVED,
            availability_rule_id="prior_event_completion_conservative_v1",
            availability_source="official_box_score_publication_lag_v1",
            source_evidence_table="raw_responses", source_evidence_id=evidence,
            source_event_completed_at="2026-07-08T03:00:00.000000Z")
    after = strict_answer()
    assert before is None and after is None
    # And the Lane-R row genuinely exists -- the test is not vacuous.
    assert conn.execute(
        "SELECT COUNT(*) FROM reconstructed_input_provenance").fetchone()[0] == 1


def test_observed_at_is_never_rewritten_by_f018(
    conn: sqlite3.Connection, ctx: Ctx
) -> None:
    """f018 added no trigger or default that touches an existing timestamp."""

    seed_lineup(conn, game_ref_id=ctx.game_ref_id, team_id=ctx.home_team_id,
                observed_at=LATE, is_confirmed=True)
    assert conn.execute(
        "SELECT observed_at FROM lineup_snapshots").fetchone()[0] == LATE
    triggers = [
        str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name IN "
            "('lineup_snapshots', 'entity_match_decisions', 'game_status_history')")
    ]
    assert triggers, "the pre-existing guards should still be present"
    assert all(not t.startswith(("trg_rcv", "trg_ida", "trg_idf", "trg_xwk", "trg_rip"))
               for t in triggers)


# --------------------------------------------------------------------------- #
# 4. The Lane-R reader exists now. It must still change nothing here.
# --------------------------------------------------------------------------- #
def test_the_lane_r_reader_is_a_separate_type_reached_a_separate_way() -> None:
    """Lane selection is a type, not a flag (architecture §12).

    The strict reader must not know the retrospective one exists, and neither
    can be constructed from the other.
    """

    import sports_quant.pit.asof as strict_module
    from sports_quant.retrospective.reader import RetrospectiveResearchReader

    assert not issubclass(RetrospectiveResearchReader, AsOfReader)
    assert not issubclass(AsOfReader, RetrospectiveResearchReader)
    for attr in dir(strict_module):
        assert "etrospective" not in attr, attr

    strict_source = inspect.getsource(strict_module)
    code_only = "".join(strict_source.split('"""')[::2])
    for banned in ("retrospective", "effective_at", "reconstructed",
                   "availability_basis", "RetrospectiveResearchReader"):
        assert banned not in code_only, (
            f"the strict forward reader references {banned!r}")


def test_the_lane_r_reader_cannot_be_handed_a_strict_cutoff_object() -> None:
    """The two lanes do not even share a cutoff type by accident.

    `AsOfReader` takes a parsed `Cutoff`; the Lane-R reader takes an ISO string
    it parses itself. Passing a `Cutoff` where an ISO string is expected must
    fail rather than silently stringify into something comparable.
    """

    from sports_quant.retrospective.reader import RetrospectiveResearchReader

    signature = inspect.signature(RetrospectiveResearchReader.__init__)
    assert "cutoff" in signature.parameters
    annotation = signature.parameters["cutoff"].annotation
    assert annotation in ("str", str), annotation


def test_importing_the_lane_r_reader_does_not_alter_the_strict_reader() -> None:
    """Import order must not matter: no monkeypatching, no registration."""

    before = sorted(dir(AsOfReader))
    import sports_quant.retrospective.reader  # noqa: F401

    assert sorted(dir(AsOfReader)) == before
    digest = hashlib.sha256(
        inspect.getsource(_feature_cutoff).encode("utf-8")).hexdigest()
    assert digest == _FEATURE_CUTOFF_V17_SHA256

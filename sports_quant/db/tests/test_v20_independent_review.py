"""Independent adversarial review of v20 historical market event observations.

Each test in the DEFECT sections below fails against `c56c4dc` (the reviewed
v20 implementation) and passes after the f021 repairs. The remaining sections
are independent re-derivations of claims the implementation made -- written so
they cannot be satisfied by the production helper agreeing with itself.

No provider is contacted, no credit is spent, and nothing under ``data/`` is
opened.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from pathlib import Path

import pytest

from sports_quant.db.engine import Database
from sports_quant.db.schema import CURRENT_SCHEMA_VERSION, utc_now_iso
from sports_quant.retrospective import sources
from sports_quant.retrospective.market_observations import (
    OBSERVATION_CONTENT_POLICY_VERSION,
    MarketEventObservation,
    observation_content_hash,
    observation_id,
    verify_observation_content_hashes,
)

ODDS = "the_odds_api:basketball_nba"
GEN = "v4"
EVENT_A = "be25eb82b82629d959c1e5ccb8dcc1e7"
EVENT_B = "111a955795876d50988b15c219ce0796"
BUCKET = "2026-03-05T23:10:00.000000Z"
SNAP = "2026-03-05T23:05:00.000000Z"

_COLS = ("observation_id", "league_id", "provider", "namespace_generation",
         "sport_key", "provider_event_id", "requested_at_bucket",
         "provider_snapshot_timestamp", "commence_time", "home_team_raw",
         "away_team_raw", "observation_content_hash", "raw_response_id",
         "observed_at", "created_at")


@pytest.fixture()
def db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "rev.db"
    Database(path).migrate()
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    now = utc_now_iso()
    conn.execute("INSERT INTO leagues (league_id, code, name, sport, created_at, "
                 "updated_at) VALUES ('lg_nba','NBA','NBA','basketball',?,?)",
                 (now, now))
    conn.execute(
        "INSERT INTO ingestion_runs (run_id, command, provider, operation, "
        "args_json, status, requested_at, started_at, started_monotonic_ns, "
        "requests_made, records_received, records_normalized, records_inserted, "
        "records_deduplicated, records_rejected, records_updated, tool_version, "
        "created_at) VALUES ('run_1','x',?, 'op','{}','started',?,?,1,"
        "0,0,0,0,0,0,0,'v',?)", (ODDS, now, now, now))
    conn.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, body, "
        "body_bytes, body_hash, content_hash, requested_at, received_at, "
        "elapsed_ns, created_at) VALUES ('raw_ok','run_1',?, '/e','{}',200,'{}',"
        "'ORIGINAL',8,'h','c',?,?,1,?)", (ODDS, now, now, now))
    conn.commit()
    return conn


def raw_insert(conn: sqlite3.Connection, **over: object) -> None:
    now = utc_now_iso()
    values: dict[str, object] = dict(
        observation_id="hme_probe", league_id="lg_nba", provider=ODDS,
        namespace_generation=GEN, sport_key="basketball_nba",
        provider_event_id=EVENT_A, requested_at_bucket=BUCKET,
        provider_snapshot_timestamp=SNAP, commence_time=None,
        home_team_raw="Boston Celtics", away_team_raw="Miami Heat",
        observation_content_hash="hash", raw_response_id="raw_ok",
        observed_at=now, created_at=now)
    values.update(over)
    conn.execute(
        f"INSERT INTO historical_market_event_observations ({', '.join(_COLS)}) "  # noqa: S608
        f"VALUES ({', '.join('?' * len(_COLS))})",
        tuple(values[c] for c in _COLS))
    conn.commit()


def obs(**over: object) -> MarketEventObservation:
    base: dict[str, object] = dict(
        league_id="lg_nba", provider=ODDS, namespace_generation=GEN,
        sport_key="basketball_nba", provider_event_id=EVENT_A,
        requested_at_bucket=BUCKET, provider_snapshot_timestamp=SNAP,
        commence_time=None, home_team_raw="H", away_team_raw="A")
    base.update(over)
    return MarketEventObservation(**base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# DEFECT 1 (f021) -- REPLACE silently mutated append-only rows
# --------------------------------------------------------------------------- #
def test_replace_into_cannot_mutate_an_observation(db: sqlite3.Connection) -> None:
    """Reproduced at c56c4dc: home_team_raw went from 'Boston Celtics' to 'PWNED'.

    f020 guarded only BEFORE UPDATE / BEFORE DELETE, and SQLite's REPLACE
    conflict resolution deletes without firing DELETE triggers unless
    ``PRAGMA recursive_triggers`` is on -- a per-connection setting an attacker
    simply omits.
    """

    raw_insert(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            f"REPLACE INTO historical_market_event_observations "  # noqa: S608
            f"({', '.join(_COLS)}) VALUES ('hme_probe','lg_nba',?,?, "
            "'basketball_nba', ?, ?, ?, NULL, 'PWNED','A','h','raw_ok',?,?)",
            (ODDS, GEN, EVENT_A, BUCKET, SNAP, utc_now_iso(), utc_now_iso()))
    assert db.execute(
        "SELECT home_team_raw FROM historical_market_event_observations"
    ).fetchone()["home_team_raw"] == "Boston Celtics"


def test_insert_or_replace_cannot_mutate_an_observation(
    db: sqlite3.Connection,
) -> None:
    raw_insert(db)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            f"INSERT OR REPLACE INTO historical_market_event_observations "  # noqa: S608
            f"({', '.join(_COLS)}) VALUES ('hme_probe','lg_nba',?,?, "
            "'basketball_nba', ?, ?, ?, NULL, 'PWNED','A','h','raw_ok',?,?)",
            (ODDS, GEN, EVENT_A, BUCKET, SNAP, utc_now_iso(), utc_now_iso()))


def test_replace_cannot_swap_the_cited_raw_response_body(
    db: sqlite3.Connection,
) -> None:
    """An observation's citation is meaningless if the payload can be swapped."""

    raw_insert(db)
    now = utc_now_iso()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        db.execute(
            "REPLACE INTO raw_responses (raw_response_id, run_id, provider, "
            "endpoint, request_params_json, http_status, response_headers_json, "
            "body, body_bytes, body_hash, content_hash, requested_at, "
            "received_at, elapsed_ns, created_at) VALUES ('raw_ok','run_1',?, "
            "'/e','{}',200,'{}','TAMPERED',8,'h','c',?,?,1,?)",
            (ODDS, now, now, now))
    assert db.execute(
        "SELECT body FROM raw_responses WHERE raw_response_id='raw_ok'"
    ).fetchone()["body"] == "ORIGINAL"


@pytest.mark.parametrize("table", [
    "identity_audit_records",
    "static_crosswalk_provenance",
    "reconstruction_corpus_versions",
])
def test_the_lane_r_provenance_chain_has_a_replace_guard(
    db: sqlite3.Connection, table: str,
) -> None:
    """The three tables a fabricated observation would pass through."""

    triggers = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name = ? "
        "AND sql LIKE '%BEFORE INSERT%'", (table,)).fetchall()
    assert any("refusing to replace" in row["sql"] for row in triggers), (
        f"{table} has no BEFORE INSERT replace guard; REPLACE would bypass its "
        "append-only triggers")


def test_a_lane_r_row_cannot_be_replaced_with_different_content(
    db: sqlite3.Connection,
) -> None:
    """Behavioural proof on the one chain table that seeds standalone."""

    now = utc_now_iso()

    def corpus(digest: str) -> None:
        db.execute(
            "INSERT INTO reconstruction_corpus_versions (corpus_version_id, "
            "provenance_class, league_id, reconstruction_policy_version, "
            "cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
            "target_set_digest, g1_variant, semantic_digest, created_at) VALUES "
            "('rcv_x','reconstructed_research','lg_nba','p','cut','v1','SRC',"
            "'TGT','g1_b_core', ?, ?)", (digest, now))
        db.commit()

    corpus("SEM_ORIGINAL")
    with pytest.raises(sqlite3.IntegrityError, match="refusing to replace"):
        db.execute(
            "REPLACE INTO reconstruction_corpus_versions (corpus_version_id, "
            "provenance_class, league_id, reconstruction_policy_version, "
            "cutoff_policy_id, cutoff_policy_version, source_corpus_digest, "
            "target_set_digest, g1_variant, semantic_digest, created_at) VALUES "
            "('rcv_x','reconstructed_research','lg_nba','p','cut','v1','TAMPERED',"
            "'TGT','g1_b_core','SEM_TAMPERED', ?)", (now,))
    assert db.execute(
        "SELECT source_corpus_digest FROM reconstruction_corpus_versions"
    ).fetchone()["source_corpus_digest"] == "SRC"


def test_insert_or_ignore_of_identical_evidence_still_works(
    db: sqlite3.Connection,
) -> None:
    """The guard must not break legitimate idempotent re-insertion.

    `RAISE(ABORT)` is not suppressed by `OR IGNORE` (only `RAISE(IGNORE)` is), so
    an existence-only guard would turn a harmless no-op into a hard error. The
    real suite caught exactly that. Content-aware guards keep it working.
    """

    now = utc_now_iso()
    for _ in range(2):
        db.execute(
            "INSERT OR IGNORE INTO raw_responses (raw_response_id, run_id, "
            "provider, endpoint, request_params_json, http_status, "
            "response_headers_json, body, body_bytes, body_hash, content_hash, "
            "requested_at, received_at, elapsed_ns, created_at) VALUES "
            "('raw_ok','run_1',?, '/e','{}',200,'{}','ORIGINAL',8,'h','c',?,?,1,?)",
            (ODDS, now, now, now))
    db.commit()
    assert db.execute(
        "SELECT COUNT(*) c FROM raw_responses WHERE raw_response_id='raw_ok'"
    ).fetchone()["c"] == 1


def test_insert_or_ignore_of_DIFFERENT_content_is_refused(
    db: sqlite3.Connection,
) -> None:
    """An idempotent-looking spelling must not silently discard a conflict."""

    now = utc_now_iso()
    with pytest.raises(sqlite3.IntegrityError, match="refusing to replace"):
        db.execute(
            "INSERT OR IGNORE INTO raw_responses (raw_response_id, run_id, "
            "provider, endpoint, request_params_json, http_status, "
            "response_headers_json, body, body_bytes, body_hash, content_hash, "
            "requested_at, received_at, elapsed_ns, created_at) VALUES "
            "('raw_ok','run_1',?, '/e','{}',200,'{}','DIFFERENT',9,'h2','c2',?,?,1,?)",
            (ODDS, now, now, now))


# --------------------------------------------------------------------------- #
# DEFECT 2 (f021) -- a BLOB bypassed the exact event-id format contract
# --------------------------------------------------------------------------- #
def test_a_blob_event_id_is_refused(db: sqlite3.Connection) -> None:
    """Reproduced at c56c4dc: the BLOB was ACCEPTED and stored as typeof='blob'.

    GLOB coerces for comparison and ``length()`` on a blob is its byte count, so
    both halves of f020's CHECK passed. SQLite exempts BLOBs from TEXT affinity,
    so the value stayed a blob -- present in the table and invisible to every
    exact-equality lookup.
    """

    with pytest.raises(sqlite3.IntegrityError, match="must be TEXT"):
        raw_insert(db, provider_event_id=EVENT_A.encode())


def test_the_f020_check_alone_would_have_admitted_the_blob(
    db: sqlite3.Connection,
) -> None:
    """Shows precisely why typeof() was required: GLOB and length() both pass."""

    row = db.execute(
        "SELECT ? GLOB '[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]"
        "[0-9a-f][0-9a-f]' AS globs, length(?) AS len, typeof(?) AS t",
        (EVENT_A.encode(), EVENT_A.encode(), EVENT_A.encode())).fetchone()
    assert row["globs"] == 1 and row["len"] == 32 and row["t"] == "blob"


@pytest.mark.parametrize("column", [
    "provider", "namespace_generation", "sport_key", "home_team_raw",
    "away_team_raw", "observation_content_hash", "requested_at_bucket",
    "provider_snapshot_timestamp", "commence_time"])
def test_blob_values_are_refused_in_every_identity_column(
    db: sqlite3.Connection, column: str,
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="must be TEXT"):
        raw_insert(db, **{column: b"blobbed"})


# --------------------------------------------------------------------------- #
# DEFECT 3 (verifier) -- a forged content hash was stored and digest-bound
# --------------------------------------------------------------------------- #
def test_a_forged_content_hash_is_detected_by_the_verifier(
    db: sqlite3.Connection,
) -> None:
    """Nothing recomputes at write time, and the digest folds the STORED column.

    So a fabricated row is digest-bound exactly as if genuine. The verifier is
    the guard that must run before an observation corpus is digested, audited or
    curated.
    """

    raw_insert(db, observation_content_hash="not-a-real-hash")
    mismatches = verify_observation_content_hashes(db)
    assert len(mismatches) == 1
    assert mismatches[0].stored_content_hash == "not-a-real-hash"
    assert mismatches[0].recomputed_content_hash != "not-a-real-hash"


def test_a_forged_observation_id_is_detected_by_the_verifier(
    db: sqlite3.Connection,
) -> None:
    genuine = obs(home_team_raw="Boston Celtics", away_team_raw="Miami Heat")
    raw_insert(db, observation_id="hme_totally_made_up",
               observation_content_hash=observation_content_hash(genuine))
    mismatches = verify_observation_content_hashes(db)
    assert len(mismatches) == 1
    assert mismatches[0].recomputed_observation_id == observation_id(genuine)


def test_a_genuine_row_verifies_clean(db: sqlite3.Connection) -> None:
    genuine = obs(home_team_raw="Boston Celtics", away_team_raw="Miami Heat")
    raw_insert(db, observation_id=observation_id(genuine),
               observation_content_hash=observation_content_hash(genuine))
    assert verify_observation_content_hashes(db) == []


# --------------------------------------------------------------------------- #
# DEFECT 4 -- audited_source_tables defaulted instead of refusing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider", [
    "banana", "", "BALLDONTLIE", " balldontlie", "balldontlie ",
    ODDS, "the_odds_api"])
def test_an_unrecognized_provider_gets_no_audited_source_set(
    provider: str,
) -> None:
    """Reproduced at c56c4dc: every one of these returned the LINKING set.

    Same fail-OPEN shape G5 repair R4 closed in ATTESTED_GENERATIONS, where a
    typo produced a *verified* namespace.
    """

    with pytest.raises(sources.SourceCorpusError, match="undeclared"):
        sources.audited_source_tables(provider)


@pytest.mark.parametrize("provider", ["balldontlie", "mlb_statsapi"])
def test_official_providers_keep_the_three_table_set(provider: str) -> None:
    assert sources.audited_source_tables(provider) == sources.AUDITED_SOURCE_TABLES
    assert len(sources.AUDITED_SOURCE_TABLES) == 3


def test_no_linking_provider_is_registered_at_this_version() -> None:
    assert sources.REGISTERED_LINKING_PROVIDERS == frozenset()


# --------------------------------------------------------------------------- #
# Old-corpus digest compatibility, re-derived independently
# --------------------------------------------------------------------------- #
def test_market_rows_do_not_perturb_an_official_corpus_digest(
    db: sqlite3.Connection,
) -> None:
    before = sources.source_corpus_digest(
        db, league_id="lg_nba", provider="balldontlie")
    genuine = obs()
    raw_insert(db, observation_id=observation_id(genuine),
               observation_content_hash=observation_content_hash(genuine))
    after = sources.source_corpus_digest(
        db, league_id="lg_nba", provider="balldontlie")
    assert before == after


def test_the_official_digest_payload_is_independently_reproducible(
    db: sqlite3.Connection,
) -> None:
    """Rebuild the digest from the documented rule, not by calling the helper."""

    from streaming.event_envelope import canonical_json

    payload: dict[str, object] = {
        "policy": sources.SOURCE_DIGEST_POLICY_VERSION,
        "league_id": "lg_nba",
        "provider": "balldontlie",
    }
    for table in sources.AUDITED_SOURCE_TABLES:
        columns = sources.digest_columns_for(table)
        where = "WHERE provider = ?"
        params: tuple[object, ...] = ("balldontlie",)
        if "league_id" in columns:
            where += " AND league_id = ?"
            params = ("balldontlie", "lg_nba")
        rows = db.execute(
            f"SELECT {', '.join(columns)} FROM {table} {where}",  # noqa: S608
            params).fetchall()
        reduced = sorted(
            canonical_json({c: row[c] for c in columns}) for row in rows)
        payload[table] = {
            "rows": len(reduced),
            "digest": hashlib.sha256("\n".join(reduced).encode()).hexdigest()}
    expected = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    assert sources.source_corpus_digest(
        db, league_id="lg_nba", provider="balldontlie") == expected


def test_the_linking_digest_is_unreachable_while_no_provider_is_registered(
    db: sqlite3.Connection,
) -> None:
    with pytest.raises(sources.SourceCorpusError):
        sources.source_corpus_digest(db, league_id="lg_nba", provider=ODDS)


# --------------------------------------------------------------------------- #
# Content hash re-derived independently, and its collision surface
# --------------------------------------------------------------------------- #
def test_content_hash_equals_an_independently_built_digest() -> None:
    payload = {
        "policy": OBSERVATION_CONTENT_POLICY_VERSION,
        "league_id": "lg_nba", "provider": ODDS, "namespace_generation": GEN,
        "sport_key": "basketball_nba", "provider_event_id": EVENT_A,
        "requested_at_bucket": BUCKET, "provider_snapshot_timestamp": SNAP,
        "commence_time": None, "home_team_raw": "H", "away_team_raw": "A",
    }
    expected = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    assert observation_content_hash(obs()) == expected


@pytest.mark.parametrize("left,right", [
    ({"commence_time": None}, {"commence_time": BUCKET}),
    ({"home_team_raw": "a|b", "away_team_raw": "c"},
     {"home_team_raw": "a", "away_team_raw": "b|c"}),
    ({"home_team_raw": 'a","away_team_raw":"b'},
     {"home_team_raw": "a", "away_team_raw": "b"}),
    ({"home_team_raw": "a\nb"}, {"home_team_raw": "ab"}),
    ({"home_team_raw": "a\x00b"}, {"home_team_raw": "ab"}),
    ({"home_team_raw": "1"}, {"home_team_raw": "01"}),
    ({"home_team_raw": "X", "away_team_raw": "Y"},
     {"home_team_raw": "Y", "away_team_raw": "X"}),
    ({"requested_at_bucket": BUCKET, "provider_snapshot_timestamp": SNAP},
     {"requested_at_bucket": SNAP, "provider_snapshot_timestamp": BUCKET}),
    ({"provider_event_id": EVENT_A}, {"provider_event_id": EVENT_B}),
])
def test_no_two_distinct_observations_share_a_content_hash(
    left: dict, right: dict,
) -> None:
    assert observation_content_hash(obs(**left)) != observation_content_hash(
        obs(**right))


def test_unicode_normalization_forms_stay_distinct() -> None:
    """Labels are stored verbatim, so NFC and NFD are different evidence."""

    nfc = unicodedata.normalize("NFC", "Montréal")
    nfd = unicodedata.normalize("NFD", "Montréal")
    assert nfc != nfd
    assert observation_content_hash(obs(home_team_raw=nfc)) != \
        observation_content_hash(obs(home_team_raw=nfd))


def test_the_hash_is_stable_across_processes() -> None:
    """No PYTHONHASHSEED dependence: recomputed in a fresh interpreter."""

    import os
    import subprocess
    import sys
    repo = Path(__file__).resolve().parents[3]
    code = (
        f"import sys; sys.path.insert(0, r'{repo}');"
        "from sports_quant.retrospective.market_observations import "
        "MarketEventObservation, observation_content_hash as H;"
        f"print(H(MarketEventObservation(league_id='lg_nba', provider={ODDS!r},"
        f" namespace_generation={GEN!r}, sport_key='basketball_nba',"
        f" provider_event_id={EVENT_A!r}, requested_at_bucket={BUCKET!r},"
        f" provider_snapshot_timestamp={SNAP!r}, commence_time=None,"
        " home_team_raw='H', away_team_raw='A')))")
    # A full env copy: clearing it removes PATH/SYSTEMROOT and the interpreter
    # will not start. Only PYTHONHASHSEED is forced, which is the variable that
    # would expose any reliance on Python's randomized string hashing.
    env = {**os.environ, "PYTHONHASHSEED": "1"}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, check=True, env=env, cwd=repo)
    assert out.stdout.strip() == observation_content_hash(obs())


# --------------------------------------------------------------------------- #
# Raw-response binding boundary, stated as tests rather than prose
# --------------------------------------------------------------------------- #
def test_an_observation_may_still_cite_an_unrelated_valid_response(
    db: sqlite3.Connection,
) -> None:
    """RETAINED LIMITATION, deliberately not repaired here.

    Same provider, HTTP 200, but a *current /odds* response whose body contains
    no such event. The database cannot check this without parsing the payload,
    which belongs to the Stage-A parser. Recorded so the identity task cannot
    mistake storage for verified provenance.
    """

    now = utc_now_iso()
    db.execute(
        "INSERT INTO raw_responses (raw_response_id, run_id, provider, endpoint, "
        "request_params_json, http_status, response_headers_json, body, "
        "body_bytes, body_hash, content_hash, requested_at, received_at, "
        "elapsed_ns, created_at) VALUES ('raw_current','run_1',?, "
        "'/v4/sports/basketball_nba/odds','{}',200,'{}','[]',2,'h2','c',?,?,1,?)",
        (ODDS, now, now, now))
    db.commit()
    raw_insert(db, raw_response_id="raw_current")
    assert db.execute(
        "SELECT raw_response_id FROM historical_market_event_observations"
    ).fetchone()["raw_response_id"] == "raw_current"


def test_a_wrong_provider_or_failed_response_is_still_refused(
    db: sqlite3.Connection,
) -> None:
    """The two checks the database CAN make deterministically still hold."""

    now = utc_now_iso()
    for rid, status, provider in (("raw_bad", 500, ODDS),
                                  ("raw_other", 200, "balldontlie")):
        db.execute(
            "INSERT INTO raw_responses (raw_response_id, run_id, provider, "
            "endpoint, request_params_json, http_status, response_headers_json, "
            "body, body_bytes, body_hash, content_hash, requested_at, "
            "received_at, elapsed_ns, created_at) VALUES (?, 'run_1', ?, '/e', "
            "'{}', ?, '{}', '[]', 2, ?, 'c', ?, ?, 1, ?)",
            (rid, provider, status, rid, now, now, now))
    db.commit()
    with pytest.raises(sqlite3.IntegrityError, match="non-200"):
        raw_insert(db, raw_response_id="raw_bad")
    with pytest.raises(sqlite3.IntegrityError, match="different provider"):
        raw_insert(db, raw_response_id="raw_other")


# --------------------------------------------------------------------------- #
# Trust chain: how far can a fabricated observation get today?
# --------------------------------------------------------------------------- #
def test_a_fabricated_observation_cannot_reach_canonical_identity(
    db: sqlite3.Connection,
) -> None:
    """The exact guard, named: no linking provider is registered.

    A fabricated observation can be stored. It cannot be digested (no audited
    source set for its provider), so it cannot bind an audit, so it cannot back
    a crosswalk. The chain fails closed at the FIRST step, not the last.
    """

    raw_insert(db)
    with pytest.raises(sources.SourceCorpusError):
        sources.source_corpus_digest(db, league_id="lg_nba", provider=ODDS)
    assert db.execute(
        "SELECT COUNT(*) c FROM identity_audit_records").fetchone()["c"] == 0
    assert db.execute(
        "SELECT COUNT(*) c FROM static_crosswalk_provenance").fetchone()["c"] == 0
    assert db.execute("SELECT COUNT(*) c FROM games").fetchone()["c"] == 0


# --------------------------------------------------------------------------- #
# Non-regression
# --------------------------------------------------------------------------- #
def test_f020_is_preserved_byte_for_byte() -> None:
    """The repair appended; it did not rewrite applied migration evidence."""

    from sports_quant.db.engine import discover_migrations

    by_name = {m.name: m for m in discover_migrations()}
    assert "f020_historical_market_event_observations" in by_name
    assert "f021_append_only_replace_and_event_id_type" in by_name
    assert by_name["f020_historical_market_event_observations"].version == 20
    assert by_name["f021_append_only_replace_and_event_id_type"].version == 21
    assert len(by_name) == CURRENT_SCHEMA_VERSION


def test_f021_adds_no_table(tmp_path: Path) -> None:
    """Triggers only: the table count is unchanged from v20."""

    from sports_quant.db.engine import discover_migrations

    partial = tmp_path / "mig20"
    partial.mkdir()
    for migration in discover_migrations():
        if migration.version <= 20:
            (partial / f"{migration.name}.sql").write_text(
                migration.sql, encoding="utf-8")
    at20 = tmp_path / "at20.db"
    Database(at20, migrations_dir=partial).migrate()
    count20 = sqlite3.connect(at20).execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]

    Database(at20).migrate()
    count21 = sqlite3.connect(at20).execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
    assert count20 == count21 == 53


def test_the_odds_api_still_holds_no_provider_authority() -> None:
    from sports_quant.matching.service import OFFICIAL_PROVIDER_BY_LEAGUE
    from sports_quant.retrospective.namespaces import QUALIFIED_PROVIDERS
    from sports_quant.retrospective.provenance import ATTESTED_GENERATIONS

    assert ODDS not in OFFICIAL_PROVIDER_BY_LEAGUE.values()
    assert ODDS not in sources.PROVIDER_LEAGUES
    assert ODDS not in ATTESTED_GENERATIONS
    assert not any(ODDS in key for key in QUALIFIED_PROVIDERS)
    assert not hasattr(sources, "LINKING_NAMESPACES")

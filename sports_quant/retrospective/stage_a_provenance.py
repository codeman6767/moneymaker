"""Stage-A plan/acquisition provenance, evidence lanes, and the certification gate.

The gate is the only component that recomputes digests
------------------------------------------------------
A digest string is caller-supplied and forgeable at INSERT: a SQLite trigger
cannot compute SHA-256 over evidence rows, and append-only protection only
prevents REWRITING a forged value. So no lane digest, plan digest or acquisition
set digest here is ever trusted because it is stored -- :func:`certify_stage_a`
recomputes each one from exact evidence membership and compares.

Certification is DERIVED, never stored
--------------------------------------
There is deliberately no ``certified`` column anywhere. A stored verdict would be
exactly the caller-supplied claim this module exists to avoid. Counts are
returned for reporting only; every accept/reject decision rests on KEYED SET
equalities, because equal counts can hide a double-counted response, a bucket
with two attempts beside one with none, or a target dropped while the bucket
count is unchanged.

The database is the evidence ledger
-----------------------------------
Certification reads DB rows only. The external checkpoint is operational resume
state: a stale, missing or copied checkpoint may cause conservative re-work, but
can never change a scientific verdict.

This module performs no network I/O.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from typing import Final, Iterable, Mapping, Optional, Sequence

from ..db.ids import (
    new_evidence_lane_binding_id,
    new_stage_a_acquisition_id,
    new_stage_a_attempt_id,
    new_stage_a_plan_id,
    new_stage_a_probe_registration_id,
)
from ..db.schema import utc_now_iso
from .historical_events_projection import (
    HISTORICAL_EVENTS_ENDPOINTS,
    STAGE_A_ALLOWED_REQUEST_PARAMS,
    project_historical_events_response,
    verify_historical_event_projections,
)
from .market_observations import verify_observation_content_hashes
from .provenance import G1Variant, ProvenanceClass
from .stage_a_manifest import StageAManifest, canonical_json
from .stage_a_policies import (
    FrozenDigestPolicy,
    digest_policy_for_lane,
    require_acquisition_policy,
    require_probe_policy,
    require_projection_policy,
)

MARKET_EVENTS_E0_LANE: Final = "market_events_e0"

#: Outcomes that mean a valid zero-or-more-event snapshot was preserved and must
#: therefore project into typed observations.
PROJECTING_OUTCOMES: Final[frozenset[str]] = frozenset(
    {"success_full_snapshot", "success_empty_data", "reused_probe_response"})

#: Outcomes that count as a bucket being satisfied by real evidence.
TERMINAL_SUCCESS_OUTCOMES: Final[frozenset[str]] = PROJECTING_OUTCOMES


class StageAProvenanceError(RuntimeError):
    """A Stage-A provenance write or verification failed. Always fails closed."""


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Deterministic digests
# --------------------------------------------------------------------------- #
def acquisition_set_digest(acquisition_ids: Iterable[str]) -> str:
    """Digest over the SORTED member-acquisition set of a lane.

    Sorted so member insertion order is never part of identity. This is what
    makes both directions of the omission attack detectable: a lane citing {A}
    whose evidence digest covers A+B, and a lane citing {A,B} whose evidence
    digest covers only A.
    """

    members = sorted(set(acquisition_ids))
    if not members:
        raise StageAProvenanceError(
            "an evidence lane must have at least one member acquisition")
    return _hash(canonical_json({"kind": "acquisition_set", "members": members}))


def lane_evidence_digest(
    conn: sqlite3.Connection,
    *,
    policy: FrozenDigestPolicy,
    provider: str,
    namespace_generation: str,
    raw_response_ids: Sequence[str],
) -> str:
    """Digest the lane's evidence rows under a FROZEN policy.

    The table and column set comes from the frozen policy, never from the live
    mutable source registry, so registering a provider or adding a table later
    cannot silently redefine what an accepted lane's digest meant.
    """

    if not raw_response_ids:
        raise StageAProvenanceError(
            "refusing to digest an evidence lane with no preserved responses")
    rows: list[list[object]] = []
    placeholders = ",".join("?" for _ in raw_response_ids)
    for table in policy.table_names():
        columns = policy.columns_for(table)
        selected = ", ".join(columns)
        cursor = conn.execute(
            f"SELECT {selected} FROM {table} "
            f"WHERE provider = ? AND namespace_generation = ? "
            f"AND raw_response_id IN ({placeholders}) "
            f"ORDER BY {selected}",
            (provider, namespace_generation, *raw_response_ids),
        )
        for row in cursor:
            rows.append([table, *[None if v is None else str(v) for v in row]])
    # Sorted for order independence: SQLite row order is not part of the evidence.
    rows.sort(key=lambda r: canonical_json({"r": [str(x) for x in r]}))
    return _hash(canonical_json({
        "kind": "evidence_lane",
        "policy": policy.version,
        "provider": provider,
        "namespace_generation": namespace_generation,
        "rows": rows,
    }))


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def record_plan(
    conn: sqlite3.Connection,
    manifest: StageAManifest,
    *,
    manifest_commit_sha: str,
    manifest_content_digest: str,
    manifest_path: str,
) -> str:
    """Persist a declared plan with its targets and buckets, all closed together.

    ``manifest_path`` is stored as convenience provenance and is NOT part of any
    digest: the same logical plan checked out under a different filesystem root
    has one identity.
    """

    require_acquisition_policy(manifest.acquisition_policy_version)
    require_projection_policy(manifest.projection_policy_version)

    now = utc_now_iso()
    plan_id = new_stage_a_plan_id()
    # ATOMIC: the plan row, its bucket set and its target set are ONE declaration
    # that the architecture requires to be "closed together". Sequential writes
    # without a savepoint can leave a partial plan -- a declared plan whose target
    # population is truncated -- which is precisely the selection-bias shape the
    # target binding exists to prevent.
    conn.execute("SAVEPOINT record_stage_a_plan")
    try:
        _write_plan(conn, manifest, plan_id=plan_id, now=now,
                    manifest_commit_sha=manifest_commit_sha,
                    manifest_content_digest=manifest_content_digest,
                    manifest_path=manifest_path)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT record_stage_a_plan")
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT record_stage_a_plan")
    return plan_id


def _write_plan(
    conn: sqlite3.Connection,
    manifest: StageAManifest,
    *,
    plan_id: str,
    now: str,
    manifest_commit_sha: str,
    manifest_content_digest: str,
    manifest_path: str,
) -> None:
    conn.execute(
        "INSERT INTO stage_a_plans (plan_id, plan_digest, manifest_commit_sha,"
        " manifest_content_digest, manifest_path, manifest_format_version,"
        " plan_policy_version, league_id, provider, namespace_generation,"
        " sport_key, official_source_corpus_digest, official_target_set_digest,"
        " decision_horizon_minutes, bucket_floor_seconds,"
        " acquisition_policy_version, projection_policy_version,"
        " cost_policy_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (plan_id, manifest.plan_digest(), manifest_commit_sha,
         manifest_content_digest, manifest_path, manifest.manifest_format_version,
         manifest.plan_policy_version, manifest.league_id, manifest.provider,
         manifest.namespace_generation, manifest.sport_key,
         manifest.official_source_corpus_digest, manifest.official_target_set_digest,
         manifest.decision_horizon_minutes, manifest.bucket_floor_seconds,
         manifest.acquisition_policy_version, manifest.projection_policy_version,
         manifest.cost_policy_version, now),
    )
    # Buckets first: a target's trigger requires its bucket to already exist.
    for bucket in sorted(set(manifest.buckets)):
        conn.execute(
            "INSERT INTO stage_a_planned_buckets (plan_id, requested_at_bucket,"
            " created_at) VALUES (?,?,?)", (plan_id, bucket, now))
    for target in sorted(manifest.targets, key=lambda t: t.canonical_game_id):
        conn.execute(
            "INSERT INTO stage_a_plan_targets (plan_id, canonical_game_id,"
            " requested_at_bucket, created_at) VALUES (?,?,?,?)",
            (plan_id, target.canonical_game_id, target.requested_at_bucket, now))


def register_acquisition(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    acquisition_policy_version: str,
    projection_policy_version: str,
    request_budget: int,
    credit_budget: int,
) -> str:
    """Open ONE execution of a plan, before any transport.

    A plan may be executed more than once, so this deliberately does not derive
    the acquisition id from the plan.

    ``registered_at`` is NOT a parameter. The reconciled architecture deleted
    the caller-supplied ``declared_at`` precisely because it was backdatable,
    and re-exposing the replacement clock would restore the same
    fetch-then-declare bypass through the trusted API: acquire a response, then
    register an acquisition dated before it, then record it as an ordinary
    success. The clock is taken from ``utc_now_iso()`` here so the ONLY way to
    produce a backdated acquisition is direct SQL, which is the stated
    tamper-evidence boundary rather than a supported feature.
    """

    require_acquisition_policy(acquisition_policy_version)
    require_projection_policy(projection_policy_version)
    now = utc_now_iso()
    acquisition_id = new_stage_a_acquisition_id()
    conn.execute(
        "INSERT INTO stage_a_acquisitions (acquisition_id, plan_id,"
        " acquisition_policy_version, projection_policy_version, request_budget,"
        " credit_budget, registered_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (acquisition_id, plan_id, acquisition_policy_version,
         projection_policy_version, request_budget, credit_budget, now, now),
    )
    return acquisition_id


def register_probe_response(
    conn: sqlite3.Connection,
    *,
    raw_response_id: str,
    probe_report_commit_sha: str,
    probe_report_path: str,
    probe_policy_version: str,
    registered_at: Optional[str] = None,
) -> str:
    """Register a documented capability probe response as reuse-eligible.

    This grants NO identity semantics whatsoever: not that an audit was accepted,
    not that the event id is stable, not that any event maps to a canonical game,
    and not that the provider is trusted for identity. It says only that this
    exact preserved response was an independently documented probe that the
    Stage-A gate may CONSIDER, subject to every other eligibility condition.
    """

    require_probe_policy(probe_policy_version)
    now = utc_now_iso()
    registration_id = new_stage_a_probe_registration_id()
    conn.execute(
        "INSERT INTO stage_a_probe_registrations (probe_registration_id,"
        " raw_response_id, probe_policy_version, probe_report_commit_sha,"
        " probe_report_path, registered_at, created_at) VALUES (?,?,?,?,?,?,?)",
        (registration_id, raw_response_id, probe_policy_version,
         probe_report_commit_sha, probe_report_path, registered_at or now, now),
    )
    return registration_id


def record_attempt(
    conn: sqlite3.Connection,
    *,
    acquisition_id: str,
    requested_at_bucket: str,
    outcome: str,
    raw_response_id: Optional[str] = None,
    attempt_ordinal: int = 1,
) -> str:
    """Record one request attempt and its honest outcome."""

    now = utc_now_iso()
    attempt_id = new_stage_a_attempt_id()
    conn.execute(
        "INSERT INTO stage_a_request_attempts (attempt_id, acquisition_id,"
        " requested_at_bucket, attempt_ordinal, outcome, raw_response_id,"
        " created_at) VALUES (?,?,?,?,?,?,?)",
        (attempt_id, acquisition_id, requested_at_bucket, attempt_ordinal,
         outcome, raw_response_id, now),
    )
    return attempt_id


# --------------------------------------------------------------------------- #
# Certification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StageACertification:
    """A DERIVED verdict. Never persisted, never caller-supplied."""

    acquisition_id: str
    certified: bool
    failures: tuple[str, ...] = ()
    #: Reporting only. Never the basis of the verdict.
    counts: dict[str, int] = field(default_factory=dict)

    def require(self) -> None:
        if not self.certified:
            raise StageAProvenanceError(
                "Stage-A certification failed:\n  - " + "\n  - ".join(self.failures))


def _fetch_plan(conn: sqlite3.Connection, acquisition_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT p.*, q.projection_policy_version AS acq_projection_policy,"
        " q.registered_at AS acq_registered_at"
        " FROM stage_a_plans p JOIN stage_a_acquisitions q ON q.plan_id = p.plan_id"
        " WHERE q.acquisition_id = ?", (acquisition_id,)).fetchone()
    if row is None:
        raise StageAProvenanceError(f"unknown acquisition {acquisition_id!r}")
    return row


def certify_stage_a(
    conn: sqlite3.Connection,
    *,
    acquisition_id: str,
    manifest: StageAManifest,
    manifest_text: Optional[str] = None,
) -> StageACertification:
    """Recompute every Stage-A claim from evidence and return a derived verdict.

    Each check below is a keyed SET equality or a recomputed digest. None of them
    reads a stored count or a stored verdict.
    """

    conn.row_factory = sqlite3.Row
    failures: list[str] = []
    plan = _fetch_plan(conn, acquisition_id)
    plan_id = plan["plan_id"]

    # 1. Manifest integrity: the persisted plan identity must equal the digest
    #    recomputed from the manifest content.
    if plan["plan_digest"] != manifest.plan_digest():
        failures.append(
            "persisted plan_digest does not match the digest recomputed from the "
            "committed manifest")
    if manifest_text is not None:
        recomputed = _hash(manifest_text)
        if recomputed != plan["manifest_content_digest"]:
            failures.append(
                "committed manifest content digest does not match the persisted "
                "plan row; the manifest file changed after declaration")

    # 2/3. DB target population and mapping == committed manifest.
    db_targets = {
        str(r["canonical_game_id"]): str(r["requested_at_bucket"])
        for r in conn.execute(
            "SELECT canonical_game_id, requested_at_bucket FROM stage_a_plan_targets"
            " WHERE plan_id = ?", (plan_id,))
    }
    manifest_targets = manifest.target_map()
    if set(db_targets) != set(manifest_targets):
        missing = sorted(set(manifest_targets) - set(db_targets))
        extra = sorted(set(db_targets) - set(manifest_targets))
        failures.append(
            f"declared target population differs from the manifest "
            f"(missing={missing}, extra={extra})")
    elif db_targets != manifest_targets:
        failures.append(
            "target -> bucket mapping in the database differs from the manifest")

    # 3b. Budgets and policy versions are stored provenance and must AGREE with
    #     the declared manifest, or the recorded hard budget is decorative: an
    #     acquisition could claim credit_budget=0 for a 160-request Odds plan and
    #     still certify.
    acquisition = conn.execute(
        "SELECT request_budget, credit_budget, acquisition_policy_version"
        " FROM stage_a_acquisitions WHERE acquisition_id = ?",
        (acquisition_id,)).fetchone()
    if int(acquisition["request_budget"]) != int(manifest.request_budget):
        failures.append(
            f"acquisition request_budget {acquisition['request_budget']} does not "
            f"equal the declared manifest budget {manifest.request_budget}")
    if int(acquisition["credit_budget"]) != int(manifest.credit_budget):
        failures.append(
            f"acquisition credit_budget {acquisition['credit_budget']} does not "
            f"equal the declared manifest budget {manifest.credit_budget}")
    if str(acquisition["acquisition_policy_version"]) != \
            manifest.acquisition_policy_version:
        failures.append(
            "acquisition policy version does not equal the manifest's")
    if str(plan["acq_projection_policy"]) != manifest.projection_policy_version:
        failures.append(
            "acquisition projection policy version does not equal the manifest's")
    for field_name, declared_value in (
            ("provider", manifest.provider),
            ("namespace_generation", manifest.namespace_generation),
            ("sport_key", manifest.sport_key),
            ("league_id", manifest.league_id),
            ("cost_policy_version", manifest.cost_policy_version),
            ("official_source_corpus_digest",
             manifest.official_source_corpus_digest),
            ("official_target_set_digest", manifest.official_target_set_digest)):
        if str(plan[field_name]) != str(declared_value):
            failures.append(
                f"persisted plan {field_name} does not equal the manifest's")

    # 4. Planned bucket set.
    db_buckets = {
        str(r["requested_at_bucket"]) for r in conn.execute(
            "SELECT requested_at_bucket FROM stage_a_planned_buckets WHERE plan_id = ?",
            (plan_id,))
    }
    if db_buckets != set(manifest.buckets):
        failures.append("planned bucket set differs from the manifest")

    # 5/6. Attempt reconciliation: every attempt classified, none discarded.
    attempts = list(conn.execute(
        "SELECT requested_at_bucket, attempt_ordinal, outcome, raw_response_id"
        " FROM stage_a_request_attempts WHERE acquisition_id = ?", (acquisition_id,)))
    attempted_buckets = [str(a["requested_at_bucket"]) for a in attempts]

    # 7. Terminal bucket reconciliation. First pass forbids retries, so each
    #    planned bucket must carry exactly one attempt.
    if len(attempted_buckets) != len(set(attempted_buckets)):
        failures.append(
            "a bucket carries more than one attempt; the first-pass policy "
            "forbids retries, so its terminal classification is ambiguous")
    not_requested = sorted(db_buckets - set(attempted_buckets))
    if not_requested:
        failures.append(
            f"{len(not_requested)} planned bucket(s) were never requested; the "
            f"acquisition is incomplete (first: {not_requested[:3]})")
    undeclared = sorted(set(attempted_buckets) - db_buckets)
    if undeclared:
        failures.append(f"attempt(s) cite undeclared bucket(s): {undeclared[:3]}")

    # {attempt raw_response_ids} is injective within the acquisition.
    response_ids = [str(a["raw_response_id"]) for a in attempts
                    if a["raw_response_id"] is not None]
    if len(response_ids) != len(set(response_ids)):
        failures.append(
            "a raw response is cited by more than one attempt in this acquisition")

    # 8. Probe-reuse eligibility.
    reused = {str(a["raw_response_id"]) for a in attempts
              if a["outcome"] == "reused_probe_response"}
    ordinary = {str(a["raw_response_id"]) for a in attempts
                if a["outcome"] in PROJECTING_OUTCOMES
                and a["outcome"] != "reused_probe_response"}
    if reused & ordinary:
        failures.append(
            "a response is counted both as an ordinary success and as a probe reuse")
    if reused:
        registered = {
            str(r["raw_response_id"]) for r in conn.execute(
                "SELECT raw_response_id FROM stage_a_probe_registrations")
        }
        unregistered = sorted(reused - registered)
        if unregistered:
            failures.append(
                f"reused probe response(s) without a probe registration: "
                f"{unregistered[:3]}")
        failures.extend(_probe_reuse_failures(conn, attempts))

    # 9. Raw-response timestamp integrity + plan-before-network for ordinary use.
    failures.extend(_response_integrity_failures(conn, attempts, plan))

    # 10/11/12/13/14. Projection and observation integrity.
    failures.extend(_observation_failures(conn, attempts, plan))

    counts = {
        "planned_buckets": len(db_buckets),
        "plan_targets": len(db_targets),
        "attempts": len(attempts),
        "responses": len(set(response_ids)),
        "reused_probe_responses": len(reused),
        "not_requested": len(not_requested),
    }
    return StageACertification(
        acquisition_id=acquisition_id,
        certified=not failures,
        failures=tuple(failures),
        counts=counts,
    )


def _probe_reuse_failures(
    conn: sqlite3.Connection, attempts: Sequence[sqlite3.Row]
) -> list[str]:
    """Every reviewed probe-reuse condition, checked deterministically."""

    failures: list[str] = []
    for attempt in attempts:
        if attempt["outcome"] != "reused_probe_response":
            continue
        raw_id = str(attempt["raw_response_id"])
        row = conn.execute(
            "SELECT endpoint, http_status, request_params_json FROM raw_responses"
            " WHERE raw_response_id = ?", (raw_id,)).fetchone()
        if row is None:
            failures.append(f"reused probe {raw_id!r} cites no preserved response")
            continue
        if str(row["endpoint"]) not in HISTORICAL_EVENTS_ENDPOINTS:
            failures.append(
                f"reused probe {raw_id!r} is not from the historical events endpoint")
        if int(row["http_status"]) != 200:
            failures.append(f"reused probe {raw_id!r} is not an HTTP 200 response")
        # Unfiltered request shape only: a filtered request (e.g. eventIds=<one>)
        # would make a self-consistent response whose complete projection contains
        # only the convenient event.
        try:
            import json as _json
            params = _json.loads(str(row["request_params_json"]))
            extra = sorted(set(params) - STAGE_A_ALLOWED_REQUEST_PARAMS)
            if extra:
                failures.append(
                    f"reused probe {raw_id!r} used filtered request parameters: {extra}")
        except (ValueError, TypeError):
            failures.append(f"reused probe {raw_id!r} has unreadable request params")

    # At most one eligible reused probe per bucket -- otherwise a curator could
    # silently select the convenient candidate.
    per_bucket: dict[str, int] = {}
    for attempt in attempts:
        if attempt["outcome"] == "reused_probe_response":
            bucket = str(attempt["requested_at_bucket"])
            per_bucket[bucket] = per_bucket.get(bucket, 0) + 1
    for bucket, count in sorted(per_bucket.items()):
        if count > 1:
            failures.append(
                f"bucket {bucket!r} has {count} reused probe responses; refusing "
                f"rather than selecting one")
    return failures


def _response_integrity_failures(
    conn: sqlite3.Connection, attempts: Sequence[sqlite3.Row], plan: sqlite3.Row
) -> list[str]:
    failures: list[str] = []
    registered_at = str(plan["acq_registered_at"])
    for attempt in attempts:
        if attempt["raw_response_id"] is None:
            continue
        raw_id = str(attempt["raw_response_id"])
        row = conn.execute(
            "SELECT requested_at, received_at, provider FROM raw_responses"
            " WHERE raw_response_id = ?", (raw_id,)).fetchone()
        if row is None:
            failures.append(f"attempt cites unknown raw response {raw_id!r}")
            continue
        if str(row["provider"]) != str(plan["provider"]):
            failures.append(
                f"raw response {raw_id!r} is from provider {row['provider']!r}, "
                f"not the plan's {plan['provider']!r}")
        # Re-checked here, not only in the trigger, because rows written before
        # f022 existed were never subject to it.
        if str(row["received_at"]) < str(row["requested_at"]):
            failures.append(
                f"raw response {raw_id!r} was received before it was requested")
        if (attempt["outcome"] != "reused_probe_response"
                and str(row["requested_at"]) < registered_at):
            failures.append(
                f"ordinary attempt cites raw response {raw_id!r} acquired before "
                f"the acquisition was registered")
    return failures


def _observation_failures(
    conn: sqlite3.Connection, attempts: Sequence[sqlite3.Row], plan: sqlite3.Row
) -> list[str]:
    """Observation completeness, orphan detection and observed_at ownership."""

    failures: list[str] = []
    acquisition_response_ids = {
        str(a["raw_response_id"]) for a in attempts if a["raw_response_id"] is not None}
    should_project = {
        str(a["raw_response_id"]) for a in attempts
        if a["outcome"] in PROJECTING_OUTCOMES and a["raw_response_id"] is not None}

    if not acquisition_response_ids:
        return failures

    placeholders = ",".join("?" for _ in acquisition_response_ids)
    observed = list(conn.execute(
        "SELECT o.raw_response_id, o.observed_at, r.received_at"
        " FROM historical_market_event_observations o"
        " JOIN raw_responses r ON r.raw_response_id = o.raw_response_id"
        f" WHERE o.raw_response_id IN ({placeholders})",
        tuple(acquisition_response_ids)))

    for row in observed:
        if str(row["observed_at"]) != str(row["received_at"]):
            failures.append(
                f"observation on response {row['raw_response_id']!r} has an "
                f"observed_at that is not the cited response's received_at")

    # An orphan is an observation citing a response this acquisition never
    # acquired -- evidence that entered the lane from outside the ledger.
    orphans = [
        str(r["raw_response_id"]) for r in conn.execute(
            "SELECT DISTINCT o.raw_response_id FROM"
            " historical_market_event_observations o"
            " LEFT JOIN stage_a_request_attempts a"
            "   ON a.raw_response_id = o.raw_response_id"
            " WHERE a.attempt_id IS NULL")
    ]
    if orphans:
        failures.append(
            f"{len(orphans)} observation response(s) belong to no request attempt "
            f"(first: {orphans[:3]})")

    # A response recorded as a full snapshot must actually carry observations.
    with_observations = {str(r["raw_response_id"]) for r in observed}
    for attempt in attempts:
        if attempt["outcome"] != "success_full_snapshot":
            continue
        raw_id = str(attempt["raw_response_id"])
        if raw_id not in with_observations:
            failures.append(
                f"response {raw_id!r} is recorded as a full snapshot but projected "
                f"no observations")
    unexpected = with_observations - should_project
    if unexpected:
        failures.append(
            f"observation(s) exist for response(s) not recorded as projecting "
            f"evidence: {sorted(unexpected)[:3]}")

    # ---------------------------------------------------------------------
    # COMPOSE the independently accepted projection / body verifier.
    #
    # Everything above is bookkeeping ABOUT observations; none of it opens the
    # preserved body. Without this block a response whose body lists N events
    # can be recorded with a single convenient observation and still certify --
    # which is exactly the L1 selective-materialization defect the projection
    # verifier was accepted to close. Reimplementing a weaker copy here would
    # reintroduce it, so the accepted verifier is CALLED.
    # ---------------------------------------------------------------------
    for raw_id in sorted(should_project):
        report = verify_historical_event_projections(conn, raw_id)
        if not report.verified:
            detail = report.detail or (report.rejection_code.value
                                       if report.rejection_code else "")
            if report.missing:
                failures.append(
                    f"response {raw_id!r} body contains {len(report.missing)} "
                    f"event(s) with no stored observation: "
                    f"{list(report.missing)[:3]}")
            if report.unexpected:
                failures.append(
                    f"response {raw_id!r} has {len(report.unexpected)} stored "
                    f"observation(s) not derivable from its body: "
                    f"{list(report.unexpected)[:3]}")
            if report.hash_mismatches:
                failures.append(
                    f"response {raw_id!r} has {len(report.hash_mismatches)} "
                    f"observation(s) whose content hash or id disagrees with "
                    f"their own columns")
            if not (report.missing or report.unexpected or report.hash_mismatches):
                failures.append(
                    f"response {raw_id!r} failed body projection verification: "
                    f"{detail or report.verdict}")

    # The outcome label is a CALLER CLAIM. Derive full-vs-empty from the body so
    # a real market event cannot be erased by mislabelling the ledger, and a
    # malformed body cannot be recorded as a success.
    failures.extend(_outcome_claim_failures(conn, attempts))

    # Content-hash integrity across the whole observation set, using the
    # accepted verifier rather than a local re-derivation.
    mismatches = verify_observation_content_hashes(conn)
    scoped = [m for m in mismatches if _observation_is_in(conn, m.observation_id,
                                                          acquisition_response_ids)]
    if scoped:
        failures.append(
            f"{len(scoped)} observation(s) in this acquisition have a forged or "
            f"stale content hash (first: {scoped[0].observation_id!r})")
    return failures


def _observation_is_in(
    conn: sqlite3.Connection, observation_id: str, response_ids: set[str]
) -> bool:
    row = conn.execute(
        "SELECT raw_response_id FROM historical_market_event_observations"
        " WHERE observation_id = ?", (observation_id,)).fetchone()
    return row is not None and str(row["raw_response_id"]) in response_ids


def _outcome_claim_failures(
    conn: sqlite3.Connection, attempts: Sequence[sqlite3.Row]
) -> list[str]:
    """Verify each recorded outcome against what the preserved body actually is."""

    failures: list[str] = []
    for attempt in attempts:
        outcome = str(attempt["outcome"])
        if outcome not in PROJECTING_OUTCOMES:
            continue
        raw_id = str(attempt["raw_response_id"])
        row = conn.execute(
            "SELECT * FROM raw_responses WHERE raw_response_id = ?",
            (raw_id,)).fetchone()
        if row is None:
            continue
        try:
            projection = project_historical_events_response(row)
        except Exception as exc:  # noqa: BLE001 - any projection failure is a refusal
            failures.append(
                f"response {raw_id!r} is recorded as {outcome!r} but its body does "
                f"not project as a valid historical events snapshot: "
                f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        event_count = len(projection.observation_ids)
        if outcome == "success_empty_data" and event_count:
            failures.append(
                f"response {raw_id!r} is recorded as success_empty_data but its "
                f"body contains {event_count} event(s); a real market event "
                f"cannot be erased by the ledger label")
        if outcome == "success_full_snapshot" and not event_count:
            failures.append(
                f"response {raw_id!r} is recorded as success_full_snapshot but "
                f"its body contains no events")
    return failures


# --------------------------------------------------------------------------- #
# Corpus enrichment -- content-addressed, by supersession
# --------------------------------------------------------------------------- #
def enrich_corpus_with_market_lane(
    conn: sqlite3.Connection,
    repository: object,
    *,
    parent_corpus_id: str,
    acquisition_ids: Sequence[str],
    provider: str,
    namespace_generation: str,
    manifests: Mapping[str, StageAManifest],
    manifest_texts: Mapping[str, str],
) -> tuple[str, str]:
    """Derive the enriched corpus C2 from official corpus C1 plus a certified lane.

    The corpus is CONTENT-ADDRESSED: f018 declares ``UNIQUE (semantic_digest)``
    with "The semantic digest IS the corpus identity", and that digest already
    includes ``market_evidence_digest``. So the E0 lane is NOT appended to C1.
    A new corpus C2 is recorded whose ``market_evidence_digest`` is the certified
    lane digest, C2 supersedes C1, and C1 is never touched -- every audit and
    crosswalk bound to C1 keeps its exact meaning forever.

    Returns ``(new_corpus_version_id, lane_binding_id)``.
    """

    conn.row_factory = sqlite3.Row
    members = list(dict.fromkeys(acquisition_ids))
    if len(members) != len(list(acquisition_ids)):
        # Silently collapsing a duplicate would mask an upstream bug that built
        # the member list wrongly.
        raise StageAProvenanceError(
            "acquisition_ids contains a duplicate; pass each member exactly once")
    if not members:
        raise StageAProvenanceError(
            "refusing to build an evidence lane with no member acquisitions")

    parent = conn.execute(
        "SELECT * FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
        (parent_corpus_id,)).fetchone()
    if parent is None:
        raise StageAProvenanceError(f"unknown parent corpus {parent_corpus_id!r}")

    # EVERY member must pass the full deterministic gate first. "Certification is
    # derived, never stored" is only true if the consumer that creates
    # load-bearing downstream provenance actually INVOKES the gate -- a derived
    # verdict nobody is required to call is not a trust gate at all. Without
    # this, an acquisition missing 159 of 160 buckets could still mint a C2 that
    # commits to its market evidence.
    for acquisition_id in members:
        manifest = manifests.get(acquisition_id)
        if manifest is None:
            raise StageAProvenanceError(
                f"no manifest supplied for member acquisition {acquisition_id!r}; "
                f"a lane cannot be certified without the declared plan it claims "
                f"to execute")
        text = manifest_texts.get(acquisition_id)
        if text is None:
            # `manifest_text` is optional on certify_stage_a so a low-level
            # subset check can run without the file. It is MANDATORY here:
            # allowing it to be omitted on the corpus-building path would make
            # "skip the source-control binding" a supported mode of the only
            # call that creates load-bearing downstream provenance.
            raise StageAProvenanceError(
                f"no committed manifest text supplied for acquisition "
                f"{acquisition_id!r}; the corpus gate must verify the persisted "
                f"content digest against the committed artefact")
        report = certify_stage_a(
            conn, acquisition_id=acquisition_id, manifest=manifest,
            manifest_text=text)
        if not report.certified:
            raise StageAProvenanceError(
                f"refusing to build an evidence lane from uncertified acquisition "
                f"{acquisition_id!r}:\n  - " + "\n  - ".join(report.failures))

    # Every member acquisition must have been planned against THIS parent's
    # official provenance, or the lane would be attachable to an unrelated corpus.
    projection_versions: set[str] = set()
    response_ids: list[str] = []
    for acquisition_id in members:
        plan = _fetch_plan(conn, acquisition_id)
        if plan["official_source_corpus_digest"] != parent["source_corpus_digest"]:
            raise StageAProvenanceError(
                f"acquisition {acquisition_id!r} was planned from a different "
                f"official source corpus than {parent_corpus_id!r}")
        if plan["official_target_set_digest"] != parent["target_set_digest"]:
            raise StageAProvenanceError(
                f"acquisition {acquisition_id!r} was planned against a different "
                f"official target set than {parent_corpus_id!r}")
        projection_versions.add(str(plan["acq_projection_policy"]))
        response_ids.extend(
            str(r["raw_response_id"]) for r in conn.execute(
                "SELECT raw_response_id FROM stage_a_request_attempts"
                " WHERE acquisition_id = ? AND raw_response_id IS NOT NULL",
                (acquisition_id,)))

    if len(projection_versions) != 1:
        raise StageAProvenanceError(
            f"lane members use mixed projection policies {sorted(projection_versions)}; "
            f"a single lane cannot describe the union -- split them into separate lanes")
    projection_policy_version = projection_versions.pop()

    policy = digest_policy_for_lane(MARKET_EVENTS_E0_LANE)
    digest = lane_evidence_digest(
        conn, policy=policy, provider=provider,
        namespace_generation=namespace_generation,
        raw_response_ids=sorted(set(response_ids)))
    set_digest = acquisition_set_digest(members)

    # ATOMIC. The three writes below (corpus, lane, membership) are one
    # provenance construction. Without a savepoint, a failure at the membership
    # step leaves an ORPHAN C2 -- a content-addressed corpus committing to market
    # evidence with no lane provenance to reconstruct it from, which is worse
    # than no corpus at all because the commitment is unfalsifiable.
    conn.execute("SAVEPOINT enrich_corpus_with_market_lane")
    try:
        return _write_lane(
            conn, repository, parent=parent, parent_corpus_id=parent_corpus_id,
            members=members, provider=provider,
            namespace_generation=namespace_generation, policy=policy,
            digest=digest, set_digest=set_digest,
            projection_policy_version=projection_policy_version)
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT enrich_corpus_with_market_lane")
        raise
    finally:
        conn.execute("RELEASE SAVEPOINT enrich_corpus_with_market_lane")


def _write_lane(
    conn: sqlite3.Connection,
    repository: object,
    *,
    parent: sqlite3.Row,
    parent_corpus_id: str,
    members: Sequence[str],
    provider: str,
    namespace_generation: str,
    policy: FrozenDigestPolicy,
    digest: str,
    set_digest: str,
    projection_policy_version: str,
) -> tuple[str, str]:
    enriched = repository.record_corpus_version(  # type: ignore[attr-defined]
        # The parent's stored values are the raw enum VALUES; the repository
        # takes the enum members, so they are rehydrated rather than passed
        # through as strings.
        provenance_class=ProvenanceClass(parent["provenance_class"]),
        league_id=parent["league_id"],
        reconstruction_policy_version=parent["reconstruction_policy_version"],
        cutoff_policy_id=parent["cutoff_policy_id"],
        cutoff_policy_version=parent["cutoff_policy_version"],
        source_corpus_digest=parent["source_corpus_digest"],
        target_set_digest=parent["target_set_digest"],
        g1_variant=G1Variant(parent["g1_variant"]),
        evidence_registry_digest=parent["evidence_registry_digest"],
        static_identity_map_digest=parent["static_identity_map_digest"],
        # THE point of this function: the corpus identity now commits to the lane.
        market_evidence_digest=digest,
        code_version=parent["code_version"],
        supersedes_corpus_version_id=parent_corpus_id,
    )
    if enriched.corpus_version_id == parent_corpus_id:
        raise StageAProvenanceError(
            "enriched corpus collapsed back onto its parent; the market evidence "
            "digest did not change the corpus identity")

    lane_binding_id = new_evidence_lane_binding_id()
    now = utc_now_iso()
    conn.execute(
        "INSERT INTO corpus_evidence_lane_bindings (lane_binding_id,"
        " corpus_version_id, evidence_lane, provider, namespace_generation,"
        " league_id, digest_policy_version, lane_evidence_digest,"
        " acquisition_set_digest, projection_policy_version, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (lane_binding_id, enriched.corpus_version_id, MARKET_EVENTS_E0_LANE,
         provider, namespace_generation, parent["league_id"], policy.version,
         digest, set_digest, projection_policy_version, now),
    )
    for acquisition_id in sorted(members):
        conn.execute(
            "INSERT INTO corpus_evidence_lane_acquisitions (lane_binding_id,"
            " acquisition_id, created_at) VALUES (?,?,?)",
            (lane_binding_id, acquisition_id, now))
    return enriched.corpus_version_id, lane_binding_id


def verify_lane_binding(
    conn: sqlite3.Connection, *, lane_binding_id: str
) -> tuple[str, ...]:
    """Recompute a stored lane's digests from its actual membership.

    A lane digest is never trusted because it was inserted. This is what makes a
    forged digest unusable: both omission directions fail here.
    """

    conn.row_factory = sqlite3.Row
    lane = conn.execute(
        "SELECT * FROM corpus_evidence_lane_bindings WHERE lane_binding_id = ?",
        (lane_binding_id,)).fetchone()
    if lane is None:
        raise StageAProvenanceError(f"unknown lane binding {lane_binding_id!r}")

    failures: list[str] = []
    members = [str(r["acquisition_id"]) for r in conn.execute(
        "SELECT acquisition_id FROM corpus_evidence_lane_acquisitions"
        " WHERE lane_binding_id = ?", (lane_binding_id,))]
    if not members:
        return ("lane binding has no member acquisitions",)

    if acquisition_set_digest(members) != str(lane["acquisition_set_digest"]):
        failures.append(
            "stored acquisition_set_digest does not match the lane's actual "
            "membership")

    response_ids: list[str] = []
    for acquisition_id in members:
        response_ids.extend(str(r["raw_response_id"]) for r in conn.execute(
            "SELECT raw_response_id FROM stage_a_request_attempts"
            " WHERE acquisition_id = ? AND raw_response_id IS NOT NULL",
            (acquisition_id,)))

    policy = digest_policy_for_lane(str(lane["evidence_lane"]))
    if policy.version != str(lane["digest_policy_version"]):
        failures.append(
            f"lane records digest policy {lane['digest_policy_version']!r} but its "
            f"lane kind resolves to {policy.version!r}")
    recomputed = lane_evidence_digest(
        conn, policy=policy, provider=str(lane["provider"]),
        namespace_generation=str(lane["namespace_generation"]),
        raw_response_ids=sorted(set(response_ids)))
    if recomputed != str(lane["lane_evidence_digest"]):
        failures.append(
            "stored lane_evidence_digest does not match the evidence its member "
            "acquisitions actually contain")

    corpus_commitment = conn.execute(
        "SELECT market_evidence_digest FROM reconstruction_corpus_versions"
        " WHERE corpus_version_id = ?", (str(lane["corpus_version_id"]),)).fetchone()
    if (str(lane["evidence_lane"]) == MARKET_EVENTS_E0_LANE
            and corpus_commitment is not None
            and str(corpus_commitment["market_evidence_digest"]) != recomputed):
        failures.append(
            "parent corpus does not commit to the recomputed lane digest")
    return tuple(failures)

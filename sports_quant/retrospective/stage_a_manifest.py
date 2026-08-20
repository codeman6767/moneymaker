"""The Stage-A acquisition manifest: a deterministic, path-free scientific plan.

Why this is a NEW format rather than a reuse of ``f1a-manifest-v1``
------------------------------------------------------------------
The v22 architecture review examined whether the reviewed F1A manifest could
carry a Stage-A plan and proved it could not:

* ``_SUPPORTED_COST_POLICY_VERSIONS`` is ``{"mlb-cost-v1", "bdl-cost-v1"}``; the
  Odds API has no entry, and ``parse`` refuses anything else.
* ``PilotManifest.body()`` hashes ``scratch_db`` and ``checkpoint_path`` --
  MACHINE-LOCAL FILESYSTEM PATHS -- into the manifest identity. The same logical
  plan checked out at ``C:\\repo`` and ``/home/x/repo`` would have two different
  hashes, which disqualifies it as a reproducible scientific declaration.
* It has no representation for the target population, the target->bucket
  mapping, or the official parent-corpus provenance that Stage A must bind.

So this module reuses the reviewed INFRASTRUCTURE -- canonical JSON, duplicate
key rejection, secret-free bodies, SHA-256 identity -- under a new, additive
format version. ``f1a-manifest-v1`` is untouched and keeps parsing exactly as
before; no existing pilot manifest changes by one byte.

What the plan digest binds, and what it deliberately does not
------------------------------------------------------------
It binds league/provider/namespace/sport, the official parent corpus provenance,
every policy version, the exact sorted bucket set, the exact target set, and the
target->bucket mapping.

It binds the MAPPING, not merely the bucket set, because binding the bucket set
alone is not sufficient: the pilot maps 239 targets onto 160 buckets, so by
pigeonhole many buckets serve more than one target. Dropping one target from a
shared bucket leaves the sorted bucket set byte-identical -- a target silently
disappears from the declared population while the digest is unchanged.

It binds NO local path, no wall clock, no random id and no secret. ``manifest_path``
exists on the persisted plan row as convenience provenance and is never hashed.

This module performs no network and no database I/O.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final, Optional

STAGE_A_MANIFEST_FORMAT_VERSION: Final = "stage-a-manifest-v1"
STAGE_A_PLAN_POLICY_VERSION: Final = "stage-a-plan-v1"

#: Formats this build can read. Additive: a future v2 appends here.
SUPPORTED_STAGE_A_MANIFEST_VERSIONS: Final[frozenset[str]] = frozenset(
    {STAGE_A_MANIFEST_FORMAT_VERSION})
SUPPORTED_STAGE_A_PLAN_VERSIONS: Final[frozenset[str]] = frozenset(
    {STAGE_A_PLAN_POLICY_VERSION})

#: The decision horizon and flooring contract the first pass was designed around.
#: Persisted per plan rather than assumed, so a plan built under a different
#: horizon is a different plan rather than a silently mixed one.
DEFAULT_DECISION_HORIZON_MINUTES: Final = 60
DEFAULT_BUCKET_FLOOR_SECONDS: Final = 300


#: The exact top-level field set of a stage-a-manifest-v1 body. `plan_digest` is
#: popped before this check because it is carried alongside the body, not in it.
_KNOWN_TOP_LEVEL_FIELDS: Final[frozenset[str]] = frozenset({
    "manifest_format_version", "plan_policy_version", "league_id", "provider",
    "namespace_generation", "sport_key", "official_source_corpus_digest",
    "official_target_set_digest", "targets", "buckets",
    "decision_horizon_minutes", "bucket_floor_seconds", "request_budget",
    "credit_budget", "acquisition_policy_version", "projection_policy_version",
    "cost_policy_version"})

_KNOWN_TARGET_FIELDS: Final[frozenset[str]] = frozenset(
    {"canonical_game_id", "requested_at_bucket"})


class StageAManifestError(RuntimeError):
    """A Stage-A manifest is missing, tampered, non-canonical, or unsupported."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON object keys instead of silently taking the last.

    Python's default is last-value-wins, which would let a tampered manifest
    carry two ``targets`` arrays where a reader sees only one.
    """

    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise StageAManifestError(f"duplicate JSON key in Stage-A manifest: {key!r}")
        seen[key] = value
    return seen



def _exact_str(payload: dict[str, Any], field: str, *, where: str = "manifest") -> str:
    """A JSON string, exactly. No coercion.

    `str(...)` would turn the number ``12345`` into ``"12345"``, ``true`` into
    ``"True"`` and ``null`` into the literal string ``"None"`` -- the committed
    artefact would then assert something the frozen parser silently rewrote,
    which is the same defect class as an ignored unknown field.
    """

    value = payload.get(field)
    if not isinstance(value, str):
        raise StageAManifestError(
            f"Stage-A {where} field {field!r} must be a JSON string, got "
            f"{type(value).__name__}; {STAGE_A_MANIFEST_FORMAT_VERSION} does not "
            f"coerce types")
    if not value or value != value.strip():
        raise StageAManifestError(
            f"Stage-A {where} field {field!r} must be non-empty and free of outer "
            f"whitespace")
    return value


def _exact_int(payload: dict[str, Any], field: str, *, minimum: int = 0) -> int:
    """A JSON integer, exactly. No floats, no numeric strings, no booleans.

    ``bool`` is an ``int`` subclass in Python, so ``True`` would otherwise pass an
    ``isinstance(value, int)`` check and become ``1``. ``int(60.9)`` would
    silently truncate a committed ``60.9`` to ``60``.
    """

    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StageAManifestError(
            f"Stage-A manifest field {field!r} must be a JSON integer, got "
            f"{type(value).__name__}; {STAGE_A_MANIFEST_FORMAT_VERSION} does not "
            f"coerce types")
    if value < minimum:
        raise StageAManifestError(
            f"Stage-A manifest field {field!r} must be >= {minimum}, got {value}")
    # SQLite stores a signed 64-bit integer; a larger value would round-trip
    # differently than the committed artefact states.
    if value > 2 ** 63 - 1:
        raise StageAManifestError(
            f"Stage-A manifest field {field!r} exceeds the exactly storable "
            f"integer range")
    return value


def _reject_non_standard(value: str) -> float:
    raise StageAManifestError(
        f"Stage-A manifest contains the non-standard JSON constant {value!r}; "
        f"only strict JSON is accepted")


def canonical_json(payload: dict[str, Any]) -> str:
    """Stable canonical JSON: sorted keys, no insignificant whitespace, UTF-8."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class StageATarget:
    """One declared target: a canonical game and the bucket that serves it."""

    canonical_game_id: str
    requested_at_bucket: str


@dataclass(frozen=True)
class StageAManifest:
    """A complete, canonical, hashable Stage-A acquisition plan."""

    league_id: str
    provider: str
    namespace_generation: str
    sport_key: str
    #: Provenance of the OFFICIAL corpus the targets were drawn from. Lane
    #: attachment later refuses a parent corpus that does not match both.
    official_source_corpus_digest: str
    official_target_set_digest: str
    targets: tuple[StageATarget, ...]
    buckets: tuple[str, ...]
    decision_horizon_minutes: int
    bucket_floor_seconds: int
    request_budget: int
    credit_budget: int
    acquisition_policy_version: str
    projection_policy_version: str
    cost_policy_version: str
    manifest_format_version: str = STAGE_A_MANIFEST_FORMAT_VERSION
    plan_policy_version: str = STAGE_A_PLAN_POLICY_VERSION

    def body(self) -> dict[str, Any]:
        """The canonical, secret-free, PATH-FREE body used for the plan digest."""

        return {
            "manifest_format_version": self.manifest_format_version,
            "plan_policy_version": self.plan_policy_version,
            "league_id": self.league_id,
            "provider": self.provider,
            "namespace_generation": self.namespace_generation,
            "sport_key": self.sport_key,
            "official_source_corpus_digest": self.official_source_corpus_digest,
            "official_target_set_digest": self.official_target_set_digest,
            # Sorted so that enumeration order is never part of identity.
            "targets": [
                {"canonical_game_id": t.canonical_game_id,
                 "requested_at_bucket": t.requested_at_bucket}
                for t in sorted(self.targets, key=lambda t: t.canonical_game_id)
            ],
            "buckets": sorted(self.buckets),
            "decision_horizon_minutes": self.decision_horizon_minutes,
            "bucket_floor_seconds": self.bucket_floor_seconds,
            "request_budget": self.request_budget,
            "credit_budget": self.credit_budget,
            "acquisition_policy_version": self.acquisition_policy_version,
            "projection_policy_version": self.projection_policy_version,
            "cost_policy_version": self.cost_policy_version,
        }

    def canonical(self) -> str:
        return canonical_json(self.body())

    def plan_digest(self) -> str:
        """The SEMANTIC identity of this plan. Contains no local path."""

        return _hash(self.canonical())

    def target_map(self) -> dict[str, str]:
        return {t.canonical_game_id: t.requested_at_bucket for t in self.targets}


def validate(manifest: StageAManifest) -> None:
    """Structural validation. Refuses rather than repairing.

    Every check here is one the certification gate would otherwise have to
    discover after credits were already spent.
    """

    if manifest.manifest_format_version not in SUPPORTED_STAGE_A_MANIFEST_VERSIONS:
        raise StageAManifestError(
            f"unsupported Stage-A manifest format version: "
            f"{manifest.manifest_format_version!r}")
    if manifest.plan_policy_version not in SUPPORTED_STAGE_A_PLAN_VERSIONS:
        raise StageAManifestError(
            f"unsupported Stage-A plan policy version: {manifest.plan_policy_version!r}")
    if not manifest.targets:
        raise StageAManifestError("a Stage-A plan must declare at least one target")
    if not manifest.buckets:
        raise StageAManifestError("a Stage-A plan must declare at least one bucket")
    if manifest.decision_horizon_minutes <= 0:
        raise StageAManifestError("decision_horizon_minutes must be positive")
    if manifest.bucket_floor_seconds <= 0:
        raise StageAManifestError("bucket_floor_seconds must be positive")
    if manifest.request_budget < 0 or manifest.credit_budget < 0:
        raise StageAManifestError("budgets may not be negative")

    buckets = list(manifest.buckets)
    if len(set(buckets)) != len(buckets):
        raise StageAManifestError("the declared bucket set contains a duplicate")

    seen: set[str] = set()
    for target in manifest.targets:
        if target.canonical_game_id in seen:
            # A target appearing twice could otherwise map to two buckets, which
            # would make "which bucket serves this target" ambiguous.
            raise StageAManifestError(
                f"target {target.canonical_game_id!r} is declared more than once")
        seen.add(target.canonical_game_id)
        if target.requested_at_bucket not in set(buckets):
            raise StageAManifestError(
                f"target {target.canonical_game_id!r} maps to bucket "
                f"{target.requested_at_bucket!r}, which the plan does not declare")

    # Every declared bucket must serve at least one target: an unserved bucket is
    # a request that no target justifies, i.e. a credit spent for nothing.
    served = {t.requested_at_bucket for t in manifest.targets}
    orphaned = sorted(set(buckets) - served)
    if orphaned:
        raise StageAManifestError(
            f"declared bucket(s) serve no target: {orphaned}")

    # The request budget must cover the declared work, or the plan is known to be
    # unexecutable before a single credit is spent.
    if manifest.request_budget < len(buckets):
        raise StageAManifestError(
            f"request_budget {manifest.request_budget} cannot cover "
            f"{len(buckets)} declared buckets")


def dumps(manifest: StageAManifest) -> str:
    """Serialize to canonical JSON, with the digest carried alongside the body."""

    validate(manifest)
    payload = manifest.body()
    payload["plan_digest"] = manifest.plan_digest()
    return canonical_json(payload)


def loads(text: str) -> StageAManifest:
    """Parse and verify a committed Stage-A manifest.

    The embedded ``plan_digest`` is never trusted: it is recomputed from the body
    and compared, so a manifest whose declared identity disagrees with its own
    content is refused rather than read.
    """

    try:
        payload = json.loads(text, object_pairs_hook=_no_duplicate_keys,
                             parse_constant=_reject_non_standard)
    except json.JSONDecodeError as exc:
        raise StageAManifestError(f"Stage-A manifest is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageAManifestError("Stage-A manifest must be a JSON object")

    declared: Optional[str] = payload.pop("plan_digest", None)

    # stage-a-manifest-v1 is a CLOSED schema. An unknown top-level field is
    # REFUSED rather than ignored: a future producer could believe such a field
    # is load-bearing while this version silently drops it, which would change
    # the committed artefact's content digest without changing the semantic plan
    # digest -- two artefacts meaning different things but certifying alike.
    unknown = sorted(set(payload) - _KNOWN_TOP_LEVEL_FIELDS)
    if unknown:
        raise StageAManifestError(
            f"Stage-A manifest declares unknown top-level field(s) {unknown}; "
            f"{STAGE_A_MANIFEST_FORMAT_VERSION} is a closed schema, so a field "
            f"this version cannot interpret is refused rather than ignored")

    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list):
        raise StageAManifestError("Stage-A manifest 'targets' must be a list")
    targets: list[StageATarget] = []
    for entry in raw_targets:
        if not isinstance(entry, dict):
            raise StageAManifestError("each Stage-A target must be an object")
        unknown_target = sorted(set(entry) - _KNOWN_TARGET_FIELDS)
        if unknown_target:
            raise StageAManifestError(
                f"Stage-A target declares unknown field(s) {unknown_target}; "
                f"a target is a closed object in "
                f"{STAGE_A_MANIFEST_FORMAT_VERSION}")
        for required in ("canonical_game_id", "requested_at_bucket"):
            if required not in entry:
                raise StageAManifestError(
                    f"Stage-A target is missing required field {required!r}")
        targets.append(StageATarget(
            canonical_game_id=_exact_str(entry, "canonical_game_id",
                                         where="target"),
            requested_at_bucket=_exact_str(entry, "requested_at_bucket",
                                           where="target")))

    raw_buckets = payload.get("buckets")
    if not isinstance(raw_buckets, list):
        raise StageAManifestError("Stage-A manifest 'buckets' must be a list")

    try:
        for required in sorted(_KNOWN_TOP_LEVEL_FIELDS):
            if required not in payload:
                raise StageAManifestError(
                    f"Stage-A manifest is missing required field {required!r}")
        for index, bucket in enumerate(raw_buckets):
            if not isinstance(bucket, str) or not bucket or bucket != bucket.strip():
                raise StageAManifestError(
                    f"Stage-A manifest bucket #{index} must be a non-empty JSON "
                    f"string without outer whitespace, got "
                    f"{type(bucket).__name__}")
        manifest = StageAManifest(
            league_id=_exact_str(payload, "league_id"),
            provider=_exact_str(payload, "provider"),
            namespace_generation=_exact_str(payload, "namespace_generation"),
            sport_key=_exact_str(payload, "sport_key"),
            official_source_corpus_digest=_exact_str(
                payload, "official_source_corpus_digest"),
            official_target_set_digest=_exact_str(
                payload, "official_target_set_digest"),
            targets=tuple(targets),
            buckets=tuple(raw_buckets),
            decision_horizon_minutes=_exact_int(
                payload, "decision_horizon_minutes", minimum=1),
            bucket_floor_seconds=_exact_int(
                payload, "bucket_floor_seconds", minimum=1),
            request_budget=_exact_int(payload, "request_budget"),
            credit_budget=_exact_int(payload, "credit_budget"),
            acquisition_policy_version=_exact_str(
                payload, "acquisition_policy_version"),
            projection_policy_version=_exact_str(
                payload, "projection_policy_version"),
            cost_policy_version=_exact_str(payload, "cost_policy_version"),
            manifest_format_version=_exact_str(payload, "manifest_format_version"),
            plan_policy_version=_exact_str(payload, "plan_policy_version"),
        )
    except KeyError as exc:
        raise StageAManifestError(
            f"Stage-A manifest is missing required field {exc}") from None

    validate(manifest)

    if declared is not None and declared != manifest.plan_digest():
        raise StageAManifestError(
            "Stage-A manifest declares a plan_digest that does not match its own "
            "content; refusing to read a manifest whose identity disagrees with "
            "what it says")
    return manifest


def manifest_content_digest_bytes(raw: bytes) -> str:
    """Digest the EXACT committed blob bytes.

    This is the load-bearing form. Hashing decoded text would fingerprint a
    newline-normalized rendering rather than the artefact that was committed.
    """

    return hashlib.sha256(raw).hexdigest()


def manifest_content_digest(text: str) -> str:
    """Digest of the committed manifest FILE bytes, as stored on the plan row.

    Distinct from ``plan_digest``: this fingerprints the exact committed text
    (formatting included), while ``plan_digest`` is the semantic identity. The
    verifier compares both -- the first proves the file was not edited, the
    second proves the meaning is the declared one.
    """

    return _hash(text)

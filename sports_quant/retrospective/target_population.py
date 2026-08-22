"""Corpus target-population binding: construct-then-seal, and the verifier (v23).

A corpus is TARGET-BOUND only if every one of these holds:

* a seal row exists;
* its policy versions are known;
* `member_count > 0` and matches the stored membership exactly;
* run bindings exist and equal the run set REQUIRED by the precommitted manifest;
* no listing cap bound the acquisition;
* the listing cursor chain closes;
* every provider game projects to a resolved canonical member;
* the recomputed members / derivation / binding digests reproduce the stored
  `target_set_digest`;
* the scoped source digest reproduces `source_corpus_digest`.

Anything else is LEGACY / TARGET-UNBOUND -- including a corpus whose
`target_set_digest` happens to be a plausible 64-hex string. No pattern creates
authority; only recomputation does.

The target-bound corpus is a **sibling**, not a superseding restatement, of the
target-unbound official corpus: it makes a claim the old corpus never made, so
`supersedes_corpus_version_id` is deliberately left NULL and would manufacture a
lineage if set.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Final, Optional

from ..db.schema import utc_now_iso
from .listing_projection import (
    BALLDONTLIE_PROVIDER,
    LISTING_PROJECTION_POLICY_V1,
    NBA_GAMES_LISTING_ENDPOINT,
    SUPPORTED_LISTING_PROJECTION_POLICIES,
    ListingProjectionError,
    admitted_listing_responses,
    project_targets,
    verify_cursor_chain,
    verify_response_integrity,
)
from .target_binding import (
    SUPPORTED_TARGET_SET_POLICIES,
    TARGET_SET_POLICY_V1,
    TargetBindingError,
    derivation_digest,
    members_digest,
    target_binding_digest,
)

__all__ = [
    "ACQUISITION_COMPLETENESS_POLICY_V1",
    "TARGET_SOURCE_SCOPE_POLICY_V1",
    "TARGET_BOUND_RECONSTRUCTION_POLICY_V1",
    "SUPPORTED_ACQUISITION_COMPLETENESS_POLICIES",
    "TargetPopulationError",
    "AcquisitionBinding",
    "TargetPopulationReport",
    "load_acquisition_binding",
    "required_listing_runs",
    "scoped_source_digest",
    "seal_target_population",
    "verify_corpus_target_population",
    "verified_target_members",
]

#: How "the required run set" and "no cap bound" are decided.
ACQUISITION_COMPLETENESS_POLICY_V1: Final = "acquisition-completeness-v1"
#: How the bound listing evidence is fingerprinted into `source_corpus_digest`.
TARGET_SOURCE_SCOPE_POLICY_V1: Final = "target-source-scope-v1"
#: The reconstruction policy a target-bound corpus declares. A legacy corpus can
#: never acquire it retroactively, so target-boundness is visible in the corpus
#: row itself and not only in child tables.
TARGET_BOUND_RECONSTRUCTION_POLICY_V1: Final = "target-bound-reconstruction-v1"

SUPPORTED_ACQUISITION_COMPLETENESS_POLICIES: Final = frozenset(
    {ACQUISITION_COMPLETENESS_POLICY_V1})

#: The manifest family that authorizes game-listing acquisition. A manifest that
#: does not declare it cannot bind a listing-derived target population (RV-6).
LISTING_FAMILY: Final = "games"

_TARGETS_TABLE: Final = "reconstruction_corpus_targets"
_RUNS_TABLE: Final = "reconstruction_corpus_target_runs"
_SEALS_TABLE: Final = "reconstruction_corpus_target_seals"
_MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024


class TargetPopulationError(RuntimeError):
    """Target-population construction or verification refused."""


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Acquisition manifest + checkpoint binding
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AcquisitionBinding:
    """A precommitted acquisition manifest, optionally cross-checked by a
    resume checkpoint.

    The manifest is the AUTHORITY: it is precommitted and hashed. The checkpoint
    is preserved evidence used to cross-check, never to define target membership
    -- `stage_game_ids` records the SELECTED set, and selection is not the same
    claim as the complete official population.
    """

    manifest_hash: str
    plan_version: str
    provider: str
    league: str
    date_range: str
    families: tuple[str, ...]
    #: Validated at parse time (RV-5), never re-split from `date_range` on demand.
    start_date: str
    end_date: str
    max_pages: Optional[int]
    max_records: Optional[int]
    max_games: Optional[int]
    checkpoint_stage_game_ids: tuple[str, ...] = ()
    checkpoint_present: bool = False


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Refuse duplicate JSON keys instead of silently taking the last value.

    Independent review defect RV-4. `json.loads` is last-value-wins, so a
    manifest carrying `"plan_version":"good"` followed by `"plan_version":"evil"`
    parsed as `"evil"` while a human reading the file sees the first. This is the
    third time the B2 defect class has appeared, so it is refused here too.
    """

    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise TargetPopulationError(
                f"duplicate JSON key {key!r}; a provenance artefact must have "
                f"exactly one value per field")
        seen[key] = value
    return seen


def _reject_non_standard(value: str) -> float:
    raise TargetPopulationError(
        f"non-standard JSON constant {value!r}; only strict JSON is accepted")


def _read_json(path: Path, *, what: str) -> tuple[bytes, dict[str, Any]]:
    if path.is_symlink():
        raise TargetPopulationError(f"refusing to read a {what} through a symlink: {path}")
    if not path.is_file():
        raise TargetPopulationError(f"no {what} at {path}")
    if path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise TargetPopulationError(f"{what} too large: {path}")
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"),
                             object_pairs_hook=_reject_duplicate_keys,
                             parse_constant=_reject_non_standard)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TargetPopulationError(f"{what} is not valid JSON: {exc}") from None
    if not isinstance(payload, dict):
        raise TargetPopulationError(f"{what} root must be a JSON object")
    return raw, payload


def _exact_str(payload: dict[str, Any], field: str, *, what: str) -> str:
    """A real JSON string. No coercion.

    Independent review defect RV-3: `str(manifest["plan_version"])` turned
    `None` into `"None"`, `True` into `"True"` and `1` into `"1"`, so three
    different manifests could claim the same plan version. `bool` is checked
    first because it is an `int` subclass.
    """

    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, str):
        raise TargetPopulationError(
            f"{what} field {field!r} must be a JSON string, got "
            f"{type(value).__name__}")
    if not value or value != value.strip():
        raise TargetPopulationError(
            f"{what} field {field!r} must be non-empty and carry no surrounding "
            f"whitespace")
    return value


def _opt_int(bounds: dict[str, Any], key: str) -> Optional[int]:
    value = bounds.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TargetPopulationError(f"manifest bound {key} must be an integer")
    return value


def _parse_date_range(text: str) -> tuple[str, str]:
    """`YYYY-MM-DD..YYYY-MM-DD`, both real calendar dates, start <= end.

    Independent review defect RV-5: splitting on `".."` accepted a missing
    delimiter (start == end silently), a reversed range, `2026-02-30`, embedded
    whitespace, and a third segment.
    """

    parts = text.split("..")
    if len(parts) != 2:
        raise TargetPopulationError(
            f"date_range {text!r} must be exactly 'START..END'")
    start, end = parts
    for label, value in (("start", start), ("end", end)):
        if len(value) != 10 or value != value.strip():
            raise TargetPopulationError(
                f"date_range {label} {value!r} must be a bare YYYY-MM-DD date")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise TargetPopulationError(
                f"date_range {label} {value!r} is not a real calendar date") from None
    if start > end:
        raise TargetPopulationError(
            f"date_range {text!r} ends before it starts")
    return start, end


def load_acquisition_binding(
    manifest_path: Path | str,
    *,
    checkpoint_path: Optional[Path | str] = None,
) -> AcquisitionBinding:
    """Load the precommitted manifest and, if given, cross-check the checkpoint.

    The manifest hash is the sha256 of the exact file BYTES, matching what
    `checkpoint.manifest_hash` records, so the two are directly comparable
    without re-serializing (which would introduce a canonicalization the
    historical artefacts never used).

    A manifest/checkpoint disagreement REFUSES rather than preferring one.
    """

    manifest_path = Path(manifest_path)
    raw, manifest = _read_json(manifest_path, what="acquisition manifest")
    manifest_hash = hashlib.sha256(raw).hexdigest()

    for required in ("plan_version", "league", "date_range", "families"):
        if required not in manifest:
            raise TargetPopulationError(
                f"acquisition manifest is missing required field {required!r}")

    plan_version = _exact_str(manifest, "plan_version", what="acquisition manifest")
    league = _exact_str(manifest, "league", what="acquisition manifest")
    date_range = _exact_str(manifest, "date_range", what="acquisition manifest")
    start_date, end_date = _parse_date_range(date_range)
    provider = (_exact_str(manifest, "provider", what="acquisition manifest")
                if "provider" in manifest else BALLDONTLIE_PROVIDER)

    families_raw = manifest["families"]
    if not isinstance(families_raw, list) or not families_raw:
        raise TargetPopulationError(
            "acquisition manifest families must be a non-empty list")
    families: list[str] = []
    for entry in families_raw:
        if isinstance(entry, bool) or not isinstance(entry, str) or not entry.strip():
            raise TargetPopulationError(
                f"acquisition manifest family {entry!r} must be a non-empty string")
        if entry in families:
            raise TargetPopulationError(
                f"acquisition manifest lists family {entry!r} twice")
        families.append(entry)
    # RV-6: the manifest must actually AUTHORIZE the listing family whose
    # responses the target population is derived from. Without this the binding
    # is decorative -- a manifest declaring only `stats` certified a population
    # built from `/v1/games` responses that merely happened to exist.
    if LISTING_FAMILY not in families:
        raise TargetPopulationError(
            f"acquisition manifest does not authorize the {LISTING_FAMILY!r} family "
            f"(declares {families}); it cannot bind a listing-derived target "
            f"population")

    bounds = manifest.get("bounds") or {}
    if not isinstance(bounds, dict):
        raise TargetPopulationError("acquisition manifest bounds must be an object")

    stage_ids: tuple[str, ...] = ()
    checkpoint_present = False
    if checkpoint_path is not None:
        _, checkpoint = _read_json(Path(checkpoint_path), what="resume checkpoint")
        checkpoint_present = True
        ck_families = checkpoint.get("families")
        if not isinstance(ck_families, list):
            raise TargetPopulationError("checkpoint families must be a list")
        mismatches = [
            name for name, left, right in (
                ("manifest_hash", checkpoint.get("manifest_hash"), manifest_hash),
                ("plan_version", checkpoint.get("plan_version"), plan_version),
                ("league", checkpoint.get("league"), league),
                ("date_range", checkpoint.get("date_range"), date_range),
                ("families", tuple(ck_families), tuple(families)),
            ) if left != right
        ]
        if mismatches:
            raise TargetPopulationError(
                "resume checkpoint contradicts the acquisition manifest: "
                + ", ".join(sorted(mismatches)))
        raw_ids = checkpoint.get("stage_game_ids") or []
        if not isinstance(raw_ids, list):
            raise TargetPopulationError("checkpoint stage_game_ids must be a list")
        ids: list[str] = []
        for entry in raw_ids:
            if isinstance(entry, bool) or not isinstance(entry, (str, int)):
                raise TargetPopulationError(
                    f"checkpoint stage_game_ids entry {entry!r} must be a string or "
                    f"integer provider game id")
            ids.append(str(entry))
        if len(set(ids)) != len(ids):
            raise TargetPopulationError(
                "checkpoint stage_game_ids contains a duplicate provider game id")
        stage_ids = tuple(ids)

    return AcquisitionBinding(
        manifest_hash=manifest_hash,
        plan_version=plan_version,
        provider=provider,
        league=league,
        date_range=date_range,
        families=tuple(families),
        start_date=start_date,
        end_date=end_date,
        max_pages=_opt_int(bounds, "max_pages"),
        max_records=_opt_int(bounds, "max_records"),
        max_games=_opt_int(bounds, "max_games"),
        checkpoint_stage_game_ids=stage_ids,
        checkpoint_present=checkpoint_present)


def required_listing_runs(
    conn: sqlite3.Connection,
    binding: AcquisitionBinding,
    *,
    endpoint: str = NBA_GAMES_LISTING_ENDPOINT,
    policy_version: str = ACQUISITION_COMPLETENESS_POLICY_V1,
) -> tuple[str, ...]:
    """The run set the manifest REQUIRES, derived from evidence, not from the caller.

    This is the answer to the review's first primary attack. A caller who binds
    R1+R2 and omits R3 produces membership that is internally perfect; the only
    way to catch it is to derive the required set independently and demand exact
    equality. Requirement is defined as: every run holding a successful listing
    response for this provider, endpoint and the manifest's exact date window.
    """

    if policy_version not in SUPPORTED_ACQUISITION_COMPLETENESS_POLICIES:
        raise TargetPopulationError(
            f"unknown acquisition-completeness policy {policy_version!r}")
    rows = conn.execute(
        "SELECT DISTINCT run_id, request_params_json FROM raw_responses "
        "WHERE provider = ? AND endpoint = ? AND http_status = 200",
        (binding.provider, endpoint)).fetchall()
    runs: set[str] = set()
    for run_id, params_json in rows:
        try:
            params = json.loads(params_json)
        except json.JSONDecodeError:
            continue
        if not isinstance(params, dict):
            continue
        if (str(params.get("start_date")) == binding.start_date
                and str(params.get("end_date")) == binding.end_date):
            runs.add(str(run_id))
    return tuple(sorted(runs))


def _cap_problems(binding: AcquisitionBinding, *, pages: int, provider_games: int,
                  members: int) -> list[str]:
    """Prove no truncating cap bound the listing acquisition.

    Strict inequality is deliberate. A run that used exactly `max_pages` pages
    cannot be distinguished from one the cap stopped, even if the last body
    happens to carry a null cursor -- so an unambiguous margin is required rather
    than assumed. This is the §12 rule: a deliberately capped listing acquisition
    may not be sealed as a complete target population.
    """

    problems: list[str] = []
    if binding.max_pages is not None and pages >= binding.max_pages:
        problems.append(
            f"listing used {pages} pages against max_pages={binding.max_pages}; "
            f"a bound cap cannot be distinguished from a natural terminus")
    if binding.max_records is not None and provider_games >= binding.max_records:
        problems.append(
            f"listing returned {provider_games} records against "
            f"max_records={binding.max_records}")
    if binding.max_games is not None and members >= binding.max_games:
        problems.append(
            f"listing yielded {members} games against max_games={binding.max_games}")
    return problems


def scoped_source_digest(
    rows: Sequence[sqlite3.Row],
    *,
    provider: str = BALLDONTLIE_PROVIDER,
    endpoint: str = NBA_GAMES_LISTING_ENDPOINT,
    policy_version: str = TARGET_SOURCE_SCOPE_POLICY_V1,
) -> str:
    """Fingerprint EXACTLY the bound listing evidence, nothing broader.

    The review requires the source digest and the derivation digest to refer to
    the same bounded evidence population. Inheriting a broad official corpus
    digest would overstate the source set and would let corpus A's members pair
    with corpus B's run bindings.

    Unrelated official responses elsewhere in the database do not participate,
    because they are never admitted; removing a bound response changes this
    digest AND breaks the cursor chain.
    """

    if policy_version != TARGET_SOURCE_SCOPE_POLICY_V1:
        raise TargetPopulationError(
            f"unknown target-source-scope policy {policy_version!r}")
    return _sha256(_canonical({
        "policy": policy_version,
        "provider": provider,
        "endpoint": endpoint,
        "responses": sorted(
            [str(r["raw_response_id"]), str(r["content_hash"])] for r in rows),
    }))


# --------------------------------------------------------------------------- #
# Verification
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TargetPopulationReport:
    """Every discrepancy found while verifying one corpus's target population."""

    corpus_version_id: str
    target_bound: bool = False
    member_count: int = 0
    pages: int = 0
    members: tuple[str, ...] = ()
    bound_runs: tuple[str, ...] = ()
    required_runs: tuple[str, ...] = ()
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.target_bound and not self.problems

    def as_json(self) -> dict[str, object]:
        return {
            "corpus_version_id": self.corpus_version_id,
            "target_bound": self.target_bound,
            "member_count": self.member_count,
            "listing_pages": self.pages,
            "bound_runs": list(self.bound_runs),
            "required_runs": list(self.required_runs),
            "problems": list(self.problems),
            "ok": self.ok,
        }


def _seal_row(conn: sqlite3.Connection, corpus_version_id: str) -> Optional[sqlite3.Row]:
    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            f"SELECT * FROM {_SEALS_TABLE} WHERE corpus_version_id = ?",
            (corpus_version_id,)).fetchone()
    finally:
        conn.row_factory = prior


def verify_corpus_target_population(
    conn: sqlite3.Connection,
    corpus_version_id: str,
    *,
    manifest_path: Path | str,
    checkpoint_path: Optional[Path | str] = None,
) -> TargetPopulationReport:
    """Independently re-derive the target population. Never trusts a stored digest.

    Takes no caller-supplied expected member set: the whole point is that the
    expectation comes from evidence. A Stage-A manifest may never supply it.
    """

    prior = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        corpus = conn.execute(
            "SELECT corpus_version_id, league_id, reconstruction_policy_version, "
            "       source_corpus_digest, target_set_digest "
            "FROM reconstruction_corpus_versions WHERE corpus_version_id = ?",
            (corpus_version_id,)).fetchone()
    finally:
        conn.row_factory = prior
    if corpus is None:
        return TargetPopulationReport(
            corpus_version_id,
            problems=(f"corpus {corpus_version_id!r} does not exist",))

    # 1. A seal is REQUIRED. An unsealed corpus is open by construction, so its
    #    absence is a hard failure and never a warning.
    seal = _seal_row(conn, corpus_version_id)
    if seal is None:
        return TargetPopulationReport(
            corpus_version_id,
            problems=("corpus has no target seal: it is LEGACY / TARGET-UNBOUND "
                      "(a plausible-looking target_set_digest confers no authority)",))

    problems: list[str] = []

    # 2. Frozen policy versions must be known -- never guessed by trying each.
    for column, supported in (
            ("target_set_policy_version", SUPPORTED_TARGET_SET_POLICIES),
            ("listing_projection_policy_version", SUPPORTED_LISTING_PROJECTION_POLICIES),
            ("acquisition_completeness_policy_version",
             SUPPORTED_ACQUISITION_COMPLETENESS_POLICIES)):
        if seal[column] not in supported:
            problems.append(
                f"seal declares unknown {column} {seal[column]!r}")
    if problems:
        return TargetPopulationReport(corpus_version_id, problems=tuple(problems))

    league_id = str(corpus["league_id"])

    # 3. Manifest binding.
    try:
        binding = load_acquisition_binding(manifest_path, checkpoint_path=checkpoint_path)
    except TargetPopulationError as exc:
        return TargetPopulationReport(corpus_version_id, problems=(str(exc),))
    if binding.manifest_hash != seal["acquisition_manifest_hash"]:
        problems.append(
            f"supplied manifest hashes to {binding.manifest_hash[:16]}... but the "
            f"seal commits {str(seal['acquisition_manifest_hash'])[:16]}...")
    if binding.plan_version != seal["plan_version"]:
        problems.append(
            f"manifest plan_version {binding.plan_version!r} != sealed "
            f"{seal['plan_version']!r}")
    if problems:
        return TargetPopulationReport(corpus_version_id, problems=tuple(problems))

    # 4/5. Required run set vs bound run set: exact keyed equality.
    bound_runs = tuple(sorted(
        str(r[0]) for r in conn.execute(
            f"SELECT run_id FROM {_RUNS_TABLE} WHERE corpus_version_id = ?",
            (corpus_version_id,)).fetchall()))
    required = required_listing_runs(
        conn, binding, policy_version=str(seal["acquisition_completeness_policy_version"]))
    missing = sorted(set(required) - set(bound_runs))
    extra = sorted(set(bound_runs) - set(required))
    if missing:
        problems.append(
            f"acquisition requires runs the corpus does not bind: {missing}")
    if extra:
        problems.append(
            f"corpus binds runs the acquisition does not require: {extra}")

    # 6/7. Admission + cursor-chain closure.
    try:
        rows = admitted_listing_responses(
            conn, run_ids=bound_runs or required, provider=binding.provider)
        chain = verify_cursor_chain(rows)
    except (ListingProjectionError, TargetPopulationError) as exc:
        return TargetPopulationReport(
            corpus_version_id, bound_runs=bound_runs, required_runs=required,
            problems=(*problems, str(exc)))
    # RV-1. Recompute the preserved evidence's own hashes BEFORE parsing any
    # body. The scoped source digest fingerprints the STORED content_hash, so a
    # forged body left with its original hashes would not disturb it.
    integrity = verify_response_integrity(rows)
    problems.extend(integrity)

    problems.extend(chain.problems)

    # RV-7. The checkpoint, when supplied, is cross-checked against the derived
    # provider population rather than merely loaded. `stage_game_ids` records the
    # SELECTED set, so it must be a subset of what the listing actually returned;
    # a checkpoint naming games the listing never produced is not evidence about
    # this acquisition.
    if binding.checkpoint_stage_game_ids:
        listed = set(chain.provider_game_ids)
        stray = sorted(set(binding.checkpoint_stage_game_ids) - listed)
        if stray:
            problems.append(
                f"checkpoint stage_game_ids name provider games the bound listing "
                f"never returned: {stray[:5]}")

    # 8/9/10. Projection to canonical members, failing closed.
    try:
        projection = project_targets(conn, chain=chain, league_id=league_id,
                                     provider=binding.provider)
    except ListingProjectionError as exc:
        return TargetPopulationReport(
            corpus_version_id, bound_runs=bound_runs, required_runs=required,
            problems=(*problems, str(exc)))
    problems.extend(p for p in projection.problems if p not in problems)

    # No truncating cap may have bound the acquisition.
    problems.extend(_cap_problems(
        binding, pages=chain.pages,
        provider_games=len(chain.provider_game_ids),
        members=len(projection.members)))

    # 11. Stored membership must equal the derived set exactly, both directions.
    stored_members = tuple(sorted(
        str(r[0]) for r in conn.execute(
            f"SELECT game_id FROM {_TARGETS_TABLE} WHERE corpus_version_id = ?",
            (corpus_version_id,)).fetchall()))
    if stored_members != projection.members:
        only_stored = sorted(set(stored_members) - set(projection.members))
        only_derived = sorted(set(projection.members) - set(stored_members))
        if only_stored:
            problems.append(f"stored members not derivable from evidence: {only_stored}")
        if only_derived:
            problems.append(f"derived members missing from stored membership: {only_derived}")

    # 12-15. Digest recomputation.
    if not problems:
        try:
            md = members_digest(league_id=league_id, members=list(projection.members))
            dd = derivation_digest(
                acquisition_manifest_hash=binding.manifest_hash,
                plan_version=binding.plan_version, run_ids=list(bound_runs))
            bd = target_binding_digest(
                league_id=league_id, members_digest_value=md,
                derivation_digest_value=dd)
        except TargetBindingError as exc:
            return TargetPopulationReport(
                corpus_version_id, bound_runs=bound_runs, required_runs=required,
                problems=(*problems, str(exc)))
        if bd != corpus["target_set_digest"]:
            problems.append(
                f"recomputed target-binding digest {bd[:16]}... != stored "
                f"target_set_digest {str(corpus['target_set_digest'])[:16]}...")

        # 16. Scoped source digest.
        sd = scoped_source_digest(rows, provider=binding.provider)
        if sd != corpus["source_corpus_digest"]:
            problems.append(
                f"recomputed scoped source digest {sd[:16]}... != stored "
                f"source_corpus_digest {str(corpus['source_corpus_digest'])[:16]}...")

    # 17. member_count.
    if int(seal["member_count"]) != len(stored_members):
        problems.append(
            f"seal member_count {seal['member_count']} != {len(stored_members)} "
            f"stored members")

    # 18. The corpus must declare the target-bound reconstruction policy.
    if corpus["reconstruction_policy_version"] != TARGET_BOUND_RECONSTRUCTION_POLICY_V1:
        problems.append(
            f"corpus declares reconstruction policy "
            f"{corpus['reconstruction_policy_version']!r}, not "
            f"{TARGET_BOUND_RECONSTRUCTION_POLICY_V1!r}")

    return TargetPopulationReport(
        corpus_version_id=corpus_version_id,
        target_bound=not problems,
        member_count=len(stored_members),
        pages=chain.pages,
        members=stored_members,
        bound_runs=bound_runs,
        required_runs=required,
        problems=tuple(problems))


def verified_target_members(
    conn: sqlite3.Connection,
    corpus_version_id: str,
    *,
    manifest_path: Path | str,
    checkpoint_path: Optional[Path | str] = None,
) -> tuple[str, ...]:
    """The verified member set, or raise. The single seam §AF and E0 depend on."""

    report = verify_corpus_target_population(
        conn, corpus_version_id, manifest_path=manifest_path,
        checkpoint_path=checkpoint_path)
    if not report.ok:
        raise TargetPopulationError(
            f"corpus {corpus_version_id} is not a verified target-bound corpus: "
            + "; ".join(report.problems))
    return report.members


# --------------------------------------------------------------------------- #
# Construct-then-seal
# --------------------------------------------------------------------------- #
def seal_target_population(
    conn: sqlite3.Connection,
    *,
    corpus_version_id: str,
    members: Collection[str],
    run_ids: Collection[str],
    binding: AcquisitionBinding,
    now: Optional[str] = None,
) -> None:
    """Write membership, run bindings and the seal, in that order.

    MUST be called inside the caller's savepoint together with the corpus insert,
    so a failure anywhere rolls the corpus back too and leaves no orphan identity.
    The seal is last because the database triggers enforce exactly one ordering:
    children are insertable only while unsealed, and the seal asserts the member
    count actually present.
    """

    stamp = now or utc_now_iso()
    # RV-8. Own the savepoint. The previous contract lived only in this
    # docstring, and a caller who ignored it left committed membership rows
    # behind an absent seal -- an OPEN corpus that a later caller could still
    # extend. A nested SAVEPOINT composes correctly inside a caller's own
    # transaction, so this strengthens the guarantee without removing theirs.
    conn.execute("SAVEPOINT seal_target_population")
    try:
        _seal_unguarded(conn, corpus_version_id=corpus_version_id, members=members,
                        run_ids=run_ids, binding=binding, stamp=stamp)
    except BaseException:
        conn.execute("ROLLBACK TO seal_target_population")
        conn.execute("RELEASE seal_target_population")
        raise
    conn.execute("RELEASE seal_target_population")


def _seal_unguarded(
    conn: sqlite3.Connection,
    *,
    corpus_version_id: str,
    members: Collection[str],
    run_ids: Collection[str],
    binding: AcquisitionBinding,
    stamp: str,
) -> None:
    """The write sequence. Always called inside `seal_target_population`'s savepoint."""

    ordered_members = sorted(set(members))
    if len(ordered_members) != len(list(members)):
        raise TargetPopulationError(
            "duplicate member supplied to seal_target_population; duplicates are "
            "refused, never de-duplicated")
    if not ordered_members:
        raise TargetPopulationError("a target-bound corpus requires at least one member")
    ordered_runs = sorted(set(run_ids))
    if len(ordered_runs) != len(list(run_ids)):
        raise TargetPopulationError("duplicate run supplied to seal_target_population")
    if not ordered_runs:
        raise TargetPopulationError("a target-bound corpus requires at least one run")

    conn.executemany(
        f"INSERT INTO {_TARGETS_TABLE} (corpus_version_id, game_id, created_at) "
        "VALUES (?, ?, ?)",
        [(corpus_version_id, g, stamp) for g in ordered_members])
    conn.executemany(
        f"INSERT INTO {_RUNS_TABLE} (corpus_version_id, run_id, created_at) "
        "VALUES (?, ?, ?)",
        [(corpus_version_id, r, stamp) for r in ordered_runs])
    conn.execute(
        f"INSERT INTO {_SEALS_TABLE} (corpus_version_id, target_set_policy_version, "
        " listing_projection_policy_version, acquisition_completeness_policy_version, "
        " acquisition_manifest_hash, plan_version, member_count, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (corpus_version_id, TARGET_SET_POLICY_V1, LISTING_PROJECTION_POLICY_V1,
         ACQUISITION_COMPLETENESS_POLICY_V1, binding.manifest_hash,
         binding.plan_version, len(ordered_members), stamp))

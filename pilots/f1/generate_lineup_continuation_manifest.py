"""Generate the bounded NBA lineup-continuation recovery manifest, offline.

The manifest is derived from the PROTECTED March 2026 evidence, read-only:

* the committed month manifest supplies the source manifest and plan hashes;
* the executed month database supplies its own content fingerprint, the selected
  game count, and the target set (the games whose preserved first ``/v1/lineups``
  page advertised a ``next_cursor``);
* the target set is reduced to a deterministic digest.

**No cursor value is committed.** The manifest records WHICH games are being
recovered and a digest that pins their starting points; the cursors themselves
are re-derived from the protected database at execution time and the run refuses
if the digest has moved. That keeps the committed artifact a description of
intent rather than a snapshot of provider pagination state, and it means the
manifest cannot silently drift out of step with the evidence it extends.

Generation is deterministic and byte-identical on re-run; CI asserts it.
This script makes no provider request and opens no database writable.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from sports_quant.ingest.lineup_continuation import (
    LINEUPS_PER_PAGE,
    MAX_CONTINUATION_PAGES,
    RECOVERY_CONTRACT_VERSION,
    RECOVERY_PURPOSE,
    derive_targets,
)
from sports_quant.ingest.manifest import build_manifest, plan_hash
from sports_quant.ingest.planning import Bounds, RecoveryBinding, plan_lineup_continuation
from sports_quant.ingest.scratch_db import classify_scratch_db

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]

#: Protected source evidence. Opened read-only; never modified by this script.
SOURCE_MANIFEST = HERE / "nba_coverage_2026_03.manifest.json"
SOURCE_DATABASE = ROOT / "data" / "f1_nba_2026_03_scratch.db"

#: New artifacts. The recovery never writes into the executed month database or
#: its checkpoint, both of which remain historical evidence.
RECOVERY_DB = r"data\f1_nba_lineups_2026_03_recovery.db"
RECOVERY_CKPT = r"data\f1_nba_lineups_2026_03_recovery.ckpt"
MANIFEST_OUT = HERE / "nba_lineups_2026_03_continuation.manifest.json"

DATE_RANGE = "2026-03-01..2026-03-31"
EXPECTED_SELECTED_GAMES = 239
EXPECTED_TARGETS = 40
SCHEMA_VERSION = 17
RATE_PER_MIN = 60
MAX_RETRIES = 1


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def source_database_fingerprint(path: Path) -> str:
    """The source corpus's own CONTENT digest (schema + every row).

    Deliberately the logical content digest, not the file's bytes: a byte hash
    would change if the file were merely re-packed, and the binding is about the
    evidence, not the container.
    """

    classification = classify_scratch_db(path, resume=True, expected_fingerprint=None)
    if not classification.fingerprint:
        raise SystemExit(f"could not fingerprint the source database: {path}")
    return classification.fingerprint


def build(
    *,
    source_manifest: Path = SOURCE_MANIFEST,
    source_database: Path = SOURCE_DATABASE,
    recovery_db: str = RECOVERY_DB,
    recovery_ckpt: str = RECOVERY_CKPT,
) -> tuple[Any, dict[str, Any]]:
    """Build the recovery manifest plus a sanitized generation summary."""

    committed = json.loads(source_manifest.read_text(encoding="utf-8"))
    source_manifest_hash = _sha256_file(source_manifest)
    source_plan_hash = hashlib.sha256(
        json.dumps(committed["plan_body"], sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    fingerprint = source_database_fingerprint(source_database)

    survey = derive_targets(
        source_database,
        expected_targets=EXPECTED_TARGETS,
        expected_selected_games=EXPECTED_SELECTED_GAMES,
    )

    binding = RecoveryBinding(
        purpose=RECOVERY_PURPOSE,
        contract_version=RECOVERY_CONTRACT_VERSION,
        source_manifest_hash=source_manifest_hash,
        source_plan_hash=source_plan_hash,
        source_database_fingerprint=fingerprint,
        source_date_range=DATE_RANGE,
        source_selected_games=survey.selected_games,
        target_count=survey.target_count,
        target_digest=survey.target_digest(),
        max_continuation_pages=MAX_CONTINUATION_PAGES,
    )
    bounds = Bounds(
        max_games=survey.target_count,
        max_pages=MAX_CONTINUATION_PAGES,
        max_records=LINEUPS_PER_PAGE * MAX_CONTINUATION_PAGES,
        max_retries=MAX_RETRIES,
        rate_per_min=RATE_PER_MIN,
    )
    plan = plan_lineup_continuation(date_range=DATE_RANGE, binding=binding, bounds=bounds)
    manifest = build_manifest(
        plan, scratch_db=recovery_db, checkpoint_path=recovery_ckpt,
        expected_schema_version=SCHEMA_VERSION,
    )
    summary = {
        "manifest_hash": manifest.manifest_hash(),
        "plan_hash": plan_hash(plan),
        "source_manifest_hash": source_manifest_hash,
        "source_plan_hash": source_plan_hash,
        "source_database_fingerprint": fingerprint,
        "source_selected_games": survey.selected_games,
        "target_count": survey.target_count,
        "target_digest": survey.target_digest(),
        "semantic_requests_max": plan.semantic_requests_max(),
        "request_cap": manifest.request_cap,
        "configured_rate_per_min": manifest.configured_rate_per_min,
        "provider_rate_limit_per_min": manifest.provider_rate_limit_per_min,
        "recovery_database": recovery_db,
        "recovery_checkpoint": recovery_ckpt,
        "families": list(manifest.families),
        "expected_schema_version": manifest.expected_schema_version,
    }
    return manifest, summary


def generate(out_dir: Optional[Path] = None) -> Path:
    """Write the manifest; returns the path written."""

    manifest, _summary = build()
    target = (out_dir / MANIFEST_OUT.name) if out_dir is not None else MANIFEST_OUT
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(manifest.canonical(), encoding="utf-8")
    tmp.replace(target)
    return target


if __name__ == "__main__":
    _manifest, info = build()
    path = generate()
    print(f"wrote {path}")
    for key in sorted(info):
        print(f"  {key:32s} {info[key]}")

"""One synchronous provider pass from filtered queue through durable outputs."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.deep_context.common import load_env
from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactKind,
    ArtifactProjection,
    ArtifactRow,
    CandidatePeopleProjection,
    CandidatePersonRow,
    LinkRow,
    ProjectionStatus,
    ResearchRow,
    ResearchStatus,
    ReviewSource,
    RowKind,
)
from packs.ingestion.primitives.deep_context.db.snapshots import canonical_snapshot
from packs.ingestion.primitives.deep_context.enrichment_receipt import EnrichmentReceipt
from packs.ingestion.primitives.deep_context.parallel_research import (
    config,
    normalization,
    queue,
    sdk_client,
)
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)


def _api_key(explicit: str | None) -> str:
    load_env()
    value = explicit or os.environ.get("PARALLEL_API_KEY")
    if not value:
        raise SystemExit(
            "PARALLEL_API_KEY not set (pass --api-key or add it to the repo .env)"
        )
    return value


def _progress_counts(
    total: int,
    reused: int,
    provider: dict[str, Any],
) -> dict[str, int]:
    completed = reused + sum(
        int(provider.get(key) or 0)
        for key in ("completed", "succeeded", "success")
    )
    failed = sum(
        int(provider.get(key) or 0)
        for key in ("failed", "error", "errored", "cancelled", "canceled")
    )
    completed = min(total, completed)
    failed = min(max(0, total - completed), failed)
    return {
        "total": total,
        "completed": completed,
        "pending": max(0, total - completed - failed),
        "failed": failed,
    }


def research_artifact_projections(
    params: Any,
    rows: list[dict[str, str]] | tuple[dict[str, str], ...] | None = None,
) -> tuple[ArtifactProjection, ...]:
    """Parse completed provider outputs once into typed SQLite projections."""
    if params.db is None:
        return ()
    projections: list[ArtifactProjection] = []
    seen: set[str] = set()
    for row in params.rows if rows is None else rows:
        handle = queue.candidate_handle(row)
        if handle in seen:
            continue
        seen.add(handle)
        result_path = params.output_dir / handle / "01_research_parallel.json"
        if not result_path.is_file():
            continue
        result_data = result_path.read_bytes()
        profile = json.loads(result_data)
        if not isinstance(profile, dict):
            raise ValueError(f"research artifact must be an object: {result_path}")
        try:
            person_ids = [
                str(value).strip().lower()
                for value in json.loads(row.get("source_person_ids") or "[]")
                if str(value).strip()
            ]
        except (json.JSONDecodeError, TypeError):
            person_ids = []
        if not person_ids:
            raise ValueError(f"research queue row has no person ids: {handle}")
        row_key = str(row.get("row_key") or "").strip().lower()
        public_identifier = str(
            row.get("source_candidate_public_identifier") or ""
        ).strip().lower()
        parent_id = str(row.get("parent_id") or "").strip().lower()
        raw_exists = str(row.get("candidate_exists") or "").strip()
        if (
            not parent_id
            or raw_exists not in {"0", "1"}
            or not row_key
        ):
            raise ValueError(f"research queue ownership is unresolved: {handle}")
        candidate_exists = raw_exists == "1"
        social = profile.get("social") if isinstance(profile.get("social"), dict) else {}
        linkedin_value = str(
            profile.get("linkedin_url") or social.get("linkedin_url") or ""
        ).strip()
        linkedin_url = normalize_linkedin_url(linkedin_value) if linkedin_value else None
        found_public_identifier = (
            extract_public_identifier(linkedin_url).lower() if linkedin_url else ""
        )
        artifact_key = f"research:{handle}".lower()
        now = now_iso()
        artifact = ArtifactRow(
            artifact_key=artifact_key,
            kind=ArtifactKind.RESEARCH.value,
            parent_id=parent_id,
            path=str(result_path.resolve()),
            content_fingerprint=hashlib.sha256(result_data).hexdigest(),
            status=ProjectionStatus.PROJECTED.value,
            candidate_key=row_key,
            input_fingerprint=queue.input_fingerprint(row, handle),
            payload_json=json.dumps(profile, separators=(",", ":")),
            projected_at=now,
        )
        candidate = None
        if not candidate_exists:
            candidate = LinkRow(
                row_key,
                parent_id,
                public_identifier or found_public_identifier,
                RowKind.RESEARCH.value,
                None,
                (row.get("display_name") or "").strip() or None,
                candidate_origin=int(any(
                    value.startswith("candidate:") for value in person_ids
                )),
                paid_profile=1,
                source=ReviewSource.DEEP_RESEARCH.value,
                updated_at=now_iso(),
            )
        raw_artifact = None
        raw_path = params.output_dir / handle / "00_parallel_raw.json"
        if raw_path.is_file():
            raw_data = raw_path.read_bytes()
            raw_payload = json.loads(raw_data)
            if not isinstance(raw_payload, dict):
                raise ValueError(f"raw research artifact must be an object: {raw_path}")
            raw_artifact = ArtifactRow(
                artifact_key=f"raw-result:{row_key}".lower(),
                kind=ArtifactKind.RAW_RESULT.value,
                parent_id=parent_id,
                path=str(raw_path.resolve()),
                content_fingerprint=hashlib.sha256(raw_data).hexdigest(),
                status=ProjectionStatus.PROJECTED.value,
                candidate_key=row_key,
                payload_json=json.dumps(raw_payload, separators=(",", ":")),
                projected_at=now_iso(),
            )
        projections.append(ArtifactProjection(
            artifact=artifact,
            raw_artifact=raw_artifact,
            candidate=candidate,
            candidate_people=(
                CandidatePeopleProjection(
                    row_key,
                    tuple(
                        CandidatePersonRow(row_key, person_id, parent_id)
                        for person_id in sorted(set(person_ids))
                    ),
                )
                if candidate is not None else None
            ),
            research=ResearchRow(
                handle,
                parent_id,
                (
                    ResearchStatus.COMPLETE.value
                    if linkedin_url else ResearchStatus.NO_MATCH.value
                ),
                row_key,
                artifact_key,
                params.selection_fingerprint or None,
                json.dumps(profile, separators=(",", ":")),
                now_iso(),
            ),
        ))
    return tuple(projections)


def report_progress(
    params: Any,
    status: str,
    counts: dict[str, int],
    *,
    projections: tuple[ArtifactProjection, ...] | None = None,
    **extra: Any,
) -> None:
    """Project new outputs, then publish callback progress and the receipt."""
    manifest_path = (
        Path(params.manifest) if params.manifest else params.output_dir / "manifest.json"
    )
    if params.db is not None and projections is not None:
        params.db.project_rows(projections)
    if params.owns_receipt:
        try:
            receipt = EnrichmentReceipt(manifest_path)
        except ValueError as exc:
            raise SystemExit("--manifest must end in manifest.json") from exc
        receipt.write({
            **extra, "stage": "enrich", "status": status, "counts": counts,
        })
    if params.on_progress:
        params.on_progress({"status": status, "counts": counts})


def run_research(params: Any) -> dict[str, Any]:
    """Run one synchronous paid pass; fixed completed outputs make reruns free."""
    processor = config.validate_processor(params.processor)
    rows = [dict(row) for row in params.rows]
    existing = canonical_snapshot(params.db).artifacts if params.db is not None else ()
    todo, reused = queue.filter_already_done(rows, existing)
    if params.limit is not None:
        todo = todo[: params.limit]
    total = reused + len(todo)

    def failed(error: str) -> dict[str, Any]:
        report_progress(
            params, "failed",
            {"total": total, "completed": reused, "pending": 0, "failed": len(todo)},
            error=error,
        )
        return {
            "primitive": "deep_research_contacts", "command": "run",
            "status": "failed", "error": error,
        }

    if not todo:
        report_progress(
            params,
            "research_complete",
            {"total": total, "completed": reused, "pending": 0, "failed": 0},
            provider_status={},
        )
        return {
            "primitive": "deep_research_contacts",
            "command": "run",
            "status": "no_work",
            "queue_rows": len(rows),
            "skipped_already_done": reused,
        }

    params.output_dir.mkdir(parents=True, exist_ok=True)
    inputs = [{
        "task_spec": config.TASK_SPEC,
        "input": queue.build_input(row, row["handle"]),
        "metadata": {"handle": row["handle"]},
        "processor": processor,
    } for row in todo]
    api_key = _api_key(params.api_key)
    report_progress(
        params,
        "running",
        {"total": total, "completed": reused, "pending": len(todo), "failed": 0},
        provider_status={"submitted": len(todo)},
    )

    def on_status(provider: dict[str, Any]) -> None:
        report_progress(
            params,
            "running",
            _progress_counts(total, reused, provider),
            provider_status=provider,
        )
        print(
            f"[deep_research_contacts] poll status {provider}",
            file=sys.stderr,
            flush=True,
        )

    try:
        run_count, results, errors, final_group = sdk_client.ParallelClient(
            api_key,
            params.base_url,
            params.beta_header,
        ).execute(inputs, params, on_status)
    except Exception as exc:
        return failed(f"{type(exc).__name__}: {exc}"[:300])
    if not run_count:
        return failed("Parallel returned no run ids")

    rows_by_handle = {row["handle"]: row for row in todo}
    found_name = found_linkedin = 0
    for handle, result in results.items():
        row = rows_by_handle.get(handle)
        if row is None:
            errors.append(f"{handle}: result did not match a submitted subject")
            continue
        person_dir = params.output_dir / handle
        person_dir.mkdir(parents=True, exist_ok=True)
        write_json(person_dir / "00_parallel_raw.json", result)
        normalized = normalization.parallel_to_research_json(
            result,
            row,
            handle,
            row.get("display_name") or handle,
            row.get("bio") or "",
            research_method=f"parallel-{processor}",
        )
        write_json(person_dir / "01_research_parallel.json", normalized)
        found_name += int(bool(result.get("real_name")))
        found_linkedin += int(bool(result.get("linkedin_url")))

    status = "completed" if not errors else "completed_with_errors"
    projections = research_artifact_projections(params, todo)
    report_progress(
        params,
        "research_complete" if not errors else status,
        {
            "total": total,
            "completed": reused + len(results),
            "pending": 0,
            "failed": len(errors),
        },
        projections=projections,
        provider_status=final_group,
        errors=errors,
    )
    return {
        "primitive": "deep_research_contacts",
        "command": "run",
        "status": status,
        "completed_at": now_iso(),
        "output_dir": str(params.output_dir),
        "counts": {
            "run_ids": run_count,
            "results_fetched": len(results),
            "errors": len(errors),
            "real_name_found": found_name,
            "linkedin_found": found_linkedin,
        },
        "group_status": final_group,
        "errors": errors,
    }

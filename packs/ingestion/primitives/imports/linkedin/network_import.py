#!/usr/bin/env python3
"""Idempotent local LinkedIn Connections.csv import.

This primitive is source-specific only: it parses LinkedIn's Connections.csv into
Powerpacks' shared people schema, then delegates LinkedIn profile enrichment to
`packs/ingestion/primitives/enrich/enrich_people.py`. Provider calls, cache
handling, normalization, and final `people.csv` writing live in that shared
primitive; enrichment is RapidAPI-only (no Harmonic).

Contract: ONE idempotent `run` (plus `status`, which reads the stage manifest,
and `check-keys`). A run writes its output CSVs and one `manifest.json` into a
fixed directory (`.powerpacks/network-import/discover/linkedin/` by default) and
overwrites in place — no ledger, no `continue`, no per-step state store. Reruns
are idempotent because the output path is stable.

Local artifacts only. Usable profile-cache entries hydrate without keys or
approval (cache hits never spend). Cache misses need paid RapidAPI fetches:
without `--approve-spend` the run stops at a `needs_approval` manifest (miss
count + credit estimate, clean nonzero exit) BEFORE any fetch; with the flag it
fetches when `RAPIDAPI_LINKEDIN_KEY`/`RAPIDAPI_KEY` is set and fails clearly
otherwise. Cache format/seeding is documented in `enrich/profile_cache.py`
(default cache dir `.powerpacks/network-import/profile_cache_v2`; override with
`--profile-cache-dir`). Cache hits count into `cache_hit_count`, misses into
`paid_call_count`.

Usage:
    network_import.py run --csv ~/Downloads/Connections.csv --source-user LABEL [--operator-id local] [--approve-spend]
    network_import.py status | check-keys

`run` converts the CSV locally, then enriches. Artifacts (paths exposed in the
manifest `artifacts` map; `people_csv` is the canonical interface):
`connections_for_enrichment.csv`, `source_people.csv`,
`linkedin_enrichment_queue.csv`, `rapidapi_cache_hits.csv`,
`rapidapi_cache_misses.csv`, `rapidapi_recent_failures.csv`,
`needs_resolution_queue.csv`, `skipped_enrichment.csv`,
`provider_enriched.csv`, `raw_provider_responses/`, and `people.csv`.

Changelog:
  2026-07-23 (audit decomposition): the local discover_output_dir moved to
    common/paths.py as `resolve_discover_source_dir(output_dir, "linkedin")`
    (generalized over source), and the local read_csv_rows/row_key/upsert_csv
    trio moved to packs/shared/csv_io.py as the priority-key upsert family
    (`CsvIO.upsert_dict_rows_priority` + `read_dict_rows_normalized`). The
    enrichment delegate is now imported from its decomposed homes
    (models.build_config/EnrichManifest, rapidapi_client DEFAULT_* knobs);
    enrich_people keeps the EnrichPeople orchestrator + CLI.
  2026-07-23 (audit class-sharing): the spend-gate exit code + CLI-emit helpers
    moved to common/gates.py — NEEDS_APPROVAL_CODE is an alias of
    EXIT_NEEDS_APPROVAL, and exit_code_for_status / manifest_emit_payload import
    from there (they were byte-identical to enrich_people's). The needs_approval
    payload is still the delegate enrich_people's credit-gate shape, forwarded
    unchanged.
  2026-07-23 (audit): replaced the per-step ledger runner (load_ledger/
    save_ledger/mark_step/pipeline_steps/next_pending_step/approval_id/
    block_for_delegate_approval/ensure_delegate_ledger/execute_step/
    run_until_blocked_or_done/command_continue/command_approve and the delegate
    enrich_people.ledger.json) with a LinkedInImport orchestrator that owns the
    fixed discover dir, the convert -> enrich steps, and one manifest.json. The
    enrich delegate now runs in-process via EnrichPeople; paid RapidAPI spend is
    gated by an explicit `--approve-spend` (forwarded to the delegate).
  2026-07-23 (audit): dropped the local byte-identical write_csv for the
    shared CsvIO.write_dict_rows (read_csv_rows stays local — its empty-on-
    missing + normalization behavior differs from CsvIO.read_dict_rows);
    `import csv` dropped with it.
  2026-07-23 (audit): network_import.README.md sidecar folded into this
    docstring.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.enrich import enrich_people as people_enrichment  # noqa: E402
from packs.ingestion.primitives.enrich.models import EnrichManifest, build_config  # noqa: E402
from packs.ingestion.primitives.enrich.rapidapi_client import (  # noqa: E402
    DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS,
    DEFAULT_RAPIDAPI_MAX_RPM,
    DEFAULT_RAPIDAPI_MAX_WORKERS,
)
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS,
    extract_public_identifier,
    generate_person_id,
    normalize_linkedin_url,
    normalize_people_row,
)
from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL, exit_code_for_status, manifest_emit_payload  # noqa: E402
from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, write_json  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR, DEFAULT_PROFILE_CACHE_DIR, resolve_discover_source_dir  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402

CONNECTION_COLUMNS = [
    "person_id",
    "public_identifier",
    "linkedin_url",
    "first_name",
    "last_name",
    "source_user",
    "linkedin_company",
    "linkedin_position",
    "linkedin_email",
    "connected_on",
]
# `run` exit code when paid RapidAPI cache-miss fetches are gated behind
# --approve-spend. Value + status->code mapping live in common/gates.py; kept as a
# module alias for the name callers/tests reach for.
NEEDS_APPROVAL_CODE = EXIT_NEEDS_APPROVAL


class PipelineFailed(Exception):
    """A hard, non-recoverable step failure (e.g. the Connections.csv is missing)."""


@dataclass
class LinkedInConnection:
    first_name: str
    last_name: str
    linkedin_url: str
    email_address: str
    company: str
    position: str
    connected_on: str
    public_identifier: str
    person_id: str
    source_user: str

    def row(self) -> dict[str, str]:
        return {
            "person_id": self.person_id,
            "public_identifier": self.public_identifier,
            "linkedin_url": self.linkedin_url,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "source_user": self.source_user,
            "linkedin_company": self.company,
            "linkedin_position": self.position,
            "linkedin_email": self.email_address,
            "connected_on": self.connected_on,
        }

    def people_row(self, source_csv: Path) -> dict[str, str]:
        full_name = f"{self.first_name} {self.last_name}".strip()
        provenance = {
            "source": "linkedin_csv",
            "source_user": self.source_user,
            "source_csv": str(source_csv),
            "connected_on": self.connected_on,
        }
        return normalize_people_row({
            "id": self.person_id,
            "public_identifier": self.public_identifier,
            "linkedin_url": self.linkedin_url,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "full_name": full_name,
            "current_title": self.position,
            "current_company": self.company,
            "primary_email": self.email_address,
            "all_emails": self.email_address,
            "source_channels": "linkedin_csv",
            "source_artifacts": json.dumps(provenance, sort_keys=True),
            "enrichment_provider": "linkedin_csv_source",
        })


def linkedin_export_header(line: str) -> bool:
    lowered = line.strip().lower()
    return lowered.startswith("first name,") or ("first name" in lowered and "url" in lowered and "," in lowered)


def parse_connections_csv(path: Path, source_user: str, limit: int | None = None) -> tuple[list[LinkedInConnection], dict[str, Any]]:
    if not path.exists():
        raise PipelineFailed(f"LinkedIn Connections CSV not found: {path}")
    connections: list[LinkedInConnection] = []
    seen: set[str] = set()
    duplicates = 0
    skipped = 0
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        header_line = ""
        for line in handle:
            if linkedin_export_header(line):
                header_line = line.strip()
                break
        if not header_line:
            raise PipelineFailed("Could not find LinkedIn export header row containing 'First Name' and 'URL'")
        reader = CsvIO.dict_reader(handle, fieldnames=next(CsvIO.reader([header_line])))
        for row in reader:
            url = normalize_linkedin_url(row.get("URL", ""))
            pub_id = extract_public_identifier(url)
            if not pub_id:
                skipped += 1
                continue
            if pub_id in seen:
                duplicates += 1
                continue
            seen.add(pub_id)
            connections.append(
                LinkedInConnection(
                    first_name=(row.get("First Name") or "").strip(),
                    last_name=(row.get("Last Name") or "").strip(),
                    linkedin_url=url,
                    email_address=(row.get("Email Address") or "").strip(),
                    company=(row.get("Company") or "").strip(),
                    position=(row.get("Position") or "").strip(),
                    connected_on=(row.get("Connected On") or "").strip(),
                    public_identifier=pub_id,
                    person_id=generate_person_id(pub_id),
                    source_user=source_user,
                )
            )
            if limit and len(connections) >= limit:
                break
    return connections, {"parsed": len(connections), "duplicates": duplicates, "skipped_invalid": skipped}


@dataclass(frozen=True)
class LinkedInImportConfig:
    """Frozen, keyword-only config for one LinkedIn import run. The throughput
    knobs stay `None`-able inherit sentinels; enrich/models.build_config resolves
    them to defaults inside the delegated enrichment run."""

    csv: Path
    source_user: str
    run_dir: Path
    operator_id: str = "local"
    limit: int | None = None
    profile_cache_dir: Path = DEFAULT_PROFILE_CACHE_DIR
    refresh_cache: bool = False
    company_corpus_jsonl: tuple[str, ...] = ()
    sleep_seconds: float = 0.0
    force_enrich: bool = False
    convert_only: bool = False
    max_workers: int | None = None
    max_rpm: float | None = None
    failure_retry_hours: float | None = None
    approve_spend: bool = False

    def manifest_input(self) -> dict[str, Any]:
        return {
            "csv": str(self.csv),
            "source_user": self.source_user,
            "operator_id": self.operator_id,
            "limit": self.limit,
            "profile_cache_dir": str(self.profile_cache_dir),
            "refresh_cache": self.refresh_cache,
            "company_corpus_jsonl": [str(p) for p in self.company_corpus_jsonl],
            "sleep_seconds": self.sleep_seconds,
            "force_enrich": self.force_enrich,
            "convert_only": self.convert_only,
            "approve_spend": self.approve_spend,
        }


@dataclass
class LinkedInImportManifest:
    """Typed constructor for the LinkedIn import stage `manifest.json` — the whole
    durable state contract (status + per-step timing + counts + artifact paths).
    No ledger, no run id: the discover dir is fixed so reruns overwrite here."""

    status: str
    artifact_dir: str
    input: dict[str, Any]
    counts: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    steps: dict[str, Any] = field(default_factory=dict)
    needs_approval: dict[str, Any] | None = None
    error: str | None = None
    started_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "primitive": "linkedin/network_import",
            "status": self.status,
            "artifact_dir": self.artifact_dir,
            "input": self.input,
            "counts": self.counts,
            "artifacts": self.artifacts,
            "steps": self.steps,
            "started_at": self.started_at,
            "updated_at": self.updated_at or now_iso(),
        }
        if self.needs_approval is not None:
            payload["needs_approval"] = self.needs_approval
        if self.error is not None:
            payload["error"] = self.error
        return payload


class LinkedInImport:
    """Idempotent LinkedIn Connections.csv import: convert -> delegated
    enrichment -> one manifest.json in a fixed discover dir. Owns the run dir,
    the two steps, and the manifest; the enrichment run is delegated in-process
    to enrich_people.EnrichPeople against the SAME dir so the enriched people.csv
    and provider artifacts land here. (EnrichPeople writes its own manifest.json
    first; this stage's manifest is written last and is the authoritative one for
    the dir, embedding the enrichment counts + artifacts.)"""

    def __init__(self, cfg: LinkedInImportConfig) -> None:
        self.cfg = cfg
        self.run_dir = cfg.run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)  # the one place the dir is created
        self.manifest_path = self.run_dir / "manifest.json"
        self.artifacts: dict[str, Any] = {}
        self.counts: dict[str, Any] = {}
        self.steps: dict[str, Any] = {}
        self.started_at = now_iso()

    def run(self) -> LinkedInImportManifest:
        try:
            convert = self._timed("convert", self.convert)
        except PipelineFailed as exc:
            return self._write(status="failed", error=str(exc))
        self.counts.update({
            "connections_parsed": convert.get("parsed", 0),
            "connections_total": convert.get("connections_total", 0),
            "source_people_total": convert.get("source_people_total", 0),
        })
        if self.cfg.convert_only:
            return self._write(status="completed")
        enrich = self.enrich()
        self.steps["enrich_people"] = {"status": enrich.status, "counts": enrich.counts, "steps": enrich.steps}
        self.artifacts.update(enrich.artifacts)
        for key in ("cache_hit_count", "paid_call_count", "queue_count", "recent_failure_count", "people_rows"):
            self.counts[key] = enrich.counts.get(key, 0)
        if enrich.status == "needs_approval":
            return self._write(status="needs_approval", needs_approval=enrich.needs_approval)
        if enrich.status == "failed":
            return self._write(status="failed", error=enrich.error or "enrich_people failed")
        return self._write(status="completed")

    def _timed(self, step_id: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        started = now_iso()
        summary = fn()
        self.steps[step_id] = {"status": "completed", "started_at": started, "finished_at": now_iso(), "summary": summary}
        return summary

    def _write(self, *, status: str, needs_approval: dict[str, Any] | None = None, error: str | None = None) -> LinkedInImportManifest:
        manifest = LinkedInImportManifest(
            status=status,
            artifact_dir=str(self.run_dir),
            input=self.cfg.manifest_input(),
            counts=self.counts,
            artifacts=self.artifacts,
            steps=self.steps,
            needs_approval=needs_approval,
            error=error,
            started_at=self.started_at,
            updated_at=now_iso(),
        )
        write_json(self.manifest_path, manifest.to_dict())
        return manifest

    def convert(self) -> dict[str, Any]:
        """Parse the Connections.csv into connections + source-people CSVs,
        upserted in place under the fixed discover dir."""
        inp = self.cfg.csv
        connections, stats = parse_connections_csv(inp, self.cfg.source_user, self.cfg.limit)
        connection_rows = [conn.row() for conn in connections]
        people_rows = [conn.people_row(inp) for conn in connections]
        connections_out = self.run_dir / "connections_for_enrichment.csv"
        people_out = self.run_dir / "source_people.csv"
        merged_connections = CsvIO.upsert_dict_rows_priority(
            connections_out,
            CONNECTION_COLUMNS,
            connection_rows,
            ["public_identifier", "linkedin_url", "linkedin_email", "person_id"],
        )
        merged_people = CsvIO.upsert_dict_rows_priority(
            people_out,
            PEOPLE_COLUMNS,
            people_rows,
            ["person_id", "public_identifier", "linkedin_url", "email"],
        )
        self.artifacts.update({
            "connections_csv": str(connections_out),
            "source_people_csv": str(people_out),
        })
        return {
            **stats,
            "connections_file": str(connections_out),
            "source_people_file": str(people_out),
            "connections_total": len(merged_connections),
            "source_people_total": len(merged_people),
        }

    def enrich(self) -> EnrichManifest:
        """Delegate RapidAPI enrichment to enrich_people, in-process, against the
        SAME discover dir. Returns the delegate's typed manifest (whose status
        carries needs_approval / failed straight up to this stage)."""
        source_people = self.artifacts.get("source_people_csv")
        if not source_people:
            raise PipelineFailed("convert step did not produce source_people_csv")
        cfg = build_config(
            input_csv=source_people,
            artifact_dir=self.run_dir,
            profile_cache_dir=self.cfg.profile_cache_dir,
            limit=None,
            force=self.cfg.force_enrich,
            refresh_cache=self.cfg.refresh_cache,
            company_corpus_jsonl=list(self.cfg.company_corpus_jsonl),
            sleep_seconds=self.cfg.sleep_seconds,
            max_workers=self.cfg.max_workers,
            max_rpm=self.cfg.max_rpm,
            failure_retry_hours=self.cfg.failure_retry_hours,
            approve_spend=self.cfg.approve_spend,
        )
        return people_enrichment.EnrichPeople(cfg).run()


def command_run(args: argparse.Namespace) -> int:
    run_dir = resolve_discover_source_dir(Path(args.output_dir), "linkedin")
    cfg = LinkedInImportConfig(
        csv=Path(args.csv),
        source_user=args.source_user,
        run_dir=run_dir,
        operator_id=args.operator_id,
        limit=args.limit,
        profile_cache_dir=Path(args.profile_cache_dir),
        refresh_cache=args.refresh_cache,
        company_corpus_jsonl=tuple(str(Path(p)) for p in (args.company_corpus_jsonl or [])),
        sleep_seconds=args.sleep_seconds,
        force_enrich=args.force_enrich,
        convert_only=args.convert_only,
        max_workers=args.max_workers,
        max_rpm=args.max_rpm,
        failure_retry_hours=args.failure_retry_hours,
        approve_spend=args.approve_spend,
    )
    manifest = LinkedInImport(cfg).run()
    emit(manifest_emit_payload(manifest))
    return exit_code_for_status(manifest.status)


def command_status(args: argparse.Namespace) -> int:
    run_dir = resolve_discover_source_dir(Path(args.output_dir), "linkedin")
    manifest = read_json(run_dir / "manifest.json", {}) or {}
    emit({
        "status": manifest.get("status", "unknown"),
        "artifact_dir": str(run_dir),
        "counts": manifest.get("counts", {}),
        "artifacts": manifest.get("artifacts", {}),
        "steps": manifest.get("steps", {}),
        "needs_approval": manifest.get("needs_approval"),
    })
    return 0


def command_check_keys(_: argparse.Namespace) -> int:
    return people_enrichment.command_check_keys(argparse.Namespace())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LinkedIn Connections.csv network import")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--csv", required=True, help="LinkedIn Connections.csv export")
    run.add_argument("--source-user", required=True, help="Non-secret source label for this export")
    run.add_argument("--operator-id", default="local")
    run.add_argument("--limit", type=int, help=argparse.SUPPRESS)
    run.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    run.add_argument("--approve-spend", action="store_true", help="Authorize paid RapidAPI fetches for cache misses (otherwise a run with misses stops at needs_approval)")
    run.add_argument("--convert-only", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--force-enrich", action="store_true", help="Re-enrich rows even when source rows look complete")
    run.add_argument("--profile-cache-dir", default=str(DEFAULT_PROFILE_CACHE_DIR))
    run.add_argument("--refresh-cache", action="store_true", help="Force RapidAPI calls even when cache entries exist")
    run.add_argument("--company-corpus-jsonl", action="append", default=[])
    run.add_argument("--sleep-seconds", type=float, default=0.0)
    run.add_argument("--max-workers", type=int, default=DEFAULT_RAPIDAPI_MAX_WORKERS)
    run.add_argument("--max-rpm", type=float, default=DEFAULT_RAPIDAPI_MAX_RPM)
    run.add_argument("--failure-retry-hours", type=float, default=DEFAULT_RAPIDAPI_FAILURE_RETRY_HOURS)
    run.add_argument("--no-harmonic", action="store_true", help=argparse.SUPPRESS)
    run.add_argument("--no-rapidapi", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(func=command_run)

    status = sub.add_parser("status")
    status.add_argument("--output-dir", default=str(DEFAULT_BASE_DIR))
    status.set_defaults(func=command_status)

    keys = sub.add_parser("check-keys")
    keys.set_defaults(func=command_check_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "no_rapidapi", False):
        emit({"status": "error", "error": "linkedin/network_import delegates to RapidAPI-only enrich_people; --no-rapidapi is no longer supported"})
        return 2
    try:
        return args.func(args)
    except ValueError as exc:
        emit({"status": "error", "error": str(exc)})
        return 2
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

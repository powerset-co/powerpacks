"""Gmail import orchestration for the LIVE import chain.

Exposes `GmailImport` — the orchestrator the live import dispatches. It owns the
fixed import dir, transient run state, and the two-step chain:
  - directory apply: apply the shared `directory.csv` to each Gmail queue,
    splitting resolved / unresolved / cached-negative and committing the Gmail
    observations + directory resolutions back into `directory.csv`.
  - stored-resolution apply: attach STORED resolutions only (directory +
    explicit; no Parallel calls, no RapidAPI hydration — deep-context owns
    resolution/enrichment, and `bin/deep-context migrate-legacy` adopts the
    stored era into overrides/review.csv) onto each account people.csv via
    discover_engine apply-resolutions, then materialize one merged Gmail
    people.csv.
`GmailImport.run()` then splits matched people vs candidates, runs the directory
source-account quality gate, and writes the import manifest.

The per-step helpers mutate transient `self.state` in place. The directory-commit,
resolution-merge, and queue helpers stay module-level pure transforms the steps
call; everything cross-source (the whole directory.csv contract, the resolution
normalizers, the people.csv materializers) is imported from `imports/directory.py`,
and the shared import helpers (manifest read/write, people/
candidate materialization) from `imports/common.py` + `imports/gmail/util.py`.

Loaded via `imports.common.load_gmail_import_steps`, which getattrs `GmailImport`
off this module — that name must stay defined here, and the loader + the
`importer.py` caller move together.

Changelog:
  2026-07-24: Gmail step state became transient; the import persists only its
    output files and manifest.json.
  2026-07-23 (audit): the person-vs-role classifiers is_generic_or_non_person /
    is_likely_person_name now import from common/contact_fields.py (they moved
    out of the split-up discover/gmail msgvault reader — generic name/email
    testers, not msgvault-specific).
  2026-07-23 (audit): dropped three self-contained CSV/column copies —
    LINKEDIN_RESOLUTION_COLUMNS now imports from schemas/gmail_artifacts.py,
    read_csv_rows from discover/common.py, and the local write_csv_rows is now
    CsvIO.write_dict_rows.
  2026-07-23 (audit): extracted from the retired pre-split orchestrator;
    narrowed to the surviving steps when the legacy resolve/enrich flags were
    removed; union_alias_list stopped alias loss.
  2026-07-23 (audit batch 16): progress prefix renamed to [gmail-import].
  2026-07-23 (audit batch 17): apply-resolutions child retargeted to
    gmail/discover_engine.py.
  2026-07-23 (audit batch 18): moved home to imports/gmail/.
  2026-07-23 (audit consolidation): killed the import_steps ↔ directory.py fork.
    The ~30 byte-identical directory helpers + 6 consts are now imported from
    imports/directory.py (whose build_directory_checkpoint / commit_directory_rows
    / materialize_source_merged_people_csv use the fingerprinted LF write_csv_rows
    and ISO-aware latest_interaction — so gmail-import's directory.csv is now
    written LF, not CRLF, once). now_iso/emit/unique_strings from
    common.jsonio, run_cmd/py_cmd/emit_progress from common.proc, DEFAULT_* paths
    from common.paths, source_slug from discover/common. gmail_directory_source_key
    is the shared three-arg recipe.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, now_iso, unique_strings  # noqa: E402
from packs.ingestion.primitives.common.paths import (  # noqa: E402
    DEFAULT_BASE_DIR,
    DEFAULT_DIRECTORY_CSV,
    DEFAULT_DISCOVER_DIR,
    DEFAULT_IMPORT_DIR,
    DEFAULT_PROFILE_CACHE_DIR,
    source_import_dir,
)
from packs.ingestion.primitives.common.proc import emit_progress as _emit_progress, py_cmd, run_cmd  # noqa: E402
from packs.ingestion.primitives.discover.common import read_accounts, read_csv_rows, source_slug  # noqa: E402
from packs.ingestion.primitives.common.contact_fields import (  # noqa: E402
    is_generic_or_non_person,
    is_likely_person_name,
)
from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    RESOLUTION_NEGATIVE_STATUSES,
    build_directory_checkpoint,
    commit_directory_rows,
    directory_match_for_queue_row,
    directory_row_is_found,
    directory_row_is_prior_negative,
    gmail_directory_source_key,
    load_directory_lookup,
    materialize_gmail_merged_people_csv,
    merge_resolution_rows,
    normalize_resolution_row,
    normalized_directory_row,
    parse_confidence,
    resolution_from_directory_match,
)
from packs.ingestion.primitives.imports.common import (  # noqa: E402
    copy_people_csv,
    csv_count,
    directory_source_account_quality,
    import_manifest_current,
    linked_gmail_accounts,
    normalize_directory_source_accounts,
    write_manifest,
)
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: E402
    gmail_artifacts_from_discovery,
    write_gmail_candidates,
)
from packs.ingestion.schemas.gmail_artifacts import LINKEDIN_RESOLUTION_COLUMNS  # noqa: E402
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402


def emit_progress(message: str) -> None:
    """Write one progress line to stderr, tagged for the gmail import chain."""
    _emit_progress(message, "[gmail-import]")


def artifact_dir_from_state(state: dict[str, Any]) -> Path:
    """Directory the import writes intermediate artifacts into."""
    return Path(str(state.get("artifact_dir") or DEFAULT_DISCOVER_DIR))


def _is_resolvable_person(row: dict[str, str]) -> bool:
    """Return True if the queue row looks like a real person worth resolving."""
    email = (row.get("primary_email") or row.get("email") or row.get("handle") or "").strip()
    name = (row.get("display_name") or row.get("full_name") or "").strip()
    if not email or not name:
        return False
    return not is_generic_or_non_person(email) and is_likely_person_name(name)


def apply_directory_to_gmail_queue(record: dict[str, Any], directory_csv: Path, output_dir: Path) -> dict[str, Any]:
    """Split a Gmail LinkedIn queue against directory.csv into resolved / cached-negative / unresolved."""
    queue_csv = Path(str(record.get("queue_csv") or ""))
    fields, rows = read_csv_rows(queue_csv)
    lookup = load_directory_lookup(directory_csv)
    resolved: list[dict[str, str]] = []
    unresolved: list[dict[str, str]] = []
    cached_negative: list[dict[str, str]] = []
    filtered_non_person = 0
    for row in rows:
        match = directory_match_for_queue_row(row, lookup)
        if match and directory_row_is_found(match):
            resolved.append(resolution_from_directory_match(row, match))
        elif match and directory_row_is_prior_negative(match):
            cached_negative.append(row)
        elif not _is_resolvable_person(row):
            filtered_non_person += 1
        else:
            unresolved.append(row)
    output_dir.mkdir(parents=True, exist_ok=True)
    resolutions_csv = output_dir / "directory_linkedin_resolutions.csv"
    unresolved_csv = output_dir / "unresolved_linkedin_resolution_queue.csv"
    cached_negative_csv = output_dir / "cached_negative_linkedin_resolution_queue.csv"
    CsvIO.write_dict_rows(resolutions_csv, LINKEDIN_RESOLUTION_COLUMNS, resolved)
    CsvIO.write_dict_rows(unresolved_csv, fields, unresolved)
    CsvIO.write_dict_rows(cached_negative_csv, fields, cached_negative)
    result = dict(record)
    result.update({
        "directory_csv": str(directory_csv),
        "directory_resolutions_csv": str(resolutions_csv),
        "unresolved_queue_csv": str(unresolved_csv),
        "cached_negative_queue_csv": str(cached_negative_csv),
        "input_rows": len(rows),
        "resolved": len(resolved),
        "unresolved": len(unresolved),
        "cached_negative": len(cached_negative),
        "filtered_non_person": filtered_non_person,
    })
    return result


def parse_json_list(value: Any) -> list[str]:
    """Parse a JSON array or bare value into a de-duplicated string list."""
    if isinstance(value, list):
        return unique_strings(value)
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = []
    if isinstance(parsed, list):
        return unique_strings(parsed)
    return []


def directory_rows_from_gmail_queue(record: dict[str, Any]) -> list[dict[str, str]]:
    """Directory 'observed' rows for every account/email in a Gmail queue record."""
    queue_csv = Path(str(record.get("queue_csv") or ""))
    if not queue_csv.exists():
        return []
    account_email = str(record.get("account_email") or "").strip().lower()
    _fields, rows = read_csv_rows(queue_csv)
    output: list[dict[str, str]] = []
    for row in rows:
        email = str(row.get("primary_email") or row.get("handle") or "").strip().lower()
        if not email:
            continue
        accounts = parse_json_list(row.get("account_emails")) or unique_strings(account_email)
        source_ids = parse_json_list(row.get("source_ids"))
        if not accounts:
            accounts = [account_email or ""]
        if not source_ids:
            source_ids = [""]
        for account in accounts:
            output.append(normalized_directory_row({
                "source": "gmail_msgvault",
                "source_key": gmail_directory_source_key(account, email, row.get("id") or ""),
                "source_account": account,
                "source_id": json.dumps(source_ids, ensure_ascii=False),
                "source_channels": row.get("source_channels") or "gmail_msgvault",
                "status": "observed",
                "email": email,
                "name": row.get("display_name") or row.get("full_name") or "",
                "confidence": "0",
                "evidence": json.dumps({
                    "source": "gmail_msgvault",
                    "queue_csv": str(queue_csv),
                    "account_email": account,
                    "source_ids": source_ids,
                    "total_messages": row.get("total_messages", ""),
                    "thread_count": row.get("thread_count", ""),
                    "last_interaction": row.get("last_interaction", ""),
                }, sort_keys=True),
                "reasoning": "Observed in local Gmail metadata",
            }, source_artifact=str(queue_csv), updated_at=now_iso()))
    return [row for row in output if row]


def commit_gmail_observations_to_directory(input_cfg: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    """Merge every Gmail queue's observed rows into directory.csv."""
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    rows: list[dict[str, str]] = []
    for record in gmail_queue_records(artifacts):
        if isinstance(record, dict):
            rows.extend(directory_rows_from_gmail_queue(record))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_observation_rows"] = len(rows)
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_observation_checkpoint"] = result
    return result


def commit_gmail_resolutions_to_directory(input_cfg: dict[str, Any], artifacts: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge stored per-account Gmail LinkedIn resolutions (found + negative) into directory.csv."""
    directory_csv = Path(input_cfg.get("linkedin_directory_csv") or artifacts.get("directory_csv") or DEFAULT_DIRECTORY_CSV)
    rows: list[dict[str, str]] = []
    for record in records:
        if not isinstance(record, dict) or not record.get("resolutions_csv"):
            continue
        account_email = str(record.get("account_email") or "").strip().lower()
        resolution_path = Path(str(record["resolutions_csv"]))
        if not resolution_path.exists():
            continue
        for raw_resolution in read_csv_rows(resolution_path)[1]:
            resolution = normalize_resolution_row(raw_resolution)
            email = str(resolution.get("handle") or "").strip().lower()
            if "@" not in email:
                continue
            linkedin_url = normalize_linkedin_url(resolution.get("linkedin_url") or "")
            public_identifier = extract_public_identifier(linkedin_url)
            status = str(resolution.get("status") or "").strip().lower()
            confidence = parse_confidence(resolution.get("confidence"), 0.0)
            if status == "found":
                if not public_identifier or confidence < 0.75:
                    continue
            elif status in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}):
                status = "not_found" if status not in {"failed", "error"} else status
                linkedin_url = ""
                confidence = max(confidence, 0.01)
            else:
                continue
            evidence = {
                "source": "gmail_linkedin_resolution",
                "account_email": account_email,
                "resolutions_csv": record.get("resolutions_csv"),
                "resolution_evidence": resolution.get("evidence", ""),
            }
            rows.append(normalized_directory_row({
                "source": "gmail_msgvault",
                "source_key": gmail_directory_source_key(account_email, email),
                "source_account": account_email,
                "source_channels": "gmail_msgvault",
                "status": status,
                "email": email,
                "name": resolution.get("matched_name") or "",
                "linkedin_url": linkedin_url,
                "confidence": f"{confidence:.2f}",
                "matched_name": resolution.get("matched_name") or "",
                "matched_headline": resolution.get("matched_headline") or "",
                "evidence": json.dumps(evidence, sort_keys=True),
                "reasoning": resolution.get("reasoning") or "",
                "_priority": 82,
            }, source_artifact=str(record.get("resolutions_csv") or ""), updated_at=now_iso()))
    result = commit_directory_rows(directory_csv, rows)
    result["gmail_resolution_rows"] = len(rows)
    result["gmail_resolution_found_rows"] = sum(1 for row in rows if row.get("status") == "found")
    result["gmail_resolution_negative_rows"] = sum(1 for row in rows if row.get("status") in (RESOLUTION_NEGATIVE_STATUSES | {"not_found"}))
    artifacts["directory_csv"] = str(directory_csv)
    artifacts["gmail_directory_resolution_checkpoint"] = result
    return result


def combine_gmail_resolution_records(records: list[dict[str, Any]], run_dir: Path) -> list[dict[str, Any]]:
    """Group per-account resolution CSVs by (slug, people_csv) and merge each into one file."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or not record.get("people_csv") or not record.get("resolutions_csv"):
            continue
        slug = source_slug(record.get("account_email") or record.get("slug") or record.get("people_csv") or "all")
        key = (slug, str(record["people_csv"]))
        group = grouped.setdefault(key, {"account_email": record.get("account_email", ""), "slug": slug, "people_csv": record["people_csv"], "resolution_paths": []})
        group["resolution_paths"].append(Path(str(record["resolutions_csv"])))
    combined: list[dict[str, Any]] = []
    for (slug, people_csv), group in sorted(grouped.items(), key=lambda item: item[0]):
        rows = merge_resolution_rows(group["resolution_paths"])
        if not rows:
            continue
        out_dir = run_dir / f"gmail-combined-resolutions-{slug}"
        out_path = out_dir / "linkedin_resolutions.csv"
        CsvIO.write_dict_rows(out_path, LINKEDIN_RESOLUTION_COLUMNS, rows)
        combined.append({
            "account_email": group.get("account_email", ""),
            "slug": slug,
            "people_csv": people_csv,
            "resolutions_csv": str(out_path),
            "resolution_sources": [str(path) for path in group["resolution_paths"]],
            "resolved": len(rows),
        })
    return combined


def ordered_records(records: list[dict[str, Any]], account_order: list[str] | None = None) -> list[dict[str, Any]]:
    """Sort account records by the configured account order, then by a stable key."""
    order = {email: index for index, email in enumerate(account_order or []) if email}
    return sorted(
        records,
        key=lambda record: (
            order.get(str(record.get("account_email") or ""), len(order)),
            str(record.get("account_email") or record.get("slug") or record.get("people_csv") or record.get("queue_csv") or ""),
        ),
    )


def gmail_queue_records(artifacts: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize the per-account Gmail queue records out of the run artifacts."""
    queue_records = artifacts.get("gmail_linkedin_resolution_queue_csvs") or []
    if not queue_records and artifacts.get("gmail_linkedin_resolution_queue_csv"):
        queue_records = [{"account_email": "", "queue_csv": artifacts.get("gmail_linkedin_resolution_queue_csv"), "people_csv": artifacts.get("gmail_people_csv"), "slug": "all"}]
    return [record for record in queue_records if isinstance(record, dict) and record.get("queue_csv")]


class GmailImport:
    """Orchestrates the directory-only Gmail import.

    Owns the fixed import dir, transient run state, the two-step chain (directory apply -> stored-resolution
    apply + people materialization), the matched-people / candidates split, the
    directory source-account quality gate, and the import manifest. The step
    methods mutate `self.state` in place instead of threading a state dict
    through free functions; the module-level directory-commit / resolution-merge
    / queue helpers stay pure transforms the steps call.

    A step failure ends the run with status `failed` (steps record their own
    error in transient state); there is no approval gate — nothing here spends. The
    contract token is supplied by the caller (importer.py owns it for the package
    __init__ re-export)."""

    def __init__(self, *, args: argparse.Namespace, contract: str) -> None:
        self.args = args
        self.contract = contract
        self.import_dir = source_import_dir("gmail")
        self.state: dict[str, Any] = {}

    # --- transient run state --------------------------------------------------

    def _mark_step(self, step: str, status: str, **extra: Any) -> None:
        """Update one step's status/timestamps in transient state."""
        rec = self.state.setdefault("steps", {}).setdefault(step, {"id": step})
        if status == "running" and "started_at" not in rec:
            rec["started_at"] = now_iso()
        if status in {"completed", "failed", "blocked", "skipped"}:
            rec["finished_at"] = now_iso()
        rec["status"] = status
        rec.update({k: v for k, v in extra.items() if v is not None})

    def _begin_step(self, step: str, message: str) -> None:
        """Mark a step running and emit a progress line."""
        self._mark_step(step, "running")
        emit_progress(message)

    # --- steps ----------------------------------------------------------------

    def _directory_step(self) -> bool:
        """Apply directory.csv LinkedIn mappings to every Gmail queue, recording resolved/unresolved/cached-negative."""
        input_cfg = self.state.get("input", {})
        artifacts = self.state.setdefault("artifacts", {})
        queue_records = ordered_records(gmail_queue_records(artifacts), unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
        if not queue_records:
            checkpoint = build_directory_checkpoint(input_cfg, artifacts)
            artifacts["directory_csv"] = checkpoint["directory_csv"]
            self._mark_step("gmail_directory", "skipped", reason="no Gmail LinkedIn queue", checkpoint=checkpoint)
            return True
        self._begin_step("gmail_directory", f"Applying directory LinkedIn mappings to {len(queue_records)} Gmail queue(s).")
        observation_checkpoint = commit_gmail_observations_to_directory(input_cfg, artifacts)
        checkpoint = build_directory_checkpoint(input_cfg, artifacts)
        directory_csv = Path(checkpoint["directory_csv"])
        artifacts["directory_csv"] = str(directory_csv)
        artifacts["gmail_directory_by_slug"] = {}
        by_slug = artifacts["gmail_directory_by_slug"]
        artifacts["gmail_directory_resolution_records"] = []
        artifacts["gmail_unresolved_linkedin_resolution_queue_csvs"] = []
        artifacts["gmail_cached_negative_linkedin_resolution_queue_csvs"] = []
        results = []
        total_resolved = 0
        total_unresolved = 0
        total_cached_negative = 0
        for index, record in enumerate(queue_records):
            slug = source_slug(record.get("account_email") or record.get("slug") or f"queue-{index}")
            out_dir = artifact_dir_from_state(self.state) / f"gmail-directory-{slug}"
            result = apply_directory_to_gmail_queue(record, directory_csv, out_dir)
            result["slug"] = slug
            by_slug[slug] = result
            total_resolved += int(result.get("resolved") or 0)
            total_unresolved += int(result.get("unresolved") or 0)
            total_cached_negative += int(result.get("cached_negative") or 0)
            if int(result.get("resolved") or 0) > 0:
                artifacts["gmail_directory_resolution_records"].append({
                    "account_email": record.get("account_email", ""),
                    "resolutions_csv": result.get("directory_resolutions_csv"),
                    "people_csv": record.get("people_csv"),
                    "slug": slug,
                    "source": "directory",
                    "resolved": result.get("resolved"),
                })
            if int(result.get("unresolved") or 0) > 0:
                artifacts["gmail_unresolved_linkedin_resolution_queue_csvs"].append({
                    "account_email": record.get("account_email", ""),
                    "queue_csv": result.get("unresolved_queue_csv"),
                    "people_csv": record.get("people_csv"),
                    "slug": slug,
                    "source": "directory_unresolved",
                    "unresolved": result.get("unresolved"),
                })
            if int(result.get("cached_negative") or 0) > 0:
                artifacts["gmail_cached_negative_linkedin_resolution_queue_csvs"].append({
                    "account_email": record.get("account_email", ""),
                    "queue_csv": result.get("cached_negative_queue_csv"),
                    "people_csv": record.get("people_csv"),
                    "slug": slug,
                    "source": "directory_cached_negative",
                    "cached_negative": result.get("cached_negative"),
                })
            results.append(result)
        self._mark_step("gmail_directory", "completed", checkpoint=checkpoint, observation_checkpoint=observation_checkpoint, resolved=total_resolved, unresolved=total_unresolved, cached_negative=total_cached_negative, payload={"results": results})
        if total_cached_negative:
            emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_cached_negative} already attempted, {total_unresolved} unresolved.")
        else:
            emit_progress(f"Gmail directory mappings applied: {total_resolved} resolved, {total_unresolved} unresolved.")
        return True

    def _apply_and_enrich_step(self) -> bool:
        """Apply the combined stored Gmail resolutions to each account's people.csv and materialize the merged Gmail people artifact."""
        input_cfg = self.state.get("input", {})
        artifacts = self.state.setdefault("artifacts", {})
        raw_resolution_records: list[dict[str, Any]] = []
        if input_cfg.get("gmail_resolutions_csv"):
            people_records = [
                record for record in ordered_records(
                    artifacts.get("gmail_people_records") or [],
                    unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")),
                )
                if isinstance(record, dict) and record.get("people_csv")
            ]
            raw_resolution_records.extend([
                {
                    "account_email": record.get("account_email", ""),
                    "resolutions_csv": input_cfg.get("gmail_resolutions_csv"),
                    "people_csv": record.get("people_csv"),
                    "slug": record.get("slug") or record.get("account_email") or f"account-{index}",
                    "source": "explicit",
                }
                for index, record in enumerate(people_records)
            ])
        raw_resolution_records.extend(record for record in artifacts.get("gmail_directory_resolution_records") or [] if isinstance(record, dict))
        raw_resolution_records.extend(record for record in artifacts.get("gmail_linkedin_resolutions_csvs") or [] if isinstance(record, dict))
        if raw_resolution_records:
            commit_gmail_resolutions_to_directory(input_cfg, artifacts, raw_resolution_records)
        resolution_records = combine_gmail_resolution_records(raw_resolution_records, artifact_dir_from_state(self.state))
        if not resolution_records:
            self._mark_step("gmail_apply_enrich", "skipped", reason="no gmail resolutions")
            return True
        resolution_records = ordered_records(resolution_records, unique_strings(input_cfg.get("gmail_account_emails") or input_cfg.get("gmail_account_email")))
        checkpoint = build_directory_checkpoint(input_cfg, artifacts)
        artifacts["directory_csv"] = checkpoint["directory_csv"]
        artifacts["directory_checkpoint"] = checkpoint
        artifacts["gmail_apply_enrich_by_slug"] = {}
        by_slug = artifacts["gmail_apply_enrich_by_slug"]
        artifacts["gmail_resolved_people_csvs"] = []
        artifacts["gmail_final_people_csvs"] = []
        artifacts["gmail_combined_resolutions_csvs"] = resolution_records
        self._begin_step("gmail_apply_enrich", f"Applying Gmail LinkedIn matches for {len(resolution_records)} account file(s).")
        results = []
        final_people_csvs = []
        for index, record in enumerate(resolution_records):
            slug = source_slug(record.get("account_email") or record.get("slug") or f"account-{index}")
            account_dir = Path(str(record.get("people_csv") or "")).parent
            resolved_dir = account_dir / "resolved"
            apply_cmd = py_cmd(
                "packs/ingestion/primitives/discover/gmail/discover_engine.py",
                "apply-resolutions",
                "--people-csv", str(record["people_csv"]),
                "--resolutions-csv", str(record["resolutions_csv"]),
                "--output-dir", str(resolved_dir),
            )
            code, payload, stderr = run_cmd(apply_cmd, prefix="[gmail-import]")
            if code != 0:
                self._mark_step("gmail_apply_enrich", "failed", error=stderr or payload)
                self.state["status"] = "failed"
                emit({"status": "failed", "step_id": "gmail_apply_enrich", "error": stderr or payload})
                return False
            resolved_people = payload.get("people_csv") or record["people_csv"]
            artifacts.setdefault("gmail_resolved_people_csvs", []).append(resolved_people)
            artifacts["gmail_resolved_people_csv"] = resolved_people
            result = {"account_email": record.get("account_email", ""), "slug": slug, "apply": payload, "people_csv": resolved_people}
            final_people_csvs.append(resolved_people)
            artifacts["gmail_people_csv"] = resolved_people
            result["final_people_csv"] = resolved_people
            by_slug[slug] = result
            results.append(result)
        artifacts["gmail_account_final_people_csvs"] = final_people_csvs
        artifacts["gmail_final_people_csvs"] = final_people_csvs
        gmail_merge = materialize_gmail_merged_people_csv(final_people_csvs, DEFAULT_BASE_DIR / "gmail" / "people.gmail.csv")
        artifacts["gmail_merged_people"] = gmail_merge
        if gmail_merge.get("status") == "completed" and gmail_merge.get("people_csv"):
            artifacts["gmail_merged_people_csv"] = gmail_merge.get("people_csv")
            artifacts["gmail_final_people_csvs"] = [str(gmail_merge.get("people_csv"))]
            artifacts["gmail_people_csv"] = str(gmail_merge.get("people_csv"))
        self._mark_step("gmail_apply_enrich", "completed", payload={"results": results, "gmail_merged_people": gmail_merge})
        emit_progress("Gmail LinkedIn matches applied and enrichment completed.")
        return True

    # --- orchestration --------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """The whole import: fingerprint no-op check -> build transient state -> the
        two step methods (directory match, then apply + people materialization)
        -> candidates + directory quality checks -> the import manifest."""
        args = self.args
        expected_input = {
            "pipeline_contract": self.contract,
            "mode": "directory-only",
        }
        current = import_manifest_current("gmail", expected_input, import_dir=DEFAULT_IMPORT_DIR)
        if current and not getattr(args, "force", False):
            return current
        accounts = read_accounts(args.accounts)
        import_dir = self.import_dir
        emails = linked_gmail_accounts(accounts)
        self.state = {
            "primitive": "import_contacts_gmail",
            "source": "gmail",
            "status": "running",
            "artifact_dir": str(import_dir),
            "input": {
                "operator_id": args.operator_id,
                "from_accounts": str(args.accounts),
                "gmail_account_emails": emails,
                # Directory-only, always: this import applies the directory and any
                # STORED resolutions; resolution + enrichment live in deep-context
                # (migrate-legacy for the stored era, judged lookups for new people).
                "linkedin_directory_csv": str(DEFAULT_DIRECTORY_CSV),
                "profile_cache_dir": str(DEFAULT_PROFILE_CACHE_DIR),
            },
            "steps": {},
            "artifacts": gmail_artifacts_from_discovery(),
        }
        state = self.state
        if not state["artifacts"].get("gmail_linkedin_resolution_queue_csvs"):
            reason = "no Gmail discovery queue"
            status = "skipped"
            if state["artifacts"].get("gmail_linkedin_resolution_queue_csv") or state["artifacts"].get("gmail_invalid_discovery_records"):
                reason = "gmail_discovery_missing_per_account_people_csv"
            return write_manifest("gmail", {
                "status": status,
                "reason": reason,
                "artifact_dir": str(import_dir),
                "artifacts": state.get("artifacts", {}),
            }, import_dir=DEFAULT_IMPORT_DIR)
        for step in (self._directory_step, self._apply_and_enrich_step):
            if not step():
                return write_manifest("gmail", {
                    "status": "failed",
                    "artifact_dir": str(import_dir),
                    "steps": state.get("steps", {}),
                    "artifacts": state.get("artifacts", {}),
                }, import_dir=DEFAULT_IMPORT_DIR)
        state["status"] = "completed"
        people_csv = copy_people_csv("gmail", str(state.get("artifacts", {}).get("gmail_merged_people_csv") or state.get("artifacts", {}).get("gmail_people_csv") or ""), import_dir=DEFAULT_IMPORT_DIR)
        candidates = write_gmail_candidates(state.get("artifacts", {}), import_dir)
        directory_normalization = normalize_directory_source_accounts("gmail")
        directory_quality = directory_source_account_quality("gmail")
        if directory_quality["status"] != "ok":
            return write_manifest("gmail", {
                "status": "failed",
                "reason": "directory_source_account_quality_failed",
                "artifact_dir": str(import_dir),
                "outputs": {
                    "people_csv": people_csv,
                    "directory_csv": str(DEFAULT_DIRECTORY_CSV),
                },
                "directory_normalization": directory_normalization,
                "directory_quality": directory_quality,
                "steps": state.get("steps", {}),
                "artifacts": state.get("artifacts", {}),
            }, import_dir=DEFAULT_IMPORT_DIR)
        return write_manifest("gmail", {
            "status": "completed",
            "artifact_dir": str(import_dir),
            "input": {
                **expected_input,
                "discovery_manifest": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "manifest.json"),
                "contacts_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "contacts.csv"),
                "linkedin_resolution_queue_csv": str(DEFAULT_BASE_DIR / "discover" / "gmail" / "linkedin_resolution_queue.csv"),
            },
            "outputs": {
                "people_csv": people_csv,
                "candidates_csv": candidates["candidates_csv"],
                "directory_csv": str(DEFAULT_DIRECTORY_CSV),
            },
            "stats": {
                "people": csv_count(people_csv),
                "candidates": candidates["candidates"],
            },
            "candidates": candidates,
            "steps": state.get("steps", {}),
            "directory_normalization": directory_normalization,
            "directory_quality": directory_quality,
            "artifacts": state.get("artifacts", {}),
        }, import_dir=DEFAULT_IMPORT_DIR)

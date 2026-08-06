"""Migrate legacy Parallel LinkedIn resolutions into the reviewable retarget format.

The retired gmail orchestrator web-researched contacts email-by-email and attached
the found LinkedIn (accepted at >=0.75) directly to their people rows — no judge,
no review queue. Today's import replays those stored links forever, while
deep-context never audits them (the people are outside its population), so a
wrong-person link stays silently wrong.

This migrator turns each still-unverified legacy link into the NEW format: a
pending `retarget` proposal in overrides/review.csv — the exact shape
deep-research proposals use — so the EXISTING machinery takes over: the retarget
judge verifies the profile against the person's message-derived dossier facts,
auto-stand rules absorb the confident verdicts, the Check-LinkedIn queue shows the
ambiguous rest, and an approved row flows through apply_retargets (cache-first
enrichment) into the fan-in. Nothing new to operate; the legacy links simply
enter the loop instead of bypassing it.

Flow: read the gmail people rows -> parse each into a `LegacyRow` -> `eligibility()`
decides its fate first-rule-wins -> an eligible row becomes a `Candidate` (proposal
+ optional judge task, built from its CACHED profile view) -> on --apply --judge the
tasks fan out and each returns the llm_reject* fields applied to its proposal ->
upsert_retargets writes the pending rows. No manifest file: the returned dict is
emitted to stdout, and overrides/review.csv is the only durable write.

Scope per person (all conditions):
  - gmail people row with enrichment_provider=parallel_linkedin_resolution
  - NOT already in merged/people.csv (those were admitted via enrichment and are
    corroborated by the LinkedIn-import lane)
  - has a facts file (the judge's evidence; no facts -> nothing to verify against)
  - no user decision and no already-judged retarget on the row (sticky upsert
    preserves user rows regardless — the skip just keeps counts honest)

Default is a dry run (counts only, no writes, no spend). `--apply` writes pending
proposals. `--apply --judge` additionally judges each proposal against the CACHED
RapidAPI profile (profile_cache_v2) through the same judge as deep-research
proposals — spend-bearing unless `--no-llm` (deterministic fallback, tests only).

Run: uv run --project . python -m packs.ingestion.primitives.deep_context.migrate_legacy_resolutions

Changelog:
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
  2026-07-30 (style pass): `run(args)` became the construct-and-run
    `MigrateLegacyResolutions` class over a thin argparse `main()`; the six
    scattered eligibility branches collapsed into the first-rule-wins
    `eligibility()`; rows parse into the frozen `LegacyRow` at the boundary and
    carry through as `Candidate`; the judge fan-out returns reject fields the
    caller applies instead of stashing `_judge_task` on the proposal dict;
    `import json` moved to the top. No behavior change.
"""
from __future__ import annotations

import argparse
import csv
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    emit,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    ROOT,
)
from packs.ingestion.primitives.deep_context.db.models import (
    ApprovedState,
    RESEARCH_CONFIRM_THRESHOLD,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.db.snapshots import identity_snapshot
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.common.paths import DEFAULT_DIRECTORY_CSV, source_import_dir
from packs.ingestion.primitives.deep_context.reconcile_deep_research import (
    judge_concurrency,
    proposal_fingerprint,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    dossier_view,
    judge_research_proposal,
    research_proposal_task,
    research_reject_fields,
    upsert_retargets,
)

LEGACY_PROVIDER = "parallel_linkedin_resolution"
# Built from the shared path helpers rather than re-spelled (primitives/common/paths.py).
GMAIL_PEOPLE_CSV = source_import_dir("gmail") / "people.csv"
DIRECTORY_CSV = DEFAULT_DIRECTORY_CSV
USER_APPROVED = {ApprovedState.YES.value, ApprovedState.NO.value}
CANONICAL_DB = ROOT / "deep-context.sqlite"


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_json(path: Path) -> dict[str, Any]:
    # Local to this module (not common.jsonio.read_json) because the callers below
    # index the result: a non-dict JSON payload must degrade to {}, not to a crash.
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def cache_profile_view(record: dict[str, Any]) -> dict[str, Any]:
    """Judge-facing identity view of a CACHED RapidAPI profile.

    Mirrors the role of the deep-research profile view (name/headline/positions/
    education/location evidence for the retarget judge) but is built from
    profile_cache_v2 — the legacy links' profiles are already cached, so the
    judge pass costs no provider calls. Fingerprints stay internally consistent
    because migration and its judge both use this builder."""
    prof = record.get("normalized_profile") or {}
    if not isinstance(prof, dict) or not prof.get("success"):
        return {}
    positions = []
    for exp in prof.get("experiences") or []:
        if not isinstance(exp, dict):
            continue
        label = " — ".join(part for part in (
            str(exp.get("title") or "").strip(), str(exp.get("company") or "").strip()) if part)
        if label:
            positions.append(label)
    education = []
    for row in prof.get("education") or []:
        if not isinstance(row, dict):
            continue
        label = ", ".join(part for part in (
            str(row.get("degree") or "").strip(), str(row.get("school") or "").strip()) if part)
        if label:
            education.append(label)
    return {
        "name": str(prof.get("full_name") or ""),
        "headline": str(prof.get("headline") or ""),
        "location": str(prof.get("location_str") or ""),
        "positions": positions[:8],
        "education": education[:4],
        "summary": str(record.get("simple_summary") or prof.get("summary") or "")[:400],
        "source": "profile_cache_v2",
    }


def legacy_provenance(directory_csv: Path) -> dict[str, dict[str, str]]:
    """pub -> best {confidence, reasoning, email} the legacy era recorded in directory.csv."""
    best: dict[str, dict[str, str]] = {}
    for row in _read_rows(directory_csv):
        pub = (row.get("public_identifier") or "").strip().lower()
        if not pub or (row.get("status") or "").strip().lower() != "found":
            continue
        try:
            conf = float(row.get("confidence") or 0)
        except ValueError:
            conf = 0.0
        prior = best.get(pub)
        if prior is None or conf > float(prior.get("confidence") or 0):
            best[pub] = {
                "confidence": f"{conf:.2f}",
                "reasoning": (row.get("reasoning") or "").strip(),
                "email": (row.get("email") or "").strip().lower(),
            }
    return best


@dataclass(frozen=True)
class LegacyRow:
    """One gmail people.csv row, normalized once at the boundary.

    `public_identifier` and `provider` are lowercased here (the identity/lookup
    keys); `person_id` keeps its stored case because it names the facts file and
    is written back onto the proposal."""

    person_id: str
    public_identifier: str
    linkedin_url: str
    full_name: str
    provider: str

    @classmethod
    def from_row(cls, row: dict[str, str]) -> LegacyRow:
        return cls(
            person_id=(row.get("id") or "").strip(),
            public_identifier=(row.get("public_identifier") or "").strip().lower(),
            linkedin_url=(row.get("linkedin_url") or "").strip(),
            full_name=(row.get("full_name") or "").strip(),
            provider=(row.get("enrichment_provider") or "").strip().lower(),
        )


@dataclass(frozen=True)
class Candidate:
    """An eligible person's retarget proposal plus, when judging, its judge task.

    `cached_profile` is independent of `judge_task`: the cached profile view is
    read for every eligible person (so a dry run can report `no_cached_profile`),
    while a task is only built when the run will actually judge."""

    proposal: dict[str, Any]
    judge_task: dict[str, Any] | None
    cached_profile: bool


def eligibility(row: LegacyRow, *, seen_pubs: set[str], merged_ids: set[str],
                overrides: dict[str, dict[str, str]], facts_dir: Path) -> str:
    """First rule that fires wins — the one place a legacy row's fate is decided.

    Returns "" for a row that is not a legacy candidate at all (uncounted: it is
    not a legacy row, is unusable, or repeats a pub already handled), otherwise a
    `counts` skip key, or "eligible"."""
    if row.provider != LEGACY_PROVIDER:
        return ""
    if not row.public_identifier or not row.linkedin_url or not row.person_id:
        return ""
    if row.public_identifier in seen_pubs:
        return ""
    if row.person_id.lower() in merged_ids:
        return "skipped_in_merged"
    prior = overrides.get(row.public_identifier) or {}
    if (prior.get("approved") or "").strip().lower() in USER_APPROVED:
        return "skipped_user_decided"
    if ((prior.get("action") or "").strip().lower() == "retarget"
            and (prior.get("llm_judge_fingerprint") or "").strip()):
        return "skipped_already_judged"
    if not (facts_dir / f"{row.person_id}.jsonl").exists():
        return "skipped_no_facts"
    return "eligible"


class MigrateLegacyResolutions:
    """Legacy Parallel links -> pending `retarget` rows in overrides/review.csv.

    Deliberately NOT a pipeline Node: it declares no artifacts and writes no
    manifest file. `run()` returns the payload dict the CLI emits, and the sticky
    upsert into the overrides CSV is its only durable write."""

    def __init__(
        self,
        *,
        db: Db,
        gmail_people: Path | None = None,
        merged_people: Path | None = None,
        directory_csv: Path | None = None,
        overrides: Path | None = None,
        facts_dir: Path | None = None,
        raw_dir: Path | None = None,
        cache_dir: Path | None = None,
        confirm_threshold: float = RESEARCH_CONFIRM_THRESHOLD,
        limit: int = 0,
        apply: bool = False,
        judge: bool = False,
        no_llm: bool = False,
        model: str = DEFAULT_MODEL,
        reasoning_effort: str = "medium",
        timeout: int = 120,
        max_retries: int = 6,
    ) -> None:
        self.db = db
        self.gmail_people = Path(gmail_people or GMAIL_PEOPLE_CSV)
        self.merged_people = Path(merged_people or DEFAULT_PEOPLE_CSV)
        self.directory_csv = Path(directory_csv or DIRECTORY_CSV)
        self.overrides_csv = Path(overrides or LINKEDIN_OVERRIDES_CSV)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.cache_dir = Path(cache_dir or PROFILE_CACHE_DIR)
        self.confirm_threshold = confirm_threshold
        self.limit = limit
        self.apply = apply
        self.judge = judge
        self.no_llm = no_llm
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout = timeout
        self.max_retries = max_retries

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        merged_ids = {(r.get("id") or "").strip().lower()
                      for r in _read_rows(self.merged_people)} - {""}
        overrides = {
            row.key: {name: value for name, value in asdict(row).items() if name != "key"}
            for row in identity_snapshot(self.db).review_rows
        }
        provenance = legacy_provenance(self.directory_csv)

        counts = {
            "legacy_rows": 0, "eligible": 0,
            "skipped_in_merged": 0, "skipped_no_facts": 0,
            "skipped_user_decided": 0, "skipped_already_judged": 0,
            "no_cached_profile": 0, "judged": 0,
        }
        candidates: list[Candidate] = []
        seen_pubs: set[str] = set()
        for raw in _read_rows(self.gmail_people):
            row = LegacyRow.from_row(raw)
            verdict = eligibility(row, seen_pubs=seen_pubs, merged_ids=merged_ids,
                                  overrides=overrides, facts_dir=self.facts_dir)
            if not verdict:
                continue
            seen_pubs.add(row.public_identifier)
            counts["legacy_rows"] += 1
            if verdict != "eligible":
                counts[verdict] += 1
                continue
            counts["eligible"] += 1
            candidate = self.build_candidate(row, provenance.get(row.public_identifier) or {})
            if not candidate.cached_profile:
                counts["no_cached_profile"] += 1
            candidates.append(candidate)
            if self.limit and len(candidates) >= self.limit:
                break

        # `build_candidate` only mints a judge task on --apply, so `self.apply` is a
        # deliberate SECOND gate on the paid judge: defense-in-depth, so a future
        # change that starts building tasks in a dry run cannot start spending.
        pending = [c for c in candidates if c.judge_task is not None]
        if pending and self.apply:
            counts["judged"] = self.judge_all(pending)

        proposals = [c.proposal for c in candidates]
        manifest: dict[str, Any] = {
            "source": "migrate_legacy_resolutions",
            "status": "dry_run" if not self.apply else "completed",
            **counts,
            "proposals": len(proposals),
            "overrides_csv": str(self.overrides_csv),
            "judge": bool(self.judge),
            "confirm_threshold": self.confirm_threshold,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "updated_at": now_iso(),
        }
        if not self.apply:
            if self.judge:
                per_lo, per_hi = 0.004, 0.02
                would = counts["eligible"] - counts["no_cached_profile"]
                manifest["estimated_judge_cost_usd_low"] = round(would * per_lo, 2)
                manifest["estimated_judge_cost_usd_high"] = round(would * per_hi, 2)
            return manifest
        manifest.update(upsert_retargets(self.db, proposals))
        # Re-stamp: the writer's counts merge in, but the run's status is ours.
        manifest["status"] = "completed"
        return manifest

    def build_candidate(self, row: LegacyRow, provenance: dict[str, str]) -> Candidate:
        """Shape one eligible person's pending proposal from its legacy provenance."""
        confidence = float(provenance.get("confidence") or 0)
        reasoning = provenance.get("reasoning") or ""
        email = provenance.get("email") or ""
        proposal: dict[str, Any] = {
            "old_public_identifier": row.public_identifier,
            "new_linkedin_url": row.linkedin_url,
            "linkedin_url": row.linkedin_url,
            "match_emails": [email] if email else [],
            "match_phones": [],
            "person_id": row.person_id,
            "confidence": confidence,
            "reason": (f"migrated legacy parallel resolution "
                       f"(legacy conf {confidence:.2f}): {reasoning[:200]}").strip().rstrip(":"),
            "source": "legacy-migration",
        }
        view = cache_profile_view(_read_json(self.cache_dir / f"{row.public_identifier}.json"))
        task: dict[str, Any] | None = None
        # Judging is APPLY-only: a dry run must stay $0 (it reports the would-judge
        # count + cost estimate instead of calling the provider).
        if self.judge and view and self.apply:
            dossier = dossier_view([row.person_id], self.facts_dir, self.raw_dir)
            task = research_proposal_task(
                dossier, view, name=row.full_name,
                match_emails=proposal["match_emails"], confidence=confidence,
                unverified=True)  # legacy links skipped verification by construction
            proposal["judge_fingerprint"] = proposal_fingerprint(
                row.public_identifier, row.linkedin_url, dossier, view)
        return Candidate(proposal=proposal, judge_task=task, cached_profile=bool(view))

    def judge_one(self, task: dict[str, Any]) -> dict[str, str]:
        """Judge one proposal and RETURN the llm_reject* fields its verdict implies;
        the caller stamps them onto the matching proposal."""
        verdict = judge_research_proposal(
            task, use_llm=not self.no_llm, model=self.model, effort=self.reasoning_effort,
            timeout=self.timeout, max_retries=self.max_retries)
        return research_reject_fields(verdict, self.confirm_threshold)

    def judge_all(self, pending: list[Candidate]) -> int:
        """Bounded fan-out, mirroring propose_retargets_from_output: each judge call is a
        self-contained sync wrapper, so a thread pool keeps its retry/timeout semantics."""
        judged = 0
        with ThreadPoolExecutor(max_workers=min(judge_concurrency(), len(pending))) as pool:
            futures = {pool.submit(self.judge_one, c.judge_task): c.proposal for c in pending}
            for future in as_completed(futures):
                futures[future].update(future.result())
                judged += 1
        return judged


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Migrate legacy Parallel LinkedIn resolutions into pending retarget proposals.")
    p.add_argument("--gmail-people", default=str(GMAIL_PEOPLE_CSV))
    p.add_argument("--merged-people", default=str(DEFAULT_PEOPLE_CSV))
    p.add_argument("--directory-csv", default=str(DIRECTORY_CSV))
    p.add_argument("--overrides", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--db", default=str(CANONICAL_DB))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--cache-dir", default=str(PROFILE_CACHE_DIR))
    p.add_argument("--confirm-threshold", type=float, default=RESEARCH_CONFIRM_THRESHOLD)
    p.add_argument("--limit", type=int, default=0, help="Cap migrated people (0 = all)")
    p.add_argument("--apply", action="store_true", help="Write proposals (default: dry run)")
    p.add_argument("--judge", action="store_true",
                   help="Also judge each proposal against its CACHED profile (spend unless --no-llm)")
    p.add_argument("--no-llm", action="store_true", help="Deterministic judge fallback (tests only)")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default="medium", choices=["minimal", "low", "medium", "high"])
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--max-retries", type=int, default=6)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit(MigrateLegacyResolutions(
        db=Db(Path(args.db)),
        gmail_people=Path(args.gmail_people),
        merged_people=Path(args.merged_people),
        directory_csv=Path(args.directory_csv),
        overrides=Path(args.overrides),
        facts_dir=Path(args.facts_dir),
        raw_dir=Path(args.raw_dir),
        cache_dir=Path(args.cache_dir),
        confirm_threshold=args.confirm_threshold,
        limit=args.limit,
        apply=args.apply,
        judge=args.judge,
        no_llm=args.no_llm,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        timeout=args.timeout,
        max_retries=args.max_retries,
    ).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

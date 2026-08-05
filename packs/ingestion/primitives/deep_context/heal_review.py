"""Self-heal pass run BEFORE the review UI serves (`bin/deep-context heal`).

A definitive, first-class step of `bin/deep-context review <stage>`: it always
runs, always announces itself (a clean store prints one `[heal] ... (nothing
to do)` line), and stamps its summary into the review stage manifest. No
approval gate: invoking review/heal IS the consent — the pre-run count lines
are information. Uncapped by default (2026-08-05: a silent 200-cap left 43 of
a real store's 243 judge-skips unhealed and read as "heal ran, still
broken"); --cap is an optional manual bound and a capped run says what it
left behind.

What this actually does, in order:
  1. LEGACY SCRUBS — `ensure_owner_phones` + `resolve_stored_identity_policy`,
     exactly as the review server runs them at boot (idempotent; kept there too).
  2. FETCH — select every UNDECIDED candidate whose stored verdict is the
     judge-skip ("needs_review", confidence 0.0, no usable LinkedIn profile)
     with an attached URL, and ask the RapidAPI client for FRESH truth: one
     `get_profile(fresh=True)` per candidate (the client owns cache-vs-fetch;
     repeats never re-bill).
  3. JUDGE — parents whose candidate came back CONTENT re-judge through the
     normal `ReconcileLinkedin` subset pass (same judge, same write path, so
     the confirm/detach bars auto-apply exactly as usual). Skipped with a
     one-liner when no OpenAI key is configured.
  4. TERMINATE — a candidate whose FRESH answer is EMPTY is a confirmed dead
     link: detach the row (machine-grade, approved=auto), then stand a FREE
     identity when one exists — (a) an existing synthetic-people.csv row for
     the person gates to yes; (b) an existing deep-research output whose
     proposed URL is the now-dead link mints a synthetic via a scoped
     `AssembleSyntheticProfile` run (prune=False); (c) otherwise the person
     stays a pending re-research card. Never any new paid research, never a
     human yes/no row. An ERROR answer terminates NOBODY — an outage or a
     keyless install must not detach anyone.

Idempotent: judged rows carry a real verdict, terminated rows carry
approved=auto, so the next run selects nothing, fetches nothing, and spends
nothing.

Session gate: standalone `bin/deep-context heal` refuses while a review server
owns the review session (single-writer contract). `--pre-restart` — passed ONLY
by the `review` verb — skips that gate: review heals FIRST (the old UI keeps
serving while the pass runs), then immediately stops the server and boots a
fresh one that re-reads disk, so the live server's in-memory model never
outlives these writes.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.legacy import (
    ensure_owner_phones,
    resolve_stored_identity_policy,
)
from packs.ingestion.primitives.deep_context.assemble_synthetic_profile import (
    AssembleSyntheticProfile,
    profile_is_usable,
)
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    FACTS_DIR,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    OWNER_JSON,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    RAW_DIR,
    REVIEW_MANIFEST,
    VERDICTS_CSV,
    VERDICTS_JSONL,
    emit,
    ensure_no_review_session,
    load_env,
    now_iso,
    read_jsonl,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    NO_PROFILE_REASON,
    ReconcileLinkedin,
    count_pending,
)
from packs.ingestion.primitives.deep_context.review_store import (
    HEAL_DETACH_SOURCE,
    OVERRIDE_COLUMNS,
    load_override_rows,
    write_override_rows,
)
from packs.ingestion.primitives.deep_context.review_web.decisions import (
    apply_synthetic_decision,
)
from packs.ingestion.primitives.deep_context.review_web.model import (
    SYNTHETIC_PEOPLE_CSV,
)
from packs.ingestion.primitives.enrich.rapidapi_client import (
    PROFILE_CONTENT,
    PROFILE_EMPTY,
    RapidApiClient,
)
from packs.ingestion.primitives.imports.common import write_manifest
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.ingestion.primitives.deep_context.review_db import commit_review_rows

# UNCAPPED by default (owner directive 2026-08-05): the heal is a definitive
# always-run task and a silent cap reads as "heal ran, still broken" — the
# first real 243-candidate store proved it. The RapidAPI client's rate limiter
# is the natural throttle; --cap remains only as a manual bound, and a capped
# run SAYS what it left behind.
HEAL_BATCH_CAP: int | None = None
_FETCH_WORKERS = 8


@dataclass(frozen=True)
class HealCandidate:
    """One judge-skipped, undecided (parent, attached-LinkedIn) pair."""

    parent_slug: str
    pub: str
    url: str
    person_ids: tuple[str, ...] = ()
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()


def _say(line: str) -> None:
    print(f"[heal] {line}", file=sys.stderr, flush=True)


class HealReview:
    """Construct-and-run: `HealReview().run()` returns the JSON-able summary."""

    def __init__(
        self,
        *,
        review_csv: Path | None = None,
        verdicts_jsonl: Path | None = None,
        verdicts_csv: Path | None = None,
        people_csv: Path | None = None,
        profile_cache_dir: Path | None = None,
        synthetic_csv: Path | None = None,
        index_json: Path | None = None,
        facts_dir: Path | None = None,
        raw_dir: Path | None = None,
        parents_dir: Path | None = None,
        deep_research_dir: Path | None = None,
        owner_json: Path | None = None,
        review_manifest: Path | None = None,
        cap: int | None = HEAL_BATCH_CAP,
    ) -> None:
        self.review_csv = Path(review_csv or LINKEDIN_OVERRIDES_CSV)
        self.verdicts_jsonl = Path(verdicts_jsonl or VERDICTS_JSONL)
        self.verdicts_csv = Path(verdicts_csv or VERDICTS_CSV)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.synthetic_csv = Path(synthetic_csv or SYNTHETIC_PEOPLE_CSV)
        self.index_json = Path(index_json or INDEX_JSON)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.parents_dir = Path(parents_dir or PARENTS_DIR)
        self.deep_research_dir = Path(deep_research_dir or DEEP_RESEARCH_DIR)
        self.owner_json = Path(owner_json or OWNER_JSON)
        self.review_manifest = Path(review_manifest or REVIEW_MANIFEST)
        self.cap = None if cap is None else max(1, int(cap))

    # ---- selection ---------------------------------------------------------

    def select_candidates(self) -> tuple[list[HealCandidate], int, int]:
        """(candidates, skipped_pending_retarget, uncapped_total).

        A candidate is a judge-skipped stored verdict (needs_review, 0.0,
        NO_PROFILE_REASON) with an attached URL whose review row is still
        undecided (approved not in yes/no/auto). A pending retarget proposing
        a DIFFERENT profile is already a live review card and is left to that
        flow. Human yes/no rows are never candidates."""
        rows = load_override_rows(self.review_csv)
        seen: set[str] = set()
        out: list[HealCandidate] = []
        skipped_retarget = 0
        for rec in read_jsonl(self.verdicts_jsonl):
            if rec.get("no_link"):
                continue
            verdict = rec.get("verdict") or {}
            if (verdict.get("verdict") != "needs_review"
                    or float(verdict.get("confidence") or 0) != 0.0
                    or (verdict.get("reason") or "") != NO_PROFILE_REASON):
                continue
            pub = (rec.get("candidate_key") or "").strip().lower()
            url = ((rec.get("linkedin") or {}).get("linkedin_url") or "").strip()
            if not pub or not url or pub in seen:
                continue
            seen.add(pub)
            row = rows.get(pub) or {}
            if (row.get("approved") or "").strip().lower() in {"yes", "no", "auto"}:
                continue
            new_pub = (row.get("new_public_identifier") or "").strip().lower()
            if ((row.get("action") or "").strip().lower() == "retarget"
                    and new_pub not in {"", pub}):
                skipped_retarget += 1
                continue
            out.append(HealCandidate(
                parent_slug=str(rec.get("parent_slug") or ""),
                pub=pub, url=url,
                person_ids=tuple(str(p) for p in rec.get("person_ids") or []),
                match_emails=tuple(str(e) for e in rec.get("match_emails") or []),
                match_phones=tuple(str(p) for p in rec.get("match_phones") or []),
            ))
        out.sort(key=lambda c: (c.parent_slug, c.pub))
        capped = out if self.cap is None else out[: self.cap]
        if len(capped) < len(out):
            _say(f"cap {self.cap}: healing {len(capped)} of {len(out)} — "
                 f"run review again for the remaining {len(out) - len(capped)}")
        return capped, skipped_retarget, len(out)

    # ---- fetch -------------------------------------------------------------

    def fetch_states(self, candidates: list[HealCandidate]) -> dict[str, dict[str, Any]]:
        """pub -> get_profile result. One fresh client call per candidate; the
        client owns cache-vs-fetch and the no-rebill promise."""
        if not candidates:
            return {}
        _say(f"requesting fresh profiles for {len(candidates)} attached links "
             "(RapidAPI, billed per fetch; repeats served from cache)")
        client = RapidApiClient()
        states: dict[str, dict[str, Any]] = {}
        done = 0
        with ThreadPoolExecutor(max_workers=min(_FETCH_WORKERS, len(candidates))) as pool:
            for candidate, result in zip(candidates, pool.map(
                    lambda c: client.get_profile(
                        c.pub, c.url, cache_dir=self.profile_cache_dir, fresh=True),
                    candidates)):
                states[candidate.pub] = result
                done += 1
                if done % 25 == 0:
                    _say(f"profiles {done}/{len(candidates)}")
        return states

    # ---- judge -------------------------------------------------------------

    def rejudge(self, candidates: list[HealCandidate]) -> dict[str, Any]:
        """Re-judge the CONTENT candidates' parents through the normal subset
        reconcile pass — the judge, thresholds, and review.csv write path are
        exactly the standing ones."""
        summary: dict[str, Any] = {"candidates": len(candidates), "parents": 0,
                                   "verified": 0, "detached": 0, "pending": 0,
                                   "restored_pending_retargets": 0,
                                   "skipped_no_openai_key": False}
        if not candidates:
            return summary
        parents = sorted({c.parent_slug for c in candidates if c.parent_slug})
        summary["parents"] = len(parents)
        load_env()
        if not (os.environ.get("OPENAI_API_KEY") or "").strip():
            summary["skipped_no_openai_key"] = True
            _say(f"no OpenAI key — leaving {len(candidates)} hydrated candidates "
                 "for the next reconcile run")
            return summary
        _say(f"re-judging {len(candidates)} candidates across {len(parents)} people "
             "(OpenAI, ~cents)")
        # The subset pass's write_overrides restamps EVERY machine row from the
        # stored verdicts, which would erase pending deep-research retarget
        # proposals on rows OUTSIDE the healed scope. Snapshot them and restore
        # any collateral loss after the run — a freshly judged pub keeps its
        # new verdict (the proposal was made against the old, profile-less
        # evidence), and a row the pass decided (auto/yes/no) stands.
        healed_pubs = {c.pub for c in candidates}
        saved_retargets = {
            pub: dict(row) for pub, row in load_override_rows(self.review_csv).items()
            if pub not in healed_pubs
            and (row.get("action") or "").strip().lower() == "retarget"
            and (row.get("approved") or "").strip().lower() not in {"yes", "no"}
            and (row.get("new_linkedin_url") or "").strip()}
        ReconcileLinkedin(
            index_json=self.index_json,
            people_csv=self.people_csv,
            profile_cache_dir=self.profile_cache_dir,
            facts_dir=self.facts_dir,
            raw_dir=self.raw_dir,
            parents_dir=self.parents_dir,
            verdicts_jsonl=self.verdicts_jsonl,
            verdicts_csv=self.verdicts_csv,
            overrides_csv=self.review_csv,
            consolidate_people_csv=self.review_csv.parent / "consolidate-people.csv",
            slug=parents,
        ).run()
        rows = load_override_rows(self.review_csv)
        restored = 0
        for pub, saved in saved_retargets.items():
            now = rows.get(pub) or {}
            if ((now.get("action") or "").strip().lower() != "retarget"
                    and not (now.get("approved") or "").strip()):
                rows[pub] = saved
                restored += 1
        if restored:
            commit_review_rows(self.review_csv, rows)
        summary["restored_pending_retargets"] = restored
        for candidate in candidates:
            row = rows.get(candidate.pub) or {}
            action = (row.get("action") or "").strip().lower()
            approved = (row.get("approved") or "").strip().lower()
            if approved == "auto" and action == "verify":
                summary["verified"] += 1
            elif approved == "auto" and action == "detach":
                summary["detached"] += 1
            elif approved not in {"yes", "no"}:
                summary["pending"] += 1
        return summary

    # ---- terminate ---------------------------------------------------------

    def _synthetic_gates(self) -> dict[str, tuple[str, str]]:
        """review-key -> (synthetic pub, current approved gate)."""
        gates: dict[str, tuple[str, str]] = {}
        if not self.synthetic_csv.exists():
            return gates
        with self.synthetic_csv.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                pub = (row.get("public_identifier") or "").strip().lower()
                if not pub.startswith("synth-"):
                    continue
                approved = (row.get("approved") or "").strip().lower()
                for key in {pub, (row.get("id") or "").strip().lower()} - {""}:
                    gates[key] = (pub, approved)
        return gates

    def _research_mintable(self, candidate: HealCandidate) -> bool:
        """Case (b) predicate: an existing engine research output for this
        parent proposed exactly the now-dead link, and its body is usable."""
        research_json = (self.deep_research_dir / candidate.parent_slug
                         / "01_research_parallel.json")
        if not research_json.is_file():
            return False
        try:
            profile = json.loads(research_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        proposed = str((profile.get("social") or {}).get("linkedin_url") or "")
        if extract_public_identifier(proposed).lower() != candidate.pub:
            return False
        return profile_is_usable(profile)

    def _mint_from_research(self, candidates: list[HealCandidate]) -> dict[str, Any]:
        """Case (b): copy each research output with its (dead) proposed URL
        cleared into a scratch dir and run ONE scoped assemble over it —
        prune=False, exactly like the guided-retarget flow's scoped mint. The
        paid artifact on disk is never modified."""
        with tempfile.TemporaryDirectory(prefix="heal-synth-") as scratch:
            scratch_dir = Path(scratch)
            for candidate in candidates:
                src = (self.deep_research_dir / candidate.parent_slug
                       / "01_research_parallel.json")
                profile = json.loads(src.read_text(encoding="utf-8"))
                profile["social"] = {**(profile.get("social") or {}), "linkedin_url": ""}
                dst = scratch_dir / candidate.parent_slug
                dst.mkdir(parents=True, exist_ok=True)
                (dst / "01_research_parallel.json").write_text(
                    json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
            payload = AssembleSyntheticProfile(
                research_dir=scratch_dir,
                queue_csv=scratch_dir / "research_queue.csv",  # absent: scope = scratch dirs
                people_csv=self.people_csv,
                verdicts_jsonl=self.verdicts_jsonl,
                out=self.synthetic_csv,
                index_json=self.index_json,
                facts_dir=self.facts_dir,
                prune=False,
            ).run()
        return payload.to_payload()

    def terminate(self, candidates: list[HealCandidate]) -> dict[str, Any]:
        """Confirmed-dead links: machine detach + the free identity ladder."""
        summary: dict[str, Any] = {"candidates": len(candidates), "detached": 0,
                                   "stood_synthetic": 0, "minted_synthetic": 0,
                                   "pending_reresearch": 0, "skipped_human_decided": 0,
                                   "assemble": None}
        if not candidates:
            return summary
        rows = load_override_rows(self.review_csv)
        gates = self._synthetic_gates()
        mintable: list[HealCandidate] = []
        for candidate in candidates:
            row = rows.get(candidate.pub)
            if row is None:
                row = {column: "" for column in OVERRIDE_COLUMNS}
                rows[candidate.pub] = row
            if (row.get("approved") or "").strip().lower() in {"yes", "no"}:
                summary["skipped_human_decided"] += 1  # a human raced us — their word stands
                continue
            row.update({
                "public_identifier": candidate.pub,
                "action": "detach", "approved": "auto",
                "new_linkedin_url": "", "new_public_identifier": "",
                "linkedin_url": row.get("linkedin_url") or candidate.url,
                "confidence": "1.000",
                "reason": "attached LinkedIn returned no profile content on a fresh fetch (dead link)",
                "match_emails": row.get("match_emails") or "|".join(candidate.match_emails),
                "match_phones": row.get("match_phones") or "|".join(candidate.match_phones),
                "person_id": row.get("person_id") or (candidate.person_ids[0] if candidate.person_ids else ""),
                "source": HEAL_DETACH_SOURCE, "updated_at": now_iso(),
            })
            summary["detached"] += 1
            # Free identity ladder: existing synthetic row -> research mint -> pending card.
            gate = next((gates[key] for key in
                         (candidate.pub, *(pid.strip().lower() for pid in candidate.person_ids))
                         if key in gates), None)
            if gate is not None:
                synth_pub, approved = gate
                if approved in {"yes", "no"}:
                    summary["stood_synthetic"] += approved == "yes"  # user-gated: their word stands
                else:
                    apply_synthetic_decision(self.synthetic_csv, synth_pub, "keep")
                    summary["stood_synthetic"] += 1
            elif self._research_mintable(candidate):
                mintable.append(candidate)
            else:
                summary["pending_reresearch"] += 1
        commit_review_rows(self.review_csv, rows)
        if mintable:
            summary["assemble"] = self._mint_from_research(mintable)
            summary["minted_synthetic"] = len(mintable)
        return summary

    # ---- run ---------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        started = time.monotonic()
        owner_backfilled = ensure_owner_phones(self.owner_json)
        scrubs = resolve_stored_identity_policy(
            self.review_csv, self.index_json, self.people_csv, self.synthetic_csv)
        queue_before = count_pending(self.review_csv)

        candidates, skipped_retarget, uncapped = self.select_candidates()
        states = self.fetch_states(candidates)
        content = [c for c in candidates if states[c.pub]["state"] == PROFILE_CONTENT]
        empty_fetched = [c for c in candidates
                         if states[c.pub]["state"] == PROFILE_EMPTY and states[c.pub]["fetched"]]
        # An EMPTY served from the cache without a fetch this run (keyless
        # install) is NOT a fresh confirmation — leave those people alone.
        empty_unfetched = sum(1 for c in candidates
                              if states[c.pub]["state"] == PROFILE_EMPTY
                              and not states[c.pub]["fetched"])
        errors = sum(1 for c in candidates if states[c.pub]["state"] not in
                     {PROFILE_CONTENT, PROFILE_EMPTY})

        rejudge = self.rejudge(content)
        if rejudge["candidates"] and not rejudge["skipped_no_openai_key"]:
            # The subset judge pass restamps every machine row from stored
            # verdicts, undoing stored-policy promotions (connection
            # auto-verifies, superseded punts) on rows OUTSIDE the healed
            # scope. Re-apply the idempotent policy so one heal leaves the
            # store fully settled instead of waiting for the next boot scrub.
            for key, value in resolve_stored_identity_policy(
                    self.review_csv, self.index_json, self.people_csv,
                    self.synthetic_csv).items():
                scrubs[key] = scrubs.get(key, 0) + value
        terminated = self.terminate(empty_fetched)
        queue_after = count_pending(self.review_csv)

        scrub_total = sum(int(v) for v in scrubs.values()) + int(bool(owner_backfilled))
        summary = {
            "primitive": "heal_review",
            "status": "completed",
            "owner_phones_backfilled": bool(owner_backfilled),
            "legacy_scrub": scrubs,
            "queue_pending_before": queue_before,
            "queue_pending_after": queue_after,
            "candidates": len(candidates),
            "candidates_uncapped": uncapped,
            "capped": uncapped > len(candidates),
            "cap": self.cap,
            "skipped_pending_retarget": skipped_retarget,
            "profiles": {
                "content": len(content),
                "empty_fetched": len(empty_fetched),
                "empty_unfetched": empty_unfetched,
                "error": errors,
                "fetched": sum(1 for s in states.values() if s.get("fetched")),
                "from_cache": sum(1 for s in states.values() if s.get("from_cache")),
            },
            "rejudge": rejudge,
            "terminated": terminated,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
        self._stamp_review_manifest(summary)
        tail = "" if candidates or scrub_total else " (nothing to do)"
        _say(f"scrubs {scrub_total} · fetched {summary['profiles']['fetched']} · "
             f"judged {rejudge['candidates'] if not rejudge['skipped_no_openai_key'] else 0} · "
             f"dead-links {terminated['detached']}{tail}")
        return summary

    def _stamp_review_manifest(self, summary: dict[str, Any]) -> None:
        """Merge the heal summary into the review stage manifest (the file
        review-status reads); stage writes carry the block forward."""
        try:
            existing = json.loads(self.review_manifest.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
        if not isinstance(existing, dict):
            existing = {}
        receipt = {**existing, "heal": summary}
        receipt.pop("updated_at", None)
        receipt.pop("created_at", None)
        write_manifest(self.review_manifest.parent.name, receipt,
                       import_dir=self.review_manifest.parent.parent)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Self-heal pass before review serve: legacy scrubs, fresh-fetch + "
                    "re-judge of judge-skipped links, free dead-link termination.")
    parser.add_argument("--cap", type=int, default=HEAL_BATCH_CAP,
                        help="Optional manual bound on candidates per run (default: uncapped; "
                             "a capped run reports what it left behind)")
    parser.add_argument("--pre-restart", action="store_true",
                        help="Skip the no-review-session gate: the caller stops and restarts the "
                             "review server immediately after this pass (bin/deep-context review only)")
    args = parser.parse_args(argv)
    if not args.pre_restart:
        ensure_no_review_session("heal_review")
    emit(HealReview(cap=args.cap).run())
    return 0


if __name__ == "__main__":
    sys.exit(main())

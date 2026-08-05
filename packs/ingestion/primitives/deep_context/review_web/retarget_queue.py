"""User-guided retarget queue behind the /directory person pane.

A user reading a dossier who believes the attached LinkedIn is the WRONG person
submits free-text guidance ("the Jordan Bravo who ran DevRel at Acme", or a
pasted profile URL). Each submission enqueues ONE guided re-research that runs
the SAME machinery the enrichment flow already uses:

  build_queue (reconcile_deep_research)  -> one research row; the guidance
                                            rides in as `retarget_hint`, which
                                            the Parallel prompt treats as the
                                            strongest identity clue
  run_research (deep_research_contacts)  -> the paid Parallel.ai call; the
                                            submit click IS the user's spend
                                            approval (~$0.05 core2x + judge)
  propose_retargets_from_output          -> the identity judge vets the
                                            (dossier x proposed profile) pair
                                            and sticky-upserts a `retarget`
                                            row into review.csv

Everything after the submit is automatic — the guidance click was the human's
word, so there is no second review queue:
  * guidance ASSERTING a LinkedIn URL is the person (a quick LLM intent read;
    plain URL presence offline) -> applied directly, no research, no judge,
    no spend — the same trust as the review-stage fix form. And if that read
    ever misses, the post-judge net still applies a research result whose
    profile the guidance literally references;
  * judge CONFIRM at/above the confirm bar -> the retarget auto-approves
    (`approved=yes`, `source=user-guidance`) and the profile hydrates
    cache-first via RapidAPI;
  * no usable LinkedIn (nothing found, or the judge rejected the proposal) ->
    the old wrong link detaches (the user already said it is the wrong person)
    and a synthetic profile assembled from the fresh research supersedes it as
    the standing identity;
  * only an unusable research output lands `no_match`.
Results are also mirrored into the engine's per-handle research home so a
later enrichment pass reuses them for free instead of re-billing.

The queue itself is memory-only and serial: submits never block, a single
daemon worker drains items one at a time, and a restart forgets progress but
never decisions — every durable effect lives in review.csv and the research
artifacts under `retarget-guidance/`. The guidance itself is durable
(`<handle>/guidance.json`) and doubles as the paid result's cache key: an
IDENTICAL re-submit (e.g. retrying after a crash) reuses the existing research
for free, while changed guidance sidelines the old output to `.bkup` (paid
artifacts are never deleted) and re-researches.

Changelog:
  2026-08-04: fail-closed loop fixes — the re-judge row blanking moved to
    after run_research succeeds, so ANY failed job (missing key, network,
    blocked queue) returns the person to review unchanged; every applied
    outcome settles the parent's other candidate rows so an answered person
    never bounces back into the linear queue.
  2026-08-04 (review batch, 4-model review): blanking keys on updated_at vs
    the request's submitted_at (mid-job human decisions stand even when they
    write the same value) and never re-opens a sibling's human yes/no (a
    shared pub row can be another parent's confirmed identity); a crash after
    the blanking write restores approved AND the paid-verdict fingerprint;
    settle_siblings skips machine-applied `auto` rows (matching /decide's
    withdrawal guard) and settles folded synthetic options through their
    approve gate; a mid-job human "no" vetoes the automatic synthetic-stands
    gate; queue items carry queue_slug (the review queue's parent slug —
    synthetic parents' dossier_slug differs) for inflight exclusion and
    failure notes; failed_notes_from_items reduces the newest-first snapshot
    correctly (an old failure never shadows a later outcome).
"""
from __future__ import annotations

import asyncio
import csv
import json
import re
import shutil
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.review_web.decisions import apply_synthetic_decision, sync_synthetic_gate
from packs.ingestion.primitives.deep_context.review_web.model import SYNTHETIC_PEOPLE_CSV
from packs.ingestion.primitives.deep_context import assemble_synthetic_profile
from packs.ingestion.primitives.deep_context import deep_research_contacts
from packs.ingestion.primitives.deep_context import reconcile_deep_research
from packs.indexing.lib.openai_responses import (
    make_async_client,
    parse_json_response,
    responses_kwargs,
)
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    FACTS_DIR,
    RAW_DIR,
    RECONCILE_DIR,
    load_env,
    load_owner,
    owner_background_block,
)
from packs.ingestion.primitives.deep_context.deep_research_contacts import (
    PROCESSOR_PRICING_USD,
    ResearchRunParams,
)
from packs.ingestion.primitives.deep_context.reconcile_deep_research import (
    DEFAULT_PROCESSOR,
    QUEUE_FIELDS,
    RESEARCH_OK_STATUSES,
    build_queue,
    load_people_rows,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import load_override_rows
from packs.ingestion.primitives.deep_context.review_store import (
    RESEARCH_CONFIRM_THRESHOLD,
    write_override_rows,
)
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)

# Fixed output home for guided re-research (one subdir per person handle,
# overwritten in place — no run ids). Separate from the engine's deep-research
# dir so `filter_already_done` there never mistakes a guided rerun for done work.
GUIDED_RETARGET_DIR = RECONCILE_DIR / "retarget-guidance"

# Shown on the submit button: one Parallel task + one identity-judge call.
ESTIMATED_COST_USD = round(PROCESSOR_PRICING_USD[DEFAULT_PROCESSOR] + 0.01, 2)

# States an item moves through; ACTIVE ones block a duplicate submit for the
# same person and keep the UI polling.
ACTIVE_STATES = ("queued", "researching", "judging", "hydrating")
TERMINAL_STATES = ("applied", "synthetic", "no_match", "failed")

# Research result files mirrored into the engine's research home so a later
# enrichment pass reuses the guided result for free instead of re-billing.
_RESEARCH_FILES = ("00_parallel_raw.json", "01_research_parallel.json")

_REJECT_TRUTHY = {"1", "true", "yes"}

# A LinkedIn profile URL inside the guidance text, scheme optional.
_GUIDANCE_LINKEDIN_RE = re.compile(
    r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%.]+", re.IGNORECASE)


def linkedin_url_in_guidance(guidance: str) -> tuple[str, str]:
    """(normalized url, public identifier) when the guidance contains a
    LinkedIn profile URL; ("", "") otherwise. The OFFLINE fallback for
    `specified_linkedin_url` — plain presence, no intent reading."""
    match = _GUIDANCE_LINKEDIN_RE.search(guidance or "")
    if not match:
        return "", ""
    raw = match.group(0)
    if not raw.lower().startswith("http"):
        raw = f"https://{raw}"
    url = normalize_linkedin_url(raw)
    pub = extract_public_identifier(url).lower()
    return (url, pub) if pub else ("", "")


_SPECIFIED_URL_PROMPT = (
    "The user wrote guidance about which LinkedIn profile belongs to a person. Decide "
    "whether the user is AFFIRMING that a specific LinkedIn profile URL IS that "
    "person's correct profile.\n\n"
    "Output specified_linkedin_url = that URL ONLY when the guidance affirms it:\n"
    "- \"this is the right one <url>\" / \"use <url>\" / \"their profile is <url>\"\n"
    "- a hedged affirmation still counts: \"i think it's <url>\" / \"pretty sure it's <url>\"\n"
    "- the guidance is essentially just the URL by itself\n\n"
    "Output specified_linkedin_url = \"\" (empty) in EVERY other case, including:\n"
    "- the user says a URL is NOT the person, is wrong, or should be avoided\n"
    "- a URL mentioned only as context (a coworker's page, a page where the person "
    "is mentioned, a company page)\n"
    "- no URL in the guidance at all\n\n"
    "When in doubt, output the empty string.")

_SPECIFIED_URL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {"specified_linkedin_url": {"type": "string"}},
    "required": ["specified_linkedin_url"],
}


# The intent read needs no reasoning: gpt-5-mini at minimal effort went 10/10
# on the live affirm/negate/context/hedge cases where gpt-5.2-minimal leaked
# negated URLs through. Small fails SAFE here (misses fall through to research
# + the post-judge net; a false extract would mis-attach a profile).
INTENT_MODEL = "gpt-5-mini"


def specified_linkedin_url(guidance: str, *, use_llm: bool,
                           model: str = INTENT_MODEL, timeout: int = 60) -> tuple[str, str]:
    """(url, pub) the user ASSERTS is the correct profile, else ("", "").

    A quick LLM intent read — a mentioned URL is not necessarily an assertion
    ("NOT this one" must extract nothing) — falling back to plain URL presence
    when offline or when the call fails."""
    if not (guidance or "").strip():
        return "", ""
    if not use_llm:
        return linkedin_url_in_guidance(guidance)

    async def driver() -> str:
        client = make_async_client(timeout=timeout)
        try:
            kwargs = responses_kwargs(model, effort="minimal",
                                      schema=_SPECIFIED_URL_SCHEMA,
                                      schema_name="specified_linkedin")
            response = await client.responses.create(
                model=model,
                input=[{"role": "system", "content": _SPECIFIED_URL_PROMPT},
                       {"role": "user", "content": guidance.strip()}],
                **kwargs)
            return str(parse_json_response(
                response, "specified_linkedin").get("specified_linkedin_url") or "")
        finally:
            await client.close()

    try:
        load_env()
        raw = asyncio.run(driver()).strip()
    except Exception:
        return linkedin_url_in_guidance(guidance)
    if not raw:
        return "", ""
    url = normalize_linkedin_url(raw if raw.lower().startswith("http") else f"https://{raw}")
    pub = extract_public_identifier(url).lower()
    return (url, pub) if pub else ("", "")


def _mirror_into_engine_home(src_dir: Path, dst_dir: Path) -> None:
    """Copy the guided research result into the engine's per-handle research
    home, sidelining anything already there (.bkup — paid artifacts are never
    deleted). A later enrichment pass then reuses the guided result for free
    instead of re-billing the same person."""
    if not any((src_dir / name).exists() for name in _RESEARCH_FILES):
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    for name in _RESEARCH_FILES:
        stale = dst_dir / name
        if stale.exists():
            stale.replace(stale.with_suffix(".json.bkup"))
        fresh = src_dir / name
        if fresh.exists():
            shutil.copy2(fresh, dst_dir / name)


@dataclass(frozen=True)
class GuidedRetarget:
    """One submit, parsed at the endpoint boundary from the in-memory parent."""

    slug: str
    pub: str  # the CURRENT (suspect) identity — the review.csv row key
    name: str
    guidance: str
    person_ids: tuple[str, ...] = ()
    linkedin_url: str = ""
    # EVERY real candidate row key on the parent, from the endpoint that can
    # see them all. Settlement iterates these — no deriving row keys from URLs.
    candidate_pubs: tuple[str, ...] = ()
    # Folded synthetic options on the parent (synth- pubs). An applied identity
    # settles these through their approve gate in synthetic-people.csv, or a
    # mixed parent bounces back into the queue as a synthetic-option card.
    synthetic_pubs: tuple[str, ...] = ()
    # The parent slug AS THE REVIEW QUEUE KEYS IT (parent["slug"]). `slug` is
    # the dossier slug (research handle, directory pane) — for a synthetic
    # parent the two differ, and inflight exclusion / failure notes must use
    # this one or the wrong parent gets excluded/annotated.
    queue_slug: str = ""
    # Endpoint click time (now_iso). Rows touched AFTER this are human
    # decisions made while the job ran — they always stand.
    submitted_at: str = ""
    match_emails: tuple[str, ...] = ()
    match_phones: tuple[str, ...] = ()


def failed_notes_from_items(items: list[dict[str, Any]]) -> dict[str, str]:
    """slug -> failure detail for people whose NEWEST guided re-research
    FAILED. ``items`` is a queue snapshot, NEWEST FIRST — the first item seen
    per slug is the latest, so an old failure never shadows a later success
    (and a later failure is never shadowed by an old success)."""
    latest: dict[str, dict[str, Any]] = {}
    for item in items:
        slug = str(item.get("queue_slug") or item.get("slug") or "").strip().lower()
        if slug and slug not in latest:
            latest[slug] = item
    return {slug: str(item.get("detail") or "the job did not finish")
            for slug, item in latest.items() if item.get("state") == "failed"}


def _synthetic_gate(path: Path, pub: str) -> str:
    """Current approve-gate value for one synthetic row ('' when absent)."""
    if not path.exists():
        return ""
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if (row.get("public_identifier") or "").strip().lower() == pub:
                return (row.get("approved") or "").strip().lower()
    return ""


def run_guided_retarget(request: GuidedRetarget, *,
                        review_path: Path,
                        people_csv: Path,
                        facts_dir: Path = FACTS_DIR,
                        raw_dir: Path = RAW_DIR,
                        out_dir: Path = GUIDED_RETARGET_DIR,
                        engine_dir: Path = DEEP_RESEARCH_DIR,
                        synthetic_path: Path = SYNTHETIC_PEOPLE_CSV,
                        use_llm: bool = True,
                        on_progress: Callable[[str, str], None] | None = None,
                        write: Callable[[Path, dict[str, dict[str, str]]], None] = write_override_rows,
                        ) -> dict[str, Any]:
    """Run one guided re-research end to end; returns the item outcome.

    Outcome dicts: {"state": "applied", "new_url", "confidence", "detail"} |
    {"state": "synthetic", "detail"} | {"state": "no_match", "detail"} |
    {"state": "failed", "detail"}.
    """
    report = on_progress or (lambda state, detail: None)
    key = request.pub.strip().lower()
    if not key:
        return {"state": "failed", "detail": "person has no review key"}

    def settle_siblings(rows: dict[str, dict[str, str]], winner_key: str) -> None:
        """An applied identity answers the WHOLE parent: every other pending
        candidate row settles as detached, or the parent bounces straight back
        into the linear queue showing its leftover wrong links. The guard
        matches /decide's sibling withdrawal: a human yes/no always stands,
        and a machine-applied `auto` row (already non-pending, possibly a
        shared row confirmed for a DIFFERENT parent) is never touched. A
        folded synthetic option settles through its approve gate in
        synthetic-people.csv, exactly like /decide's withdrawal."""
        for row_key in {k for k in request.candidate_pubs if k} - {winner_key}:
            row_now = rows.setdefault(row_key, {"public_identifier": row_key})
            if str(row_now.get("approved") or "").strip().lower() not in {"yes", "no", "auto"}:
                row_now.update({"action": "detach", "approved": "yes",
                                "source": "user-guidance", "new_linkedin_url": "",
                                "new_public_identifier": "", "updated_at": now_iso()})
        for synth_pub in request.synthetic_pubs:
            pub_now = str(synth_pub or "").strip().lower()
            if not pub_now or pub_now == winner_key:
                continue
            if _synthetic_gate(synthetic_path, pub_now) in {"yes", "no"}:
                continue  # the user already gated it — their word stands
            try:
                apply_synthetic_decision(synthetic_path, pub_now, "detach")
            except ValueError:
                pass  # row pruned between render and apply — nothing to settle

    # The user handed us the answer: a URL they ASSERT is the right profile IS
    # the decision — the same trust as the review-stage fix form. No research,
    # no judge, no spend; the retarget applies directly. Intent is read by a
    # quick LLM call (regex-presence only offline / on failure).
    given_url, given_pub = specified_linkedin_url(request.guidance, use_llm=use_llm)
    if given_pub:
        rows = load_override_rows(review_path)
        row_now = rows.setdefault(key, {"public_identifier": request.pub})
        row_now.update({"action": "retarget", "approved": "yes",
                        "new_linkedin_url": given_url,
                        "new_public_identifier": given_pub,
                        "confidence": "1.000",
                        "reason": "LinkedIn URL provided by the user",
                        "source": "user-guidance",
                        "llm_reject": "", "llm_reject_confidence": "",
                        "llm_reject_reason": "", "updated_at": now_iso()})
        settle_siblings(rows, key)
        write(review_path, rows)
        return {"state": "applied", "new_url": given_url, "confidence": "1.000",
                "detail": "user-provided LinkedIn applied directly (no research needed)"}

    subset_row = {
        "parent_slug": request.slug,
        "person_ids": list(request.person_ids),
        "name": request.name,
        # Keying the research handle on the CURRENT pub makes old_pub == the
        # exact review.csv row the directory pane reads back.
        "candidate_key": request.pub,
        "linkedin": {"linkedin_url": request.linkedin_url},
        "verdict": {"reason": "the user flagged the attached LinkedIn as the wrong person"},
        "match_emails": list(request.match_emails),
        "match_phones": list(request.match_phones),
    }
    people = load_people_rows(people_csv) if people_csv.exists() else {}
    queue_rows = build_queue([subset_row], people, facts_dir, raw_dir)
    if not queue_rows:
        return {"state": "failed", "detail": "could not build a research row for this person"}
    row = queue_rows[0]
    # The user's words become the prompt's `User retarget hint` — its strongest
    # clue. The machine context (wrong-link note + owner background) stays in
    # known_info exactly as build_queue wrote it.
    row["retarget_hint"] = request.guidance.strip()

    handle = str(row.get("handle") or request.slug)
    handle_dir = out_dir / handle
    # The guidance is durable (it survives crashes and restarts), and it is the
    # paid result's cache key: an IDENTICAL re-submit reuses the existing
    # research for free (run_research skips already-done handles); only CHANGED
    # guidance sidelines the old result and re-researches.
    guidance_file = handle_dir / "guidance.json"
    prior_guidance = ""
    if guidance_file.exists():
        try:
            prior_guidance = str(json.loads(
                guidance_file.read_text(encoding="utf-8")).get("guidance") or "")
        except (json.JSONDecodeError, OSError):
            prior_guidance = ""
    if request.guidance.strip() != prior_guidance.strip():
        for name in _RESEARCH_FILES:
            stale = handle_dir / name
            if stale.exists():
                stale.replace(stale.with_suffix(".json.bkup"))
    handle_dir.mkdir(parents=True, exist_ok=True)
    guidance_file.write_text(json.dumps(
        {"guidance": request.guidance.strip(), "submitted_at": now_iso()},
        ensure_ascii=False, indent=2), encoding="utf-8")

    out_dir.mkdir(parents=True, exist_ok=True)
    queue_csv = out_dir / "research_queue.csv"
    with queue_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=QUEUE_FIELDS)
        writer.writeheader()
        writer.writerows(queue_rows)

    report("researching", f"Parallel.ai {DEFAULT_PROCESSOR} research running")
    try:
        research = deep_research_contacts.run_research(ResearchRunParams(
            input_csv=queue_csv, output_dir=out_dir, processor=DEFAULT_PROCESSOR))
    except SystemExit as exc:  # the primitive's guard paths (missing key, bad queue)
        return {"state": "failed", "detail": f"research blocked: {exc}"}
    except Exception as exc:
        return {"state": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    status = str(research.get("status") or "failed")
    if status not in RESEARCH_OK_STATUSES:
        detail = str(research.get("error") or f"research ended {status}")
        return {"state": "failed", "detail": detail}

    # Research succeeded — NOW the person must actually re-judge: blank the
    # sticky `approved` and the judge fingerprint on their existing rows.
    # (Deliberately after run_research: a failed job leaves review.csv exactly
    # as it was, so the person returns to the queue in their original state.)
    # The person's row can be keyed by a person/candidate id while each OLD
    # LinkedIn lives on its own pub-keyed row — blank them all, or a wrong
    # link survives the whole re-research untouched. Rules:
    #   * a row touched AFTER the submit is a human decision made while the
    #     job ran — it always stands;
    #   * the request's OWN key row re-opens regardless of its pre-submit
    #     value (resubmitting guidance is exactly the re-open ask);
    #   * a sibling row's human yes/no or machine-applied `auto` never
    #     re-opens — one pub row can be a DIFFERENT parent's confirmed
    #     identity, and blanking it here would let settle_siblings launder
    #     that decision into an automatic detach (same set as settle's guard).
    # `blanked` remembers prior values so a crash below restores them.
    submitted_at = str(request.submitted_at or "")
    blanked: dict[str, tuple[str, str]] = {}
    rows = load_override_rows(review_path)
    for row_key in {key, *request.candidate_pubs} - {""}:
        prior = rows.get(row_key)
        if prior is None:
            continue
        if submitted_at and str(prior.get("updated_at") or "") > submitted_at:
            continue
        approved = str(prior.get("approved") or "").strip().lower()
        if row_key != key and approved in {"yes", "no", "auto"}:
            continue
        blanked[row_key] = (str(prior.get("approved") or ""),
                            str(prior.get("llm_judge_fingerprint") or ""))
        prior["approved"] = ""
        prior["llm_judge_fingerprint"] = ""
    if blanked:
        write(review_path, rows)

    # Everything from here to the settle writes can still raise (owner load,
    # judge LLM, artifact mirror). A failure must not strand the rows we just
    # blanked — restore them (approved AND the paid-verdict fingerprint) and
    # fail the job honestly.
    try:
        report("judging", "identity judge reviewing the proposed profile")
        owner = load_owner()
        reconcile_deep_research.propose_retargets_from_output(
            out_dir, [subset_row], review_path,
            facts_dir=facts_dir, raw_dir=raw_dir, use_llm=use_llm,
            owner_block=owner_background_block(owner) if owner else "",
            confirm_threshold=RESEARCH_CONFIRM_THRESHOLD)
    except (Exception, SystemExit) as exc:
        rows = load_override_rows(review_path)
        restored = False
        for row_key, (prior_approved, prior_fp) in blanked.items():
            prior = rows.get(row_key)
            # Restore ONLY rows the judge never reached: a fresh fingerprint
            # means the paid verdict already wrote — resurrecting the stale
            # approval there would auto-attach a judge-rejected profile.
            if (prior is not None
                    and not str(prior.get("approved") or "").strip()
                    and not str(prior.get("llm_judge_fingerprint") or "").strip()):
                prior["approved"] = prior_approved
                prior["llm_judge_fingerprint"] = prior_fp
                restored = True
        if restored:
            write(review_path, rows)
        return {"state": "failed", "detail": f"{type(exc).__name__}: {exc}"}
    # Mirroring the paid artifacts into the engine's research home is
    # bookkeeping — it must neither fail the job nor trigger the restore
    # after the judge has already written its verdict.
    try:
        _mirror_into_engine_home(handle_dir, engine_dir / handle)
    except OSError:
        pass

    rows = load_override_rows(review_path)
    after = rows.get(key) or {}
    action = str(after.get("action") or "").strip().lower()
    new_url = str(after.get("new_linkedin_url") or "").strip()
    rejected = str(after.get("llm_reject") or "").strip().lower() in _REJECT_TRUTHY
    if action == "retarget" and new_url and not rejected:
        # Judge confirmed at/above the bar — the guidance submit was the
        # human's word, so the retarget stands without a second review
        # pass. A human decision that landed mid-job stays untouched.
        if str(after.get("approved") or "").strip().lower() not in {"yes", "no"}:
            after["approved"] = "yes"
            after["source"] = "user-guidance"
        settle_siblings(rows, key)
        write(review_path, rows)
        return {"state": "applied", "new_url": new_url,
                "confidence": str(after.get("confidence") or ""),
                "detail": str(after.get("reason") or "")}

    # No usable LinkedIn (nothing found, or the judge rejected the proposal).
    reason = (str(after.get("llm_reject_reason") or "").strip()
              if rejected else "research found no LinkedIn for this guidance")
    if rejected and new_url:
        # The user's word outranks the judge's corroboration bar: when the
        # research came back with the very profile the guidance references
        # (Parallel was told the hint is the strongest clue), apply it even
        # though the judge could not corroborate it from the dossier alone.
        proposed_pub = (str(after.get("new_public_identifier") or "").strip().lower()
                        or extract_public_identifier(new_url).lower())
        if proposed_pub and proposed_pub in request.guidance.lower():
            after.update({"approved": "yes", "source": "user-guidance",
                          "llm_reject": "", "llm_reject_confidence": "",
                          "llm_reject_reason": "", "updated_at": now_iso()})
            settle_siblings(rows, key)
            write(review_path, rows)
            return {"state": "applied", "new_url": new_url,
                    "confidence": str(after.get("confidence") or ""),
                    "detail": "research returned the profile your guidance references"}

    # The user already said the old link is the wrong person, so it detaches
    # NOW; when the research found nothing, a synthetic profile assembled from
    # it supersedes the old identity — everything automatic, no second review.
    # EXCEPT when the user decided this row while the research ran (the card
    # stays interactive after queueing): an explicit human yes/no made after
    # submit outranks the job's automatic detach and is left untouched.
    # Detach the person's row AND every old LinkedIn's own pub-keyed row
    # (they can differ); a human decision made while research ran wins per row.
    # A human decision made WHILE the job ran (the UI's Skip writes
    # detach/yes) vetoes the automatic synthetic apply below — read it off
    # updated_at vs the submit time BEFORE the loop stamps its own detaches.
    human_decided_mid_job = bool(submitted_at) and any(
        str((rows.get(k) or {}).get("approved") or "").strip().lower() in {"yes", "no"}
        and str((rows.get(k) or {}).get("updated_at") or "") > submitted_at
        for k in {key, *request.candidate_pubs} - {""})
    for row_key in {key, *request.candidate_pubs} - {""}:
        row_now = rows.setdefault(row_key, {"public_identifier": row_key})
        if str(row_now.get("approved") or "").strip().lower() not in {"yes", "no"}:
            row_now.update({"action": "detach", "approved": "yes",
                            "source": "user-guidance", "new_linkedin_url": "",
                            "new_public_identifier": "", "updated_at": now_iso()})
    write(review_path, rows)

    if rejected and new_url:
        # Research DID find a profile but the judge could not corroborate it —
        # assemble deliberately skips outputs that carry a LinkedIn (the
        # retarget path owns them), so no synthetic is possible here. The wrong
        # link is detached; naming the URL in guidance would apply it directly.
        return {"state": "no_match", "new_url": new_url,
                "detail": (f"judge could not verify {new_url} ({reason}); the old link was "
                           "detached — paste the URL as guidance to apply it directly")}
    report("judging", "assembling a synthetic profile from the research")
    # run() returns the TYPED manifest (pipeline/contract.py Node.run) — read
    # attributes, never dict-get.
    assembly = assemble_synthetic_profile.AssembleSyntheticProfile(
        research_dir=out_dir, queue_csv=queue_csv, prune=False).run()
    stands = (int(getattr(assembly, "built", 0) or 0) > 0
              or int(getattr(assembly, "preserved_user_rows", 0) or 0) > 0)
    if stands:
        if human_decided_mid_job:
            return {"state": "no_match",
                    "detail": ("synthetic profile built but left pending — you decided "
                               "this person while the research ran, and that stands")}
        # Deterministic, linear review: the user explicitly asked for this
        # re-research, so the assembled synthetic APPLIES (gate yes) — the
        # person never returns to the queue for a second confirmation.
        for gate_key in (key, *(str(pid) for pid in request.person_ids or ())):
            if gate_key and sync_synthetic_gate(synthetic_path, gate_key, "yes"):
                break
        return {"state": "synthetic",
                "detail": f"no LinkedIn confirmed — synthetic profile now stands ({reason})"}
    return {"state": "no_match",
            "detail": f"{reason}; research output was not usable for a synthetic profile"}


class RetargetQueue:
    """Serial in-memory queue of guided retargets.

    ``submit()`` never blocks; one daemon worker drains items through the
    injected ``runner(request, report)`` one at a time. ``on_change`` fires on
    every state transition (the server points it at its view nudge)."""

    def __init__(self, runner: Callable[[GuidedRetarget, Callable[[str, str], None]], dict[str, Any]],
                 *, on_change: Callable[[], None] | None = None) -> None:
        self._runner = runner
        self._on_change = on_change or (lambda: None)
        self._lock = threading.Lock()
        self._pending: deque[tuple[GuidedRetarget, dict[str, Any]]] = deque()
        self._items: list[dict[str, Any]] = []
        self._worker: threading.Thread | None = None

    def submit(self, request: GuidedRetarget) -> dict[str, Any]:
        """Enqueue one guided retarget; raises ValueError on a duplicate active one."""
        with self._lock:
            key = request.pub.strip().lower()
            if any(item["pub"] == key and item["state"] in ACTIVE_STATES
                   for item in self._items):
                raise ValueError(f"{request.name or request.pub} is already being retargeted")
            item = {"slug": request.slug, "pub": key,
                    "queue_slug": request.queue_slug or request.slug,
                    "name": request.name or request.slug,
                    "guidance": request.guidance,
                    "state": "queued", "detail": "",
                    "submitted_at": now_iso(), "updated_at": now_iso()}
            self._items.append(item)
            self._pending.append((request, item))
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(
                    target=self._drain, name="guided-retargets", daemon=True)
                self._worker.start()
        self._notify()
        return dict(item)

    def snapshot(self) -> list[dict[str, Any]]:
        """All items newest-first — the /api/retargets payload."""
        with self._lock:
            return [dict(item) for item in reversed(self._items)]

    def has_active(self) -> bool:
        with self._lock:
            return any(item["state"] in ACTIVE_STATES for item in self._items)

    def _set(self, item: dict[str, Any], state: str, detail: str = "") -> None:
        with self._lock:
            item["state"] = state
            item["detail"] = detail
            item["updated_at"] = now_iso()
        self._notify()

    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:
            pass  # a view nudge must never break the queue

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._worker = None
                    return
                request, item = self._pending.popleft()

            def report(state: str, detail: str, _item: dict[str, Any] = item) -> None:
                self._set(_item, state, detail)

            self._set(item, "researching", "starting research")
            try:
                result = self._runner(request, report)
            # BaseException on purpose: primitives raise SystemExit on guard
            # paths, which `except Exception` misses (same rationale as the
            # server's pipeline-job runner).
            except BaseException as exc:
                self._set(item, "failed", f"{type(exc).__name__}: {exc}")
                continue
            state = str(result.get("state") or "failed")
            with self._lock:
                item.update({k: str(v) for k, v in result.items()
                             if k not in {"state", "detail"}})
            self._set(item, state, str(result.get("detail") or ""))

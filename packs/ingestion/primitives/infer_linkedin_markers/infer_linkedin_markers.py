"""Mine per-person email context for LinkedIn-resolution markers (LLM step).

Takes the local-only context produced by ``build_email_context`` (recent email
subjects + snippets per contact) and asks an LLM to infer the kind of signals a
human uses to pinpoint someone's LinkedIn profile when there's no photo: what
school they went to, what city they're from, clubs/affiliations, current and
past employers, the relationship to me, and associated people -- each with the
evidence it came from and a confidence.

This is the spend step. It reads the SAME local context (subjects/snippets) and
sends it to the configured OpenAI model. It measures token usage with tiktoken
(input estimate) and the API's own usage block (ground truth) so we can size
cost before scaling.

Outputs (one fixed directory, overwrite in place):
  <out-dir>/markers.jsonl    one record per person (markers + per-call usage)
  <out-dir>/manifest.json    sample composition + token/cost totals
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import tiktoken
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

# Repo root on path so `packs.*` imports resolve when run as a script (incl. worktrees).
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from packs.indexing.lib.llm_config import (
    CHAT_MODEL_PRICES_PER_1K_USD,
    DEFAULT_OPENAI_CONCURRENCY,
    api_call_kwargs,
    openai_price_multiplier,
)
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int

DEFAULT_CONTEXT = Path(".powerpacks/network-import/discover/email-context/email_context.jsonl")
DEFAULT_OUT_DIR = Path(".powerpacks/network-import/discover/email-context/markers")
DEFAULT_MODEL = "gpt-5.2"

# Closed set of marker categories. ONLY signals that help resolve a LinkedIn
# profile -- professional, educational, location, and network. No hobbies.
MARKER_CATEGORIES = [
    "current_employer",        # company/org they currently work at
    "past_employer",           # a prior company/org
    "job_title",               # role / title / occupation / seniority
    "industry",                # sector or field of work
    "school",                  # university / college / high school attended or affiliated
    "field_of_study",          # major / degree / discipline
    "location",                # city / region / country (current or hometown -- note which in value)
    "professional_affiliation",# industry org, conference, hackathon, open-source/community (NOT hobbies)
    "online_identifier",       # personal website, GitHub/X/other handle, phone/WhatsApp number, or alt email/domain
    "canonical_name",          # corrected/fuller real name when context reveals it differs from display name
]
RELATIONSHIPS = [
    "colleague", "family", "friend", "classmate",
    "vendor_service", "recruiter", "acquaintance", "unknown",
]

SYSTEM_PROMPT = (
    "You are helping resolve a contact to their correct LinkedIn profile. You are "
    "given a person's name, email, a domain-based company guess, and a handful of "
    "recent email subjects + short snippets from MY mailbox (I am the account owner).\n\n"
    "Extract ONLY signals that materially help identify this person's LinkedIn "
    "profile, and classify each into EXACTLY ONE of these categories:\n"
    "- current_employer: company/org they currently work at\n"
    "- past_employer: a prior company/org\n"
    "- job_title: role, title, occupation, or seniority\n"
    "- industry: sector or field of work\n"
    "- school: university, college, or high school attended/affiliated\n"
    "- field_of_study: major, degree, or discipline\n"
    "- location: city / region / country (say whether current or hometown in the value)\n"
    "- professional_affiliation: industry org, professional conference, hackathon, or "
    "open-source/technical community\n"
    "- online_identifier: personal website, GitHub/X/other handle, phone or WhatsApp/"
    "FaceTime number, or an alternate professional domain/email\n"
    "- canonical_name: a corrected or fuller real name when the context reveals the "
    "display name is partial/wrong (e.g. 'Amir' -> 'Amirteymour Moazami')\n\n"
    "Each email is labeled with who sent it: 'from the contact' (their own words) or "
    "'from me' (my words to them). EVERY marker must describe THE CONTACT themselves -- "
    "never another person mentioned in an email. Facts about a third party who appears "
    "in a thread (a realtor, a colleague who is cc'd, someone quoted) are NOT markers "
    "for this contact. Prefer evidence from emails the contact sent.\n\n"
    "Rules:\n"
    "- DO NOT emit markers for personal hobbies, food, music/entertainment, travel, "
    "family logistics, pets, appointments, shopping, or other content that does not "
    "help pin down a LinkedIn profile. If a contact only has such content, return an "
    "empty markers list.\n"
    "- Infer ONLY from the provided context. Do NOT invent facts. Every marker must "
    "cite the evidence (the subject/snippet phrase it came from) and a confidence in [0,1].\n"
    "- Set is_person=false for automated/newsletter/transactional senders.\n"
    "- linkedin_query: the single best search to type into LinkedIn to find this exact "
    "person (name plus the strongest professional/education/location disambiguators), "
    "or empty if not resolvable.\n"
    "- Be conservative: a wrong high-confidence marker is worse than an omitted one."
)

MARKERS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "is_person": {"type": "boolean", "description": "True if a real resolvable individual (not automated/newsletter)."},
        "relationship": {"type": "string", "enum": RELATIONSHIPS, "description": "My likely relationship to them."},
        "canonical_name": {"type": "string", "description": "Fuller/corrected real name if context reveals it; else empty."},
        "markers": {
            "type": "array",
            "description": "Each LinkedIn-resolution signal, classified into a fixed category, with evidence.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "category": {"type": "string", "enum": MARKER_CATEGORIES},
                    "value": {"type": "string", "description": "The concrete value, e.g. 'Roblox', 'USC', 'Los Altos, CA (current)'."},
                    "evidence": {"type": "string", "description": "The subject/snippet phrase this was inferred from."},
                    "confidence": {"type": "number"},
                },
                "required": ["category", "value", "evidence", "confidence"],
            },
        },
        "linkedin_query": {"type": "string"},
        "overall_confidence": {"type": "number", "description": "Confidence we could pinpoint the LinkedIn profile from these markers, [0,1]."},
    },
    "required": ["is_person", "relationship", "canonical_name", "markers", "linkedin_query", "overall_confidence"],
}


def load_dotenv_upward(start: Path) -> None:
    """Load the first .env found walking up from *start* (worktrees share the repo root)."""
    for directory in [start, *start.parents]:
        env_path = directory / ".env"
        if env_path.is_file():
            for raw in env_path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
            return


def get_encoder() -> "tiktoken.Encoding":
    try:
        return tiktoken.get_encoding("o200k_base")
    except Exception:
        return tiktoken.get_encoding("cl100k_base")


def build_user_prompt(rec: dict[str, Any]) -> str:
    lines = [
        f"Name: {rec.get('full_name') or '(unknown)'}",
        f"Email: {rec.get('email')}",
        f"Domain-based company guess: {rec.get('company_guess') or '(none)'}",
        f"Email type: {rec.get('primary_email_type')}",
        f"Total messages with me: {rec.get('total_messages')}",
        "",
        "Recent emails (newest first):",
    ]
    for i, e in enumerate(rec.get("recent_emails") or [], 1):
        subject = e.get("subject") or "(no subject)"
        snippet = e.get("snippet") or ""
        sender = "from the contact" if e.get("from_role") == "contact" else "from me"
        lines.append(f"{i}. [{sender}, {(e.get('at') or '')[:10]}] {subject}")
        if snippet:
            lines.append(f"   snippet: {snippet}")
    return "\n".join(lines)


def select_sample(records: list[dict[str, Any]], email_type: str, n: int, exclude: set[str]) -> list[dict[str, Any]]:
    pool = [
        r for r in records
        if r.get("primary_email_type") == email_type
        and str(r.get("email", "")).lower() not in exclude
        and (r.get("recent_emails"))
    ]
    pool.sort(key=lambda r: -int(r.get("total_messages") or 0))
    return pool[:n]


def price_for(model: str) -> dict[str, float]:
    return CHAT_MODEL_PRICES_PER_1K_USD.get(model, {"input": 0.00175, "output": 0.01400})


def _base_record(rec: dict[str, Any], tiktoken_input: int) -> dict[str, Any]:
    return {
        "email": rec.get("email"),
        "full_name": rec.get("full_name"),
        "primary_email_type": rec.get("primary_email_type"),
        "company_guess": rec.get("company_guess"),
        "usage": {
            "tiktoken_input": tiktoken_input,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
        },
    }


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (APITimeoutError, APIConnectionError)):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


def owner_prior_block(owner_context: str) -> str:
    """Optional prior about the mailbox owner, used to disambiguate personal contacts."""
    owner_context = (owner_context or "").strip()
    if not owner_context:
        return ""
    return (
        "\n\nContext about the mailbox owner (me): " + owner_context + "\n"
        "Friends, classmates, and family of mine often share my school or hometown. "
        "You MAY use this as a prior to (a) enrich linkedin_query (e.g. add my school or "
        "city when the contact is plausibly a school/hometown connection) and (b) emit a "
        "school or location marker ONLY for clearly personal contacts (relationship = "
        "friend/classmate/family), tagging the value with '(hypothesis: shared with mailbox "
        "owner)' and confidence <= 0.4. Never present an owner-derived guess as established fact."
    )


async def infer_one_async(
    client: AsyncOpenAI,
    model: str,
    rec: dict[str, Any],
    encoder: "tiktoken.Encoding",
    semaphore: asyncio.Semaphore,
    max_retries: int,
    system_prompt: str,
) -> dict[str, Any]:
    """Infer markers for one contact. Holds one concurrency slot for the call;
    transient API errors retry with backoff, and any hard failure returns an
    error record (instead of raising) so one bad contact never kills the pool."""
    user_prompt = build_user_prompt(rec)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tiktoken_input = len(encoder.encode(SYSTEM_PROMPT)) + len(encoder.encode(user_prompt))
    kwargs = api_call_kwargs(model)
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.chat.completions.create(
                    model=model,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": "linkedin_markers", "strict": True, "schema": MARKERS_SCHEMA},
                    },
                    **kwargs,
                )
                break
            except Exception as exc:  # noqa: BLE001 - classify then retry or record
                attempt += 1
                if _is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                record = _base_record(rec, tiktoken_input)
                record["markers"] = {"_error": f"{type(exc).__name__}: {exc}"[:300]}
                record["error"] = True
                return record

    content = response.choices[0].message.content or "{}"
    try:
        markers = json.loads(content)
    except json.JSONDecodeError:
        markers = {"_parse_error": content[:500]}
    usage = response.usage
    completion_details = getattr(usage, "completion_tokens_details", None)
    reasoning_tokens = int(getattr(completion_details, "reasoning_tokens", 0) or 0) if completion_details else 0
    record = _base_record(rec, tiktoken_input)
    record["markers"] = markers
    record["usage"].update({
        "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
        "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
        "reasoning_tokens": reasoning_tokens,
        "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
    })
    return record


def select_targets(records: list[dict[str, Any]], args: argparse.Namespace, exclude: set[str]) -> list[dict[str, Any]]:
    """Pick which contacts to mark up.

    - ``--sample-work``/``--sample-personal`` (eval mode): top-N per type by volume.
    - ``--all``: every candidate.
    - default: the top ``--limit`` (500) contacts overall, deterministically ordered
      by message volume then email — same contacts every run, no randomness.
    """
    if args.sample_work or args.sample_personal:
        return select_sample(records, "work", args.sample_work, exclude) + \
            select_sample(records, "personal", args.sample_personal, exclude)
    pool = [
        r for r in records
        if str(r.get("email", "")).lower() not in exclude and r.get("recent_emails")
    ]
    pool.sort(key=lambda r: (-int(r.get("total_messages") or 0), str(r.get("email", "")).lower()))
    if args.all:
        return pool
    return pool[: max(0, args.limit)]


def already_done_emails(markers_path: Path) -> set[str]:
    done: set[str] = set()
    if not markers_path.exists():
        return done
    for line in markers_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            done.add(str(json.loads(line).get("email")))
        except json.JSONDecodeError:
            continue
    return done


def build_manifest(markers_path: Path, args: argparse.Namespace, *, concurrency: int, new_count: int,
                   resumed: int, started: float, exclude: set[str]) -> dict[str, Any]:
    all_records = [json.loads(l) for l in markers_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    n = len(all_records)
    prompt_tokens = sum(r["usage"]["prompt_tokens"] for r in all_records)
    completion_tokens = sum(r["usage"]["completion_tokens"] for r in all_records)
    reasoning_tokens = sum(r["usage"]["reasoning_tokens"] for r in all_records)
    tiktoken_input = sum(r["usage"]["tiktoken_input"] for r in all_records)
    errors = sum(1 for r in all_records if r.get("error") or "_error" in (r.get("markers") or {}))
    price = price_for(args.model)
    mult = openai_price_multiplier()
    cost = (prompt_tokens / 1000.0) * price["input"] * mult + (completion_tokens / 1000.0) * price["output"] * mult
    manifest = {
        "source": "infer_linkedin_markers",
        "status": "completed",
        "model": args.model,
        "concurrency": concurrency,
        "service_tier_multiplier": mult,
        "people_total": n,
        "new_this_run": new_count,
        "resumed_skipped": resumed,
        "errors": errors,
        "sample_work": sum(1 for r in all_records if r["primary_email_type"] == "work"),
        "sample_personal": sum(1 for r in all_records if r["primary_email_type"] == "personal"),
        "excluded": sorted(exclude),
        "tokens": {
            "tiktoken_input_estimate": tiktoken_input,
            "api_prompt_tokens": prompt_tokens,
            "api_completion_tokens": completion_tokens,
            "api_reasoning_tokens": reasoning_tokens,
            "avg_prompt_tokens": round(prompt_tokens / n, 1) if n else 0,
            "avg_completion_tokens": round(completion_tokens / n, 1) if n else 0,
        },
        "estimated_cost_usd": round(cost, 4),
        "estimated_cost_per_person_usd": round(cost / n, 5) if n else 0,
        "output": str(markers_path),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
    }
    return manifest


def maybe_open(path: Path, do_open: bool) -> None:
    """Open the CSV in the OS default app when --open is set. macOS only and
    best-effort, so headless/CI/remote runs (the default) are never affected."""
    if not do_open or sys.platform != "darwin" or not path.exists():
        return
    try:
        subprocess.run(["open", str(path)], check=False)
    except Exception as exc:  # never let opening a file fail the run
        print(f"[infer_linkedin_markers] could not open {path}: {exc}", file=sys.stderr)


def write_markers_csv(markers_path: Path, out_dir: Path) -> Path:
    """Flat one-row-per-person CSV: identity fields + one column per marker category."""
    csv_path = out_dir / "markers.csv"
    header = [
        "email", "full_name", "type", "company_guess", "is_person", "relationship",
        "overall_confidence", "canonical_name", "linkedin_query",
    ] + MARKER_CATEGORIES + ["error"]
    records = [json.loads(l) for l in markers_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header)
        writer.writeheader()
        for rec in records:
            m = rec.get("markers") or {}
            by_cat: dict[str, list[str]] = {c: [] for c in MARKER_CATEGORIES}
            for mk in (m.get("markers") or []):
                cat = mk.get("category")
                if cat in by_cat:
                    by_cat[cat].append(f"{mk.get('value', '')} ({mk.get('confidence')})")
            row = {
                "email": rec.get("email", ""),
                "full_name": rec.get("full_name", ""),
                "type": rec.get("primary_email_type", ""),
                "company_guess": rec.get("company_guess", ""),
                "is_person": m.get("is_person", ""),
                "relationship": m.get("relationship", ""),
                "overall_confidence": m.get("overall_confidence", ""),
                "canonical_name": m.get("canonical_name", ""),
                "linkedin_query": m.get("linkedin_query", ""),
                "error": rec.get("error", "") or m.get("_error", "") or m.get("_parse_error", ""),
            }
            for cat in MARKER_CATEGORIES:
                row[cat] = "; ".join(by_cat[cat])
            writer.writerow(row)
    return csv_path


async def run_async(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    load_dotenv_upward(Path(__file__).resolve().parent)
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("OPENAI_API_KEY not found in environment or .env")

    records = [json.loads(l) for l in Path(args.context).read_text(encoding="utf-8").splitlines() if l.strip()]
    exclude = {e.strip().lower() for e in (args.exclude or []) if e.strip()}
    targets = select_targets(records, args, exclude)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    markers_path = out_dir / "markers.jsonl"
    if args.force and markers_path.exists():
        markers_path.unlink()

    done = already_done_emails(markers_path)
    # select_targets already applied the default --limit / --all / sample selection.
    todo = [r for r in targets if str(r.get("email")) not in done]

    concurrency = args.concurrency or env_or_profile_int(
        "POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=DEFAULT_OPENAI_CONCURRENCY
    )
    encoder = get_encoder()
    system_prompt = SYSTEM_PROMPT + owner_prior_block(args.owner_context)
    client = AsyncOpenAI(api_key=api_key, timeout=args.timeout, max_retries=0)
    semaphore = asyncio.Semaphore(max(1, concurrency))

    fh = markers_path.open("a", encoding="utf-8")
    counter = {"n": 0}

    def on_result(res: dict[str, Any]) -> None:
        # Called as each slot completes; single-threaded in the event loop, so
        # incremental appends are safe and give us crash/interrupt resumability.
        fh.write(json.dumps(res, ensure_ascii=False) + "\n")
        fh.flush()
        counter["n"] += 1

    try:
        coros = [infer_one_async(client, args.model, rec, encoder, semaphore, args.max_retries, system_prompt) for rec in todo]
        await drain_pool(coros, on_result)
    finally:
        fh.close()
        await client.close()

    csv_path = write_markers_csv(markers_path, out_dir)
    manifest = build_manifest(
        markers_path, args, concurrency=concurrency, new_count=counter["n"],
        resumed=len(targets) - len(todo), started=started, exclude=exclude,
    )
    manifest["output_csv"] = str(csv_path)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    maybe_open(csv_path, args.open)
    return manifest


def run(args: argparse.Namespace) -> dict[str, Any]:
    return asyncio.run(run_async(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Infer LinkedIn-resolution markers from email context (LLM).")
    parser.add_argument("--context", default=str(DEFAULT_CONTEXT), help="email_context.jsonl path")
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--sample-work", type=int, default=0, help="Eval mode: top-N work contacts by volume (0 = off)")
    parser.add_argument("--sample-personal", type=int, default=0, help="Eval mode: top-N personal contacts by volume (0 = off)")
    parser.add_argument("--all", action="store_true", help="Process every contact (overrides --limit)")
    parser.add_argument("--limit", type=int, default=500, help="Default mode: top-N contacts by message volume (deterministic)")
    parser.add_argument("--exclude", action="append", default=[], help="Email to exclude (repeatable)")
    parser.add_argument("--concurrency", type=int, default=0, help="In-flight slots (0 = tier profile, default 256)")
    parser.add_argument("--max-retries", type=int, default=4, help="Retries per call on transient API errors")
    parser.add_argument("--owner-context", default="", help="Prior about the mailbox owner (e.g. 'Went to UCLA; from Palo Alto, CA') to disambiguate personal contacts")
    parser.add_argument("--open", action="store_true", help="Open markers.csv when done (macOS, interactive; off by default for headless runs)")
    parser.add_argument("--force", action="store_true", help="Ignore existing markers.jsonl and re-run from scratch")
    parser.add_argument("--timeout", type=int, default=120)
    return parser


def main(argv: list[str] | None = None) -> int:
    manifest = run(build_parser().parse_args(argv))
    print(json.dumps(manifest, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

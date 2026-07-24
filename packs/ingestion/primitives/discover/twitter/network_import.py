#!/usr/bin/env python3
"""Manifest-only local Twitter/X network import orchestrator.

Runs the Twitter/X discovery pipeline into Powerpacks-local artifacts under
`.powerpacks/network-import/discover/twitter/<handle>/` (no Postgres writes; no
local CSV input — Twitter source data comes from RapidAPI):
- `load_or_crawl`: RapidAPI Twitter follower crawl -> `followers_dump.csv`
- `score_candidates`: local heuristic score -> `candidates.csv`
- `moe_evaluate`: OpenAI mixture-of-experts triage -> `moe_evaluated.csv`
- `pre_resolve_linkedin`: free parallel LinkedIn URL extraction from
  bio/website/link aggregators -> `linkedin_resolved.csv` plus
  `linkedin_resolution_queue.csv` for candidates needing a later lookup
- `validate_linkedin`: parallel RapidAPI LinkedIn validation ->
  `linkedin_validated.csv`
- `format_people`: canonical `people.csv` plus temporary
  `people_harmonic_all.csv` compatibility alias

Raw provider payloads are kept in `raw_twitter_responses/` and
`raw_linkedin_responses/` for audit/debug.

State model (manifest-only, no ledger):
  One idempotent `run` per handle writes to the single fixed output dir above and
  overwrites in place, with exactly one `manifest.json` recording per-step status,
  counts, timing, and config/input signature. Resume comes from the ARTIFACTS and
  their manifest signatures, not a step ledger: a step is skipped only when every
  required output exists and its relevant config and input/output contents still
  match. Downstream steps are independently reused when their exact signatures
  remain valid.

Spend gates: `load_or_crawl` (RapidAPI Twitter), `moe_evaluate` (OpenAI), and
`validate_linkedin` (RapidAPI LinkedIn) are spend-bearing. They run only with
`--approve-spend`; without it `run` stops before the first spend step that would
call an API and emits a `needs_approval` payload naming the step + estimated
calls (exit 20). Once an output is on disk that step is cached, so a later rerun
without `--approve-spend` advances past it. The hidden `--limit` row cap exists
only for tiny local smoke tests.

Usage:
    network_import.py run --handle myhandle --max-pages 5            # stops at spend gate
    network_import.py run --handle myhandle --max-pages 5 --approve-spend
    network_import.py status --handle myhandle
    network_import.py check-keys   # key presence only; never prints values

Env: `RAPIDAPI_TWITTER_KEY` (falls back to `RAPIDAPI_KEY`) for Twitter/X,
`RAPIDAPI_LINKEDIN_KEY` (falls back to `RAPIDAPI_KEY`) for LinkedIn validation,
`OPENAI_API_KEY` for MOE evaluation.

Changelog:
  2026-07-23 (audit class-sharing): the spend-gate contract moved to
    common/gates.py — the inline exit-code map in cmd_run is now
    exit_code_for_status, and the needs_approval payload is built by the shared
    needs_approval_payload (step/provider/estimated_calls shape, unchanged bytes).
    StagePayload + write_stage_manifest now import from common/manifests.py
    (source_slug still comes from discover/common).
  2026-07-23 (audit): replaced the resumable ledger step-machine
    (run/approve/continue/status + network_import.ledger.json) with a manifest-only
    single-`run` orchestrator (TwitterDiscovery). Resume is now by artifact
    freshness (output newer than inputs), spend consent is the single
    `--approve-spend` flag (the `approve`/`continue` subcommands and the ledger are
    gone), and the stage records a typed `manifest.json`. Step logic is unchanged;
    the local `source_slug` duplicate now comes from discover/common; dead
    `subprocess` / `normalize_people_row` imports dropped.
  2026-07-23 (audit): dropped the local byte-identical read_csv/write_csv for
    the shared CsvIO.read_dict_rows / CsvIO.write_dict_rows; `import csv`
    dropped with them.
  2026-07-23 (audit): network_import.README.md sidecar folded into this
    docstring.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
import shlex
import shutil
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

# Repo-root bootstrap so `packs.*` imports work in module AND script mode
# (script-mode never imports the package __init__, so this must be in-file).
_REPO_ROOT = Path(__file__).resolve().parents[5]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.gates import (  # noqa: E402
    exit_code_for_status,
    needs_approval_payload,
)
from packs.ingestion.primitives.common.jsonio import emit, now_iso, read_json, sha256_file, write_json  # noqa: E402
from packs.ingestion.primitives.common.manifests import StagePayload, write_stage_manifest  # noqa: E402
from packs.ingestion.primitives.common.paths import DEFAULT_BASE_DIR  # noqa: E402
from packs.ingestion.primitives.discover.common import source_slug  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS,
    generate_person_id,
)
from packs.shared.csv_io import CsvIO  # noqa: E402

# Fixed per-stage output root (base/discover/twitter). The handle dir hangs off
# this; a run overwrites in place, so reruns are idempotent by path. Module-level
# so tests can patch it to a scratch dir.
TWITTER_DISCOVER_DIR = DEFAULT_BASE_DIR / "discover" / "twitter"
TWITTER_API_BASE = "https://twitter241.p.rapidapi.com"
LINKEDIN_API_BASE = "https://professional-network-data.p.rapidapi.com"

# Fixed output filenames — the durable per-stage contract. Each step's output is
# looked up by these names, which is also how resume-by-artifact freshness works.
FOLLOWERS_DUMP = "followers_dump.csv"
CANDIDATES = "candidates.csv"
MOE_EVALUATED = "moe_evaluated.csv"
MOE_USAGE = "moe_usage.json"
LINKEDIN_RESOLVED = "linkedin_resolved.csv"
LINKEDIN_QUEUE = "linkedin_resolution_queue.csv"
LINKEDIN_VALIDATED = "linkedin_validated.csv"
PEOPLE = "people.csv"
PEOPLE_LEGACY = "people_harmonic_all.csv"
RAW_TWITTER_DIR = "raw_twitter_responses"
RAW_LINKEDIN_DIR = "raw_linkedin_responses"

# Logical artifact key -> filename, used to build the manifest `artifacts` map (and
# its fingerprints) from whichever fixed outputs exist after a run.
ARTIFACT_FILES = {
    "followers_dump_csv": FOLLOWERS_DUMP,
    "candidates_csv": CANDIDATES,
    "moe_evaluated_csv": MOE_EVALUATED,
    "moe_usage_json": MOE_USAGE,
    "linkedin_resolved_csv": LINKEDIN_RESOLVED,
    "linkedin_resolution_queue_csv": LINKEDIN_QUEUE,
    "linkedin_validated_csv": LINKEDIN_VALIDATED,
    "people_csv": PEOPLE,
    "people_harmonic_all_csv": PEOPLE_LEGACY,
    "raw_twitter_responses_dir": RAW_TWITTER_DIR,
    "raw_linkedin_responses_dir": RAW_LINKEDIN_DIR,
}

FOLLOWERS_COLUMNS = [
    "handle", "display_name", "bio", "follower_count", "following_count",
    "verified", "location", "website_url", "twitter_user_id", "first_seen_at", "source",
]
CANDIDATE_COLUMNS = FOLLOWERS_COLUMNS + [
    "enrichment_score", "heuristic_verdict", "linkedin_url", "linkedin_status",
]
MOE_COLUMNS = CANDIDATE_COLUMNS + [
    "moe_verdict", "moe_composite", "moe_confidence", "moe_top_expert",
    "moe_top_signal", "moe_top_reasoning", "moe_expert_scores", "moe_raw",
]
VALIDATED_COLUMNS = MOE_COLUMNS + [
    "linkedin_validation_status", "linkedin_name", "linkedin_headline", "linkedin_exp_count", "rapidapi_response",
]
LINK_AGGREGATORS = ["linktr.ee", "linktree.com", "bio.link", "beacons.ai", "lnk.bio", "campsite.bio", "solo.to", "tap.bio", "carrd.co", "about.me"]
LINKEDIN_URL_PATTERN = re.compile(r"https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9_.%-]+)")


EXPERTS: dict[str, dict[str, Any]] = {
    "deep_tech": {
        "emoji": "🔬", "weight": 1.0,
        "prompt": """You are the Deep Tech Scout. Find technical founders/researchers with commercialization potential. High signal: PhDs/postdocs at top labs, frontier AI/research engineers, professors with applied research, OSS maintainers, founding engineers/CTOs with deep technical bios. Ignore follower count. Score 0-10 and reason briefly.""",
    },
    "serial_founder": {
        "emoji": "🚀", "weight": 1.2,
        "prompt": """You are the Serial Founder Radar. Identify repeat founders, YC/Thiel/elite accelerator alumni, founders with fundraising/exits/traction, credible stealth builders, and ex-founders likely to build again. Score 0-10 and reason briefly.""",
    },
    "network_insider": {
        "emoji": "💰", "weight": 0.8,
        "prompt": """You are the Network Insider. Identify people with exceptional capital/dealflow access: VC partners, angels, LPs, allocators, family offices, well-connected advisors, AngelList/syndicate/platform people. Score 0-10 and reason briefly.""",
    },
    "operator_to_founder": {
        "emoji": "🏢", "weight": 1.0,
        "prompt": """You are the Operator-to-Founder scout. Identify senior operators/founding engineers/product leaders/head-of roles at elite companies who are likely to leave and start venture-scale companies. Score 0-10 and reason briefly.""",
    },
    "rising_star": {
        "emoji": "🌱", "weight": 0.9,
        "prompt": """You are the Rising Star scout. Find young builders with outsized signals: AI/OSS traction, fast-growing projects, elite schools/labs, unusual ambition, strong early social proof. Score 0-10 and reason briefly.""",
    },
    "notable_figure": {
        "emoji": "🏛️", "weight": 0.7,
        "prompt": """You are the Notable Figure judge. Identify influential non-builders worth knowing: major creators, journalists, policy people, celebrities, community leaders, and category experts with real influence. Score 0-10 and reason briefly.""",
    },
}
EXPERT_WEIGHT_SCALE = 5.0 / sum(float(e["weight"]) for e in EXPERTS.values())
MOE_BATCH_SIZE = 25

# Spend-step display labels and providers, surfaced in the needs_approval payload.
STEP_LABELS = {
    "load_or_crawl": "RapidAPI Twitter follower crawl",
    "moe_evaluate": "OpenAI MOE expert evaluation",
    "validate_linkedin": "RapidAPI LinkedIn profile validation",
}
STEP_PROVIDERS = {
    "load_or_crawl": "rapidapi_twitter",
    "moe_evaluate": "openai",
    "validate_linkedin": "rapidapi_linkedin",
}


class PipelineFailed(Exception):
    """A hard failure (missing key, missing handle, provider error) that aborts the run."""


def generate_linkedin_id(public_identifier: str) -> str:
    """Stable person id derived from a LinkedIn public identifier."""
    return generate_person_id(public_identifier)


def generate_synthetic_id(handle: str) -> str:
    """Deterministic synthetic person id for a Twitter handle with no LinkedIn match."""
    import uuid
    return str(uuid.uuid5(uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890"), f"twitter:{handle.lower().strip()}"))


def http_json(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, dict[str, Any] | None, str]:
    """GET/POST `url`, returning `(status_code, parsed_json_or_None, error_text)`."""
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
            return response.status, json.loads(raw) if raw else None, ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            data = None
        return exc.code, data, raw[:1000]
    except Exception as exc:
        return 0, None, str(exc)


def extract_linkedin_from_text(text: str) -> str:
    """Return the first canonical `linkedin.com/in/<slug>` URL in `text`, else ''."""
    if not text:
        return ""
    match = LINKEDIN_URL_PATTERN.search(text)
    if not match:
        return ""
    slug = match.group(1).rstrip("/")
    return f"https://www.linkedin.com/in/{slug}"


def extract_linkedin_slug(url: str) -> str:
    """Return the lowercased public-identifier slug from a LinkedIn profile URL."""
    return urllib.parse.unquote((LINKEDIN_URL_PATTERN.search(url or "") or ["", ""])[1]).rstrip("/").lower()


def is_link_aggregator(url: str) -> bool:
    """True when `url` points at a known link-aggregator (linktree/bio.link/...)."""
    low = (url or "").lower()
    return any(domain in low for domain in LINK_AGGREGATORS)


def fetch_text(url: str, timeout: int = 10) -> str:
    """Fetch a public page's text (best-effort; '' on any error)."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_twitter_user(data: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten a nested Twitter user payload into a flat contact dict, or None."""
    user_obj: Any = data
    for key in ("result", "data", "user", "result"):
        if isinstance(user_obj, dict) and key in user_obj:
            user_obj = user_obj[key]
        else:
            break
    if user_obj is data and isinstance(data, dict):
        user_obj = data.get("result", data)
    if not isinstance(user_obj, dict):
        return None
    core = user_obj.get("core") if isinstance(user_obj.get("core"), dict) else {}
    legacy = user_obj.get("legacy") if isinstance(user_obj.get("legacy"), dict) else {}
    avatar = user_obj.get("avatar") if isinstance(user_obj.get("avatar"), dict) else {}
    location_obj = user_obj.get("location")
    user_id = str(user_obj.get("rest_id") or legacy.get("id_str") or "")
    username = (core.get("screen_name") or legacy.get("screen_name") or "").lower()
    if not user_id and not username:
        return None
    location = location_obj.get("location", "") if isinstance(location_obj, dict) else (location_obj or "")
    website = ""
    try:
        urls = legacy.get("entities", {}).get("url", {}).get("urls", [])
        if urls:
            website = urls[0].get("expanded_url") or urls[0].get("url") or ""
    except Exception:
        pass
    return {
        "handle": username,
        "display_name": core.get("name") or legacy.get("name") or "",
        "bio": legacy.get("description") or "",
        "follower_count": legacy.get("followers_count") or 0,
        "following_count": legacy.get("friends_count") or 0,
        "verified": bool(user_obj.get("is_blue_verified") or legacy.get("verified")),
        "location": location,
        "website_url": website,
        "twitter_user_id": user_id,
        "profile_image_url": avatar.get("image_url") or legacy.get("profile_image_url_https") or "",
        "created_at": core.get("created_at") or "",
        "raw": user_obj,
    }


def parse_followers_timeline(data: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Parse a followers timeline page into `(users, next_bottom_cursor)`."""
    users: list[dict[str, Any]] = []
    next_cursor = ""
    result = data.get("result", data) if isinstance(data, dict) else {}
    timeline = result.get("timeline", result) if isinstance(result, dict) else {}
    instructions = timeline.get("instructions", []) if isinstance(timeline, dict) else []
    for instruction in instructions:
        for entry in instruction.get("entries", []):
            content = entry.get("content", {})
            if content.get("entryType") == "TimelineTimelineCursor":
                if content.get("cursorType") == "Bottom":
                    next_cursor = content.get("value") or ""
                continue
            user_result = content.get("itemContent", {}).get("user_results", {}).get("result", {})
            if user_result and user_result.get("__typename") != "UserUnavailable":
                parsed = parse_twitter_user({"result": {"data": {"user": {"result": user_result}}}})
                if parsed:
                    parsed["raw"] = user_result
                    users.append(parsed)
    return users, next_cursor


def twitter_get_user(handle: str, api_key: str) -> dict[str, Any]:
    """Look up a Twitter user by handle via RapidAPI (raises PipelineFailed on failure)."""
    status, data, error = http_json(
        "GET", f"{TWITTER_API_BASE}/user",
        headers={"x-rapidapi-host": "twitter241.p.rapidapi.com", "x-rapidapi-key": api_key},
        params={"username": handle.lstrip("@")}, timeout=60,
    )
    if status != 200 or not data:
        raise PipelineFailed(f"Twitter user lookup failed for @{handle}: HTTP {status} {error}")
    parsed = parse_twitter_user(data)
    if not parsed:
        raise PipelineFailed(f"Twitter user lookup returned no parseable user for @{handle}")
    parsed["raw_response"] = data
    return parsed


def twitter_followers_page(user_id: str, api_key: str, cursor: str = "") -> tuple[list[dict[str, Any]], str, dict[str, Any] | None, int, str]:
    """Fetch one followers page: `(users, next_cursor, raw, status_code, error)`."""
    params = {"user": str(user_id), "count": "100"}
    if cursor:
        params["cursor"] = cursor
    status, data, error = http_json(
        "GET", f"{TWITTER_API_BASE}/followers",
        headers={"x-rapidapi-host": "twitter241.p.rapidapi.com", "x-rapidapi-key": api_key},
        params=params, timeout=90,
    )
    if status != 200 or not data:
        return [], "", data, status, error
    users, next_cursor = parse_followers_timeline(data)
    return users, next_cursor, data, status, ""


def score_row(row: dict[str, Any]) -> int:
    """Heuristic 0-100 enrichment score from a follower's bio/website/verified/reach."""
    bio = (row.get("bio") or "").lower()
    website = (row.get("website_url") or "").lower()
    score = 0
    checks = [
        (("founder" in bio or "co-founder" in bio or "cofounder" in bio), 20),
        (("ceo" in bio or "chief executive" in bio), 15),
        (any(x in bio for x in ["investor", "angel", "vc ", "venture capital", "partner at", "gp at", "general partner"]), 25),
        (any(x in bio for x in ["cto", "vp engineer", "head of", "director"]), 10),
        (any(x in bio for x in ["@yc", "ycombinator", "y combinator", "yc "]), 15),
        (any(x in bio for x in ["sequoia", "a16z", "benchmark", "greylock", "lightspeed", "accel", "founders fund"]), 20),
        (any(x in bio for x in ["ai ", "artificial intel", "machine learn", "llm", "gpt", "deep learn"]), 8),
        (any(x in bio for x in ["crypto", "web3", "blockchain", "defi", "onchain"]), 5),
        ("fintech" in bio, 5),
        (any(x in bio for x in ["stanford", "harvard", "hbs", " mit ", "phd", "ph.d"]), 8),
        (("github" in bio or "github.com" in website), 5),
        (("linkedin" in bio or "linkedin.com" in website), 3),
        (("substack" in bio or "substack.com" in website), 3),
        (any(x in bio for x in ["ex-", "former", "previously", "prev ", "prev."]), 3),
        (any(x in bio for x in ["series ", "raised", "backed by", "portfolio"]), 10),
        (any(x in bio for x in ["hiring", "we're building", "join us"]), 3),
        (len(bio) > 50, 3),
        (str(row.get("verified", "")).lower() in {"true", "1", "yes"}, 5),
        (bool(website), 5),
    ]
    for ok, points in checks:
        if ok:
            score += points
    try:
        followers = int(row.get("follower_count") or 0)
    except ValueError:
        followers = 0
    if followers >= 100000:
        score += 25
    elif followers >= 50000:
        score += 20
    elif followers >= 10000:
        score += 15
    elif followers >= 5000:
        score += 10
    elif followers >= 1000:
        score += 5
    return score


def heuristic_verdict(score: int) -> str:
    """Bucket a heuristic score into `enrich` / `maybe` / `skip`."""
    if score >= 50:
        return "enrich"
    if score >= 20:
        return "maybe"
    return "skip"


def normalize_name(name: str) -> str:
    """Lowercase, strip accents/parentheticals/suffixes to a bare comparison name."""
    name = re.sub(r"\(.*?\)", "", name or "")
    name = re.split(r"\s*[|•/—–]\s*", name)[0]
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    clean = re.sub(r"[^\w\s'-]", "", ascii_only).strip().lower()
    for suffix in [" phd", " md", " jr", " sr", " iii", " ii", " mba", " cfa", " esq"]:
        clean = clean.replace(suffix, "")
    return clean.strip()


def names_match(twitter_name: str, linkedin_first: str, linkedin_last: str) -> bool:
    """Fuzzy check that a Twitter display name and a LinkedIn first/last are the same person."""
    tw = normalize_name(twitter_name)
    first = normalize_name(linkedin_first)
    last = normalize_name(linkedin_last)
    full = f"{first} {last}".strip()
    if tw == full:
        return True
    parts = tw.split()
    if not parts:
        return False
    if first and last:
        first_match = any(part == first or (len(part) >= 3 and len(first) >= 3 and part[:3] == first[:3]) for part in parts)
        last_match = any(part == last or (len(part) >= 4 and len(last) >= 4 and part[:4] == last[:4]) for part in parts)
        if first_match and last_match:
            return True
    return bool(first and len(parts[0]) >= 3 and parts[0][:3] == first[:3] and (len(parts) == 1 or not last))


def parse_linkedin_profile(data: dict[str, Any] | None, public_identifier: str) -> dict[str, Any]:
    """Normalize a RapidAPI LinkedIn profile payload into a flat profile dict."""
    if not isinstance(data, dict):
        return {}
    profile = data.get("data") if isinstance(data.get("data"), dict) else data
    first = profile.get("first_name") or profile.get("firstName") or ""
    last = profile.get("last_name") or profile.get("lastName") or ""
    full = profile.get("full_name") or profile.get("fullName") or ""
    if full and (not first or not last):
        parts = full.split(" ", 1)
        first = first or (parts[0] if parts else "")
        last = last or (parts[1] if len(parts) > 1 else "")
    experiences = profile.get("experiences") or profile.get("experience") or profile.get("positions") or []
    education = profile.get("education") or profile.get("educations") or []
    return {
        "public_identifier": profile.get("public_identifier") or profile.get("username") or public_identifier,
        "first_name": first,
        "last_name": last,
        "full_name": full or f"{first} {last}".strip(),
        "headline": profile.get("headline") or "",
        "summary": profile.get("summary") or profile.get("about") or "",
        "city": profile.get("city") or "",
        "state": profile.get("state") or "",
        "country": profile.get("country") or "",
        "location_raw": profile.get("location_str") or (profile.get("location") if isinstance(profile.get("location"), str) else ""),
        "profile_picture_url": profile.get("profile_pic_url") or profile.get("profilePicture") or "",
        "work_experiences": experiences if isinstance(experiences, list) else [],
        "education": education if isinstance(education, list) else [],
    }


def rapidapi_linkedin_profile(linkedin_url: str, api_key: str) -> tuple[int, dict[str, Any] | None, str]:
    """Fetch a LinkedIn profile by URL via RapidAPI: `(status, data, error)`."""
    status, data, error = http_json(
        "GET", f"{LINKEDIN_API_BASE}/get-profile-data-by-url",
        headers={"x-rapidapi-host": "professional-network-data.p.rapidapi.com", "x-rapidapi-key": api_key},
        params={"url": linkedin_url}, timeout=90,
    )
    return status, data, error


def openai_chat_json(system_prompt: str, user_prompt: str, model: str) -> dict[str, Any]:
    """Call OpenAI chat completions in JSON mode; return the parsed object + `_usage`."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise PipelineFailed("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            raw = response.read().decode("utf-8", errors="replace")
            data = json.loads(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise PipelineFailed(f"OpenAI request failed: HTTP {exc.code} {raw[:500]}") from exc
    except Exception as exc:
        raise PipelineFailed(f"OpenAI request failed: {exc}") from exc
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PipelineFailed(f"OpenAI returned non-JSON content: {content[:500]}") from exc
    parsed["_usage"] = data.get("usage", {})
    return parsed


def candidate_context(idx: int, row: dict[str, Any]) -> dict[str, Any]:
    """Build the compact per-candidate context object handed to the MOE experts."""
    return {
        "idx": idx,
        "handle": row.get("handle", ""),
        "name": row.get("display_name", ""),
        "bio": row.get("bio", ""),
        "followers": int(float(row.get("follower_count") or 0)),
        "following": int(float(row.get("following_count") or 0)),
        "verified": str(row.get("verified", "")).lower() in {"true", "1", "yes"},
        "location": row.get("location", ""),
        "website_url": row.get("website_url", ""),
        "heuristic_score": int(float(row.get("enrichment_score") or 0)),
        "heuristic_verdict": row.get("heuristic_verdict", ""),
        "source": row.get("source", ""),
    }


def evaluate_expert_batch(expert_name: str, batch: list[dict[str, Any]], model: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    """Run one expert over one batch; return `(expert, {idx: signal}, usage)`."""
    expert = EXPERTS[expert_name]
    user_prompt = (
        "Evaluate these Twitter/X enrichment candidates through your lens. "
        "Return ONLY JSON with this shape: {\"signals\":[{\"idx\":0,\"signal_strength\":0-10,\"reasoning\":\"short\"}, ...]}. "
        "Return one signal for every idx.\n\n"
        + json.dumps(batch, ensure_ascii=False, indent=2)
    )
    result = openai_chat_json(str(expert["prompt"]), user_prompt, model)
    by_idx: dict[int, dict[str, Any]] = {}
    for item in result.get("signals", []):
        try:
            idx = int(item.get("idx"))
            signal = max(0, min(10, int(item.get("signal_strength", 0))))
        except Exception:
            continue
        by_idx[idx] = {"signal_strength": signal, "reasoning": str(item.get("reasoning", ""))[:500]}
    return expert_name, by_idx, result.get("_usage", {})


def aggregate_expert_signals(expert_results: dict[str, dict[str, Any]], expert_names: list[str]) -> dict[str, Any]:
    """Weight/combine per-expert signals into a MOE verdict + composite + top expert."""
    weighted_sum = 0.0
    weight_sum = 0.0
    max_signal = 0
    max_expert = ""
    details: list[str] = []
    for name in expert_names:
        expert = EXPERTS[name]
        result = expert_results.get(name, {})
        signal = int(result.get("signal_strength") or 0)
        weight = float(expert["weight"]) * EXPERT_WEIGHT_SCALE
        weighted_sum += signal * weight
        weight_sum += weight
        if signal > max_signal:
            max_signal = signal
            max_expert = name
        details.append(f"{expert['emoji']}{name}={signal}")
    composite = weighted_sum / weight_sum if weight_sum else 0.0
    if max_signal >= 8:
        verdict = "enrich"
        confidence = min(0.95, 0.7 + (max_signal - 8) * 0.1 + composite / 20)
    elif composite >= 5.0:
        verdict = "enrich"
        confidence = min(0.90, 0.6 + (composite - 5) / 10)
    elif composite >= 3.0:
        verdict = "maybe"
        confidence = 0.5 + (composite - 3) / 10
    else:
        verdict = "skip"
        confidence = min(0.90, 0.6 + (3 - composite) / 10)
    return {
        "moe_verdict": verdict,
        "moe_composite": round(composite, 2),
        "moe_confidence": round(confidence, 2),
        "moe_top_expert": max_expert,
        "moe_top_signal": max_signal,
        "moe_top_reasoning": expert_results.get(max_expert, {}).get("reasoning", ""),
        "moe_expert_scores": " | ".join(details),
        "moe_raw": json.dumps(expert_results, ensure_ascii=False),
    }


def split_batches(items: list[Any], batch_size: int) -> list[list[Any]]:
    """Chunk `items` into fixed-size batches."""
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def fetch_aggregator_pair(item: tuple[int, str]) -> tuple[int, str]:
    """Fetch one link-aggregator page and extract a LinkedIn URL: `(idx, url)`."""
    idx, url = item
    return idx, extract_linkedin_from_text(fetch_text(url))


def validate_linkedin_row(item: tuple[int, dict[str, str], str, Path]) -> tuple[int, dict[str, Any]]:
    """Validate one pre-resolved LinkedIn URL against RapidAPI, classifying the match."""
    idx, row, key, raw_dir = item
    result: dict[str, Any] = dict(row)
    status = row.get("linkedin_status", "")
    url = row.get("linkedin_url", "")
    if not (status in {"found", "found_pre_resolved"} and url):
        result.update({"linkedin_validation_status": status or "no_url"})
        return idx, result
    slug = extract_linkedin_slug(url)
    code, data, error = rapidapi_linkedin_profile(url, key)
    write_json(raw_dir / f"{row.get('handle') or slug}.json", {"status_code": code, "error": error, "data": data})
    profile = parse_linkedin_profile(data, slug)
    exp_count = len(profile.get("work_experiences") or [])
    if code != 200 or not data:
        validation = "found_invalid"
    elif not names_match(row.get("display_name", ""), profile.get("first_name", ""), profile.get("last_name", "")):
        validation = "found_mismatch"
    elif exp_count == 0 and not profile.get("headline"):
        validation = "found_abandoned"
    else:
        validation = "found"
    result.update({
        "linkedin_validation_status": validation,
        "linkedin_status": validation,
        "linkedin_name": profile.get("full_name", ""),
        "linkedin_headline": profile.get("headline", ""),
        "linkedin_exp_count": exp_count,
        "rapidapi_response": json.dumps(data) if data else "",
    })
    return idx, result


def load_rapidapi_json(row: dict[str, Any]) -> dict[str, Any] | None:
    """Parse the stored `rapidapi_response` JSON blob on a validated row, or None."""
    raw = row.get("rapidapi_response") or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


@dataclass
class TwitterInput:
    """Frozen-ish run configuration for one handle's discovery pipeline."""

    handle: str = ""
    source: str = "twitter"
    limit: int | None = None
    min_score: int = 20
    verdicts: str = "enrich,maybe"
    max_pages: int = 1
    skip_moe: bool = False
    moe_model: str = "gpt-4o-mini"
    moe_experts: str = "all"
    moe_workers: int = 6
    linkedin_workers: int = 10
    aggregator_workers: int = 10
    skip_aggregator_fetch: bool = False
    sleep_seconds: float = 0.0


@dataclass
class TwitterDiscoveryManifest(StagePayload):
    """Typed stage manifest for a Twitter/X discovery run. One per handle dir; the
    durable state contract (per-step status/counts/timing + artifact fingerprints).
    None-valued optionals are dropped by StagePayload.to_payload()."""

    handle: str = ""
    status: str = ""  # completed | needs_approval | failed
    steps: dict[str, Any] = field(default_factory=dict)
    step_signatures: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, Any] = field(default_factory=dict)
    input: dict[str, Any] = field(default_factory=dict)
    needs_approval: dict[str, Any] | None = None
    error: str | None = None
    updated_at: str = ""
    source: str = "twitter"
    primitive: str = "twitter/network_import"


# --- step functions: each reads its fixed inputs and writes its fixed outputs ---
# Step LOGIC is unchanged from the ledger version; only the state plumbing (ledger
# artifacts dict -> fixed paths under `out_dir`, `ledger["input"]` -> the `cfg`
# dataclass) changed. The orchestrator owns freshness, spend gating, and timing.

def step_load_or_crawl(cfg: TwitterInput, out_dir: Path) -> dict[str, Any]:
    """Crawl the operator's followers via RapidAPI into followers_dump.csv + raw dumps.
    Spend-bearing (RapidAPI Twitter)."""
    out = out_dir / FOLLOWERS_DUMP
    raw_dir = out_dir / RAW_TWITTER_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    key = os.getenv("RAPIDAPI_TWITTER_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()
    if not key:
        raise PipelineFailed("RAPIDAPI_TWITTER_KEY/RAPIDAPI_KEY is not set")
    handle = cfg.handle.lstrip("@")
    if not handle:
        raise PipelineFailed("--handle is required")
    user = twitter_get_user(handle, key)
    write_json(raw_dir / f"user_{handle}.json", user.get("raw_response"))
    cursor = ""
    seen: set[str] = set()
    for page in range(int(cfg.max_pages or 1)):
        users, cursor, raw, status, error = twitter_followers_page(user["twitter_user_id"], key, cursor)
        write_json(raw_dir / f"followers_{handle}_{page}.json", {"status": status, "error": error, "raw": raw})
        for u in users:
            if u["handle"] in seen:
                continue
            seen.add(u["handle"])
            rows.append({
                "handle": u["handle"],
                "display_name": u["display_name"],
                "bio": u["bio"],
                "follower_count": u["follower_count"],
                "following_count": u["following_count"],
                "verified": u["verified"],
                "location": u["location"],
                "website_url": u["website_url"],
                "twitter_user_id": u["twitter_user_id"],
                "first_seen_at": now_iso(),
                "source": cfg.source,
            })
            if cfg.limit and len(rows) >= int(cfg.limit):
                break
        if cfg.limit and len(rows) >= int(cfg.limit):
            rows = rows[: int(cfg.limit)]
            break
        if not cursor or cursor == "0" or not users:
            break
        time.sleep(float(cfg.sleep_seconds or 0.0))
    CsvIO.write_dict_rows(out, FOLLOWERS_COLUMNS, rows)
    return {"rows": len(rows), "output_file": str(out)}


def step_score_candidates(cfg: TwitterInput, out_dir: Path) -> dict[str, Any]:
    """Heuristic-score the follower dump and keep enrich/maybe candidates -> candidates.csv."""
    rows = CsvIO.read_dict_rows(out_dir / FOLLOWERS_DUMP)
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        score = score_row(r)
        if score < int(cfg.min_score or 0) and heuristic_verdict(score) == "skip":
            continue
        row = {col: r.get(col, "") for col in FOLLOWERS_COLUMNS}
        row.update({
            "enrichment_score": score,
            "heuristic_verdict": heuristic_verdict(score),
            "linkedin_url": r.get("linkedin_url", ""),
            "linkedin_status": r.get("linkedin_status", ""),
        })
        out_rows.append(row)
    out_rows.sort(key=lambda x: int(x.get("enrichment_score") or 0), reverse=True)
    out = out_dir / CANDIDATES
    CsvIO.write_dict_rows(out, CANDIDATE_COLUMNS, out_rows)
    return {"rows": len(out_rows), "output_file": str(out)}


def step_moe_evaluate(cfg: TwitterInput, out_dir: Path) -> dict[str, Any]:
    """MOE-triage candidates via OpenAI experts -> moe_evaluated.csv (+ moe_usage.json).
    Spend-bearing unless --skip-moe, which falls back to the heuristic verdict."""
    rows = CsvIO.read_dict_rows(out_dir / CANDIDATES)
    out = out_dir / MOE_EVALUATED
    if cfg.skip_moe:
        (out_dir / MOE_USAGE).unlink(missing_ok=True)
        out_rows = []
        for row in rows:
            r = dict(row)
            r.update({
                "moe_verdict": r.get("heuristic_verdict") or "skip",
                "moe_composite": "",
                "moe_confidence": "",
                "moe_top_expert": "heuristic",
                "moe_top_signal": "",
                "moe_top_reasoning": "MOE skipped; using heuristic verdict.",
                "moe_expert_scores": "",
                "moe_raw": "",
            })
            out_rows.append(r)
        CsvIO.write_dict_rows(out, MOE_COLUMNS, out_rows)
        return {"rows": len(out_rows), "skipped": True, "output_file": str(out)}

    expert_names = list(EXPERTS) if cfg.moe_experts in {"", "all", None} else [x.strip() for x in str(cfg.moe_experts).split(",") if x.strip()]
    unknown = [name for name in expert_names if name not in EXPERTS]
    if unknown:
        raise PipelineFailed(f"Unknown MOE experts: {', '.join(unknown)}")
    contexts = [candidate_context(i, row) for i, row in enumerate(rows)]
    batches = split_batches(contexts, MOE_BATCH_SIZE)
    model = str(cfg.moe_model or "gpt-4o-mini")
    max_workers = max(1, min(int(cfg.moe_workers or 6), len(expert_names) * max(1, len(batches))))
    by_handle: dict[str, dict[str, dict[str, Any]]] = {row.get("handle", ""): {} for row in rows}
    raw_usage: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = []
        for expert in expert_names:
            for batch in batches:
                futures.append(executor.submit(evaluate_expert_batch, expert, batch, model))
        for fut in concurrent.futures.as_completed(futures):
            expert_name, by_idx, usage = fut.result()
            raw_usage.append({"expert": expert_name, "usage": usage})
            for idx, result in by_idx.items():
                if 0 <= idx < len(rows):
                    by_handle.setdefault(rows[idx].get("handle", ""), {})[expert_name] = result
    out_rows: list[dict[str, Any]] = []
    for row in rows:
        expert_results = by_handle.get(row.get("handle", ""), {})
        agg = aggregate_expert_signals(expert_results, expert_names)
        merged = dict(row)
        merged.update(agg)
        out_rows.append(merged)
    CsvIO.write_dict_rows(out, MOE_COLUMNS, out_rows)
    write_json(out_dir / MOE_USAGE, raw_usage)
    counts: dict[str, int] = {}
    for row in out_rows:
        counts[row.get("moe_verdict", "")] = counts.get(row.get("moe_verdict", ""), 0) + 1
    return {"rows": len(out_rows), "verdict_counts": counts, "experts": expert_names, "output_file": str(out)}


def step_pre_resolve_linkedin(cfg: TwitterInput, out_dir: Path) -> dict[str, Any]:
    """Free LinkedIn pre-resolution from bio/website/link-aggregators -> linkedin_resolved.csv
    plus linkedin_resolution_queue.csv for the still-unresolved rows."""
    verdicts = {v.strip() for v in str(cfg.verdicts or "enrich,maybe").split(",") if v.strip()}
    src = out_dir / MOE_EVALUATED if (out_dir / MOE_EVALUATED).exists() else out_dir / CANDIDATES
    rows = CsvIO.read_dict_rows(src)
    output: list[dict[str, Any]] = []
    pre_count = 0
    aggregator_jobs: list[tuple[int, str]] = []
    for row in rows:
        verdict = row.get("moe_verdict") or row.get("heuristic_verdict")
        if verdict not in verdicts:
            continue
        url = row.get("linkedin_url", "") or extract_linkedin_from_text(row.get("website_url", "")) or extract_linkedin_from_text(row.get("bio", ""))
        if url and not row.get("linkedin_status"):
            row["linkedin_url"] = url
            row["linkedin_status"] = "found_pre_resolved"
            pre_count += 1
        elif not url and not cfg.skip_aggregator_fetch and is_link_aggregator(row.get("website_url", "")):
            aggregator_jobs.append((len(output), row["website_url"]))
        output.append(row)
    fetched = 0
    if aggregator_jobs:
        workers = max(1, int(cfg.aggregator_workers or 10))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for idx, url in executor.map(fetch_aggregator_pair, aggregator_jobs):
                if url and idx < len(output) and not output[idx].get("linkedin_url"):
                    output[idx]["linkedin_url"] = url
                    output[idx]["linkedin_status"] = "found_pre_resolved"
                    pre_count += 1
                    fetched += 1
    out = out_dir / LINKEDIN_RESOLVED
    queue = out_dir / LINKEDIN_QUEUE
    unresolved = [r for r in output if not r.get("linkedin_url")]
    CsvIO.write_dict_rows(out, MOE_COLUMNS, output)
    CsvIO.write_dict_rows(queue, MOE_COLUMNS, unresolved)
    return {
        "rows": len(output),
        "pre_resolved": pre_count,
        "aggregator_resolved": fetched,
        "needs_resolution": len(unresolved),
        "output_file": str(out),
        "resolution_queue": str(queue),
    }


def step_validate_linkedin(cfg: TwitterInput, out_dir: Path) -> dict[str, Any]:
    """Validate pre-resolved LinkedIn URLs against RapidAPI -> linkedin_validated.csv
    (+ raw_linkedin_responses/). Spend-bearing when any row carries a URL to check."""
    key = os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()
    if not key:
        raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")
    rows = CsvIO.read_dict_rows(out_dir / LINKEDIN_RESOLVED)
    raw_dir = out_dir / RAW_LINKEDIN_DIR
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_rows: list[dict[str, Any]] = [dict(row) for row in rows]
    jobs = [(i, row, key, raw_dir) for i, row in enumerate(rows)]
    workers = max(1, int(cfg.linkedin_workers or 10))
    if cfg.sleep_seconds:
        workers = 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for idx, result in executor.map(validate_linkedin_row, jobs):
            out_rows[idx] = result
            if cfg.sleep_seconds:
                time.sleep(float(cfg.sleep_seconds or 0.0))
    stats: dict[str, int] = {}
    for row in out_rows:
        status = row.get("linkedin_validation_status") or row.get("linkedin_status") or "no_url"
        stats[status] = stats.get(status, 0) + 1
    out = out_dir / LINKEDIN_VALIDATED
    CsvIO.write_dict_rows(out, VALIDATED_COLUMNS, out_rows)
    return {"rows": len(out_rows), "stats": stats, "workers": workers, "output_file": str(out)}


def step_format_people(cfg: TwitterInput, out_dir: Path) -> dict[str, Any]:
    """Format validated rows into the canonical people.csv (+ people_harmonic_all.csv alias)."""
    src = out_dir / LINKEDIN_VALIDATED if (out_dir / LINKEDIN_VALIDATED).exists() else out_dir / LINKEDIN_RESOLVED
    rows = CsvIO.read_dict_rows(src)
    # source_artifacts mirrors the ledger version: the *_csv artifacts that exist,
    # ordered by their logical key name (people.csv is written after this, so it is
    # correctly excluded).
    csv_artifacts = {key: ARTIFACT_FILES[key] for key in ARTIFACT_FILES if key.endswith("_csv") and not key.startswith("people")}
    source_artifacts = [str(out_dir / name) for key, name in sorted(csv_artifacts.items()) if (out_dir / name).exists()]
    people: list[dict[str, Any]] = []
    for row in rows:
        linkedin_url = row.get("linkedin_url", "")
        public_id = extract_linkedin_slug(linkedin_url) if linkedin_url else ""
        data = load_rapidapi_json(row)
        profile = parse_linkedin_profile(data, public_id) if data else {}
        full_name = profile.get("full_name") or row.get("linkedin_name") or row.get("display_name") or row.get("handle") or ""
        first = profile.get("first_name", "")
        last = profile.get("last_name", "")
        if not first and full_name:
            parts = full_name.split(" ", 1)
            first = parts[0]
            last = parts[1] if len(parts) > 1 else ""
        experiences = profile.get("work_experiences") or []
        education = profile.get("education") or []
        current_title = ""
        current_company = ""
        current_company_urn = ""
        if experiences:
            exp = experiences[0]
            if isinstance(exp, dict):
                current_title = str(exp.get("title") or "")
                current_company = str(exp.get("company_name") or exp.get("company") or "")
                current_company_urn = str(exp.get("company_urn") or "")
        person_id = generate_linkedin_id(public_id) if public_id else generate_synthetic_id(row.get("handle", full_name))
        people.append({
            "id": person_id,
            "public_identifier": public_id,
            "linkedin_url": linkedin_url,
            "first_name": first,
            "last_name": last,
            "full_name": full_name,
            "headline": profile.get("headline") or row.get("linkedin_headline") or row.get("bio", ""),
            "summary": profile.get("summary", ""),
            "city": profile.get("city", ""),
            "state": profile.get("state", ""),
            "country": profile.get("country", ""),
            "location_raw": profile.get("location_raw") or row.get("location", ""),
            "profile_picture_url": profile.get("profile_picture_url", ""),
            "work_experiences": json.dumps(experiences),
            "education": json.dumps(education),
            "current_title": current_title,
            "current_company": current_company,
            "current_company_urn": current_company_urn,
            "entity_urn": "",
            "enrichment_provider": "rapidapi_linkedin" if data else "twitter_synthetic",
            "enriched_at": now_iso() if data else "",
            "harmonic_response": "",
            "harmonic_location": "",
            "twitter_handle": row.get("handle", ""),
            "twitter_response": json.dumps({
                "handle": row.get("handle", ""),
                "source": row.get("source", ""),
                "follower_count": row.get("follower_count", ""),
                "enrichment_score": row.get("enrichment_score", ""),
                "moe_verdict": row.get("moe_verdict", ""),
                "linkedin_status": row.get("linkedin_status", ""),
                "linkedin_validation_status": row.get("linkedin_validation_status", ""),
            }, ensure_ascii=False),
            "rapidapi_response": row.get("rapidapi_response", ""),
            "source_channels": "twitter",
            "source_artifacts": json.dumps(source_artifacts, ensure_ascii=False),
        })
    out = out_dir / PEOPLE
    legacy = out_dir / PEOPLE_LEGACY
    CsvIO.write_dict_rows(out, PEOPLE_COLUMNS, people)
    shutil.copyfile(out, legacy)
    return {"rows": len(people), "output_file": str(out), "legacy_output_file": str(legacy)}


@dataclass(frozen=True)
class StepSpec:
    """One pipeline step and its complete file/config cache contract."""

    name: str
    func: Callable[[TwitterInput, Path], dict[str, Any]]
    outputs: tuple[str, ...]
    inputs: tuple[str, ...]
    config: tuple[str, ...]
    spend: bool


def _moe_outputs(cfg: TwitterInput) -> tuple[str, ...]:
    return (MOE_EVALUATED,) if cfg.skip_moe else (MOE_EVALUATED, MOE_USAGE)


STEPS: list[StepSpec] = [
    StepSpec("load_or_crawl", step_load_or_crawl, (FOLLOWERS_DUMP,), (), ("handle", "source", "max_pages", "limit"), True),
    StepSpec("score_candidates", step_score_candidates, (CANDIDATES,), (FOLLOWERS_DUMP,), ("min_score",), False),
    StepSpec("moe_evaluate", step_moe_evaluate, (MOE_EVALUATED,), (CANDIDATES,), ("skip_moe", "moe_model", "moe_experts"), True),
    StepSpec("pre_resolve_linkedin", step_pre_resolve_linkedin, (LINKEDIN_RESOLVED, LINKEDIN_QUEUE), (MOE_EVALUATED, CANDIDATES), ("verdicts", "skip_aggregator_fetch"), False),
    StepSpec("validate_linkedin", step_validate_linkedin, (LINKEDIN_VALIDATED,), (LINKEDIN_RESOLVED,), (), True),
    StepSpec("format_people", step_format_people, (PEOPLE, PEOPLE_LEGACY), (LINKEDIN_VALIDATED, LINKEDIN_RESOLVED), (), False),
]


def _step_outputs(spec: StepSpec, cfg: TwitterInput) -> tuple[str, ...]:
    """Return every output required for this invocation of a step."""
    return _moe_outputs(cfg) if spec.name == "moe_evaluate" else spec.outputs


def _step_signature(spec: StepSpec, cfg: TwitterInput, out_dir: Path) -> dict[str, Any]:
    """Bind a cached step to its config and exact input/output file contents."""
    def files(names: tuple[str, ...]) -> dict[str, Any]:
        return {
            name: ({"size": path.stat().st_size, "sha256": sha256_file(path)} if path.is_file() else {"exists": False})
            for name in names
            for path in [out_dir / name]
        }

    return {
        "config": {name: getattr(cfg, name) for name in spec.config},
        "inputs": files(spec.inputs),
        "outputs": files(_step_outputs(spec, cfg)),
    }


def _is_fresh(outputs: list[Path], signature: dict[str, Any], previous_signature: dict[str, Any]) -> bool:
    """A step is reusable only when all outputs exist and its stored signature matches."""
    return bool(outputs) and all(path.is_file() for path in outputs) and previous_signature == signature


def _can_adopt_signature(
    spec: StepSpec,
    cfg: TwitterInput,
    out_dir: Path,
    previous: dict[str, Any],
    previous_step: dict[str, Any],
    inline_signature: dict[str, Any] | None,
    current_signature: dict[str, Any],
) -> bool:
    """Safely upgrade a completed legacy step to the current signature contract."""
    if previous.get("status") != "completed" or previous_step.get("status") not in {"completed", "cached"}:
        return False
    if any(not (out_dir / name).is_file() for name in (*spec.inputs, *_step_outputs(spec, cfg))):
        return False
    expected_config = current_signature["config"]
    if inline_signature is not None:
        if inline_signature.get("config") != expected_config:
            return False
        if inline_signature.get("inputs") != current_signature["inputs"]:
            return False
    else:
        previous_input = previous.get("input")
        if not isinstance(previous_input, dict):
            return False
        if any(name not in previous_input or previous_input[name] != value for name, value in expected_config.items()):
            return False
    if spec.name == "load_or_crawl":
        rows = CsvIO.read_dict_rows(out_dir / FOLLOWERS_DUMP)
        if any(row.get("source") != cfg.source for row in rows):
            return False
    return True


def moe_would_call_api(cfg: TwitterInput, out_dir: Path) -> bool:
    """True when the MOE step would actually spend (not --skip-moe and candidates exist)."""
    if cfg.skip_moe:
        return False
    candidates = out_dir / CANDIDATES
    if not candidates.exists():
        return True
    return bool(CsvIO.read_dict_rows(candidates))


def validation_would_call_api(out_dir: Path) -> bool:
    """True when validation would spend: some pre-resolved row carries a URL to check."""
    resolved = out_dir / LINKEDIN_RESOLVED
    if not resolved.exists():
        return True
    for row in CsvIO.read_dict_rows(resolved):
        if row.get("linkedin_url") and row.get("linkedin_status") in {"found", "found_pre_resolved"}:
            return True
    return False


class TwitterDiscovery:
    """Manifest-only Twitter/X discovery orchestrator.

    One idempotent ``run`` per handle into the fixed dir
    ``TWITTER_DISCOVER_DIR/<slug>/``, overwriting in place with a single
    ``manifest.json`` (per-step status/counts/timing + artifact fingerprints).
    Resume comes from the artifacts and their per-step manifest signatures, not a
    ledger: a step is skipped only when all outputs and its config/input signature
    match; each downstream step is reassessed against its exact signature. Spend
    steps (crawl, MOE, LinkedIn validate) require ``--approve-spend``; without it
    ``run`` stops at the first spend step that would call an API and returns a
    ``needs_approval`` payload naming the step + estimated calls."""

    def __init__(self, cfg: TwitterInput, *, approve_spend: bool) -> None:
        self.cfg = cfg
        self.approve_spend = approve_spend
        self.dir = TWITTER_DISCOVER_DIR / source_slug(cfg.handle)
        self.manifest_path = self.dir / "manifest.json"

    def run(self) -> dict[str, Any]:
        """Advance the pipeline, skipping fresh steps, until it completes, blocks on
        spend, or a step fails. Always writes the stage manifest and returns it."""
        self.dir.mkdir(parents=True, exist_ok=True)
        previous = read_json(self.manifest_path, {}) or {}
        previous_steps = previous.get("steps") if isinstance(previous.get("steps"), dict) else {}
        stored_signatures = previous.get("step_signatures") if isinstance(previous.get("step_signatures"), dict) else {}
        self.step_signatures = dict(stored_signatures)
        previous_input = previous.get("input") if isinstance(previous.get("input"), dict) else None
        self.represented_input = dict(previous_input) if previous_input is not None else asdict(self.cfg)
        steps: dict[str, Any] = {}
        for spec in STEPS:
            outputs = [self.dir / name for name in _step_outputs(spec, self.cfg)]
            signature = _step_signature(spec, self.cfg, self.dir)
            previous_step = previous_steps.get(spec.name) if isinstance(previous_steps.get(spec.name), dict) else {}
            previous_signature = self.step_signatures.get(spec.name)
            fresh = isinstance(previous_signature, dict) and _is_fresh(outputs, signature, previous_signature)
            if not fresh and not self.step_signatures.get(spec.name):
                inline_signature = previous_step.get("signature") if isinstance(previous_step.get("signature"), dict) else None
                fresh = _can_adopt_signature(
                    spec, self.cfg, self.dir, previous, previous_step, inline_signature, signature,
                )
            if fresh:
                steps[spec.name] = {
                    "status": "cached",
                    "output_file": str(outputs[0]),
                    "signature": signature,
                }
                self.step_signatures[spec.name] = signature
                self._record_config(spec)
                continue
            if spec.spend and not self.approve_spend and self._would_call_api(spec.name):
                return self._needs_approval(spec, steps)
            started = time.time()
            try:
                result = spec.func(self.cfg, self.dir)
            except Exception as exc:
                steps[spec.name] = {"status": "failed", "error": str(exc)}
                return self._write("failed", steps, error=f"{spec.name}: {exc}")
            steps[spec.name] = {
                "status": "completed",
                "counts": result,
                "seconds": round(time.time() - started, 3),
                "signature": _step_signature(spec, self.cfg, self.dir),
            }
            self.step_signatures[spec.name] = steps[spec.name]["signature"]
            self._record_config(spec)
        return self._write("completed", steps)

    def _record_config(self, spec: StepSpec) -> None:
        """Mark only settings whose owning step completed or was validly cached."""
        for name in spec.config:
            self.represented_input[name] = getattr(self.cfg, name)

    def _would_call_api(self, name: str) -> bool:
        """Whether a spend step would actually hit its provider on this run."""
        if name == "load_or_crawl":
            return True
        if name == "moe_evaluate":
            return moe_would_call_api(self.cfg, self.dir)
        if name == "validate_linkedin":
            return validation_would_call_api(self.dir)
        return False

    def _estimate(self, name: str) -> int:
        """Best-effort provider-call estimate for the needs_approval message."""
        if name == "load_or_crawl":
            return int(self.cfg.max_pages or 1) + 1  # user lookup + follower pages
        if name == "moe_evaluate":
            candidates = self.dir / CANDIDATES
            rows = CsvIO.read_dict_rows(candidates) if candidates.exists() else []
            experts = list(EXPERTS) if self.cfg.moe_experts in {"", "all", None} else [x.strip() for x in str(self.cfg.moe_experts).split(",") if x.strip()]
            batches = -(-len(rows) // MOE_BATCH_SIZE) if rows else 0
            return len(experts) * batches
        if name == "validate_linkedin":
            resolved = self.dir / LINKEDIN_RESOLVED
            rows = CsvIO.read_dict_rows(resolved) if resolved.exists() else []
            return sum(1 for r in rows if r.get("linkedin_url") and r.get("linkedin_status") in {"found", "found_pre_resolved"})
        return 0

    def _needs_approval(self, spec: StepSpec, steps: dict[str, Any]) -> dict[str, Any]:
        """Record the blocked spend step and write a needs_approval manifest."""
        provider = STEP_PROVIDERS[spec.name]
        estimate = self._estimate(spec.name)
        steps[spec.name] = {"status": "needs_approval", "provider": provider, "estimated_calls": estimate}
        return self._write("needs_approval", steps, needs_approval=needs_approval_payload(
            step=spec.name,
            provider=provider,
            estimated_calls=estimate,
            message=f"Approval required before {STEP_LABELS[spec.name]} (~{estimate} {provider} calls). Re-run with --approve-spend.",
            continue_command=self._continue_command(),
        ))

    def _artifacts(self) -> dict[str, Any]:
        """The subset of fixed output artifacts that exist, keyed by logical name."""
        out: dict[str, Any] = {}
        for key, name in ARTIFACT_FILES.items():
            path = self.dir / name
            if path.exists():
                out[key] = str(path)
        return out

    def _continue_command(self) -> str:
        """A shell-safe re-run command preserving the complete requested config."""
        args = [
            "uv", "run", "--project", ".", "python",
            "packs/ingestion/primitives/discover/twitter/network_import.py",
            "run", "--handle", self.cfg.handle, "--approve-spend",
            "--source", self.cfg.source,
            "--max-pages", str(self.cfg.max_pages),
            "--min-score", str(self.cfg.min_score),
            "--verdicts", self.cfg.verdicts,
            "--moe-model", self.cfg.moe_model,
            "--moe-experts", self.cfg.moe_experts,
            "--moe-workers", str(self.cfg.moe_workers),
            "--linkedin-workers", str(self.cfg.linkedin_workers),
            "--aggregator-workers", str(self.cfg.aggregator_workers),
            "--sleep-seconds", str(self.cfg.sleep_seconds),
        ]
        if self.cfg.limit is not None:
            args.extend(["--limit", str(self.cfg.limit)])
        if self.cfg.skip_moe:
            args.append("--skip-moe")
        if self.cfg.skip_aggregator_fetch:
            args.append("--skip-aggregator-fetch")
        return shlex.join(args)

    def _write(self, status: str, steps: dict[str, Any], *, needs_approval: dict[str, Any] | None = None, error: str | None = None) -> dict[str, Any]:
        """Build the typed manifest and write it (fingerprinted, no-op when unchanged)."""
        payload = TwitterDiscoveryManifest(
            handle=self.cfg.handle,
            status=status,
            steps=steps,
            step_signatures=self.step_signatures,
            artifacts=self._artifacts(),
            input=self.represented_input,
            needs_approval=needs_approval,
            error=error,
            updated_at=now_iso(),
        )
        return write_stage_manifest(self.manifest_path, payload)


def build_input(args: argparse.Namespace) -> TwitterInput:
    """Build a TwitterInput from parsed `run` args (handle normalized, source defaulted)."""
    handle = args.handle.lstrip("@").strip().lower()
    if not handle:
        raise PipelineFailed("--handle is required")
    return TwitterInput(
        handle=handle,
        source=args.source or handle,
        limit=args.limit,
        min_score=args.min_score,
        verdicts=args.verdicts,
        max_pages=args.max_pages,
        skip_moe=args.skip_moe,
        moe_model=args.moe_model,
        moe_experts=args.moe_experts,
        moe_workers=args.moe_workers,
        linkedin_workers=args.linkedin_workers,
        aggregator_workers=args.aggregator_workers,
        skip_aggregator_fetch=args.skip_aggregator_fetch,
        sleep_seconds=args.sleep_seconds,
    )


def cmd_run(args: argparse.Namespace) -> int:
    """`run`: one idempotent pipeline pass for a handle; emits the stage manifest."""
    cfg = build_input(args)
    payload = TwitterDiscovery(cfg, approve_spend=args.approve_spend).run()
    emit(payload)
    return exit_code_for_status(str(payload.get("status")))


def cmd_status(args: argparse.Namespace) -> int:
    """`status`: read and emit a handle's manifest.json (artifacts-only, no spend)."""
    out_dir = TWITTER_DISCOVER_DIR / source_slug(args.handle.lstrip("@").strip().lower())
    manifest = read_json(out_dir / "manifest.json")
    if not manifest:
        emit({"status": "missing", "manifest": str(out_dir / "manifest.json")})
        return 0
    emit(manifest)
    return 0


def cmd_check_keys(args: argparse.Namespace) -> int:
    """`check-keys`: report presence of the RapidAPI/OpenAI env vars, never values."""
    emit({
        "status": "ok",
        "has_rapidapi_key": bool(os.getenv("RAPIDAPI_KEY")),
        "has_rapidapi_twitter_key": bool(os.getenv("RAPIDAPI_TWITTER_KEY")),
        "has_rapidapi_linkedin_key": bool(os.getenv("RAPIDAPI_LINKEDIN_KEY")),
        "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
    })
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI: `run` (single idempotent pass), `status`, `check-keys`."""
    parser = argparse.ArgumentParser(
        description=(
            "Powerpacks-local Twitter/X network import via RapidAPI. Spend-bearing steps "
            "(crawl, MOE, LinkedIn validation) require --approve-spend; without it run stops "
            "at the first spend step with a needs_approval payload. Reruns are idempotent by "
            "fixed path and resume from artifacts on disk. An internal row cap exists only for "
            "local smoke tests; do not use caps in real ingestion workflows."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="Run one idempotent discovery pass for a handle (stops at the first spend gate without --approve-spend)")
    run.add_argument("--handle", required=True, help="Operator Twitter/X handle whose followers should be imported")
    run.add_argument("--approve-spend", action="store_true", help="Consent to the spend-bearing steps (RapidAPI crawl, OpenAI MOE, RapidAPI LinkedIn validation)")
    run.add_argument("--source", default="", help="Source label; defaults to --handle")
    run.add_argument("--max-pages", type=int, default=1, help="Maximum RapidAPI follower pages to crawl")
    run.add_argument("--min-score", type=int, default=20, help="Minimum heuristic score for enrichment candidates")
    run.add_argument("--verdicts", default="enrich,maybe", help="MOE verdicts to carry into LinkedIn resolution")
    run.add_argument("--moe-model", default="gpt-4o-mini", help="OpenAI model for MOE expert evaluation")
    run.add_argument("--moe-experts", default="all", help="Comma-separated experts or all")
    run.add_argument("--moe-workers", type=int, default=6, help="Parallel MOE expert request workers")
    run.add_argument("--linkedin-workers", type=int, default=10, help="Parallel RapidAPI LinkedIn validation workers")
    run.add_argument("--aggregator-workers", type=int, default=10, help="Parallel link-aggregator fetch workers")
    run.add_argument("--skip-aggregator-fetch", action="store_true", help="Do not fetch public link-aggregator pages during free pre-resolution")
    run.add_argument("--sleep-seconds", type=float, default=0.0, help="Delay between external API requests; forces serial LinkedIn validation when set")
    run.add_argument("--limit", type=int, default=None, help=argparse.SUPPRESS)
    run.add_argument("--skip-moe", action="store_true", help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", help="Show run status from a handle's manifest.json")
    status.add_argument("--handle", required=True, help="Handle whose manifest.json to read")
    status.set_defaults(func=cmd_status)

    keys = sub.add_parser("check-keys", help="Check whether local RapidAPI/OpenAI env vars are present without printing values")
    keys.set_defaults(func=cmd_check_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry: dispatch a subcommand, translating failures into JSON + exit codes."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PipelineFailed as exc:
        emit({"status": "failed", "error": str(exc)})
        return 1
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Resumable local Twitter/X network import orchestrator.

Ports the Twitter/X discovery pipeline into Powerpacks-local artifacts:
- optional RapidAPI Twitter follower crawl -> followers_dump.csv
- local heuristic score -> candidates.csv
- optional OpenAI MOE expert evaluation (approval-gated)
- free parallel LinkedIn URL pre-resolution from bio/website/link aggregators
- optional parallel RapidAPI LinkedIn validation (approval-gated)
- people_harmonic_all-compatible skeleton output

Stdlib-only. No DB writes. No external API calls before approval.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS, normalize_people_row
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
    from packs.ingestion.schemas.people_schema import PEOPLE_SCHEMA_COLUMNS as PEOPLE_COLUMNS, normalize_people_row

DEFAULT_LEDGER = Path(".powerpacks/network-import/twitter/import-run.json")
DEFAULT_BASE_DIR = Path(".powerpacks/network-import")
TWITTER_API_BASE = "https://twitter241.p.rapidapi.com"
LINKEDIN_API_BASE = "https://professional-network-data.p.rapidapi.com"

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
PIPELINE_STEPS = ["load_or_crawl", "score_candidates", "moe_evaluate", "pre_resolve_linkedin", "validate_linkedin", "format_people"]
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


class PipelineBlocked(Exception):
    def __init__(self, payload: dict[str, Any], code: int = 20) -> None:
        super().__init__(payload.get("message") or "blocked")
        self.payload = payload
        self.code = code


class PipelineFailed(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def sha(value: str, length: int = 12) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def generate_linkedin_id(public_identifier: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"linkedin:{public_identifier.lower().strip()}"))


def generate_synthetic_id(handle: str) -> str:
    import uuid
    return str(uuid.uuid5(uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890"), f"twitter:{handle.lower().strip()}"))


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def http_json(method: str, url: str, *, headers: dict[str, str] | None = None, params: dict[str, str] | None = None, timeout: int = 60) -> tuple[int, dict[str, Any] | None, str]:
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
    if not text:
        return ""
    match = LINKEDIN_URL_PATTERN.search(text)
    if not match:
        return ""
    slug = match.group(1).rstrip("/")
    return f"https://www.linkedin.com/in/{slug}"


def extract_linkedin_slug(url: str) -> str:
    return urllib.parse.unquote((LINKEDIN_URL_PATTERN.search(url or "") or ["", ""])[1]).rstrip("/").lower()


def is_link_aggregator(url: str) -> bool:
    low = (url or "").lower()
    return any(domain in low for domain in LINK_AGGREGATORS)


def fetch_text(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def parse_twitter_user(data: dict[str, Any]) -> dict[str, Any] | None:
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
    if score >= 50:
        return "enrich"
    if score >= 20:
        return "maybe"
    return "skip"


def normalize_name(name: str) -> str:
    name = re.sub(r"\(.*?\)", "", name or "")
    name = re.split(r"\s*[|•/—–]\s*", name)[0]
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_only = "".join(c for c in nfkd if not unicodedata.combining(c))
    clean = re.sub(r"[^\w\s'-]", "", ascii_only).strip().lower()
    for suffix in [" phd", " md", " jr", " sr", " iii", " ii", " mba", " cfa", " esq"]:
        clean = clean.replace(suffix, "")
    return clean.strip()


def names_match(twitter_name: str, linkedin_first: str, linkedin_last: str) -> bool:
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
    status, data, error = http_json(
        "GET", f"{LINKEDIN_API_BASE}/get-profile-data-by-url",
        headers={"x-rapidapi-host": "professional-network-data.p.rapidapi.com", "x-rapidapi-key": api_key},
        params={"url": linkedin_url}, timeout=90,
    )
    return status, data, error



def openai_chat_json(system_prompt: str, user_prompt: str, model: str) -> dict[str, Any]:
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
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def fetch_aggregator_pair(item: tuple[int, str]) -> tuple[int, str]:
    idx, url = item
    return idx, extract_linkedin_from_text(fetch_text(url))


def validate_linkedin_row(item: tuple[int, dict[str, str], str, Path]) -> tuple[int, dict[str, Any]]:
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


@dataclass
class TwitterInput:
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


def load_ledger(path: Path) -> dict[str, Any]:
    ledger = read_json(path, {}) or {}
    ledger.setdefault("primitive", "twitter_network_import")
    ledger.setdefault("version", 1)
    ledger.setdefault("created_at", now_iso())
    ledger.setdefault("updated_at", now_iso())
    ledger.setdefault("steps", {})
    ledger.setdefault("approvals", {})
    ledger.setdefault("artifacts", {})
    return ledger


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    ledger["updated_at"] = now_iso()
    write_json(path, ledger)


def mark_step(ledger: dict[str, Any], step_id: str, status: str, **extra: Any) -> None:
    rec = ledger.setdefault("steps", {}).setdefault(step_id, {"id": step_id})
    if status == "running" and "started_at" not in rec:
        rec["started_at"] = now_iso()
    if status in {"completed", "failed", "blocked_approval", "skipped"}:
        rec["finished_at"] = now_iso()
    rec["status"] = status
    rec.update({k: v for k, v in extra.items() if v is not None})


def next_pending_step(ledger: dict[str, Any]) -> str | None:
    for step_id in PIPELINE_STEPS:
        if ledger.setdefault("steps", {}).get(step_id, {}).get("status") != "completed":
            return step_id
    return None


def approval_id(ledger: dict[str, Any], step_id: str) -> str:
    return f"{ledger.get('run_id', 'run')}:{step_id}"


def is_approved(ledger: dict[str, Any], step_id: str) -> bool:
    return bool(ledger.setdefault("approvals", {}).get(approval_id(ledger, step_id)))


def block_for_approval(ledger_path: Path, ledger: dict[str, Any], step_id: str, message: str) -> None:
    app_id = approval_id(ledger, step_id)
    ledger["blocked"] = {"step_id": step_id, "approval_id": app_id, "approval_type": "external_api_spend"}
    mark_step(ledger, step_id, "blocked_approval", approval_id=app_id, approval_type="external_api_spend")
    save_ledger(ledger_path, ledger)
    raise PipelineBlocked({
        "status": "blocked_approval",
        "step_id": step_id,
        "approval_id": app_id,
        "approval_type": "external_api_spend",
        "message": message,
        "ledger": str(ledger_path),
        "continue_command": f"uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py approve --ledger {ledger_path} && uv run --project . python packs/ingestion/primitives/twitter_network_import/twitter_network_import.py continue --ledger {ledger_path}",
    })


def step_load_or_crawl(ledger: dict[str, Any]) -> dict[str, Any]:
    inp = ledger["input"]
    out = Path(ledger["run_dir"]) / "followers_dump.csv"
    raw_dir = Path(ledger["run_dir"]) / "raw_twitter_responses"
    raw_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    key = os.getenv("RAPIDAPI_TWITTER_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()
    if not key:
        raise PipelineFailed("RAPIDAPI_TWITTER_KEY/RAPIDAPI_KEY is not set")
    handle = inp.get("handle", "").lstrip("@")
    if not handle:
        raise PipelineFailed("--handle is required")
    user = twitter_get_user(handle, key)
    write_json(raw_dir / f"user_{handle}.json", user.get("raw_response"))
    cursor = ""
    seen: set[str] = set()
    for page in range(int(inp.get("max_pages") or 1)):
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
                "source": handle,
            })
            if inp.get("limit") and len(rows) >= int(inp["limit"]):
                break
        if inp.get("limit") and len(rows) >= int(inp["limit"]):
            rows = rows[: int(inp["limit"])]
            break
        if not cursor or cursor == "0" or not users:
            break
        time.sleep(float(inp.get("sleep_seconds") or 0.0))
    write_csv(out, FOLLOWERS_COLUMNS, rows)
    ledger["artifacts"]["followers_dump_csv"] = str(out)
    ledger["artifacts"]["raw_twitter_responses_dir"] = str(raw_dir)
    return {"rows": len(rows), "output_file": str(out)}


def step_score_candidates(ledger: dict[str, Any]) -> dict[str, Any]:
    inp = ledger["input"]
    rows = read_csv(Path(ledger["artifacts"]["followers_dump_csv"]))
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        score = score_row(r)
        if score < int(inp.get("min_score") or 0) and heuristic_verdict(score) == "skip":
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
    out = Path(ledger["run_dir"]) / "candidates.csv"
    write_csv(out, CANDIDATE_COLUMNS, out_rows)
    ledger["artifacts"]["candidates_csv"] = str(out)
    return {"rows": len(out_rows), "output_file": str(out)}


def step_moe_evaluate(ledger: dict[str, Any]) -> dict[str, Any]:
    inp = ledger["input"]
    rows = read_csv(Path(ledger["artifacts"]["candidates_csv"]))
    out = Path(ledger["run_dir"]) / "moe_evaluated.csv"
    if inp.get("skip_moe"):
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
        write_csv(out, MOE_COLUMNS, out_rows)
        ledger["artifacts"]["moe_evaluated_csv"] = str(out)
        return {"rows": len(out_rows), "skipped": True, "output_file": str(out)}

    expert_names = list(EXPERTS) if inp.get("moe_experts") in {"", "all", None} else [x.strip() for x in str(inp.get("moe_experts")).split(",") if x.strip()]
    unknown = [name for name in expert_names if name not in EXPERTS]
    if unknown:
        raise PipelineFailed(f"Unknown MOE experts: {', '.join(unknown)}")
    contexts = [candidate_context(i, row) for i, row in enumerate(rows)]
    batches = split_batches(contexts, MOE_BATCH_SIZE)
    model = str(inp.get("moe_model") or "gpt-4o-mini")
    max_workers = max(1, min(int(inp.get("moe_workers") or 6), len(expert_names) * max(1, len(batches))))
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
    write_csv(out, MOE_COLUMNS, out_rows)
    write_json(Path(ledger["run_dir"]) / "moe_usage.json", raw_usage)
    ledger["artifacts"]["moe_evaluated_csv"] = str(out)
    ledger["artifacts"]["moe_usage_json"] = str(Path(ledger["run_dir"]) / "moe_usage.json")
    counts: dict[str, int] = {}
    for row in out_rows:
        counts[row.get("moe_verdict", "")] = counts.get(row.get("moe_verdict", ""), 0) + 1
    return {"rows": len(out_rows), "verdict_counts": counts, "experts": expert_names, "output_file": str(out)}


def step_pre_resolve_linkedin(ledger: dict[str, Any]) -> dict[str, Any]:
    inp = ledger["input"]
    verdicts = {v.strip() for v in str(inp.get("verdicts") or "enrich,maybe").split(",") if v.strip()}
    src = Path(ledger["artifacts"].get("moe_evaluated_csv") or ledger["artifacts"]["candidates_csv"])
    rows = read_csv(src)
    output: list[dict[str, Any]] = []
    pre_count = 0
    aggregator_jobs: list[tuple[int, str]] = []
    for idx, row in enumerate(rows):
        verdict = row.get("moe_verdict") or row.get("heuristic_verdict")
        if verdict not in verdicts:
            continue
        url = row.get("linkedin_url", "") or extract_linkedin_from_text(row.get("website_url", "")) or extract_linkedin_from_text(row.get("bio", ""))
        if url and not row.get("linkedin_status"):
            row["linkedin_url"] = url
            row["linkedin_status"] = "found_pre_resolved"
            pre_count += 1
        elif not url and not inp.get("skip_aggregator_fetch") and is_link_aggregator(row.get("website_url", "")):
            aggregator_jobs.append((len(output), row["website_url"]))
        output.append(row)
    fetched = 0
    if aggregator_jobs:
        workers = max(1, int(inp.get("aggregator_workers") or 10))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            for idx, url in executor.map(fetch_aggregator_pair, aggregator_jobs):
                if url and idx < len(output) and not output[idx].get("linkedin_url"):
                    output[idx]["linkedin_url"] = url
                    output[idx]["linkedin_status"] = "found_pre_resolved"
                    pre_count += 1
                    fetched += 1
    out = Path(ledger["run_dir"]) / "linkedin_resolved.csv"
    queue = Path(ledger["run_dir"]) / "linkedin_resolution_queue.csv"
    unresolved = [r for r in output if not r.get("linkedin_url")]
    write_csv(out, MOE_COLUMNS, output)
    write_csv(queue, MOE_COLUMNS, unresolved)
    ledger["artifacts"]["linkedin_resolved_csv"] = str(out)
    ledger["artifacts"]["linkedin_resolution_queue_csv"] = str(queue)
    needs_resolution = len(unresolved)
    return {"rows": len(output), "pre_resolved": pre_count, "aggregator_resolved": fetched, "needs_resolution": needs_resolution, "output_file": str(out), "resolution_queue": str(queue)}


def step_validate_linkedin(ledger: dict[str, Any]) -> dict[str, Any]:
    key = os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip() or os.getenv("RAPIDAPI_KEY", "").strip()
    if not key:
        raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")
    rows = read_csv(Path(ledger["artifacts"]["linkedin_resolved_csv"]))
    raw_dir = Path(ledger["run_dir"]) / "raw_linkedin_responses"
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_rows: list[dict[str, Any]] = [dict(row) for row in rows]
    jobs = [(i, row, key, raw_dir) for i, row in enumerate(rows)]
    workers = max(1, int(ledger["input"].get("linkedin_workers") or 10))
    if ledger["input"].get("sleep_seconds"):
        workers = 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        for idx, result in executor.map(validate_linkedin_row, jobs):
            out_rows[idx] = result
            if ledger["input"].get("sleep_seconds"):
                time.sleep(float(ledger["input"].get("sleep_seconds") or 0.0))
    stats: dict[str, int] = {}
    for row in out_rows:
        status = row.get("linkedin_validation_status") or row.get("linkedin_status") or "no_url"
        stats[status] = stats.get(status, 0) + 1
    out = Path(ledger["run_dir"]) / "linkedin_validated.csv"
    write_csv(out, VALIDATED_COLUMNS, out_rows)
    ledger["artifacts"]["linkedin_validated_csv"] = str(out)
    ledger["artifacts"]["raw_linkedin_responses_dir"] = str(raw_dir)
    return {"rows": len(out_rows), "stats": stats, "workers": workers, "output_file": str(out)}

def load_rapidapi_json(row: dict[str, Any]) -> dict[str, Any] | None:
    raw = row.get("rapidapi_response") or ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def step_format_people(ledger: dict[str, Any]) -> dict[str, Any]:
    src = Path(ledger["artifacts"].get("linkedin_validated_csv") or ledger["artifacts"].get("linkedin_resolved_csv"))
    rows = read_csv(src)
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
            "twitter_response": "",
            "rapidapi_response": row.get("rapidapi_response", ""),
        })
    out = Path(ledger["run_dir"]) / "people_harmonic_all.csv"
    write_csv(out, PEOPLE_COLUMNS, people)
    ledger["artifacts"]["people_harmonic_all_csv"] = str(out)
    return {"rows": len(people), "output_file": str(out)}


STEP_FUNCS = {
    "load_or_crawl": step_load_or_crawl,
    "score_candidates": step_score_candidates,
    "moe_evaluate": step_moe_evaluate,
    "pre_resolve_linkedin": step_pre_resolve_linkedin,
    "validate_linkedin": step_validate_linkedin,
    "format_people": step_format_people,
}


def validation_would_call_api(ledger: dict[str, Any]) -> bool:
    artifact = ledger.get("artifacts", {}).get("linkedin_resolved_csv")
    if not artifact or not Path(artifact).exists():
        return True
    for row in read_csv(Path(artifact)):
        if row.get("linkedin_url") and row.get("linkedin_status") in {"found", "found_pre_resolved"}:
            return True
    return False


def moe_would_call_api(ledger: dict[str, Any]) -> bool:
    if ledger.get("input", {}).get("skip_moe"):
        return False
    artifact = ledger.get("artifacts", {}).get("candidates_csv")
    if not artifact or not Path(artifact).exists():
        return True
    return bool(read_csv(Path(artifact)))


def step_requires_approval(ledger: dict[str, Any], step_id: str) -> bool:
    if step_id == "load_or_crawl":
        return True
    if step_id == "moe_evaluate":
        return moe_would_call_api(ledger)
    if step_id == "validate_linkedin":
        return validation_would_call_api(ledger)
    return False


def ensure_api_keys_for_step(step_id: str) -> None:
    if step_id == "load_or_crawl":
        if not (os.getenv("RAPIDAPI_TWITTER_KEY") or os.getenv("RAPIDAPI_KEY")):
            raise PipelineFailed("RAPIDAPI_TWITTER_KEY/RAPIDAPI_KEY is not set")
    if step_id == "moe_evaluate":
        if not os.getenv("OPENAI_API_KEY"):
            raise PipelineFailed("OPENAI_API_KEY is not set")
    if step_id == "validate_linkedin":
        if not (os.getenv("RAPIDAPI_LINKEDIN_KEY") or os.getenv("RAPIDAPI_KEY")):
            raise PipelineFailed("RAPIDAPI_LINKEDIN_KEY/RAPIDAPI_KEY is not set")


def run_pipeline(ledger_path: Path, *, stop_after: str | None = None) -> dict[str, Any]:
    ledger = load_ledger(ledger_path)
    if not ledger.get("run_id"):
        raise PipelineFailed(f"Ledger has no run_id: {ledger_path}")
    while True:
        step_id = next_pending_step(ledger)
        if not step_id:
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
            return {"status": "completed", "ledger": str(ledger_path), "run_dir": ledger.get("run_dir"), "artifacts": ledger.get("artifacts", {})}
        if stop_after and ledger.get("steps", {}).get(stop_after, {}).get("status") == "completed":
            save_ledger(ledger_path, ledger)
            return {"status": "stopped", "ledger": str(ledger_path), "next_step": step_id}
        if step_requires_approval(ledger, step_id) and not is_approved(ledger, step_id):
            ensure_api_keys_for_step(step_id)
            label = "RapidAPI Twitter follower crawl" if step_id == "load_or_crawl" else ("OpenAI MOE expert evaluation" if step_id == "moe_evaluate" else "RapidAPI LinkedIn profile validation")
            block_for_approval(ledger_path, ledger, step_id, f"Approval required before {label}.")
        mark_step(ledger, step_id, "running")
        save_ledger(ledger_path, ledger)
        try:
            result = STEP_FUNCS[step_id](ledger)
            mark_step(ledger, step_id, "completed", result=result)
            ledger.pop("blocked", None)
            save_ledger(ledger_path, ledger)
        except PipelineBlocked:
            raise
        except Exception as exc:
            mark_step(ledger, step_id, "failed", error=str(exc))
            save_ledger(ledger_path, ledger)
            raise


def create_ledger(args: argparse.Namespace) -> dict[str, Any]:
    handle = args.handle.lstrip("@").strip().lower()
    if not handle:
        raise PipelineFailed("--handle is required")
    run_id = args.run_id or f"twitter-{handle}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{sha(handle + now_iso(), 6)}"
    run_dir = DEFAULT_BASE_DIR / "twitter" / run_id
    inp = TwitterInput(
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
    ledger = {
        "primitive": "twitter_network_import",
        "version": 1,
        "run_id": run_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "run_dir": str(run_dir),
        "input": asdict(inp),
        "steps": {},
        "approvals": {},
        "artifacts": {},
    }
    return ledger


def cmd_run(args: argparse.Namespace) -> None:
    ledger_path = Path(args.ledger)
    ledger = create_ledger(args)
    save_ledger(ledger_path, ledger)
    result = run_pipeline(ledger_path, stop_after=args.stop_after)
    emit(result)


def cmd_continue(args: argparse.Namespace) -> None:
    result = run_pipeline(Path(args.ledger), stop_after=args.stop_after)
    emit(result)


def cmd_status(args: argparse.Namespace) -> None:
    ledger = load_ledger(Path(args.ledger))
    if not ledger:
        emit({"status": "missing", "ledger": str(args.ledger)})
        return
    emit({
        "status": "blocked" if ledger.get("blocked") else ("completed" if not next_pending_step(ledger) else "running"),
        "ledger": str(args.ledger),
        "run_id": ledger.get("run_id"),
        "run_dir": ledger.get("run_dir"),
        "next_step": next_pending_step(ledger),
        "blocked": ledger.get("blocked"),
        "steps": ledger.get("steps", {}),
        "artifacts": ledger.get("artifacts", {}),
    })


def cmd_approve(args: argparse.Namespace) -> None:
    ledger_path = Path(args.ledger)
    ledger = load_ledger(ledger_path)
    if not ledger:
        raise PipelineFailed(f"Ledger not found: {ledger_path}")
    step_id = args.step or ledger.get("blocked", {}).get("step_id") or next_pending_step(ledger)
    if not step_id:
        emit({"status": "noop", "message": "No pending step", "ledger": str(ledger_path)})
        return
    app_id = approval_id(ledger, step_id)
    ledger.setdefault("approvals", {})[app_id] = {"approved_at": now_iso(), "step_id": step_id, "approved_by": "local_operator"}
    if ledger.get("blocked", {}).get("step_id") == step_id:
        ledger.pop("blocked", None)
    mark_step(ledger, step_id, "approved", approval_id=app_id)
    save_ledger(ledger_path, ledger)
    emit({"status": "approved", "step_id": step_id, "approval_id": app_id, "ledger": str(ledger_path)})


def cmd_check_keys(args: argparse.Namespace) -> None:
    emit({
        "status": "ok",
        "has_rapidapi_key": bool(os.getenv("RAPIDAPI_KEY")),
        "has_rapidapi_twitter_key": bool(os.getenv("RAPIDAPI_TWITTER_KEY")),
        "has_rapidapi_linkedin_key": bool(os.getenv("RAPIDAPI_LINKEDIN_KEY")),
        "has_openai_key": bool(os.getenv("OPENAI_API_KEY")),
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Powerpacks-local Twitter/X network import via RapidAPI. "
            "External API spend is approval-gated. An internal row cap exists only for local smoke tests; "
            "do not use caps in real ingestion workflows."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Path to import ledger JSON")
    common.add_argument("--stop-after", choices=PIPELINE_STEPS, help=argparse.SUPPRESS)

    run = sub.add_parser("run", parents=[common], help="Create a run and advance until the first approval gate")
    run.add_argument("--handle", required=True, help="Operator Twitter/X handle whose followers should be imported")
    run.add_argument("--source", default="", help="Source label; defaults to --handle")
    run.add_argument("--run-id", default="", help="Optional stable run id")
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

    cont = sub.add_parser("continue", parents=[common], help="Continue an existing run")
    cont.set_defaults(func=cmd_continue)

    approve = sub.add_parser("approve", help="Approve the current blocked external API step")
    approve.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Path to import ledger JSON")
    approve.add_argument("--step", choices=PIPELINE_STEPS, help="Step to approve; defaults to current blocked/pending step")
    approve.set_defaults(func=cmd_approve)

    status = sub.add_parser("status", help="Show run status")
    status.add_argument("--ledger", default=str(DEFAULT_LEDGER), help="Path to import ledger JSON")
    status.set_defaults(func=cmd_status)

    keys = sub.add_parser("check-keys", help="Check whether local RapidAPI env vars are present without printing values")
    keys.set_defaults(func=cmd_check_keys)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except PipelineBlocked as exc:
        emit(exc.payload)
        return exc.code
    except PipelineFailed as exc:
        emit({"status": "failed", "error": str(exc)})
        return 1
    except KeyboardInterrupt:
        emit({"status": "interrupted"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

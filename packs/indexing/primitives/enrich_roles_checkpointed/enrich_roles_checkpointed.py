#!/usr/bin/env python3
"""Checkpointed OpenAI role-enrichment stage for Powerpacks indexing.

No fake/mock/local role provider is exposed. The stage either dry-runs, replays
explicit --input-classifications, or calls OpenAI with --allow-paid.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI  # noqa: E402
from packs.indexing.lib.io import read_json, read_jsonl, write_json  # noqa: E402
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int  # noqa: E402
from packs.indexing.lib.text import dense_text  # noqa: E402
from packs.indexing.lib.role_clustering import cluster_title  # noqa: E402
from packs.indexing.lib.role_prompts import get_system_user_prompts, format_title_with_context  # noqa: E402

from packs.indexing.lib.llm_config import (  # noqa: E402
    DEFAULT_ROLE_MODEL as DEFAULT_MODEL, DEFAULT_MAX_COMPLETION_TOKENS,
    DEFAULT_OPENAI_TIMEOUT_SECONDS, DEFAULT_OPENAI_CONCURRENCY,
    CHAT_MODEL_PRICES_PER_1K_USD, api_call_kwargs, openai_price_multiplier,
)
from packs.indexing.lib.openai_stream import StopStreaming, drain_pool  # noqa: E402

DEFAULT_CHECKPOINT_EVERY = 1000
DEFAULT_ESTIMATED_OUTPUT_TOKENS_PER_ROLE = 250
ROLE_FIELDS = ["title_hash", "raw_title", "description", "cluster", "role_ids", "seniority_band", "role_type", "role_track", "specialization", "doc2query", "inferred_skills", "dense_text", "semantic_text"]

# Canonical seniority bands — must match the system prompt enum exactly.
VALID_SENIORITY_BANDS = [
    "owner", "partner", "c-suite", "vice-president", "director",
    "principal", "staff", "manager", "senior", "mid", "junior", "entry", "trainee",
]
VALID_ROLE_TYPES = [
    "executive", "management", "ic", "investor", "board", "advisor", "academic", "founder",
]

# Canonical role_ids — loaded from the prod taxonomy.
_TAXONOMY_PATH = ROOT / "packs" / "search" / "data" / "roles" / "canonical_role_taxonomy.json"


def _load_canonical_role_ids() -> list[str]:
    """Load the 180 canonical role_ids from the taxonomy file."""
    data = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for dept_info in data.get("departments", {}).values():
        ids.update(dept_info.get("functions", []))
    return sorted(ids)


def _load_dept_map() -> dict[str, str]:
    """Map each canonical role_id → its department."""
    data = json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for dept, info in data.get("departments", {}).items():
        for fn in info.get("functions", []):
            out[fn] = dept
    return out


CANONICAL_ROLE_IDS = _load_canonical_role_ids()
ROLE_ID_TO_DEPT = _load_dept_map()

# Canonical role_track values — match prod's role_filter_mappings.
VALID_ROLE_TRACKS = [
    "academic_research", "business_dev", "customer_success", "data_ml",
    "design", "engineering", "finance", "general", "governance",
    "healthcare", "investing", "legal", "marketing", "media_content",
    "noise", "operations", "people_hr", "product", "real_estate",
    "sales", "security", "strategy", "trades",
]

# Generic leadership role_ids that should be stripped when a domain role exists.
# Seniority_band already captures the level.
GENERIC_LEADERSHIP_ROLES = {
    "vice_president", "director", "head_of", "manager", "president", "principal",
}

# When only generic leadership IDs remain, resolve to a domain-specific role
# using the role_track / department. Ported from prod remap_roles.py.
TRACK_TO_DOMAIN_ROLE: dict[str, str] = {
    "sales": "sales_manager",
    "marketing": "marketing_manager",
    "engineering": "engineering_manager",
    "data_ml": "data_science_manager",
    "product": "product_manager",
    "design": "creative_director",
    "finance": "finance_manager",
    "operations": "operations_manager",
    "people_hr": "hr_manager",
    "legal": "general_counsel",
    "customer_success": "customer_success_manager",
    "business_dev": "business_development",
    "investing": "angel_investor",
    "security": "security_manager",
    "academic_research": "researcher",
    "media_content": "producer",
    "real_estate": "real_estate_developer",
    "healthcare": "healthcare_administrator",
    "strategy": "strategist",
    "governance": "board_member",
}

# Structured output schema — forces the LLM to return exactly these fields
# with constrained enum values. No parsing gymnastics needed.
ROLE_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "role_classification",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "role_ids": {
                    "type": "array",
                    "items": {"type": "string", "enum": CANONICAL_ROLE_IDS},
                    "description": "Canonical role identifiers from the taxonomy.",
                },
                "seniority_band": {
                    "type": "string",
                    "enum": VALID_SENIORITY_BANDS,
                    "description": "Seniority level.",
                },
                "role_type": {
                    "type": "string",
                    "enum": VALID_ROLE_TYPES,
                    "description": "Functional category of the role.",
                },
                "role_track": {
                    "type": ["string", "null"],
                    "enum": VALID_ROLE_TRACKS + [None],
                    "description": "Canonical department, or null if ambiguous.",
                },
                "specialization": {
                    "type": ["string", "null"],
                    "description": "Specific focus area in snake_case, or null.",
                },
                "cluster": {
                    "type": "string",
                    "description": "Title cluster used for classification.",
                },
                "doc2query": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-5 search query expansions.",
                },
                "inferred_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "3-8 relevant skills.",
                },
                "semantic_text": {
                    "type": "string",
                    "description": "30-40 word factual role description for semantic search.",
                },
                "confidence": {
                    "type": "number",
                    "description": "Classification confidence 0.0-1.0.",
                },
            },
            "required": [
                "role_ids", "seniority_band", "role_type", "role_track",
                "specialization", "cluster", "doc2query", "inferred_skills",
                "semantic_text", "confidence",
            ],
            "additionalProperties": False,
        },
    },
}

# Prod parity (aleph roles_impl.py): titles are classified in batches of ~100
# per call, grouped by cluster so each batch shares one cluster prompt. The
# batch response carries an idx per item to map results back; any title the
# model drops falls back to a single-title call.
DEFAULT_ROLE_BATCH_SIZE = 100
_BATCH_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "idx": {"type": "integer", "description": "Index of the title in the input array."},
        **ROLE_RESPONSE_SCHEMA["json_schema"]["schema"]["properties"],
    },
    "required": ["idx"] + ROLE_RESPONSE_SCHEMA["json_schema"]["schema"]["required"],
    "additionalProperties": False,
}
BATCH_ROLE_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "role_classification_batch",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "classifications": {"type": "array", "items": _BATCH_ITEM_SCHEMA},
            },
            "required": ["classifications"],
            "additionalProperties": False,
        },
    },
}


def role_batch_size() -> int:
    return max(1, int(os.getenv("POWERPACKS_ROLE_BATCH_SIZE", str(DEFAULT_ROLE_BATCH_SIZE))))


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def add_timing(state: dict[str, Any], name: str, seconds: float) -> None:
    timings = state.setdefault("timings", {})
    timings[name] = round(float(timings.get(name, 0.0) or 0.0) + seconds, 3)


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


def get_positions(person: dict[str, Any]) -> list[dict[str, Any]]:
    positions = person.get("work_experiences")
    if isinstance(positions, list):
        return [item for item in positions if isinstance(item, dict)]
    position = person.get("position")
    if isinstance(position, dict):
        return [position]
    return []


def title_from_position(person: dict[str, Any], position: dict[str, Any]) -> str:
    for key in ("title", "position_title", "position", "role", "raw_title"):
        value = clean(position.get(key) or person.get(key))
        if value:
            return value
    return ""


def description_from_position(position: dict[str, Any]) -> str:
    return clean(position.get("description") or position.get("summary"))


def company_from_position(position: dict[str, Any]) -> str:
    for key in ("company_name", "company", "organization", "employer"):
        value = position.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("company_name")
        value = clean(value)
        if value and not value.startswith("{"):
            return value
    return ""


def title_hash(title: str, description: str) -> str:
    raise RuntimeError("title_hash must come from upstream import/enrichment or Aleph bootstrap data; no local fallback hash is allowed")


def shape_role(row: dict[str, Any]) -> dict[str, Any]:
    _LIST_FIELDS = {"role_ids", "doc2query", "inferred_skills"}
    shaped = {field: row.get(field, [] if field in _LIST_FIELDS else "") for field in ROLE_FIELDS}
    for field in _LIST_FIELDS:
        if not isinstance(shaped[field], list):
            shaped[field] = [shaped[field]] if shaped[field] else []
    return shaped


def role_input(person: dict[str, Any], position: dict[str, Any]) -> dict[str, Any] | None:
    title = title_from_position(person, position)
    if not title:
        return None
    description = description_from_position(position)
    company = company_from_position(position)
    upstream_title_hash = clean(position.get("title_hash") or person.get("title_hash"))
    if not upstream_title_hash:
        raise RuntimeError(f"missing upstream title_hash for role {title!r}; rebuild import/enrichment artifacts or restore an Aleph bootstrap with title_hash values")
    return {
        "title_hash": upstream_title_hash,
        "raw_title": title,
        "description": description,
        "company_name": company,
        "headline": clean(person.get("headline")),
        "summary": clean(person.get("summary")),
        "dense_text": dense_text([title, description, company, person.get("headline"), person.get("summary")]),
    }


def load_input_classifications(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    input_path = Path(path)
    if not input_path.exists():
        raise SystemExit(f"missing input role classifications: {input_path}")
    out: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(input_path):
        th = clean(row.get("title_hash"))
        if th:
            out[th] = shape_role(row)
    return out


def role_cluster(role: dict[str, Any]) -> str:
    title = clean(role.get("raw_title"))
    return cluster_title(title) if title else "other"


def batch_role_prompt(cluster: str, roles: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Build cluster system/user messages classifying a batch of titles at once."""
    system_prompt, user_template = get_system_user_prompts(cluster)
    titles_json = []
    for i, role in enumerate(roles):
        entry: dict[str, Any] = {"idx": i, "title": clean(role.get("raw_title"))}
        company = clean(role.get("company_name"))
        if company:
            entry["company"] = company
        description = clean(role.get("description"))
        if description:
            entry["description"] = description[:200] + ("..." if len(description) > 200 else "")
        titles_json.append(entry)
    user_content = user_template.format(num_titles=len(roles), titles=json.dumps(titles_json, indent=2))
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def role_prompt(role: dict[str, Any]) -> list[dict[str, str]]:
    """Build cluster-aware system/user messages for a single role."""
    title = clean(role.get("raw_title"))
    cluster = cluster_title(title) if title else "other"
    system_prompt, user_template = get_system_user_prompts(cluster)

    # Format role as a title entry with optional context.
    title_data: dict[str, Any] = {"title": title}
    company = clean(role.get("company_name"))
    if company:
        title_data["company"] = company
    description = clean(role.get("description"))
    if description:
        title_data["description"] = description

    formatted = format_title_with_context(title_data)
    user_content = user_template.format(num_titles=1, titles=formatted)

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]


def _parse_chat_json(content: str | None, context: str) -> dict[str, Any]:
    raw = (content or "{}").strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{context} returned non-object JSON: {type(parsed).__name__}")
    return parsed


async def call_openai_role_enrichment_async(
    client: AsyncOpenAI,
    role: dict[str, Any],
    *,
    model: str,
    semaphore: asyncio.Semaphore,
    max_retries: int = 3,
) -> dict[str, Any]:
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.chat.completions.create(
                    model=model,
                    response_format=ROLE_RESPONSE_SCHEMA,
                    messages=role_prompt(role),
                    **api_call_kwargs(model),
                )
                return _parse_chat_json(response.choices[0].message.content, "OpenAI role enrichment")
            except APIStatusError as exc:
                status = int(getattr(exc, "status_code", 0) or 0)
                if status in {408, 409, 429, 500, 502, 503, 504} and attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI role enrichment failed: HTTP {status}: {getattr(exc, 'message', str(exc))}") from exc
            except (APIConnectionError, APITimeoutError, TimeoutError, asyncio.TimeoutError) as exc:
                if attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI role enrichment failed: network: {exc}") from exc
            except json.JSONDecodeError as exc:
                if attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI role enrichment returned invalid JSON: {exc}") from exc


async def call_openai_role_batch_async(
    client: AsyncOpenAI,
    cluster: str,
    roles: list[dict[str, Any]],
    *,
    model: str,
    timeout: int,
    semaphore: asyncio.Semaphore,
    max_retries: int = 3,
) -> dict[str, Any]:
    messages = batch_role_prompt(cluster, roles)
    kwargs = api_call_kwargs(model)
    # Output scales with batch size; the single-call cap would truncate the
    # batch JSON mid-array and fail parsing.
    kwargs["max_completion_tokens"] = max(int(kwargs.get("max_completion_tokens") or 0), 400 * len(roles))
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.chat.completions.create(
                    model=model,
                    response_format=BATCH_ROLE_RESPONSE_SCHEMA,
                    messages=messages,
                    timeout=max(timeout, 300),
                    **kwargs,
                )
                return _parse_chat_json(response.choices[0].message.content, "OpenAI role batch enrichment")
            except APIStatusError as exc:
                status = int(getattr(exc, "status_code", 0) or 0)
                if status in {408, 409, 429, 500, 502, 503, 504} and attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI role batch enrichment failed: HTTP {status}: {getattr(exc, 'message', str(exc))}") from exc
            except (APIConnectionError, APITimeoutError, TimeoutError, asyncio.TimeoutError) as exc:
                if attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI role batch enrichment failed: network: {exc}") from exc
            except json.JSONDecodeError as exc:
                if attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI role batch enrichment returned invalid JSON: {exc}") from exc


def _role_batch_units(
    roles: list[dict[str, Any]],
    *,
    client: AsyncOpenAI,
    model: str,
    timeout: int,
    semaphore: asyncio.Semaphore,
    max_retries: int,
    batch_size: int,
) -> list[Any]:
    """Build per-batch coroutines; each resolves its own dropped-idx fallbacks."""
    groups: dict[str, list[int]] = {}
    for i, role in enumerate(roles):
        groups.setdefault(role_cluster(role), []).append(i)

    async def run_batch(cluster: str, idxs: list[int]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        sub = [roles[i] for i in idxs]
        out: list[tuple[dict[str, Any], dict[str, Any]]] = []
        remaining = list(range(len(sub)))
        if len(sub) > 1:
            parsed = await call_openai_role_batch_async(
                client, cluster, sub, model=model, timeout=timeout, semaphore=semaphore, max_retries=max_retries,
            )
            got: dict[int, dict[str, Any]] = {}
            for item in parsed.get("classifications") or []:
                if not isinstance(item, dict):
                    continue
                j = item.get("idx")
                if isinstance(j, int) and 0 <= j < len(sub) and j not in got:
                    got[j] = {k: v for k, v in item.items() if k != "idx"}
            out.extend((sub[j], enrichment) for j, enrichment in got.items())
            remaining = [j for j in range(len(sub)) if j not in got]
        if remaining:
            singles = await asyncio.gather(*[
                call_openai_role_enrichment_async(client, sub[j], model=model, semaphore=semaphore, max_retries=max_retries)
                for j in remaining
            ])
            out.extend((sub[j], enrichment) for j, enrichment in zip(remaining, singles))
        return out

    return [
        run_batch(cluster, idxs[start:start + batch_size])
        for cluster, idxs in groups.items()
        for start in range(0, len(idxs), batch_size)
    ]


async def stream_role_enrichments_async(
    roles: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    concurrency: int,
    max_retries: int = 3,
    batch_size: int | None = None,
    on_result: Any,
) -> Any | None:
    """Stream (base, enrichment) pairs to on_result as each batch completes."""
    batch_size = batch_size if batch_size is not None else role_batch_size()
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    units = _role_batch_units(
        roles, client=client, model=model, timeout=timeout,
        semaphore=semaphore, max_retries=max_retries, batch_size=batch_size,
    )

    def handle(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
        for base, enrichment in pairs:
            on_result(base, enrichment)

    try:
        return await drain_pool(units, handle)
    finally:
        await client.close()


def stream_role_enrichments(
    roles: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    concurrency: int,
    max_retries: int = 3,
    batch_size: int | None = None,
    on_result: Any,
) -> Any | None:
    if not roles:
        return None
    return asyncio.run(stream_role_enrichments_async(
        roles, api_key=api_key, base_url=base_url, model=model, timeout=timeout,
        concurrency=concurrency, max_retries=max_retries, batch_size=batch_size, on_result=on_result,
    ))


async def call_openai_role_enrichments_async(
    roles: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int,
    concurrency: int,
    max_retries: int = 3,
    batch_size: int | None = None,
) -> list[dict[str, Any]]:
    index_of = {id(role): i for i, role in enumerate(roles)}
    results: list[dict[str, Any] | None] = [None] * len(roles)

    def on_result(base: dict[str, Any], enrichment: dict[str, Any]) -> None:
        results[index_of[id(base)]] = enrichment

    await stream_role_enrichments_async(
        roles, api_key=api_key, base_url=base_url, model=model, timeout=timeout,
        concurrency=concurrency, max_retries=max_retries, batch_size=batch_size, on_result=on_result,
    )
    return results


def call_openai_role_enrichments(
    roles: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout: int | None = None,
    concurrency: int | None = None,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    if not roles:
        return []
    return asyncio.run(call_openai_role_enrichments_async(
        roles,
        api_key=api_key,
        base_url=base_url,
        model=model,
        timeout=timeout or int(os.getenv("POWERPACKS_OPENAI_TIMEOUT_SECONDS", str(DEFAULT_OPENAI_TIMEOUT_SECONDS))),
        concurrency=concurrency or env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=DEFAULT_OPENAI_CONCURRENCY),
        max_retries=max_retries,
    ))


def call_openai_role_enrichment(role: dict[str, Any], *, api_key: str, base_url: str, model: str, timeout: int = 60, max_retries: int = 3) -> dict[str, Any]:
    return call_openai_role_enrichments([role], api_key=api_key, base_url=base_url, model=model, timeout=timeout, concurrency=1, max_retries=max_retries)[0]


def _remap_role_ids(role_ids: list[str], role_track: str) -> tuple[list[str], str]:
    """Post-process role_ids: strip generic leadership when domain roles exist,
    resolve to domain-specific role when only generics remain,
    resolve role_track to canonical department from taxonomy."""
    # Resolve department from the first domain role_id that has a taxonomy mapping.
    department = ""
    for rid in role_ids:
        dept = ROLE_ID_TO_DEPT.get(rid)
        if dept and dept not in ("general", "noise"):
            department = dept
            break
    if not department:
        department = role_track or "general"

    # Split into generic leadership vs domain-specific.
    non_generic = [rid for rid in role_ids if rid not in GENERIC_LEADERSHIP_ROLES]
    generic_only = [rid for rid in role_ids if rid in GENERIC_LEADERSHIP_ROLES]

    if non_generic and generic_only:
        # Domain roles exist — strip generic leadership, keep founder.
        kept_generic = [rid for rid in generic_only if rid == "founder"]
        role_ids = non_generic + kept_generic
    elif generic_only and not non_generic and department not in ("general", ""):
        # Only generic leadership IDs + we know the department —
        # resolve to domain-specific role.
        domain_role = TRACK_TO_DOMAIN_ROLE.get(department)
        if domain_role:
            role_ids = [domain_role]

    return role_ids, department


def merge_role(base: dict[str, Any], enrichment: dict[str, Any]) -> dict[str, Any]:
    # Handle field name variants across cluster prompts.
    seniority = enrichment.get("seniority_band") or enrichment.get("seniority", "")
    role_ids = enrichment.get("role_ids") or []
    # Engineering prompt historically used singular "role_id"; normalise.
    if not role_ids and enrichment.get("role_id"):
        rid = enrichment["role_id"]
        role_ids = [rid] if isinstance(rid, str) else list(rid)

    raw_track = enrichment.get("role_track") or ""
    role_ids, department = _remap_role_ids(role_ids, raw_track)

    row = {
        "title_hash": base["title_hash"],
        "raw_title": base["raw_title"],
        "description": base.get("description", ""),
        "dense_text": base.get("dense_text", ""),
        "cluster": enrichment.get("cluster", ""),
        "role_ids": role_ids,
        "seniority_band": seniority,
        "role_type": enrichment.get("role_type", ""),
        "role_track": department,
        "specialization": enrichment.get("specialization", ""),
        "doc2query": enrichment.get("doc2query") or [],
        "inferred_skills": enrichment.get("inferred_skills") or [],
        "semantic_text": clean(enrichment.get("semantic_text")),
    }
    return shape_role(row)


def atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return count


def state_path(output_dir: Path) -> Path:
    return output_dir / "checkpoint.json"


def chunk_path(output_dir: Path, chunk_index: int) -> Path:
    return output_dir / "chunks" / f"roles.{chunk_index:06d}.jsonl"


def default_state(flattened: Path, output_dir: Path, checkpoint_every: int, provider: str, input_classifications: str | None) -> dict[str, Any]:
    return {"status": "running", "created_at": now_iso(), "updated_at": now_iso(), "flattened": str(flattened), "output_dir": str(output_dir), "checkpoint_every": checkpoint_every, "input_rows_processed": 0, "positions_seen": 0, "unique_roles_written": 0, "chunks_written": 0, "seen_title_hashes": [], "provider": provider, "input_classifications": input_classifications, "artifact_hits": 0, "artifact_misses": 0, "paid_calls": 0, "hash_contract": "md5(normalized_title + '|' + normalized_description[:500])[:16]"}


def load_state(flattened: Path, output_dir: Path, checkpoint_every: int, force: bool, provider: str, input_classifications: str | None) -> dict[str, Any]:
    if force and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    sp = state_path(output_dir)
    if sp.exists():
        return read_json(sp)
    state = default_state(flattened, output_dir, checkpoint_every, provider, input_classifications)
    write_json(sp, state)
    return state


def save_state(output_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(state_path(output_dir), state)


def iter_unprocessed_rows(flattened: Path, start_index: int) -> Iterable[tuple[int, dict[str, Any]]]:
    for idx, row in enumerate(read_jsonl(flattened), start=1):
        if idx <= start_index:
            continue
        yield idx, row


def finalize(output_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    finalize_started = time.perf_counter()
    chunks = sorted((output_dir / "chunks").glob("roles.*.jsonl")) if (output_dir / "chunks").exists() else []
    roles: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        for row in read_jsonl(chunk):
            th = clean(row.get("title_hash"))
            if th and th not in seen:
                seen.add(th)
                roles.append(shape_role(row))
    roles.sort(key=lambda row: row["title_hash"])
    roles_path = output_dir / "roles_with_dense_text_remapped.jsonl"
    raw_titles_path = output_dir / "raw_titles.jsonl"
    mapping_path = output_dir / "role_mapping.csv"
    atomic_write_jsonl(roles_path, roles)
    atomic_write_jsonl(raw_titles_path, ({"title_hash": row["title_hash"], "raw_title": row["raw_title"], "description": row.get("description", "")} for row in roles))
    mapping_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = mapping_path.with_name(f".{mapping_path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        import csv
        writer = csv.DictWriter(handle, fieldnames=["title_hash", "raw_title", "expanded_title", "seniority_band", "role_track"])
        writer.writeheader()
        for row in roles:
            writer.writerow({key: row.get(key, "") for key in writer.fieldnames or []})
    tmp.replace(mapping_path)
    add_timing(state, "finalize_merge_write_seconds", time.perf_counter() - finalize_started)
    state["status"] = "completed"
    state["completed_at"] = now_iso()
    state["unique_roles_written"] = len(roles)
    save_state(output_dir, state)
    manifest = {"status": "completed", "stage": "enrich_roles_checkpointed", "provider": state.get("provider"), "input": state.get("flattened"), "checkpoint": str(state_path(output_dir)), "checkpoint_every": state.get("checkpoint_every"), "chunks": [str(path) for path in chunks], "artifacts": {"roles_with_dense_text_remapped": str(roles_path), "raw_titles": str(raw_titles_path), "role_mapping": str(mapping_path)}, "counts": {"input_rows_processed": state.get("input_rows_processed", 0), "positions_seen": state.get("positions_seen", 0), "unique_roles": len(roles), "chunks_written": len(chunks), "artifact_hits": state.get("artifact_hits", 0), "artifact_misses": state.get("artifact_misses", 0), "paid_calls": state.get("paid_calls", 0)}, "timings": state.get("timings", {})}
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def collect_role_inputs(flattened: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for person in read_jsonl(flattened):
        for position in get_positions(person):
            role = role_input(person, position)
            if role and role["title_hash"] not in seen:
                seen.add(role["title_hash"])
                out.append(role)
    return out


def estimate_role_call_shape(roles: list[dict[str, Any]], model: str, batch_size: int | None = None) -> tuple[int, int]:
    """Return (calls, input_tokens) matching the batched call shape."""
    batch_size = batch_size if batch_size is not None else role_batch_size()
    calls = 0
    input_tokens = 0
    if batch_size <= 1:
        for role in roles:
            payload = {
                "model": model,
                "response_format": ROLE_RESPONSE_SCHEMA,
                "messages": role_prompt(role),
                **api_call_kwargs(model),
            }
            input_tokens += estimate_tokens(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            calls += 1
        return calls, input_tokens
    groups: dict[str, list[dict[str, Any]]] = {}
    for role in roles:
        groups.setdefault(role_cluster(role), []).append(role)
    for cluster, group in groups.items():
        for start in range(0, len(group), batch_size):
            messages = batch_role_prompt(cluster, group[start:start + batch_size])
            input_tokens += estimate_tokens(json.dumps(messages, ensure_ascii=False, sort_keys=True))
            calls += 1
    return calls, input_tokens


def dry_run(args: argparse.Namespace) -> dict[str, Any]:
    roles = collect_role_inputs(Path(args.flattened))
    provider = "input-classifications" if getattr(args, "input_classifications", None) else args.provider
    model = getattr(args, "model", None) or os.getenv("POWERPACKS_ROLE_OPENAI_MODEL", DEFAULT_MODEL)
    estimated_calls, input_tokens = estimate_role_call_shape(roles, model)
    output_tokens = 0 if provider == "input-classifications" else len(roles) * DEFAULT_ESTIMATED_OUTPUT_TOKENS_PER_ROLE
    prices = CHAT_MODEL_PRICES_PER_1K_USD.get(model) if provider == "openai" else None
    estimated_cost = 0.0 if provider != "openai" else None
    if prices:
        estimated_cost = round(((input_tokens / 1000.0) * prices["input"] + (output_tokens / 1000.0) * prices["output"]) * openai_price_multiplier(), 6)
    return {
        "status": "dry-run",
        "stage": "enrich_roles_checkpointed",
        "provider": provider,
        "model": model,
        "unique_roles": len(roles),
        "estimated_tokens": input_tokens,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": output_tokens,
        "estimated_calls": 0 if getattr(args, "input_classifications", None) else estimated_calls,
        "estimated_openai_cost_usd": estimated_cost,
        "known_pricing": bool(prices) or provider != "openai",
        "pricing_assumption": f"{model} known OpenAI pricing; output tokens estimated" if prices else f"{model} pricing unknown; output tokens estimated",
        "would_write": [str(Path(args.output_dir) / "checkpoint.json"), str(Path(args.output_dir) / "roles_with_dense_text_remapped.jsonl")],
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    flattened = Path(args.flattened)
    output_dir = Path(args.output_dir)
    if not flattened.exists():
        raise SystemExit(f"missing flattened input: {flattened}")
    if getattr(args, "dry_run", False):
        return dry_run(args)
    requested_provider = str(args.provider)
    if args.provider not in {"openai", "tlm"}:
        raise SystemExit("role provider must be openai/tlm; no fake/mock/local provider is available")
    input_classifications = load_input_classifications(getattr(args, "input_classifications", None))
    allow_paid = bool(getattr(args, "allow_paid", False))
    provider = "input-classifications" if input_classifications else requested_provider
    if input_classifications and allow_paid:
        provider = f"input-classifications+{requested_provider}"
    api_key = getattr(args, "api_key", None) or os.getenv("OPENAI_API_KEY", "")
    if not input_classifications and not allow_paid:
        raise SystemExit(f"role provider '{args.provider}' requires --allow-paid; no paid API was called")
    if not input_classifications and not api_key:
        raise SystemExit("role provider requires OPENAI_API_KEY or --api-key; no paid API was called")
    base_url = getattr(args, "base_url", None) or os.getenv("POWERPACKS_OPENAI_BASE", "https://api.openai.com/v1")
    model = getattr(args, "model", None) or os.getenv("POWERPACKS_ROLE_OPENAI_MODEL", DEFAULT_MODEL)
    state = load_state(flattened, output_dir, args.checkpoint_every, args.force, provider, getattr(args, "input_classifications", None))
    if state.get("status") == "completed" and not args.force:
        manifest_path = output_dir / "manifest.json"
        return read_json(manifest_path) if manifest_path.exists() else {"status": "completed", "checkpoint": str(state_path(output_dir))}
    seen_hashes = set(state.get("seen_title_hashes") or [])
    batch: list[dict[str, Any]] = []
    paid_pending: list[dict[str, Any]] = []
    usage_tier = getattr(args, "openai_usage_tier", None)
    paid_concurrency = int(
        getattr(args, "concurrency", None)
        or env_or_profile_int(
            "POWERPACKS_OPENAI_CONCURRENCY",
            "openai_concurrency",
            tier=usage_tier,
            fallback=DEFAULT_OPENAI_CONCURRENCY,
        )
    )
    paid_timeout = int(os.getenv("POWERPACKS_OPENAI_TIMEOUT_SECONDS", str(DEFAULT_OPENAI_TIMEOUT_SECONDS)))
    chunks_this_run = 0
    started = time.time()

    def flush_output(force: bool = False) -> dict[str, Any] | None:
        nonlocal batch, chunks_this_run
        if not batch or (not force and len(batch) < args.checkpoint_every):
            return None
        chunk_started = time.perf_counter()
        chunk_index = int(state.get("chunks_written") or 0) + 1
        written = atomic_write_jsonl(chunk_path(output_dir, chunk_index), batch)
        state["chunks_written"] = chunk_index
        state["unique_roles_written"] = int(state.get("unique_roles_written") or 0) + written
        state["seen_title_hashes"] = sorted(seen_hashes)
        add_timing(state, "checkpoint_chunk_write_seconds", time.perf_counter() - chunk_started)
        state_started = time.perf_counter()
        save_state(output_dir, state)
        add_timing(state, "checkpoint_state_write_seconds", time.perf_counter() - state_started)
        save_state(output_dir, state)
        batch = []
        chunks_this_run += 1
        if args.stop_after_chunks and chunks_this_run >= args.stop_after_chunks:
            return {"status": "partial", "checkpoint": str(state_path(output_dir)), "chunks_written_total": state["chunks_written"], "input_rows_processed": state["input_rows_processed"], "timings": state.get("timings", {})}
        return None

    # Paid misses stream through one pool after the scan instead of blocking
    # per-wave gathers; `queued` holds back the persisted resume watermark so
    # a crash mid-pool re-queues unwritten misses (written ones are skipped by
    # seen_title_hashes, which only ever records durably written rows).
    queued: dict[str, int] = {}
    last_scan_idx = int(state.get("input_rows_processed") or 0)

    def effective_progress() -> int:
        if queued:
            return min(last_scan_idx, min(queued.values()) - 1)
        return last_scan_idx

    def run_paid_streaming() -> dict[str, Any] | None:
        nonlocal paid_pending
        if not paid_pending:
            return None
        pending = paid_pending
        paid_pending = []

        def on_role_result(base: dict[str, Any], enrichment: dict[str, Any]) -> None:
            batch.append(merge_role(base, enrichment))
            seen_hashes.add(base["title_hash"])
            queued.pop(base["title_hash"], None)
            state["paid_calls"] = int(state.get("paid_calls") or 0) + 1
            state["input_rows_processed"] = effective_progress()
            partial = flush_output()
            if partial:
                raise StopStreaming(partial)

        try:
            openai_started = time.perf_counter()
            partial = stream_role_enrichments(
                pending,
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout=paid_timeout,
                concurrency=paid_concurrency,
                on_result=on_role_result,
            )
            add_timing(state, "openai_enrichment_seconds", time.perf_counter() - openai_started)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        return partial

    local_prepare_started = time.perf_counter()
    for idx, person in iter_unprocessed_rows(flattened, int(state.get("input_rows_processed") or 0)):
        for position in get_positions(person):
            state["positions_seen"] = int(state.get("positions_seen") or 0) + 1
            base = role_input(person, position)
            if not base or base["title_hash"] in seen_hashes or base["title_hash"] in queued:
                continue
            cached = input_classifications.get(base["title_hash"]) if input_classifications else None
            if cached is not None:
                state["artifact_hits"] = int(state.get("artifact_hits") or 0) + 1
                role = merge_role(base, cached)
                seen_hashes.add(base["title_hash"])
                batch.append(role)
            else:
                if input_classifications:
                    state["artifact_misses"] = int(state.get("artifact_misses") or 0) + 1
                if not allow_paid:
                    raise SystemExit(f"missing input role classification for title_hash={base['title_hash']}")
                if not api_key:
                    raise SystemExit("role provider requires OPENAI_API_KEY or --api-key; no paid API was called")
                queued[base["title_hash"]] = idx
                paid_pending.append(base)
        last_scan_idx = idx
        state["input_rows_processed"] = effective_progress()
        if len(batch) >= args.checkpoint_every:
            add_timing(state, "local_input_prepare_seconds", time.perf_counter() - local_prepare_started)
            partial = flush_output()
            local_prepare_started = time.perf_counter()
            if partial:
                return partial
    add_timing(state, "local_input_prepare_seconds", time.perf_counter() - local_prepare_started)
    partial = run_paid_streaming()
    if partial:
        return partial
    state["input_rows_processed"] = last_scan_idx
    if batch:
        partial = flush_output(force=True)
        if partial:
            return partial
    state["elapsed_seconds_last_run"] = round(time.time() - started, 3)
    return finalize(output_dir, state)


def status(args: argparse.Namespace) -> dict[str, Any]:
    sp = state_path(Path(args.output_dir))
    if not sp.exists():
        return {"status": "missing", "checkpoint": str(sp)}
    state = read_json(sp)
    manifest = Path(args.output_dir) / "manifest.json"
    return {"status": state.get("status"), "checkpoint": str(sp), "state": state, "manifest_exists": manifest.exists()}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--flattened", required=True)
    run_p.add_argument("--output-dir", required=True)
    run_p.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY)
    run_p.add_argument("--provider", choices=["openai", "tlm"], default="openai")
    run_p.add_argument("--input-classifications", help="Precomputed Aleph roles_with_dense_text_remapped.jsonl; not a provider")
    run_p.add_argument("--api-key")
    run_p.add_argument("--base-url")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--concurrency", type=int, default=None)
    run_p.add_argument("--openai-usage-tier", default=None)
    run_p.add_argument("--allow-paid", action="store_true")
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--force", action="store_true")
    run_p.add_argument("--stop-after-chunks", type=int)
    run_p.set_defaults(func=run)
    status_p = sub.add_parser("status")
    status_p.add_argument("--output-dir", required=True)
    status_p.set_defaults(func=status)
    return parser


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    args = build_parser().parse_args()
    emit(args.func(args))


if __name__ == "__main__":
    main()

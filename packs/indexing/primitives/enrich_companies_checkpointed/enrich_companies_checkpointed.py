#!/usr/bin/env python3
"""Checkpointed company enrichment stage producing Aleph companies_corpus_v3.

Provider modes:
- artifact: replay precomputed real Aleph-shaped companies_corpus_v3 records from
  a local JSONL artifact, keyed by normalized company_name, preserving the local
  company_urn.
- openai/llm: explicit paid provider; blocked unless --allow-paid is set,
  uses the OpenAI SDK with checkpointing when approved.

Use --dry-run to validate/count/estimate without writing fake enriched outputs
or making provider calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI  # noqa: E402
from packs.indexing.lib.io import read_json, read_jsonl, write_json  # noqa: E402
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int, openai_usage_tier_choices  # noqa: E402

DEFAULT_CHECKPOINT_EVERY = 1000
DEFAULT_MODEL = "gpt-5.1"
DEFAULT_MAX_COMPLETION_TOKENS = 2500
DEFAULT_OPENAI_TIMEOUT_SECONDS = 60
DEFAULT_OPENAI_CONCURRENCY = 64
WORD_RE = re.compile(r"[a-z0-9]+")
CHAT_MODEL_PRICES_PER_1K_USD = {
    "gpt-5.2": {"input": 0.00175, "output": 0.01400},
    "gpt-5.2-chat-latest": {"input": 0.00175, "output": 0.01400},
    "gpt-5.1": {"input": 0.00125, "output": 0.01000},
    "gpt-5.1-chat-latest": {"input": 0.00125, "output": 0.01000},
    "gpt-5": {"input": 0.00125, "output": 0.01000},
    "gpt-5-chat-latest": {"input": 0.00125, "output": 0.01000},
    "gpt-5-mini": {"input": 0.00025, "output": 0.00200},
    "gpt-5-nano": {"input": 0.00005, "output": 0.00040},
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "gpt-4o-mini-2024-07-18": {"input": 0.00015, "output": 0.00060},
}
ALEPH_COMPANY_FIELDS = [
    "company_urn",
    "company_name",
    "original_name",
    "name_aliases",
    "description",
    "city",
    "state",
    "country",
    "metro_area",
    "macro_region",
    "headcount",
    "founded_year",
    "linkedin_url",
    "logo_url",
    "website_domain",
    "funding_total",
    "funding_stage",
    "last_funding_at",
    "valuation",
    "investor_urns",
    "stage",
    "accelerators",
    "yc_batches",
    "customer_type",
    "ownership_status",
    "company_type",
    "entity_types",
    "sector_types",
    "technology_types",
    "word_text",
    "char_text",
    "d2q_text",
    "doc2query",
    "semantic_text",
    "confidence_score",
]
CLASSIFICATION_FIELDS = {
    "entity_types",
    "sector_types",
    "word_text",
    "d2q_text",
    "doc2query",
    "semantic_text",
    "confidence_score",
    "customer_type",
    "company_type",
    "ownership_status",
    "technology_types",
    "stage",
    "funding_stage",
    "accelerators",
    "yc_batches",
}

# Minimal checked-in company taxonomies observed in the copied Aleph seed
# (`.powerpacks/aleph-seed/2026-05-08/pipeline_output/company/companies_corpus_v3.jsonl`).
# Unknown provider values are preserved by normalizers so future Aleph/provider
# additions are not silently dropped.
OBSERVED_ENTITY_TYPES = {
    "venture_backed_startup",
    "nonprofit",
    "government_public_sector",
    "vc_firm",
    "club_association",
    "bank",
    "insurance_carrier",
    "pe_firm",
    "foundation_endowment",
    "family_office",
    "sovereign_wealth_fund",
}
OBSERVED_SECTOR_TYPES = {
    "aerospace", "ai_ml", "bio_synbio", "climate_energy_tech", "commerce_tech",
    "creator_tools", "crypto", "cybersecurity", "data", "deep_tech", "defense_tech",
    "devops", "diagnostics", "edtech", "fintech", "gaming_gambling_tech", "hardware",
    "health_tech", "hr_tech", "infra_devtools", "insurtech", "iot", "legal_tech",
    "manufacturing_tech", "marketplaces", "marketing_tech", "material_science",
    "medical_devices", "real_estate_tech", "robotics_drones", "saas", "sales_tech",
    "semiconductors", "social_networking", "sports_wellness_tech", "supply_chain_logistics",
    "telco", "therapies", "transportation_mobility", "travel_tech",
}
OBSERVED_CUSTOMER_TYPES = {"Business (B2B)", "Consumer (B2C)", "Government (B2G)"}
OBSERVED_FUNDING_STAGES = {
    "PRE_SEED", "SEED", "SERIES_A", "SERIES_B", "SERIES_C", "SERIES_D", "SERIES_E",
    "SERIES_F", "SERIES_G", "SERIES_H", "SERIES_I", "VENTURE_UNKNOWN", "EXITED",
    "OUT_OF_BUSINESS", "STEALTH",
}
OBSERVED_COMPANY_TYPES = {"STARTUP", "UNKNOWN", "SCHOOL"}
OBSERVED_OWNERSHIP_STATUSES = {
    "PRIVATE", "ACQUIRED_OR_MERGED", "PUBLIC", "ACTIVE", "OUT_OF_BUSINESS", "INACTIVE",
    "IPO_REGISTRATION", "MANAGING",
}
REQUIRED_PROVIDER_OUTPUT_FIELDS = {
    "entity_types",
    "sector_types",
    "technology_types",
    "customer_type",
    "funding_stage",
    "company_type",
    "ownership_status",
    "stage",
    "accelerators",
    "yc_batches",
    "confidence_score",
    "doc2query",
    "d2q_text",
    "word_text",
    "semantic_text",
}

COMPANY_CLASSIFICATION_SCHEMA = {
    "entity_types": {"type": "string[]", "observed_values": sorted(OBSERVED_ENTITY_TYPES)},
    "sector_types": {"type": "string[]", "observed_values": sorted(OBSERVED_SECTOR_TYPES)},
    "technology_types": {"type": "string[]", "observed_values": []},
    "customer_type": {"type": "string", "observed_values": sorted(OBSERVED_CUSTOMER_TYPES)},
    "funding_stage": {"type": "string", "observed_values": sorted(OBSERVED_FUNDING_STAGES)},
    "company_type": {"type": "string", "observed_values": sorted(OBSERVED_COMPANY_TYPES)},
    "ownership_status": {"type": "string", "observed_values": sorted(OBSERVED_OWNERSHIP_STATUSES)},
    "stage": {"type": "string"},
    "accelerators": {"type": "string[]"},
    "yc_batches": {"type": "string[]"},
    "confidence_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    "doc2query": {"type": "string[]"},
    "d2q_text": {"type": "string"},
    "word_text": {"type": "string"},
    "semantic_text": {"type": "string"},
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def norm_name(value: Any) -> str:
    return re.sub(r"\s+", " ", clean(value).lower()).strip()


def atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                count += 1
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return count


def listify(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[0:1] == "[":
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
            except json.JSONDecodeError:
                pass
        if "," in text:
            return [part.strip() for part in text.split(",") if part.strip()]
        return [text]
    return [value]


def first_present(row: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, "", []):
            return value
    return None


def normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def normalize_customer_type(value: Any) -> str:
    if isinstance(value, list):
        value = " ".join(clean(item) for item in value)
    text = clean(value)
    upper = text.upper()
    if "B2G" in upper or "GOVERNMENT" in upper:
        return "Government (B2G)"
    if "B2C" in upper or "CONSUMER" in upper:
        return "Consumer (B2C)"
    if "B2B" in upper or "BUSINESS" in upper:
        return "Business (B2B)"
    return text if text in OBSERVED_CUSTOMER_TYPES else text


def validate_provider_output(payload: dict[str, Any]) -> None:
    missing = sorted(field for field in REQUIRED_PROVIDER_OUTPUT_FIELDS if field not in payload)
    if missing:
        raise RuntimeError(f"company classification provider output missing required fields: {', '.join(missing)}")


def normalize_classification_output(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize real provider/artifact output while preserving unknown taxonomy values."""

    out = dict(payload)
    for key in ("entity_types", "sector_types", "technology_types", "accelerators", "yc_batches", "doc2query"):
        out[key] = [clean(item) for item in listify(out.get(key)) if clean(item)]
    out["customer_type"] = normalize_customer_type(out.get("customer_type"))
    out["confidence_score"] = normalize_confidence(out.get("confidence_score"))
    for key in ("word_text", "d2q_text", "semantic_text", "stage", "company_type", "ownership_status"):
        if key in out:
            out[key] = clean(out.get(key))
    return out


def shape_company(row: dict[str, Any]) -> dict[str, Any]:
    name = clean(row.get("company_name"))
    description = clean(row.get("description"))
    semantic = clean(row.get("semantic_text")) or " ".join(part for part in [name, description, clean(row.get("entity_sector_text"))] if part)
    word_text = clean(row.get("word_text") or row.get("entity_sector_text"))
    doc2query = listify(row.get("doc2query"))
    shaped = {
        "company_urn": clean(row.get("company_urn") or row.get("id")),
        "company_name": name,
        "original_name": clean(row.get("original_name")) or name,
        "name_aliases": listify(row.get("name_aliases")) or ([name] if name else []),
        "description": description,
        "city": clean(row.get("city")),
        "state": clean(row.get("state")),
        "country": clean(row.get("country")),
        "metro_area": clean(row.get("metro_area")),
        "macro_region": clean(row.get("macro_region")),
        "headcount": row.get("headcount"),
        "founded_year": row.get("founded_year"),
        "linkedin_url": clean(row.get("linkedin_url")),
        "logo_url": clean(row.get("logo_url")),
        "website_domain": clean(row.get("website_domain")),
        "funding_total": row.get("funding_total"),
        "funding_stage": row.get("funding_stage") or "VENTURE_UNKNOWN",
        "last_funding_at": row.get("last_funding_at"),
        "valuation": row.get("valuation"),
        "investor_urns": listify(row.get("investor_urns")),
        "stage": clean(row.get("stage")),
        "accelerators": listify(row.get("accelerators")),
        "yc_batches": listify(row.get("yc_batches")),
        "customer_type": normalize_customer_type(row.get("customer_type")),
        "ownership_status": clean(row.get("ownership_status")),
        "company_type": clean(row.get("company_type")),
        "entity_types": listify(row.get("entity_types")),
        "sector_types": listify(row.get("sector_types")),
        "technology_types": listify(row.get("technology_types")),
        "word_text": word_text,
        "char_text": clean(row.get("char_text")) or " ".join([name, clean(row.get("website_domain"))]).strip(),
        "d2q_text": clean(row.get("d2q_text")) or " ".join(clean(item) for item in doc2query if clean(item)),
        "doc2query": doc2query,
        "semantic_text": semantic,
        "confidence_score": row.get("confidence_score") if row.get("confidence_score") not in (None, "") else 0.0,
    }
    shaped = normalize_classification_output(shaped)
    return {key: shaped.get(key, [] if key in {"name_aliases", "investor_urns", "entity_types", "sector_types", "technology_types", "accelerators", "yc_batches", "doc2query"} else "") for key in ALEPH_COMPANY_FIELDS}


def load_company_artifact(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}
    artifact_path = Path(path)
    if not artifact_path.exists():
        raise SystemExit(f"missing company artifact: {artifact_path}")
    return {norm_name(row.get("company_name")): row for row in read_jsonl(artifact_path) if norm_name(row.get("company_name"))}


def merge_enrichment(local: dict[str, Any], enriched: dict[str, Any]) -> dict[str, Any]:
    merged = dict(local)
    provider_payload = {field: local.get(field) for field in REQUIRED_PROVIDER_OUTPUT_FIELDS}
    provider_payload.update(enriched)
    validate_provider_output(provider_payload)
    normalized = normalize_classification_output(provider_payload)
    for key in CLASSIFICATION_FIELDS:
        if key in normalized and normalized.get(key) not in (None, ""):
            merged[key] = normalized[key]
    return shape_company(merged)


def apply_artifact(local: dict[str, Any], artifact: dict[str, dict[str, Any]], missing_policy: str) -> dict[str, Any] | None:
    if not artifact:
        raise RuntimeError("company artifact provider requires a non-empty precomputed artifact")
    cached = artifact.get(norm_name(local.get("company_name")))
    if cached:
        return merge_enrichment(local, cached)
    if missing_policy == "error":
        raise RuntimeError(f"missing precomputed company artifact for company_name={local.get('company_name')!r}")
    if missing_policy == "skip":
        return None
    raise RuntimeError(f"unsupported artifact missing policy: {missing_policy}")


def openai_classification_payload(local: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": os.getenv("POWERPACKS_COMPANY_OPENAI_MODEL", DEFAULT_MODEL),
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "Classify a company for Aleph company search. Return only JSON with keys: "
                    "entity_types, sector_types, technology_types, customer_type, funding_stage, "
                    "company_type, ownership_status, stage, accelerators, yc_batches, word_text, "
                    "d2q_text, doc2query, semantic_text, confidence_score. "
                    f"Prefer observed entity_types={sorted(OBSERVED_ENTITY_TYPES)} and sector_types={sorted(OBSERVED_SECTOR_TYPES)}. "
                    f"customer_type must be one of {sorted(OBSERVED_CUSTOMER_TYPES)} when known. "
                    "Use arrays for *_types, accelerators, yc_batches, and doc2query. "
                    "Keep JSON compact: doc2query max 6 strings and text fields under 800 characters each."
                ),
            },
            {"role": "user", "content": json.dumps(local, ensure_ascii=False, sort_keys=True)},
        ],
        "temperature": 0,
        "max_completion_tokens": int(os.getenv("POWERPACKS_COMPANY_MAX_COMPLETION_TOKENS", str(DEFAULT_MAX_COMPLETION_TOKENS))),
    }


def _parse_chat_json(content: str | None, context: str) -> dict[str, Any]:
    raw = (content or "{}").strip()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{context} returned non-object JSON")
    return parsed


async def call_openai_company_classifier_async(
    client: AsyncOpenAI,
    local: dict[str, Any],
    *,
    model: str | None,
    semaphore: asyncio.Semaphore,
    max_retries: int = 3,
) -> dict[str, Any]:
    payload = openai_classification_payload(local)
    if model:
        payload["model"] = model
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.chat.completions.create(**payload)
                return _parse_chat_json(response.choices[0].message.content, "OpenAI company classifier")
            except APIStatusError as exc:
                status = int(getattr(exc, "status_code", 0) or 0)
                if status in {408, 409, 429, 500, 502, 503, 504} and attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI company classifier failed: HTTP {status}: {getattr(exc, 'message', str(exc))}") from exc
            except (APIConnectionError, APITimeoutError, TimeoutError, asyncio.TimeoutError) as exc:
                if attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI company classifier failed: network: {exc}") from exc
            except json.JSONDecodeError as exc:
                if attempt < max_retries:
                    await asyncio.sleep(min(8.0, 0.5 * (2**attempt)))
                    attempt += 1
                    continue
                raise RuntimeError(f"OpenAI company classifier returned invalid JSON: {exc}") from exc


async def call_openai_company_classifiers_async(
    rows: list[dict[str, Any]],
    *,
    model: str | None,
    api_key: str,
    base_url: str,
    timeout: int,
    concurrency: int,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout, max_retries=0)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    try:
        return await asyncio.gather(*[
            call_openai_company_classifier_async(client, row, model=model, semaphore=semaphore, max_retries=max_retries)
            for row in rows
        ])
    finally:
        await client.close()


def call_openai_company_classifiers(
    rows: list[dict[str, Any]],
    *,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: int | None = None,
    concurrency: int | None = None,
    max_retries: int = 3,
) -> list[dict[str, Any]]:
    api_key = api_key or os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY or --api-key is required for --provider openai; no API call was made")
    if not rows:
        return []
    return asyncio.run(call_openai_company_classifiers_async(
        rows,
        model=model,
        api_key=api_key,
        base_url=(base_url or os.getenv("POWERPACKS_OPENAI_BASE") or "https://api.openai.com/v1"),
        timeout=timeout or int(os.getenv("POWERPACKS_OPENAI_TIMEOUT_SECONDS", str(DEFAULT_OPENAI_TIMEOUT_SECONDS))),
        concurrency=concurrency or env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=DEFAULT_OPENAI_CONCURRENCY),
        max_retries=max_retries,
    ))


def call_openai_company_classifier(local: dict[str, Any], *, model: str | None = None, api_key: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    return call_openai_company_classifiers([local], model=model, api_key=api_key, base_url=base_url, concurrency=1)[0]


def checkpoint_path(output_dir: Path) -> Path:
    return output_dir / "checkpoint.json"


def chunk_path(output_dir: Path, index: int) -> Path:
    return output_dir / "chunks" / f"companies.{index:06d}.jsonl"


def load_state(output_dir: Path, input_path: Path, checkpoint_every: int, provider: str, artifact_path: str | None, force: bool) -> dict[str, Any]:
    if force and output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    cp = checkpoint_path(output_dir)
    if cp.exists():
        return read_json(cp)
    state = {
        "status": "running",
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "input": str(input_path),
        "output_dir": str(output_dir),
        "checkpoint_every": checkpoint_every,
        "provider": provider,
        "artifact_path": artifact_path,
        "input_rows_processed": 0,
        "companies_written": 0,
        "chunks_written": 0,
        "artifact_hits": 0,
        "artifact_misses": 0,
        "paid_calls": 0,
    }
    write_json(cp, state)
    return state


def save_state(output_dir: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = now_iso()
    write_json(checkpoint_path(output_dir), state)


def iter_unprocessed(path: Path, start_index: int) -> Iterable[tuple[int, dict[str, Any]]]:
    for idx, row in enumerate(read_jsonl(path), start=1):
        if idx <= start_index:
            continue
        yield idx, row


def estimate_payload(input_path: Path, provider: str, artifact: dict[str, dict[str, Any]] | None = None, model_override: str | None = None) -> dict[str, Any]:
    rows = list(read_jsonl(input_path))
    artifact = artifact or {}
    model = model_override or os.getenv("POWERPACKS_COMPANY_OPENAI_MODEL", DEFAULT_MODEL)
    missing = [clean(row.get("company_name")) for row in rows if provider == "artifact" and norm_name(row.get("company_name")) not in artifact]
    estimated_input_tokens = sum(max(1, len(json.dumps(row, ensure_ascii=False)) // 4) for row in rows)
    estimated_output_tokens = len(rows) * 350
    prices = CHAT_MODEL_PRICES_PER_1K_USD.get(model) if provider == "openai" else None
    estimated_cost = 0.0 if provider != "openai" else None
    if prices:
        estimated_cost = round((estimated_input_tokens / 1000.0) * prices["input"] + (estimated_output_tokens / 1000.0) * prices["output"], 6)
    return {
        "status": "dry_run",
        "stage": "enrich_companies_checkpointed",
        "provider": provider,
        "model": model,
        "input": str(input_path),
        "companies": len(rows),
        "batches": len(rows),
        "missing_artifact_companies": missing,
        "estimated_input_tokens": estimated_input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_openai_cost_usd": estimated_cost,
        "known_pricing": bool(prices) or provider != "openai",
        "will_call_provider": False,
        "will_write_enriched_artifacts": False,
    }


def finalize(output_dir: Path, output_path: Path, state: dict[str, Any]) -> dict[str, Any]:
    chunks = sorted((output_dir / "chunks").glob("companies.*.jsonl")) if (output_dir / "chunks").exists() else []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for chunk in chunks:
        for row in read_jsonl(chunk):
            urn = clean(row.get("company_urn"))
            if urn and urn not in seen:
                seen.add(urn)
                rows.append(row)
    rows.sort(key=lambda row: clean(row.get("company_urn")))
    atomic_write_jsonl(output_path, rows)
    state["status"] = "completed"
    state["completed_at"] = now_iso()
    state["companies_written"] = len(rows)
    save_state(output_dir, state)
    manifest = {
        "status": "completed",
        "stage": "enrich_companies_checkpointed",
        "provider": state.get("provider"),
        "provider_equivalence": "precomputed_real_artifact" if state.get("provider") == "artifact" else "openai_real_provider",
        "checkpoint": str(checkpoint_path(output_dir)),
        "output": str(output_path),
        "chunks": [str(path) for path in chunks],
        "counts": {
            "input_rows_processed": state.get("input_rows_processed", 0),
            "companies": len(rows),
            "chunks_written": len(chunks),
            "artifact_hits": state.get("artifact_hits", 0),
            "artifact_misses": state.get("artifact_misses", 0),
            "paid_calls": state.get("paid_calls", 0),
        },
        "provider_notes": [
            "artifact replays precomputed real Aleph-shaped companies_corpus_v3 fields without spend",
            "openai/llm is an explicit --allow-paid provider path",
        ],
    }
    write_json(output_dir / "manifest.json", manifest)
    return manifest


def run(args: argparse.Namespace) -> dict[str, Any]:
    input_path = Path(args.input)
    output_dir = Path(args.output_dir)
    output_path = Path(args.output)
    provider = "openai" if args.provider == "llm" else str(args.provider)
    if provider not in {"artifact", "openai"}:
        raise SystemExit(f"company provider '{args.provider}' is not supported; no paid API was called")
    if not input_path.exists():
        raise SystemExit(f"missing company input JSONL: {input_path}")
    artifact = load_company_artifact(getattr(args, "artifact_path", None)) if getattr(args, "artifact_path", None) else {}
    if getattr(args, "dry_run", False) or getattr(args, "estimate", False):
        return estimate_payload(input_path, provider, artifact, getattr(args, "model", None))
    allow_paid = bool(getattr(args, "allow_paid", False))
    if provider == "openai" and not artifact and not allow_paid:
        raise SystemExit("company provider 'openai' requires --allow-paid; no paid API was called")
    state_provider = "artifact+openai" if artifact and allow_paid else provider
    state = load_state(output_dir, input_path, int(args.checkpoint_every), state_provider, getattr(args, "artifact_path", None), bool(args.force))
    if state.get("status") == "completed" and output_path.exists() and not args.force:
        manifest = output_dir / "manifest.json"
        return read_json(manifest) if manifest.exists() else {"status": "completed", "output": str(output_path)}

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

    def flush_output(force: bool = False) -> dict[str, Any] | None:
        nonlocal batch, chunks_this_run
        if not batch or (not force and len(batch) < int(args.checkpoint_every)):
            return None
        chunk_index = int(state.get("chunks_written") or 0) + 1
        written = atomic_write_jsonl(chunk_path(output_dir, chunk_index), batch)
        state["chunks_written"] = chunk_index
        state["companies_written"] = int(state.get("companies_written") or 0) + written
        save_state(output_dir, state)
        batch = []
        chunks_this_run += 1
        if args.stop_after_chunks and chunks_this_run >= args.stop_after_chunks:
            return {
                "status": "partial",
                "checkpoint": str(checkpoint_path(output_dir)),
                "chunks_written_total": state["chunks_written"],
                "input_rows_processed": state["input_rows_processed"],
                "companies_written": state["companies_written"],
            }
        return None

    def flush_paid(force: bool = False) -> dict[str, Any] | None:
        nonlocal paid_pending
        paid_flush_size = max(1, min(paid_concurrency, int(args.checkpoint_every)))
        if not paid_pending or (not force and len(paid_pending) < paid_flush_size):
            return None
        pending = paid_pending
        paid_pending = []
        try:
            enrichments = call_openai_company_classifiers(
                pending,
                model=getattr(args, "model", None),
                api_key=getattr(args, "api_key", None),
                base_url=getattr(args, "base_url", None),
                timeout=paid_timeout,
                concurrency=paid_concurrency,
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        for shaped, enrichment in zip(pending, enrichments):
            batch.append(merge_enrichment(shaped, enrichment))
        state["paid_calls"] = int(state.get("paid_calls") or 0) + len(pending)
        return flush_output(force=True)

    for idx, row in iter_unprocessed(input_path, int(state.get("input_rows_processed") or 0)):
        shaped = shape_company(row)
        cached = artifact.get(norm_name(shaped.get("company_name"))) if artifact else None
        if cached:
            try:
                shaped = merge_enrichment(shaped, cached)
            except RuntimeError as exc:
                raise SystemExit(str(exc)) from exc
            state["artifact_hits"] = int(state.get("artifact_hits") or 0) + 1
        else:
            if artifact:
                state["artifact_misses"] = int(state.get("artifact_misses") or 0) + 1
            if provider == "artifact" and not allow_paid:
                if args.artifact_missing_policy == "skip":
                    state["input_rows_processed"] = idx
                    continue
                raise SystemExit(f"missing precomputed company artifact for company_name={shaped.get('company_name')!r}")
            if not allow_paid:
                raise SystemExit("company provider 'openai' requires --allow-paid; no paid API was called")
            paid_pending.append(shaped)
            partial = flush_paid()
            if partial:
                return partial
            shaped = None
        if shaped is not None:
            batch.append(shaped)
        state["input_rows_processed"] = idx
        if len(batch) >= int(args.checkpoint_every):
            partial = flush_paid(force=True) or flush_output()
            if partial:
                return partial
    partial = flush_paid(force=True)
    if partial:
        return partial
    if batch:
        partial = flush_output(force=True)
        if partial:
            return partial
    return finalize(output_dir, output_path, state)


def status(args: argparse.Namespace) -> dict[str, Any]:
    cp = checkpoint_path(Path(args.output_dir))
    return read_json(cp) if cp.exists() else {"status": "missing", "checkpoint": str(cp)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run_p = sub.add_parser("run")
    run_p.add_argument("--input", required=True)
    run_p.add_argument("--output", required=True)
    run_p.add_argument("--output-dir", required=True)
    run_p.add_argument("--checkpoint-every", type=int, default=DEFAULT_CHECKPOINT_EVERY)
    run_p.add_argument("--provider", choices=["artifact", "openai", "llm"], default="openai")
    run_p.add_argument("--artifact-path", help="Precomputed real Aleph companies_corpus_v3.jsonl input")
    run_p.add_argument("--artifact-missing-policy", choices=["error", "skip"], default="error")
    run_p.add_argument("--dry-run", action="store_true", help="Validate/count/estimate only; no provider calls and no enriched output writes")
    run_p.add_argument("--estimate", action="store_true", help="Alias for --dry-run")
    run_p.add_argument("--allow-paid", action="store_true")
    run_p.add_argument("--model", default=None)
    run_p.add_argument("--concurrency", type=int, default=None)
    run_p.add_argument("--openai-usage-tier", choices=openai_usage_tier_choices(), default=None)
    run_p.add_argument("--api-key")
    run_p.add_argument("--base-url")
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

"""Turn a source JD into grounded capability evidence in one cached model call.

The response keeps exact source quotes for validation. The returned text contains
only the hiring company, responsibilities, experience, and explicitly optional
qualifications. Paid responses are checkpointed before parsing so a malformed
response never causes an automatic repeat charge.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from packs.search.primitives.shared.openai_client import make_openai_client

MODEL = "gpt-5.6-sol"
REASONING_EFFORT = "high"
SERVICE_TIER = "flex"
MAX_COMPLETION_TOKENS = 8192
_PROMPT = Path(__file__).resolve().parents[2] / "prompts" / "jd-cleaning.txt"
_SECTIONS = (
    ("responsibilities", "Responsibilities"),
    ("experience", "Experience"),
    ("nice_to_have", "Nice to have"),
)
_ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text", "source_quote"],
    "properties": {
        "text": {"type": "string"},
        "source_quote": {"type": "string"},
    },
}
_NULLABLE_STRING = {"type": ["string", "null"]}
_INVENTED_LABEL = re.compile(r"^\s*(?:\[(?:must|nice|gate|inferred)\]|(?:must|gate|inferred):)", re.I)
_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["company", *(key for key, _ in _SECTIONS)],
    "properties": {
        "company": {
            "type": "object",
            "additionalProperties": False,
            "required": ["what_it_does", "stage", "size", "source_quotes"],
            "properties": {
                "what_it_does": _NULLABLE_STRING,
                "stage": _NULLABLE_STRING,
                "size": _NULLABLE_STRING,
                "source_quotes": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["what_it_does", "stage", "size"],
                    "properties": {
                        key: {"type": "array", "items": {"type": "string"}}
                        for key in ("what_it_does", "stage", "size")
                    },
                },
            },
        },
        **{
            key: {"type": "array", "items": _ITEM_SCHEMA}
            for key, _ in _SECTIONS
        },
    },
}


@dataclass(frozen=True)
class _EvidenceItem:
    text: str
    source_quote: str


@dataclass(frozen=True)
class _CompanyEvidence:
    what_it_does: str | None
    stage: str | None
    size: str | None
    source_quotes: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class _StructuredJobDescription:
    title: str
    company_name: str
    company: _CompanyEvidence
    responsibilities: tuple[_EvidenceItem, ...]
    experience: tuple[_EvidenceItem, ...]
    nice_to_have: tuple[_EvidenceItem, ...]


def _system_prompt() -> str:
    prompt = _PROMPT.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError("JD cleaning prompt must not be empty")
    return prompt


def build_request(*, jd: str, title: str, company_name: str) -> dict[str, Any]:
    """Build the exact paid request for dry-run inspection and execution."""
    source = (
        f"Job title: {title}\n"
        f"Hiring company: {company_name}\n\n"
        f"<job_description>\n{jd}\n</job_description>"
    )
    return {
        "model": MODEL,
        "reasoning_effort": REASONING_EFFORT,
        "service_tier": SERVICE_TIER,
        "max_completion_tokens": MAX_COMPLETION_TOKENS,
        "store": False,
        "messages": [
            {"role": "system", "content": _system_prompt()},
            {"role": "user", "content": source},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "structured_job_description",
                "strict": True,
                "schema": _OUTPUT_SCHEMA,
            },
        },
    }


def _request_hash(request: Mapping[str, Any]) -> str:
    serialized = json.dumps(request, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _response_record(response: Any, request_hash: str, jd: str) -> dict[str, Any]:
    choice = response.choices[0] if getattr(response, "choices", None) else None
    message = getattr(choice, "message", None)
    usage = getattr(response, "usage", None)
    return {
        "request_sha256": request_hash,
        "source_sha256": hashlib.sha256(jd.encode()).hexdigest(),
        "model": str(getattr(response, "model", None) or MODEL),
        "service_tier": str(getattr(response, "service_tier", None) or SERVICE_TIER),
        "finish_reason": getattr(choice, "finish_reason", None),
        "refusal": getattr(message, "refusal", None),
        "content": getattr(message, "content", None),
        "usage": usage.model_dump() if usage is not None else {},
    }


def _checkpoint(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(record)


def _exact_object(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise RuntimeError(f"JD cleaner returned invalid {label} fields")
    return value


def _claim(value: Any, quotes: Any, source: str, label: str) -> tuple[str | None, tuple[str, ...]]:
    if value is not None and (not isinstance(value, str) or not value.strip()):
        raise RuntimeError(f"JD cleaner returned an invalid {label}")
    if not isinstance(quotes, list) or any(not isinstance(quote, str) or not quote for quote in quotes):
        raise RuntimeError(f"JD cleaner returned invalid {label} source quotes")
    if any(quote not in source for quote in quotes):
        raise RuntimeError(f"JD cleaner returned an ungrounded {label}")
    if (value is None) != (not quotes):
        raise RuntimeError(f"JD cleaner returned {label} without matching source evidence")
    return " ".join(value.split()) if value is not None else None, tuple(quotes)


def _items(value: Any, source: str, label: str) -> tuple[_EvidenceItem, ...]:
    if not isinstance(value, list):
        raise RuntimeError(f"JD cleaner returned invalid {label}")
    items = []
    for value_item in value:
        item = _exact_object(value_item, {"text", "source_quote"}, f"{label} item")
        text = item["text"]
        quote = item["source_quote"]
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError(f"JD cleaner returned an empty {label} item")
        if _INVENTED_LABEL.match(text):
            raise RuntimeError(f"JD cleaner returned an invented label in {label}")
        if not isinstance(quote, str) or not quote or quote not in source:
            raise RuntimeError(f"JD cleaner returned an ungrounded {label} item")
        items.append(_EvidenceItem(text=" ".join(text.split()), source_quote=quote))
    return tuple(items)


def _parse(record: Mapping[str, Any], *, source: str, title: str,
           company_name: str, request_hash: str) -> _StructuredJobDescription:
    if record.get("request_sha256") != request_hash:
        raise RuntimeError("JD cleaner cache does not match its request")
    if record.get("finish_reason") != "stop" or record.get("refusal"):
        raise RuntimeError("JD cleaner refused or returned an incomplete response")
    content = record.get("content")
    if not isinstance(content, str) or not content:
        raise RuntimeError("JD cleaner returned no content")
    try:
        raw = json.loads(content)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("JD cleaner returned malformed JSON") from exc
    root = _exact_object(raw, {"company", *(key for key, _ in _SECTIONS)}, "result")
    company = _exact_object(
        root["company"], {"what_it_does", "stage", "size", "source_quotes"}, "company",
    )
    quotes = _exact_object(
        company["source_quotes"], {"what_it_does", "stage", "size"}, "company source quote",
    )
    claims = {
        key: _claim(company[key], quotes[key], source, f"company {key}")
        for key in ("what_it_does", "stage", "size")
    }
    return _StructuredJobDescription(
        title=title,
        company_name=company_name,
        company=_CompanyEvidence(
            what_it_does=claims["what_it_does"][0],
            stage=claims["stage"][0],
            size=claims["size"][0],
            source_quotes={key: claims[key][1] for key in claims},
        ),
        responsibilities=_items(root["responsibilities"], source, "responsibilities"),
        experience=_items(root["experience"], source, "experience"),
        nice_to_have=_items(root["nice_to_have"], source, "nice_to_have"),
    )


def _render(structured: _StructuredJobDescription) -> str:
    company = structured.company
    lines = [
        f"Title: {structured.title}",
        " · ".join((
            f"Hiring company: {structured.company_name}",
            f"What it does: {company.what_it_does or 'Not stated'}",
            f"Stage: {company.stage or 'Not stated'}",
            f"Size: {company.size or 'Not stated'}",
        )),
    ]
    for key, heading in _SECTIONS:
        items = getattr(structured, key)
        lines.extend(("", heading))
        lines.extend((f"- {item.text}" for item in items) if items else ("Not stated.",))
    return "\n".join(lines) + "\n"


def _save_rendered(path: Path, structured: _StructuredJobDescription) -> str:
    rendered = _render(structured)
    path.write_text(rendered, encoding="utf-8")
    return rendered


def clean_job_description(
    *,
    jd: str,
    title: str,
    company_name: str,
    output_dir: Path,
    api_key: str | None = None,
    client: Any | None = None,
) -> str:
    """Return a grounded capability-only JD, reusing the exact-request cache."""
    if not isinstance(jd, str) or not jd.strip():
        raise ValueError("JD cleaner requires source text")
    if not isinstance(title, str):
        raise TypeError("JD cleaner title must be a string")
    if not isinstance(company_name, str):
        raise TypeError("JD cleaner company_name must be a string")
    if not isinstance(output_dir, Path):
        raise TypeError("JD cleaner output_dir must be a Path")

    jd = jd.strip()
    title = " ".join(title.split()) or "Not stated"
    company_name = " ".join(company_name.split()) or "Not stated"
    request = build_request(jd=jd, title=title, company_name=company_name)
    request_hash = _request_hash(request)
    cache = output_dir / f"{request_hash}.json"
    if cache.is_file():
        record = json.loads(cache.read_text(encoding="utf-8"))
        return _save_rendered(cache.with_suffix(".txt"), _parse(
            record, source=jd, title=title,
            company_name=company_name, request_hash=request_hash))

    owned_client = None
    if client is None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        client = owned_client = make_openai_client(api_key=key, timeout=600, max_retries=0)
    try:
        response = client.chat.completions.create(**request)
        record = _checkpoint(cache, _response_record(response, request_hash, jd))
    finally:
        if owned_client is not None:
            owned_client.close()
    return _save_rendered(cache.with_suffix(".txt"), _parse(
        record, source=jd, title=title,
        company_name=company_name, request_hash=request_hash))

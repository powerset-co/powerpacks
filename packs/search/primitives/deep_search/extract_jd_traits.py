"""Extract additional JD traits after Pond compilation.

The full JD, role brief, and already-scored Pond traits produce a grounded,
ordered person-trait list. Raw responses are checkpointed before parsing.
"""
from __future__ import annotations

import json
import re
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

SHARED_DIR = Path(__file__).resolve().parents[1] / "shared"
if str(SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(SHARED_DIR))
from openai_client import make_openai_client  # noqa: E402

try:  # direct script execution
    from pond_prompts import POND_PROMPT_FAMILIES, load_pond_prompt
    import precedents
except ImportError:  # module execution
    from .pond_prompts import POND_PROMPT_FAMILIES, load_pond_prompt
    from . import precedents

VALID_TARGET_LEVELS = {"senior_ic", "staff_ic", "lead", "manager", "director", "vp", "exec"}
TRAIT_KINDS = {"capability", "background", "tool"}
MAX_TRAITS = 6


def _chat_request(
    messages: list[dict[str, str]], *, model: str,
    reasoning_effort: str | None, service_tier: str | None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": model, "messages": messages, "response_format": {"type": "json_object"},
    }
    if reasoning_effort:
        request["reasoning_effort"] = reasoning_effort
    if service_tier:
        request["service_tier"] = service_tier
    return request


def role_brief(obj: Mapping[str, Any]) -> dict[str, str]:
    """Normalize the role title, archetype, level, and prompt family for trait extraction."""
    job_title = str(obj.get("job_title") or "role").strip()
    target_level = str(obj.get("target_level") or "senior_ic").strip().lower()
    if target_level not in VALID_TARGET_LEVELS:
        target_level = "senior_ic"
    family = str(obj.get("pond_prompt_family") or "general").strip().lower()
    if family not in POND_PROMPT_FAMILIES:
        family = "general"
    return {
        "job_title": job_title,
        "normalized_archetype": str(obj.get("normalized_archetype") or job_title).strip(),
        "target_level": target_level,
        "pond_prompt_family": family,
    }


def build_traits_messages(
    jd: str,
    brief: Mapping[str, str],
    system_prompt: str,
    pond_traits: Sequence[Mapping[str, Any]] = (),
    pond_query: str = "",
) -> list[dict[str, str]]:
    role = {key: brief[key] for key in ("job_title", "normalized_archetype", "target_level")}
    cards = precedents.retrieve_jd_precedents(jd, brief, collection="traits")
    precedent_context = ""
    if cards:
        precedent_context = (
            "\n\nRetrieved JD precedents:\n"
            f"{json.dumps(cards, indent=2)}\n\n"
            "Apply lessons only where the JD work matches; these are not a checklist. "
            "Do not import requirements from precedents. Ground every trait in actual JD evidence."
        )
    pond_context = (f"\n\nInitial pond query:\n{pond_query}\n\n"
                    "Return qualifications beyond what this query explicitly covers." if pond_query else "")
    if pond_traits:
        pond_context += (
            "\n\nPond traits already scored:\n"
            f"{json.dumps(list(pond_traits), indent=2)}\n\n"
            "Return only additional qualifications beyond what these pond traits explicitly cover. "
            "Use the JD to identify and group the experience that would distinguish better-fit candidates."
        )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            f"Role:\n{json.dumps(role, indent=2)}\n\nJob description:\n\n{jd.strip()}"
            f"{precedent_context}{pond_context}"
        )},
    ]


def traits_request(
    *, jd: str, brief: Mapping[str, str], model: str, system_prompt: str,
    reasoning_effort: str | None = None, service_tier: str | None = None,
    pond_traits: Sequence[Mapping[str, Any]] = (),
    pond_query: str = "",
) -> dict[str, Any]:
    return _chat_request(
        build_traits_messages(jd, brief, system_prompt, pond_traits, pond_query),
        model=model, reasoning_effort=reasoning_effort, service_tier=service_tier,
    )


def _traits(obj: Mapping[str, Any], jd_text: str | None) -> list[dict[str, str]]:
    """Verbatim-quoted traits of a known kind, in the model's order, deduped, at most MAX_TRAITS."""
    traits: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in obj.get("traits") or []:
        if not isinstance(row, Mapping):
            continue
        trait = " ".join(str(row.get("trait") or "").split())
        kind = str(row.get("kind") or "").strip().casefold()
        quote = str(row.get("evidence_quote") or "").strip()
        if jd_text is not None and quote:
            match = re.search(r"\s+".join(re.escape(word) for word in quote.split()), jd_text)
            quote = match.group() if match else ""
        if not trait or kind not in TRAIT_KINDS or not quote:
            continue
        key = _norm(trait)
        if key in seen:
            continue
        seen.add(key)
        parsed = {"trait": trait, "kind": kind, "evidence_quote": quote}
        selection_reason = " ".join(str(row.get("selection_reason") or "").split())
        if selection_reason:
            parsed["selection_reason"] = selection_reason
        traits.append(parsed)
    return traits[:MAX_TRAITS]


def _norm(value: str) -> str:
    return " ".join(value.strip().lower().split())


def _complete(client: Any, request: dict[str, Any], raw_path: Path | None) -> str:
    """One chat call; the verbatim response is checkpointed before it is parsed."""
    response = client.chat.completions.create(**request)
    raw = response.choices[0].message.content or "{}"
    if raw_path:
        raw_path.write_text(raw, encoding="utf-8")
    return raw


def extract_traits(
    *,
    jd_file: Path,
    brief: Mapping[str, str],
    pond_traits: Sequence[Mapping[str, Any]],
    model: str,
    api_key: str | None,
    system_prompt: str | None = None,
    reasoning_effort: str | None = None,
    raw_response_path: Path | None = None,
    client: Any | None = None,
    service_tier: str | None = None,
) -> list[dict[str, str]]:
    """Generate additional JD traits once Pond traits are known."""
    jd = jd_file.read_text(encoding="utf-8")
    if raw_response_path is not None and raw_response_path.is_file():
        return _traits(json.loads(raw_response_path.read_text(encoding="utf-8")), jd)
    if client is None:
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ValueError("OPENAI_API_KEY not set")
        client = make_openai_client(key)
    traits_obj = json.loads(_complete(client, traits_request(
        jd=jd,
        brief=brief,
        model=model,
        system_prompt=system_prompt or load_pond_prompt(brief, "traits"),
        reasoning_effort=reasoning_effort,
        service_tier=service_tier,
        pond_traits=pond_traits,
    ), raw_response_path))
    return _traits(traits_obj, jd)

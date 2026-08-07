"""Normalize Parallel results into the standing research artifact contract."""

from __future__ import annotations

from datetime import date
from typing import Any

from packs.ingestion.primitives.deep_context.enrich.parallel_research.models import (
    ParallelProviderResult,
)
from packs.ingestion.primitives.deep_context.enrich.parallel_research.queue import (
    ResearchQueueRow,
    input_fingerprint,
)


def parallel_to_research_json(
    provider: ParallelProviderResult,
    row: ResearchQueueRow,
    handle: str,
    name: str,
    bio: str,
    *, research_method: str = "parallel-core2x",
) -> dict[str, Any]:
    """Normalize one provider result into the standing research artifact shape."""
    real_name = provider.real_name or name or handle
    first, _, last = real_name.partition(" ")
    source_channel = (row.source_channel or "phone").strip().lower()
    completeness, gaps = provider.completeness, provider.gaps
    return {
        "research_id": f"{handle}-{date.today().isoformat()}",
        "query": f"@{handle} ({name}): {bio[:100]}",
        "status": "draft", "research_method": research_method,
        "person": {
            "full_name": real_name, "first_name": first, "last_name": last,
            "also_known_as": [handle, name] if real_name != name else [handle],
            "confidence": provider.name_confidence, "sources": [],
            "notes": provider.name_evidence or "",
        },
        "location": {
            "city": provider.location_city or "", "state": "",
            "country": provider.location_country or "", "raw": "",
            "confidence": 0.5 if provider.location_city or provider.location_country else 0.0,
            "source": "",
        },
        "headline": {
            "text": bio[:200] if bio else "", "confidence": 0.95 if bio else 0.0,
            "source": f"https://x.com/{handle}",
        },
        "summary": {
            "text": provider.summary or "", "confidence": 0.7,
            "source": "Parallel Deep Research",
        },
        "positions": [item.to_payload() for item in provider.positions],
        "education": [item.to_payload() for item in provider.education],
        "social": {
            "twitter_handle": handle if source_channel == "twitter" else None,
            "linkedin_url": provider.linkedin_url or None,
            "linkedin_status": "found" if provider.linkedin_url else "not_found",
            "github_url": provider.github_url,
            "personal_website": provider.personal_website,
            "primary_email": row.primary_email if source_channel == "email" else None,
            "primary_phone": row.phone_e164 if source_channel == "phone" else None,
        },
        "metadata": {
            "total_sources_consulted": 0, "estimated_completeness": completeness,
            "gaps": gaps, "research_date": date.today().isoformat(),
            "research_method": research_method,
            "research_notes": provider.research_notes or "",
            "source_channel": source_channel or "unknown",
            "source_identifier": row.primary_email or row.phone_e164 or handle,
            "input_fingerprint": input_fingerprint(row, handle),
        },
    }

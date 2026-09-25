"""Judge-facing LinkedIn profile view over cached profiles.

Building a view here never spends: profiles come from the projected cache only.
"""

from __future__ import annotations

from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.models import (
    IdentityProfileSource,
)
from packs.ingestion.primitives.deep_context.enrich.identity_reconcile.judge_models import (
    JudgeProfile,
)
from packs.ingestion.primitives.deep_context.enrich.profiles.models import (
    NormalizedProfile,
    ProfileExperience,
    ProfileResult,
)


def _span(entry: ProfileExperience) -> str:
    start = str(entry.starts_at or "")
    end = str(entry.ends_at or "")
    return f"{start}–{end}" if start and end else f"{start}–present" if start else end


def linkedin_view(
    row: IdentityProfileSource,
    projected: ProfileResult | None = None,
) -> JudgeProfile:
    """Parse one SQLite/provider profile into the sole judge-facing shape."""
    # "cache" means a profile fetch was attempted, successful or not; "fallback"
    # means no fetch happened and only the raw import/attached row is available.
    profile: NormalizedProfile | None = projected.normalized_profile if projected is not None else None
    if profile is not None and profile.present:
        if not profile.success:
            # Fetch attempted and failed: keep the identifier we searched for
            # rather than whatever (if anything) the failed response carried.
            public_identifier = row.public_identifier.strip().lower()
        else:
            public_identifier = (profile.public_identifier or "").strip().lower()
        experiences = profile.experiences
        education = profile.education
        location = profile.location or ""
        full_name = profile.full_name or ""
        headline = profile.headline or ""
        picture = profile.profile_pic_url or ""
        source = "cache"
    else:
        public_identifier = row.public_identifier.strip().lower()
        # IdentityProfileSource is identity-only — it has no work/education/
        # location fields to show until a hydrated ProfileResult replaces it.
        experiences = ()
        education = ()
        location = ""
        full_name = row.full_name or row.display_name
        headline = row.headline
        picture = row.profile_picture_url
        source = "fallback"
    work = []
    for item in experiences:
        title = item.title or ""
        company = item.company_name or ""
        text = " @ ".join(value for value in (title, company) if value)
        span = _span(item)
        if text:
            work.append(f"{text}{f' ({span})' if span else ''}")
    schools = []
    for item in education:
        school = item.school_name or ""
        degree = ", ".join(value for value in (item.degree, item.field) if value)
        text = f"{degree} — {school}" if degree and school else degree or school
        if text:
            schools.append(text)
    return JudgeProfile.from_payload(
        {
            "public_identifier": public_identifier,
            "linkedin_url": row.linkedin_url,
            "full_name": str(full_name),
            "headline": str(headline),
            "profile_pic_url": str(picture),
            "experiences": work,
            "education": schools,
            "location": location,
            "source": source,
            # The canonical "enough LinkedIn signal to act on" gate —
            # NormalizedProfile.present (experience or education). A
            # headline-only row is not judgeable until it hydrates.
            "has_profile": bool(profile is not None and profile.present),
        }
    )


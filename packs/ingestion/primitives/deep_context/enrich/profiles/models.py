"""Frozen rows at the RapidAPI profile boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from packs.ingestion.primitives.deep_context.shared.coerce import compact_json, text
from packs.ingestion.schemas.linkedin_profile_normalizer import normalize_linkedin_profile
from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)

# A provider verdict, but not one of rapidapi_client's PROFILE_CONTENT/EMPTY/
# ERROR states: the fetch succeeded and returned a real profile, just not the
# one requested (see `canonicalize_provider_profile` below). Kept local to
# this module — nothing upstream needs to mint it.
PROFILE_IDENTITY_MISMATCH = "identity_mismatch"


def canonicalize_provider_profile(
    payload: dict[str, Any],
    requested_pub: str,
    linkedin_url: str,
) -> dict[str, Any]:
    """Decide whose profile this payload is, and stamp/strip it accordingly.

    The provider identity policy, in one place: an echoed identity that
    disagrees with the requested one is relabeled ``identity_mismatch`` with
    every content field dropped; an agreeing (or silent) echo is canonically
    stamped with the REQUESTED identity and its company/school keys
    normalized. Returns the canonical payload dict that
    ``ProfileResult.from_payload`` parses — parsing lives there, policy here.
    """
    profile_value: object = payload.get("normalized_profile")
    profile = (
        normalize_linkedin_profile(profile_value)
        if isinstance(profile_value, dict)
        else {}
    )
    echoed_pub = ""
    mismatched = False
    if profile.get("success") is True:
        # The provider's OWN identity, as echoed in its response — may
        # legitimately differ from what we asked for (a renamed/
        # redirected slug resolves to a different profile).
        echoed_pub = (
            str(profile.get("public_identifier") or "").strip().lower()
            or extract_public_identifier(str(profile.get("linkedin_url") or "")).lower()
        )
        mismatched = bool(echoed_pub) and echoed_pub != requested_pub
        if mismatched:
            # Do NOT relabel: filing this content under the identity we
            # REQUESTED would silently attribute a stranger's profile to
            # this candidate (see module history — a forked slug
            # normalizer once split one person into two rows the same
            # way). Keep the provider's own identity for visibility, but
            # drop every content field: an explicit `success: False` and
            # an otherwise-empty profile make every existing
            # `.normalized_profile.success`/content-presence gate
            # (classify_queue, linkedin_view, propose_retargets, ...)
            # treat this exactly like no-profile-found, with no changes
            # required at those call sites.
            profile = {
                "success": False,
                "public_identifier": echoed_pub,
                "linkedin_url": (
                    normalize_linkedin_url(str(profile.get("linkedin_url") or ""))
                    or f"https://www.linkedin.com/in/{echoed_pub}"
                ),
            }
        else:
            # Agreement (or no echoed identifier): stamp the identity we
            # requested. The shared normalizer already owns nested aliases.
            profile["public_identifier"] = requested_pub
            profile["linkedin_url"] = normalize_linkedin_url(linkedin_url)
    canonical = dict(payload)
    if isinstance(profile_value, dict):
        canonical["normalized_profile"] = profile
    if mismatched:
        canonical["state"] = PROFILE_IDENTITY_MISMATCH
        canonical["detail"] = (
            f"provider resolved {echoed_pub!r} for requested {requested_pub!r}; "
            "treated as no-profile"
        )
    return canonical


def _year(value: object) -> int | str | None:
    """Keep only the year off a provider date object."""
    if not isinstance(value, dict):
        return None
    year: object = value.get("year")
    return year if isinstance(year, (int, str)) else None


@dataclass(frozen=True)
class ProfileExperience:
    title: str | None
    company_name: str | None
    starts_at: int | str | None
    ends_at: int | str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProfileExperience:
        return cls(
            text(payload.get("title")),
            text(payload.get("company_name")),
            _year(payload.get("starts_at")),
            _year(payload.get("ends_at")),
        )


@dataclass(frozen=True)
class ProfileEducation:
    school_name: str | None
    degree: str | None
    field: str | None
    starts_at: int | str | None
    ends_at: int | str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProfileEducation:
        return cls(
            text(payload.get("school_name")),
            text(payload.get("degree")),
            text(payload.get("field")),
            _year(payload.get("starts_at")),
            _year(payload.get("ends_at")),
        )


def profile_experiences(value: object) -> tuple[ProfileExperience, ...]:
    return tuple(
        ProfileExperience.from_payload(row)
        for row in value
        if isinstance(row, dict)
    ) if isinstance(value, (list, tuple)) else ()


def profile_education(value: object) -> tuple[ProfileEducation, ...]:
    return tuple(
        ProfileEducation.from_payload(row)
        for row in value
        if isinstance(row, dict)
    ) if isinstance(value, (list, tuple)) else ()


@dataclass(frozen=True)
class NormalizedProfile:
    success: bool | None
    public_identifier: str | None
    linkedin_url: str | None
    full_name: str | None
    headline: str | None
    profile_pic_url: str | None
    location_str: str | None
    city: str | None
    state: str | None
    country: str | None
    experiences: tuple[ProfileExperience, ...]
    education: tuple[ProfileEducation, ...]

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> NormalizedProfile:
        return cls(
            payload.get("success") if isinstance(payload.get("success"), bool) else None,
            text(payload.get("public_identifier")),
            text(payload.get("linkedin_url")),
            text(payload.get("full_name")),
            text(payload.get("headline")),
            text(payload.get("profile_pic_url")),
            text(payload.get("location_str")),
            text(payload.get("city")),
            text(payload.get("state")),
            text(payload.get("country")),
            profile_experiences(payload.get("experiences")),
            profile_education(payload.get("education")),
        )

    @property
    def location(self) -> str | None:
        return self.location_str or ", ".join(
            value for value in (self.city, self.state, self.country) if value
        ) or None

    @property
    def present(self) -> bool:
        """Whether this profile has the evidence required by identity policy."""
        return bool(self.experiences or self.education)


@dataclass(frozen=True)
class ProfileResult:
    """One typed profile plus the single preserved provider envelope."""

    state: str | None
    normalized_profile: NormalizedProfile
    from_cache: bool | None
    fetched: bool | None
    detail: str | None
    payload_json: str

    @classmethod
    def from_payload(
        cls,
        public_identifier: str,
        linkedin_url: str,
        payload: dict[str, Any],
    ) -> ProfileResult:
        """Parse one provider payload after `canonicalize_provider_profile`
        has decided and stamped its identity."""
        canonical = canonicalize_provider_profile(
            payload, public_identifier.strip().lower(), linkedin_url
        )
        profile_value: object = canonical.get("normalized_profile")
        profile = dict(profile_value) if isinstance(profile_value, dict) else {}
        return cls(
            text(canonical.get("state")),
            NormalizedProfile.from_payload(profile),
            canonical.get("from_cache") if isinstance(canonical.get("from_cache"), bool) else None,
            canonical.get("fetched") if isinstance(canonical.get("fetched"), bool) else None,
            text(canonical.get("detail")),
            compact_json(canonical),
        )

    def to_payload(self) -> dict[str, Any]:
        """Render only at JSON/persistence boundaries."""
        return json.loads(self.payload_json)

    def raw_payload(self) -> dict[str, Any] | None:
        """The unnormalized provider/cache `data` blob — for dossier evidence,
        distinct from `to_payload()`'s canonical stamped shape."""
        payload = json.loads(self.payload_json)
        data = payload.get("data")
        return data if isinstance(data, dict) else None


@dataclass(frozen=True)
class ProfileTarget:
    public_identifier: str | None
    linkedin_url: str | None
    candidate_key: str | None = None
    parent_id: str | None = None

    def __post_init__(self) -> None:
        if self.public_identifier:
            object.__setattr__(self, "public_identifier", self.public_identifier.strip().lower())


@dataclass(frozen=True)
class ProfileHydration:
    wanted: int
    ok: int
    failed: int
    skipped_no_key: int
    profiles: Mapping[str, ProfileResult]

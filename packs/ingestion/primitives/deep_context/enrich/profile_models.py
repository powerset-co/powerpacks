"""Frozen rows at the RapidAPI profile boundary."""

from __future__ import annotations

import json
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from packs.ingestion.schemas.people_schema import (
    extract_public_identifier,
    normalize_linkedin_url,
)

# A provider verdict, but not one of rapidapi_client's PROFILE_CONTENT/EMPTY/
# ERROR states: the fetch succeeded and returned a real profile, just not the
# one requested (see `ProfileResult.from_payload` below). Kept local to this
# module — nothing upstream needs to mint it.
PROFILE_IDENTITY_MISMATCH = "identity_mismatch"


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _year(value: object) -> int | str | None:
    """Keep only the year off a `{"year", "month", "day"}` date object — the
    typed `starts_at`/`ends_at` fields below are year-only. The full nested
    object survives untouched in `_payload_json`/`to_payload()`."""
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
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProfileExperience:
        return cls(
            str(payload["title"]) if payload.get("title") else None,
            str(payload["company_name"]) if payload.get("company_name") else None,
            _year(payload.get("starts_at")),
            _year(payload.get("ends_at")),
            _json(payload),
        )

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True)
class ProfileEducation:
    school_name: str | None
    degree: str | None
    field: str | None
    starts_at: int | str | None
    ends_at: int | str | None
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ProfileEducation:
        return cls(
            str(payload["school_name"]) if payload.get("school_name") else None,
            str(payload["degree"]) if payload.get("degree") else None,
            str(payload["field"]) if payload.get("field") else None,
            _year(payload.get("starts_at")),
            _year(payload.get("ends_at")),
            _json(payload),
        )

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


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
    _payload_json: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> NormalizedProfile:
        return cls(
            payload.get("success") if isinstance(payload.get("success"), bool) else None,
            str(payload["public_identifier"]) if payload.get("public_identifier") else None,
            str(payload["linkedin_url"]) if payload.get("linkedin_url") else None,
            str(payload["full_name"]) if payload.get("full_name") else None,
            str(payload["headline"]) if payload.get("headline") else None,
            str(payload["profile_pic_url"]) if payload.get("profile_pic_url") else None,
            str(payload["location_str"]) if payload.get("location_str") else None,
            str(payload["city"]) if payload.get("city") else None,
            str(payload["state"]) if payload.get("state") else None,
            str(payload["country"]) if payload.get("country") else None,
            profile_experiences(payload.get("experiences")),
            profile_education(payload.get("education")),
            _json(payload),
        )

    @property
    def location(self) -> str | None:
        return self.location_str or ", ".join(
            value for value in (self.city, self.state, self.country) if value
        ) or None

    @property
    def present(self) -> bool:
        """Non-empty payload, NOT decidable content — a bare success:false
        stub counts as present. `profile_cache.profile_has_content` is the
        stricter experiences-or-education bar used to gate CONTENT vs EMPTY."""
        return self._payload_json != "{}"

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)


@dataclass(frozen=True)
class ProfileResult:
    """One canonical provider result; `_payload_json` is the projection source."""

    state: str | None
    normalized_profile: NormalizedProfile
    from_cache: bool | None
    fetched: bool | None
    status_code: int | None
    detail: str | None
    attempts: int | None
    _data_json: str | None
    _payload_json: str

    @classmethod
    def from_payload(
        cls,
        public_identifier: str,
        linkedin_url: str,
        payload: dict[str, Any],
    ) -> ProfileResult:
        """Parse and canonically stamp one raw/cache provider payload once."""
        profile_value: object = payload.get("normalized_profile")
        profile = dict(profile_value) if isinstance(profile_value, dict) else {}
        requested_pub = public_identifier.strip().lower()
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
                # Agreement (or the provider echoed no identifier to compare
                # against): canonically stamp the identity we REQUESTED,
                # exactly as before.
                profile["public_identifier"] = requested_pub
                profile["linkedin_url"] = normalize_linkedin_url(linkedin_url)
                # Providers spell company/school under 2-3 different keys across
                # response shapes; canonicalize to company_name/school_name once
                # here so every downstream reader sees one key.
                profile["experiences"] = [
                    {
                        **{
                            key: value
                            for key, value in row.items()
                            if key not in {"company", "companyName", "organization"}
                        },
                        "company_name": str(
                            row.get("company_name")
                            or row.get("company")
                            or row.get("companyName")
                            or row.get("organization")
                            or ""
                        ),
                    }
                    for row in profile.get("experiences") or ()
                    if isinstance(row, dict)
                ]
                profile["education"] = [
                    {
                        **{
                            key: value
                            for key, value in row.items()
                            if key not in {"school", "schoolName", "institution"}
                        },
                        "school_name": str(
                            row.get("school_name")
                            or row.get("school")
                            or row.get("schoolName")
                            or row.get("institution")
                            or ""
                        ),
                    }
                    for row in profile.get("education") or ()
                    if isinstance(row, dict)
                ]
        canonical = dict(payload)
        if isinstance(profile_value, dict):
            canonical["normalized_profile"] = profile
        if mismatched:
            canonical["state"] = PROFILE_IDENTITY_MISMATCH
            canonical["detail"] = (
                f"provider resolved {echoed_pub!r} for requested {requested_pub!r}; "
                "treated as no-profile"
            )
        data: object = canonical.get("data")
        return cls(
            str(canonical["state"]) if canonical.get("state") else None,
            NormalizedProfile.from_payload(profile),
            canonical.get("from_cache") if isinstance(canonical.get("from_cache"), bool) else None,
            canonical.get("fetched") if isinstance(canonical.get("fetched"), bool) else None,
            int(canonical["status_code"]) if canonical.get("status_code") is not None else None,
            str(canonical["detail"]) if canonical.get("detail") else None,
            int(canonical["attempts"]) if canonical.get("attempts") is not None else None,
            _json(data) if isinstance(data, dict) else None,
            _json(canonical),
        )

    def to_payload(self) -> dict[str, Any]:
        return json.loads(self._payload_json)

    def raw_payload(self) -> dict[str, Any] | None:
        """The unnormalized provider/cache `data` blob — for dossier evidence,
        distinct from `to_payload()`'s canonical stamped shape."""
        return json.loads(self._data_json) if self._data_json is not None else None


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

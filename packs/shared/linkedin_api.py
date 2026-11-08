"""LinkedIn API credentials and routing: explicit gateway key, Powerset, RapidAPI."""
from __future__ import annotations

import os
import urllib.parse

from packs.powerset.primitives.auth import auth
from packs.shared.endpoints import gateway_url
from dataclasses import dataclass, field
from typing import Literal


POWERSET_HOST = urllib.parse.urlsplit(gateway_url()).netloc
RAPIDAPI_HOST = "professional-network-data.p.rapidapi.com"
MISSING_LINKEDIN_KEY = "Neither POWERSET_API_KEY nor RAPIDAPI_LINKEDIN_KEY is set"


@dataclass(frozen=True)
class LinkedInApiConfig:
    api_key: str = field(repr=False)
    provider: Literal["powerset", "rapidapi"] = "powerset"

    @classmethod
    def resolve(cls, api_key: str | None = None) -> LinkedInApiConfig:
        """Explicit keys retain gateway semantics; Powerset wins over RapidAPI."""
        if api_key is not None:
            return cls(api_key)
        gateway_key = auth.gateway_token()
        if gateway_key:
            return cls(gateway_key)
        return cls(os.getenv("RAPIDAPI_LINKEDIN_KEY", "").strip(), provider="rapidapi")

    @property
    def host(self) -> str:
        return POWERSET_HOST if self.provider == "powerset" else RAPIDAPI_HOST

    @property
    def base_url(self) -> str:
        if self.provider == "powerset":
            return f"{gateway_url()}/vendor/professional-network-data"
        return f"https://{RAPIDAPI_HOST}"

    @property
    def path_prefix(self) -> str:
        if self.provider == "powerset":  # keeps any path on POWERPACKS_GATEWAY_URL
            return f"{urllib.parse.urlsplit(gateway_url()).path}/vendor/professional-network-data"
        return ""

    def headers(self, freshness: str) -> dict[str, str]:
        if self.provider == "powerset":
            return {"x-powerset-key": self.api_key, "X-Freshness": freshness}
        return {"x-rapidapi-key": self.api_key, "x-rapidapi-host": RAPIDAPI_HOST}

"""LinkedIn API credentials and routing: explicit gateway key, Powerset, RapidAPI."""
from __future__ import annotations

import os

from packs.powerset.primitives.auth import auth
from dataclasses import dataclass, field
from typing import Literal


POWERSET_HOST = "proxy.powerset.dev"
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
    def path_prefix(self) -> str:
        return "/vendor/professional-network-data" if self.provider == "powerset" else ""

    def headers(self, freshness: str) -> dict[str, str]:
        if self.provider == "powerset":
            return {"x-powerset-key": self.api_key, "X-Freshness": freshness}
        return {"x-rapidapi-key": self.api_key, "x-rapidapi-host": RAPIDAPI_HOST}

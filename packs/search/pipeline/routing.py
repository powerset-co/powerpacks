"""Strict outer `$search` routing contract."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from .models import Backend, Profile, _strict


class RouteTarget(StrEnum):
    ENGINE = "engine"
    SQL = "sql"
    CONTACTS = "contacts"


@dataclass(frozen=True)
class SearchRoute:
    target: RouteTarget
    profile: Profile | None
    backend: Backend | None
    reason: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SearchRoute":
        _strict(data, {"target", "profile", "backend", "reason"}, "SearchRoute")
        target = RouteTarget(data["target"])
        profile = Profile(data["profile"]) if data.get("profile") is not None else None
        backend = Backend(data["backend"]) if data.get("backend") is not None else None
        if not str(data.get("reason") or "").strip():
            raise ValueError("SearchRoute.reason is required")
        if target == RouteTarget.ENGINE and (profile is None or backend is None):
            raise ValueError("engine routes require profile and backend")
        if target != RouteTarget.ENGINE and (profile is not None or backend is not None):
            raise ValueError("sql/contact routes must not carry profile or backend")
        return cls(target, profile, backend, str(data["reason"]).strip())

    def to_dict(self) -> dict[str, Any]:
        return {"target": self.target.value, "profile": self.profile.value if self.profile else None,
                "backend": self.backend.value if self.backend else None, "reason": self.reason}

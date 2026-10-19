"""Local provider configuration for Settings. No credentials leave except masked tails; no API calls."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packs.shared.web import account


@dataclass(frozen=True)
class _Provider:
    id: str
    name: str
    description: str
    # Each tuple is one required credential, with aliases in resolution order.
    credentials: tuple[tuple[str, ...], ...]
    dashboard_url: str
    connect_url: str


_PROVIDERS = (
    _Provider("parallel", "Parallel", "Web research", (("PARALLEL_API_KEY",),),
              "https://platform.parallel.ai", "https://platform.parallel.ai"),
    _Provider("modal", "Modal", "Hosted processing", (("MODAL_TOKEN_ID",), ("MODAL_TOKEN_SECRET",)),
              "https://modal.com/settings/usage", "https://modal.com/settings/tokens"),
    _Provider("powerset", "Powerset", "LinkedIn data via the Powerset gateway",
              (("POWERSET_API_KEY",),), "https://powerset.dev", "https://powerset.dev"),
    _Provider("openai", "OpenAI", "Models and embeddings", (("OPENAI_API_KEY",),),
              "https://platform.openai.com/usage", "https://platform.openai.com/api-keys"),
    _Provider("typesafe", "TypeSafe (JEV)", "Identity and search judgments", (("TYPESAFE_API_KEY",),),
              "https://console.typesafe.ai", "https://console.typesafe.ai/keys"),
    _Provider("turbopuffer", "TurboPuffer", "Hosted search index", (("TURBOPUFFER_API_KEY",),),
              "https://turbopuffer.com/dashboard", "https://turbopuffer.com/dashboard"),
    _Provider("rapidapi-twitter", "RapidAPI · Twitter/X", "Twitter/X contact import",
              (("RAPIDAPI_TWITTER_KEY", "RAPIDAPI_KEY"),),
              "https://rapidapi.com/developer/dashboard", "https://rapidapi.com/developer/dashboard"),
)


def read(root: Path) -> dict[str, object]:
    values = account.config(root)
    providers = []
    for provider in _PROVIDERS:
        credentials = []
        for names in provider.credentials:
            name = next((name for name in names if values.get(name, "").strip()), names[0])
            value = values.get(name, "").strip()
            credentials.append({"name": name, "configured": bool(value),
                                "masked": ("••••" + value[-4:] if len(value) > 4 else "••••") if value else None})
        providers.append({"id": provider.id, "name": provider.name, "description": provider.description,
                          "connected": all(credential["configured"] for credential in credentials),
                          "credentials": credentials, "connect_url": provider.connect_url,
                          "dashboard_url": provider.dashboard_url})
    return {"providers": providers}

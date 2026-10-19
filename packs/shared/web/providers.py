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
    connect_url: str


_PROVIDERS = (
    _Provider("parallel", "Parallel", "Web research", (("PARALLEL_API_KEY",),),
              "https://platform.parallel.ai/settings?tab=api-keys"),
    _Provider("openai", "OpenAI", "Models and embeddings", (("OPENAI_API_KEY",),),
              "https://platform.openai.com/api-keys"),
    _Provider("typesafe", "TypeSafe", "Identity and search judgments", (("TYPESAFE_API_KEY",),),
              "https://console.typesafe.ai/keys"),
    _Provider("modal", "Modal", "Hosted processing", (("MODAL_TOKEN_ID",), ("MODAL_TOKEN_SECRET",)),
              "https://modal.com/settings/tokens"),
    _Provider("rapidapi-twitter", "RapidAPI · Twitter/X", "Twitter/X contact import",
              (("RAPIDAPI_TWITTER_KEY", "RAPIDAPI_KEY"),),
              "https://rapidapi.com/davethebeast/api/twitter241"),
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
                          "credentials": credentials, "connect_url": provider.connect_url})
    return {"providers": providers}

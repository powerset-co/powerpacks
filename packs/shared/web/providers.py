"""Local provider configuration for Settings: read as masked tails, set by typing a key in (written to
the checkout's .env). No API calls."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import write_env
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
    _Provider("rapidapi-linkedin", "RapidAPI · LinkedIn", "Required for LinkedIn profiles and companies without a Powerset key",
              (("RAPIDAPI_LINKEDIN_KEY",),),
              "https://rapidapi.com/pnd-team-pnd-team/api/professional-network-data"),
    _Provider("modal", "Modal", "Hosted processing", (("MODAL_TOKEN_ID",), ("MODAL_TOKEN_SECRET",)),
              "https://modal.com/settings/tokens"),

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
        covered = provider.id == "rapidapi-linkedin" and bool(values.get("POWERSET_API_KEY", "").strip())
        providers.append({"id": provider.id, "name": provider.name + (" (optional)" if covered else ""),
                          "description": "Covered by your Powerset connection" if covered else provider.description,
                          "connected": all(credential["configured"] for credential in credentials),
                          "credentials": credentials, "connect_url": provider.connect_url})
    return {"providers": providers}


# Every credential a provider row shows, the only names Settings may set.
NAMES = frozenset(name for provider in _PROVIDERS for names in provider.credentials for name in names)


def save(root: Path, payload: object) -> dict[str, object]:
    """Writes the typed keys (`{"values": {NAME: value}}`) to the checkout's .env and answers the rows as read
    afterwards. A name that is not a provider credential, or an empty value, is refused."""
    values = payload.get("values") if isinstance(payload, dict) else None
    if not isinstance(values, dict) or not values:
        raise ValueError("values is a map of credential names to keys")
    for name, value in values.items():
        if name not in NAMES:
            raise ValueError(f"{name} is not a provider credential")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} needs a value")
    write_env(root / ".env", {name: value.strip() for name, value in values.items()})
    return read(root)

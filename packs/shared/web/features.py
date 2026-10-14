"""Local desktop feature flags; unavailable or malformed flags are off."""

from dataclasses import asdict, dataclass
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import read_json, write_json

FILE = Path(".powerpacks/desktop/features.json")


@dataclass(frozen=True)
class FeatureFlags:
    sets: bool = False
    auto_reply: bool = False

    @classmethod
    def parse(cls, payload: object) -> "FeatureFlags":
        if (not isinstance(payload, dict) or set(payload) != {"sets", "auto_reply"}
                or any(type(value) is not bool for value in payload.values())):
            raise ValueError("sets and auto_reply must be booleans")
        return cls(**payload)


def read(root: Path) -> FeatureFlags:
    try:
        return FeatureFlags.parse(read_json(root / FILE))
    except ValueError:
        return FeatureFlags()


def save(root: Path, flags: FeatureFlags) -> dict[str, bool]:
    payload = asdict(flags)
    write_json(root / FILE, payload)
    return payload

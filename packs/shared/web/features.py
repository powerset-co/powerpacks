"""Local desktop switches; remote support defaults on for Powerset accounts."""

from dataclasses import asdict, dataclass
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import read_json, write_json
from packs.shared.web import account

FILE = Path(".powerpacks/desktop/features.json")


@dataclass(frozen=True)
class FeatureFlags:
    sets: bool = False
    auto_reply: bool = False
    share: bool = False
    support: bool = False

    @classmethod
    def parse(cls, payload: object, *, support: bool = False) -> "FeatureFlags":
        if (not isinstance(payload, dict) or not {"sets", "auto_reply"} <= set(payload) <= {"sets", "auto_reply", "share", "support"}
                or any(type(value) is not bool for value in payload.values())):
            raise ValueError("sets, auto_reply, share and support must be booleans")
        return cls(**{**payload, "support": payload.get("support", support)})


def read(root: Path) -> FeatureFlags:
    support = account.is_team(account.read(root)["email"])
    try:
        return FeatureFlags.parse(read_json(root / FILE), support=support)
    except ValueError:
        return FeatureFlags(support=support)


def save(root: Path, flags: FeatureFlags) -> dict[str, bool]:
    payload = asdict(flags)
    write_json(root / FILE, payload)
    return payload

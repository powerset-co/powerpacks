"""Local desktop switches; remote support defaults on for Powerset accounts."""

from dataclasses import asdict, dataclass
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import read_json, write_json
from packs.shared.web import account

FILE = Path(".powerpacks/desktop/features.json")


@dataclass(frozen=True)
class FeatureFlags:
    support: bool = False
    # The Team page (remote assist from this machine); off until the owner turns it on in Settings.
    team: bool = False

    @classmethod
    def parse(cls, payload: object, *, support: bool = False) -> "FeatureFlags":
        # "sets", "auto_reply" and "share" are still accepted from an older features.json and ignored: local
        # sets, their auto-replies and the share upload were removed.
        if (not isinstance(payload, dict) or not set(payload) <= {"sets", "auto_reply", "share", "support", "team"}
                or any(type(value) is not bool for value in payload.values())):
            raise ValueError("support and team must be booleans")
        return cls(support=payload.get("support", support), team=payload.get("team", False))


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

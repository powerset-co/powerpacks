"""Build and project the mailbox owner's cache-first LinkedIn context."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.common import (
    CANONICAL_DB,
    OWNER_JSON,
    PROFILE_CACHE_DIR,
    PROFILE_CACHE_TEMPLATE,
    emit,
    load_env,
    normalize_phone,
)
from packs.ingestion.primitives.deep_context.db.models import OwnerContextRow
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.profile_projection import hydrate_profiles
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


def _year(value: Any) -> int | None:
    return value.get("year") if isinstance(value, dict) else None


def owner_from_profile(normalized: dict[str, Any], *, email: str = "") -> dict[str, Any]:
    education = [
        {
            "school": ed.get("school_name") or "",
            "start": _year(ed.get("starts_at")), "end": _year(ed.get("ends_at")),
            "note": " ".join(x for x in (ed.get("degree"), ed.get("field")) if x),
        }
        for ed in normalized.get("education") or []
    ]
    work = [
        {
            "company": ex.get("company_name") or "",
            "title": ex.get("title") or "",
            "start": _year(ex.get("starts_at")), "end": _year(ex.get("ends_at")),
        }
        for ex in normalized.get("experiences") or []
    ]
    location = normalized.get("location_str") or ", ".join(
        x for x in [normalized.get("city"), normalized.get("state"), normalized.get("country")] if x)
    return {
        "name": normalized.get("full_name") or "",
        "emails": [email] if email else [],
        "phones": [],
        "education": [e for e in education if e["school"]],
        "work": [w for w in work if w["company"]],
        "locations": [location] if location else [],
        "notes": normalized.get("headline") or "",
    }


def harvest_owner_phones(chat_db: Path | None = None) -> list[str]:
    """Read only the owner's phone identifiers from iMessage account metadata."""
    chat_db = chat_db if chat_db is not None else Path.home() / "Library/Messages/chat.db"
    if not chat_db.exists():
        return []
    return list(dict.fromkeys(
        phone for value in chatdb.owner_phone_identifiers(chat_db)
        if (phone := normalize_phone(value))
    ))


class BuildOwnerManifest(StageManifest):
    source: str = "build_owner"
    path: str | None = None
    name: str | None = None
    schools: list[Any] | None = None
    employers: list[Any] | None = None
    hint: str | None = None
    error: str | None = None
    from_cache: bool | None = None
    locations: list[Any] | None = None
    updated_at: str | None = None


class BuildOwner(Node):
    """Write owner.json and its complete SQLite projection."""

    name = "deep_owner"
    inputs = (Artifact(path=PROFILE_CACHE_TEMPLATE, external=True, required=False),)
    outputs = (Artifact(path=str(OWNER_JSON), writes="full_rewrite"),)
    payload = BuildOwnerManifest
    manifest = ""

    def __init__(
        self,
        *,
        linkedin_url: str = "",
        email: str = "",
        profile_cache_dir: Path | None = None,
        out: Path | None = None,
        db: Db | None = None,
        db_path: Path = CANONICAL_DB,
        force: bool = False,
    ) -> None:
        self.linkedin_url = linkedin_url
        self.email = email
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.out = Path(out or OWNER_JSON)
        self.db = db
        self.db_path = Path(db_path)
        self.force = force

    def _project(self, owner: dict[str, Any], content: bytes) -> None:
        database = self.db or Db(self.db_path)
        database.project_rows((OwnerContextRow(
            "owner", json.dumps(owner, separators=(",", ":"), ensure_ascii=False),
            str(self.out), hashlib.sha256(content).hexdigest(), now_iso(),
        ),))

    def bindings(self) -> dict[str, str]:
        return {
            PROFILE_CACHE_TEMPLATE: str(self.profile_cache_dir / "{public_identifier}.json"),
            str(OWNER_JSON): str(self.out),
        }

    def execute(self) -> BuildOwnerManifest:
        if self.out.exists() and not self.force:
            try:
                content = self.out.read_bytes()
            except OSError as exc:
                return BuildOwnerManifest(
                    status="error",
                    path=str(self.out),
                    error=f"could not read owner.json: {exc}",
                )
            try:
                parsed = json.loads(content)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                return BuildOwnerManifest(
                    status="error",
                    path=str(self.out),
                    error=f"owner.json is invalid: {exc}",
                )
            if not isinstance(parsed, dict):
                return BuildOwnerManifest(
                    status="error",
                    path=str(self.out),
                    error="owner.json must contain a JSON object",
                )
            existing = parsed
            self._project(existing, content)
            return BuildOwnerManifest(
                status="exists", path=str(self.out),
                name=existing.get("name", ""),
                schools=[e.get("school") for e in existing.get("education", [])],
                employers=[w.get("company") for w in existing.get("work", [])],
                hint="pass --force to rebuild, or --linkedin-url to point at a different profile",
            )

        url = normalize_linkedin_url(self.linkedin_url or "")
        pub = extract_public_identifier(url).lower()
        if not pub:
            return BuildOwnerManifest(
                status="error", error="no --linkedin-url given (your own LinkedIn) and owner.json not present",
            )

        load_env()
        _, profiles = hydrate_profiles(
            [{"public_identifier": pub, "linkedin_url": url}],
            self.profile_cache_dir,
        )
        result = profiles.get(pub, {})
        normalized = result.get("normalized_profile") or {}
        if normalized.get("success") is not True:
            return BuildOwnerManifest(
                status="error", error=result.get("detail") or "could not fetch the owner profile (set RAPIDAPI_KEY?)",
            )

        owner = owner_from_profile(normalized, email=self.email)
        if self.out.exists():
            try:
                previous = json.loads(self.out.read_bytes())
                previous = previous if isinstance(previous, dict) else {}
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                previous = {}
            for field in ("emails", "phones"):
                values = owner.setdefault(field, [])
                values.extend(value for value in previous.get(field) or [] if value and value not in values)
        phones = owner["phones"]
        phones.extend(phone for phone in harvest_owner_phones() if phone not in phones)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(owner, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.out.write_bytes(content)
        self._project(owner, content)
        return BuildOwnerManifest(
            status="written", path=str(self.out), from_cache=bool(result.get("from_cache")),
            name=owner["name"], schools=[e["school"] for e in owner["education"]],
            employers=[w["company"] for w in owner["work"]], locations=owner["locations"],
            updated_at=now_iso(),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build owner.json (your bio) from your LinkedIn, cache-first.")
    parser.add_argument("--linkedin-url", default="", help="The OWNER's LinkedIn URL (you)")
    parser.add_argument("--email", default="", help="The owner's email (for owner identity)")
    parser.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    parser.add_argument("--out", default=str(OWNER_JSON))
    parser.add_argument("--db", default=str(CANONICAL_DB))
    parser.add_argument("--force", action="store_true", help="Rebuild even if owner.json exists")
    args = parser.parse_args(argv)
    payload = BuildOwner(
        linkedin_url=args.linkedin_url,
        email=args.email,
        profile_cache_dir=Path(args.profile_cache_dir),
        out=Path(args.out),
        db_path=Path(args.db),
        force=args.force,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""[Context, step 0] Build the mailbox owner's profile (owner.json) from THEIR LinkedIn.

owner.json is the user's own bio timeline — schools/jobs/locations with year ranges. It is
injected as a reasoning anchor so synthesis infers SHARED context (same school/employer/era)
with each contact, and so the LinkedIn self-heal judge can weigh overlaps with you. Without it
that whole signal is lost.

This builds it deterministically from the owner's LinkedIn via the RapidAPI cache (cache-first;
a hit costs nothing) — NEVER from a web fetch of linkedin.com, which hallucinates. Run it FIRST.

Changelog:
  2026-07-27 (declared contract): `BuildOwner` is a `pipeline/contract.py:Node`
    ("deep_owner"). The RapidAPI profile cache is a declared EXTERNAL input
    (`PROFILE_CACHE_TEMPLATE` — materialized API responses several nodes
    hydrate opportunistically, no single in-graph producer) and
    `owner.json` the declared output. No manifest file today and none invented
    (`manifest=""`, declaration-only, like persist_review_identities), so every
    mode routes through the node template safely. `run(args)` became
    `execute()` — same flags, same "exists"/"error"/"written" payloads, same
    cache-first gating (a paid RapidAPI fetch still happens ONLY on a cache miss
    after an explicit --linkedin-url), same exit code 0.
  2026-07-23 (audit dedup): now_iso import from common.jsonio instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.discover.messages.extract_imessage import open_sqlite_readonly

from packs.ingestion.primitives.deep_context.common import (
    OWNER_JSON,
    PROFILE_CACHE_DIR,
    PROFILE_CACHE_TEMPLATE,
    emit,
    load_env,
    normalize_phone,
)
from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.enrich.rapidapi_client import PROFILE_ERROR, rapidapi_profile
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


def _year(value: Any) -> int | None:
    return value.get("year") if isinstance(value, dict) else None


def owner_from_profile(normalized: dict[str, Any], *, email: str = "") -> dict[str, Any]:
    """Map a normalized LinkedIn profile into the owner.json schema."""
    education = []
    for ed in normalized.get("education") or []:
        education.append({
            "school": ed.get("school") or ed.get("school_name") or "",
            "start": _year(ed.get("starts_at")), "end": _year(ed.get("ends_at")),
            "note": " ".join(x for x in [ed.get("degree"), ed.get("field")] if x),
        })
    work = []
    for ex in normalized.get("experiences") or []:
        work.append({
            "company": ex.get("company_name") or ex.get("company") or "",
            "title": ex.get("title") or "",
            "start": _year(ex.get("starts_at")), "end": _year(ex.get("ends_at")),
        })
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
    """The owner's OWN phone numbers, straight from the SOURCE: iMessage
    chat.db account metadata — `chat.account_login` P:-prefixed logins plus
    `destination_caller_id` on received messages. Metadata columns only, never
    message bodies. Downstream identifier policy drops these from every
    CONTACT's reachability.

    Deliberately no other source: derived contact CSVs are a name heuristic,
    and the wacli stores offer nothing reliable (the session store's
    paired-device JID is empty except while paired, and the message store's
    `from_me` sender rows carry dozens of other people's JIDs through group
    attribution)."""
    chat_db = chat_db if chat_db is not None else Path.home() / "Library/Messages/chat.db"
    if not chat_db.exists():
        return []
    phones: list[str] = []
    try:
        with closing(open_sqlite_readonly(chat_db)) as conn:
            for (login,) in conn.execute("SELECT DISTINCT account_login FROM chat"):
                value = str(login or "")
                if value.startswith("P:"):
                    phone = normalize_phone(value[2:])
                    if phone and phone not in phones:
                        phones.append(phone)
            rows = conn.execute(
                "SELECT DISTINCT destination_caller_id FROM message "
                "WHERE is_from_me = 0 AND destination_caller_id LIKE '+%'")
            for (caller_id,) in rows:
                phone = normalize_phone(str(caller_id or ""))
                if phone and phone not in phones:
                    phones.append(phone)
    except (sqlite3.Error, OSError):
        return phones
    return phones


class BuildOwnerManifest(StageManifest):
    """Typed payload — the union of the three raw dict shapes ("exists" / "error" /
    "written"); None-valued optionals drop in `to_payload()` so each mode emits
    exactly today's keys."""
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
    """Builds owner.json (your bio timeline) from your LinkedIn, cache-first.

    Statuses are the pre-contract strings ("exists"/"error"/"written"), never
    "completed", so the template's output verification intentionally does not
    fire — the durable output is owner.json itself."""

    name = "deep_owner"
    inputs = (
        Artifact(path=PROFILE_CACHE_TEMPLATE, external=True, required=False),
    )
    outputs = (
        Artifact(path=str(OWNER_JSON), writes="full_rewrite"),
    )
    payload = BuildOwnerManifest
    # Declaration-only node: no manifest file today, and none invented — the
    # payload is emitted by the CLI and the durable output is owner.json.
    manifest = ""

    def __init__(
        self,
        *,
        linkedin_url: str = "",
        email: str = "",
        profile_cache_dir: Path | None = None,
        out: Path | None = None,
        force: bool = False,
    ) -> None:
        self.linkedin_url = linkedin_url
        self.email = email
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.out = Path(out or OWNER_JSON)
        self.force = force

    def bindings(self) -> dict[str, str]:
        return {
            PROFILE_CACHE_TEMPLATE: str(self.profile_cache_dir / "{public_identifier}.json"),
            str(OWNER_JSON): str(self.out),
        }

    def execute(self) -> BuildOwnerManifest:
        if self.out.exists() and not self.force:
            try:
                existing = json.loads(self.out.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                existing = {}
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
                status="error",
                error="no --linkedin-url given (your own LinkedIn) and owner.json not present",
            )

        load_env()
        # ONE client call: cache-vs-fetch resolution lives inside get_profile.
        result = rapidapi_profile(pub, url, cache_dir=self.profile_cache_dir)
        from_cache = bool(result.get("from_cache"))
        normalized = result.get("normalized_profile") or {}
        if result["state"] == PROFILE_ERROR or normalized.get("success") is not True:
            return BuildOwnerManifest(
                status="error",
                error=result.get("detail") or "could not fetch the owner profile (set POWERSET_API_KEY?)",
            )

        owner = owner_from_profile(normalized, email=self.email)
        # Preserve augmentations a rebuild must not lose (msgvault adds emails;
        # phones may be hand-set), then harvest own phones from the message
        # stores' self-rows so the identifier policy can drop them everywhere.
        if self.out.exists():
            try:
                previous = json.loads(self.out.read_text(encoding="utf-8")) or {}
            except (json.JSONDecodeError, OSError):
                previous = {}
            for field in ("emails", "phones"):
                for value in previous.get(field) or []:
                    if value and value not in owner.setdefault(field, []):
                        owner[field].append(value)
        for phone in harvest_owner_phones():
            if phone not in owner["phones"]:
                owner["phones"].append(phone)
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(json.dumps(owner, indent=2) + "\n", encoding="utf-8")
        return BuildOwnerManifest(
            status="written", path=str(self.out), from_cache=from_cache,
            name=owner["name"], schools=[e["school"] for e in owner["education"]],
            employers=[w["company"] for w in owner["work"]], locations=owner["locations"],
            updated_at=now_iso(),
        )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build owner.json (your bio) from your LinkedIn, cache-first.")
    p.add_argument("--linkedin-url", default="", help="The OWNER's LinkedIn URL (you)")
    p.add_argument("--email", default="", help="The owner's email (for owner identity)")
    p.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    p.add_argument("--out", default=str(OWNER_JSON))
    p.add_argument("--force", action="store_true", help="Rebuild even if owner.json exists")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = BuildOwner(
        linkedin_url=args.linkedin_url,
        email=args.email,
        profile_cache_dir=Path(args.profile_cache_dir),
        out=Path(args.out),
        force=args.force,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    sys.exit(main())

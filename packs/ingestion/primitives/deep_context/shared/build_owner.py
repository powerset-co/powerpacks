"""Build and project the mailbox owner's cache-first LinkedIn context."""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from pathlib import Path

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.shared.common import (
    CANONICAL_DB,
    OWNER_JSON,
    PROFILE_CACHE_DIR,
    PROFILE_CACHE_TEMPLATE,
    emit,
    load_env,
    normalize_phone,
)
from packs.ingestion.primitives.deep_context.db.models import (
    OwnerContextRow,
    OwnerEducation,
    OwnerProfile,
    OwnerWork,
)
from packs.ingestion.primitives.deep_context.db.store import Db, open_existing_db
from packs.ingestion.primitives.deep_context.enrich.profiles.models import (
    NormalizedProfile,
    ProfileResult,
    ProfileTarget,
)
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import hydrate_profiles
from packs.ingestion.primitives.deep_context.manifests.build_owner_manifest import (
    BuildOwnerManifest,
)
from packs.ingestion.primitives.discover.messages import chatdb
from packs.ingestion.primitives.pipeline.contract import Artifact, Node
from packs.ingestion.schemas.people_schema import extract_public_identifier, normalize_linkedin_url


def owner_from_profile(normalized: NormalizedProfile, *, email: str = "") -> OwnerProfile:
    education = tuple(
        OwnerEducation(
            ed.school_name or "",
            ed.starts_at,
            ed.ends_at,
            " ".join(value for value in (ed.degree, ed.field) if value),
        )
        for ed in normalized.education
        if ed.school_name
    )
    work = tuple(
        OwnerWork(ex.company_name or "", ex.title or "", ex.starts_at, ex.ends_at)
        for ex in normalized.experiences
        if ex.company_name
    )
    return OwnerProfile(
        normalized.full_name or "",
        (email,) if email else (),
        education=education,
        work=work,
        locations=(normalized.location,) if normalized.location else (),
        notes=normalized.headline or "",
    )


def _owner_payload(owner: OwnerProfile) -> dict[str, object]:
    return asdict(owner)


def _owner_from_payload(payload: dict[str, object]) -> OwnerProfile:
    return OwnerProfile(
        str(payload.get("name") or ""),
        tuple(str(value) for value in payload.get("emails") or ()),
        tuple(str(value) for value in payload.get("phones") or ()),
        tuple(
            OwnerEducation(
                str(row.get("school") or ""),
                row.get("start"),
                row.get("end"),
                str(row.get("note") or ""),
            )
            for row in payload.get("education") or ()
            if isinstance(row, dict)
        ),
        tuple(
            OwnerWork(
                str(row.get("company") or ""),
                str(row.get("title") or ""),
                row.get("start"),
                row.get("end"),
            )
            for row in payload.get("work") or ()
            if isinstance(row, dict)
        ),
        tuple(str(value) for value in payload.get("locations") or ()),
        str(payload.get("notes") or ""),
        str(payload.get("linkedin_url") or ""),
    )


def harvest_owner_phones(chat_db: Path | None = None) -> list[str]:
    """Read only the owner's phone identifiers from iMessage account metadata.

    LinkedIn never supplies a phone, so this is the only source for owner.phones —
    compose_dossier later uses it to exclude the owner's own numbers from a
    contact's rendered identifiers.
    """
    chat_db = chat_db if chat_db is not None else Path.home() / "Library/Messages/chat.db"
    if not chat_db.exists():
        return []
    return list(
        dict.fromkeys(phone for value in chatdb.owner_phone_identifiers(chat_db) if (phone := normalize_phone(value)))
    )


class BuildOwner(Node):
    """Write owner.json and its complete SQLite projection.

    Downstream, the owner profile is required, not optional: selection.build_system_prompt
    (synthesis) raises without one, and every enrich identity judge (reconcile_linkedin,
    identity_reconcile, research_reconcile) anchors its overlap inference on it via
    dossier_evidence.owner_background. Consumers read the SQLite projection this class
    writes, not owner.json itself — see OWNER_JSON in shared/common.py.
    """

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
        db: Db,
        force: bool = False,
    ) -> None:
        self.linkedin_url = linkedin_url
        self.email = email
        self.profile_cache_dir = Path(profile_cache_dir or PROFILE_CACHE_DIR)
        self.out = Path(out or OWNER_JSON)
        self.db = db
        self.force = force

    def _project(self, owner: OwnerProfile, content: bytes) -> None:
        self.db.project_rows(
            (
                OwnerContextRow(
                    "owner",
                    json.dumps(_owner_payload(owner), separators=(",", ":"), ensure_ascii=False),
                    str(self.out),
                    hashlib.sha256(content).hexdigest(),
                    now_iso(),
                ),
            )
        )

    def bindings(self) -> dict[str, str]:
        return {
            PROFILE_CACHE_TEMPLATE: str(self.profile_cache_dir / "{public_identifier}.json"),
            str(OWNER_JSON): str(self.out),
        }

    def execute(self) -> BuildOwnerManifest:
        # An existing owner.json is trusted as-is, however old — there is no freshness
        # check against LinkedIn. Staleness is invisible; only --force re-fetches.
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
            existing = _owner_from_payload(parsed)
            self._project(existing, content)
            return BuildOwnerManifest(
                status="exists",
                path=str(self.out),
                name=existing.name,
                schools=[item.school for item in existing.education],
                employers=[item.company for item in existing.work],
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
        hydrated = hydrate_profiles(
            [ProfileTarget(pub, url)],
            self.profile_cache_dir,
        )
        result: ProfileResult | None = hydrated.profiles.get(pub)
        if result is None or not result.normalized_profile.success:
            return BuildOwnerManifest(
                status="error",
                error=(result.detail if result else None) or "could not fetch the owner profile (set RAPIDAPI_KEY?)",
            )

        owner = owner_from_profile(result.normalized_profile, email=self.email)
        # The owner's LinkedIn is identity data the pipeline asks for at every
        # fresh run — persist it, don't only use it to locate the cache entry.
        owner = replace(owner, linkedin_url=url)
        if self.out.exists():
            try:
                previous = json.loads(self.out.read_bytes())
                previous = _owner_from_payload(previous) if isinstance(previous, dict) else OwnerProfile("")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                # Degrades rather than raising: a corrupt previous owner.json silently
                # loses whatever emails/phones it had accumulated, instead of blocking
                # the rebuild or surfacing the loss.
                previous = OwnerProfile("")
            owner = replace(
                owner,
                emails=tuple(dict.fromkeys((*owner.emails, *previous.emails))),
                phones=tuple(dict.fromkeys((*owner.phones, *previous.phones))),
            )
        owner = replace(
            owner,
            phones=tuple(dict.fromkeys((*owner.phones, *harvest_owner_phones()))),
        )
        self.out.parent.mkdir(parents=True, exist_ok=True)
        content = (json.dumps(_owner_payload(owner), indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        self.out.write_bytes(content)
        self._project(owner, content)
        return BuildOwnerManifest(
            status="written",
            path=str(self.out),
            from_cache=bool(result.from_cache),
            name=owner.name,
            schools=[item.school for item in owner.education],
            employers=[item.company for item in owner.work],
            locations=list(owner.locations),
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
        db=open_existing_db(args.db),
        force=args.force,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

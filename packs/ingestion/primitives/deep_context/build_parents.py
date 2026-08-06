"""Build PARENT (canonical-person) dossiers from merge clusters.

A parent is a derived layer above the per-person child dossiers: for each merge
cluster (≥2 candidate records that look like the same person), it merges the
children's facts into one canonical profile and links to them as PROPOSED
children. Each child dossier gets a backref to its parent. Nothing is destroyed —
parents = f(children), children = f(messages) — so every level is repeatable and
re-running rebuilds parents idempotently.

This step is deterministic and free (it reuses the message-derived facts already
synthesized). Use the planned `--judge` LLM pass later to confirm/demote weak,
name-only candidates; here all cluster members are listed as *proposed*.

Outputs:
  parents/<slug>.md     one merged canonical dossier per cluster
  (backrefs injected into each child dossier; `parents` written to index.json)
  parents/manifest.json

This stage owns exactly ONE index.json key — `parents` — and never touches `slugs`
(compose_dossier owns that). The by_email/by_phone/by_name maps are re-derived from
both record maps on write; see the index contract in `common.py`.

Changelog:
  2026-07-30: the parent-slug artifact migration (the pre-2026-07-27 slug scheme)
    moved to the one cope-with-old-installs home, `common/legacy.py`, dated with
    its removal condition; this stage now calls it with explicit paths. Pure move
    — no behavior change.
  2026-07-27 (declared contract): `BuildParents` is a `pipeline/contract.py:Node`.
    Its per-person reads are declared as the shared `{person_id}`/`{slug}` templates,
    `index.json` is declared with `owns_columns=("parents",)` against
    compose_dossier's `("slugs",)`, the owner-alias fold declares its `owner.json`
    write, and the manifest goes through the Node template (same keys, plus the
    declared `fingerprints` block; `updated_at` is now stamped by the manifest
    writer rather than carried in the payload). `run(args)` became `execute()`;
    same flags, same file outputs.
  2026-07-27: parent slugs use eight actual parent-id digest characters; unchanged
    parent IDs migrate exact slug-keyed artifacts in place before index replacement.
  2026-07-24: writes index.json through common.write_index and no longer appends to
    the by_* maps it does not own. A parent's emails/phones come from
    common.parent_identifiers (the union of its children's index records), the same
    projection the lookup maps use.
  2026-07-23 (audit dedup): now_iso, write_json import from common.jsonio; normalize_email imports from common.contact_fields instead of deep_context.common (deduped there); no behavior change.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context import compose_dossier as compose
from packs.ingestion.primitives.deep_context.common import (
    DEEP_RESEARCH_DIR,
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    DOSSIER_TEMPLATE,
    emit,
    ensure_no_review_session,
    FACTS_DIR,
    FACTS_TEMPLATE,
    INDEX_JSON,
    LINKEDIN_OVERRIDES_CSV,
    load_index,
    load_owner,
    MERGE_CSV,
    OWNER_JSON,
    parent_identifiers,
    PARENT_TEMPLATE,
    PARENTS_DIR,
    PARENTS_MANIFEST,
    RAW_BUNDLE_TEMPLATE,
    RAW_DIR,
    read_jsonl,
    RECONCILE_DIR,
    ROOT,
    slugify,
    VERDICTS_CSV,
    VERDICTS_JSONL,
    write_index,
)
from packs.ingestion.primitives.common.jsonio import now_iso, write_json
from packs.ingestion.primitives.common.contact_fields import normalize_email, normalize_phone
from packs.ingestion.primitives.common.legacy import (
    migrate_parent_slug_artifacts,
    parent_slug_migrations,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest
from packs.ingestion.primitives.deep_context.db import views
from packs.ingestion.primitives.deep_context.db.models import (
    CanonicalGraphProjection,
    IdentifierKind,
    ParentRow,
    PersonIdentifierRow,
    PersonRow,
    PersonSourceRow,
    ReviewSource,
)
from packs.ingestion.primitives.deep_context.db.store import Db

PARENT_ANCHOR = "<!-- parent-link -->"
SYNTHETIC_PEOPLE_CSV = LINKEDIN_OVERRIDES_CSV.parent / "synthetic-people.csv"
CANONICAL_DB = ROOT / "deep-context.sqlite"


def fold_owner_aliases(owner_slugs: set[str], slugs_info: dict[str, Any], raw_dir: Path) -> list[str]:
    """Union the owner's alias emails (from the excluded is_owner people) into owner.json, so the
    owner's own addresses are known directly on future runs. Returns the newly-added emails."""
    owner = load_owner() or {}
    if not owner:
        return []
    existing = [normalize_email(e) for e in (owner.get("emails") or [])]
    added: list[str] = []
    for slug in owner_slugs:
        pid = slugs_info.get(slug, {}).get("person_id", "")
        bundle = _read_json(raw_dir / f"{pid}.json") if pid else {}
        for e in bundle.get("emails") or []:
            ne = normalize_email(e)
            if ne and "@" in ne and ne not in existing and ne not in added:
                added.append(ne)
    if added:
        owner["emails"] = (owner.get("emails") or []) + added
        write_json(OWNER_JSON, owner)
    return added


def load_pairs(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def superseded_pairs(people_csv: Path, slugs_info: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic same-person pairs from import's own witness: a matched
    people.csv row lists the candidate person-id(s) it superseded (the same
    contact row under its pre-match key). Both identities' dossier slugs join
    one cluster at confidence 1.0 — no judge needed, import SAW they were one
    contact. Ids without a dossier slug are inert (nothing to fold yet)."""
    if not people_csv.exists():
        return []
    slug_by_pid = {str((info or {}).get("person_id") or "").strip().lower(): slug
                   for slug, info in slugs_info.items()}
    out: list[dict[str, Any]] = []
    with people_csv.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("superseded_person_ids") or "").strip()
            if not raw:
                continue
            try:
                superseded = json.loads(raw)
            except json.JSONDecodeError:
                continue
            durable_slug = slug_by_pid.get((row.get("id") or "").strip().lower())
            if not durable_slug:
                continue
            name = (row.get("full_name") or "").strip()
            for pid in superseded if isinstance(superseded, list) else []:
                old_slug = slug_by_pid.get(str(pid or "").strip().lower())
                if old_slug and old_slug != durable_slug:
                    out.append({"slug_a": durable_slug, "name_a": name,
                                "slug_b": old_slug, "name_b": name,
                                "confidence": "1.0", "tone_consistent": "true",
                                "reason": "import-superseded identity: the same "
                                          "contact row under its pre-match key"})
    return out


def clusters_from_pairs(pairs: list[dict[str, Any]]) -> list[list[str]]:
    """Connected components over candidate pairs -> clusters of child slugs."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p in pairs:
        a, b = p["slug_a"], p["slug_b"]
        parent[find(a)] = find(b)
    groups: dict[str, list[str]] = {}
    for slug in list(parent):
        groups.setdefault(find(slug), []).append(slug)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def parent_id_for(child_pids: list[str]) -> str:
    """Stable parent id from the sorted child person-ids (repeatable across runs)."""
    digest = hashlib.sha1("|".join(sorted(child_pids)).encode()).hexdigest()
    return f"parent-{digest[:12]}"


def _child_line(c: dict[str, Any]) -> str:
    score = f" — judge {c['score']:.2f}" if c.get("score") else ""
    reason = f" ({c['reason']})" if c.get("reason") else ""
    chans = ", ".join(c.get("channels") or [])
    return f"- [[{c['slug']}]] **{c['name']}**{score}{reason}  ·  {chans}"


def render_parent(name: str, parent_id: str, slug: str, emails: list[str], phones: list[str],
                  merged: dict[str, Any], confirmed: list[dict[str, Any]], review: list[dict[str, Any]]) -> str:
    lines = [
        "---",
        f"parent_id: {parent_id}",
        f"name: {json.dumps(name, ensure_ascii=False)}",
        f"slug: {slug}",
        "kind: parent",
        f"children: {compose._yaml_list([c['slug'] for c in confirmed])}",
        f"needs_review: {compose._yaml_list([c['slug'] for c in review])}",
        f"emails: {compose._yaml_list(emails)}",
        f"phones: {compose._yaml_list(phones)}",
        f"confidence: {round(float(merged.get('confidence') or 0.0), 2)}",
        f"generated_at: {now_iso()}",
        "---",
        "",
        f"# {name}",
        "",
        "## Summary",
        "",
        compose.headline(merged) or "_Merged from the confirmed records below._",
        "",
        "## Confirmed children (merged)",
        "",
        "_LLM-judged same person; their facts are merged into this profile._",
        "",
        *[_child_line(c) for c in confirmed],
    ]
    if review:
        lines += [
            "", "## Needs review (NOT merged)", "",
            "_Linked only by a borderline judge call — confirm before merging in._", "",
            *[_child_line(c) for c in review],
        ]
    rel = merged.get("relationship_to_owner")
    if rel:
        lines += ["", "## Relationship & cadence", "", rel]
    if merged.get("shared_context"):
        lines += ["", "## Shared context with you", ""]
        for sc in merged["shared_context"]:
            ev = f" — _{sc['evidence']}_" if sc.get("evidence") else ""
            lines.append(f"- **{sc.get('overlap', 'other')}:** {sc['detail']}{ev}")
    who = []
    if merged.get("title"):
        who.append(f"- **Title:** {merged['title']}")
    for emp in merged.get("employers") or []:
        role = f" — {emp['role']}" if emp.get("role") else ""
        who.append(f"- **Employer ({emp.get('status', 'unknown')}):** {emp['name']}{role}")
    if merged.get("school"):
        who.append(f"- **School:** {merged['school']}")
    if merged.get("location"):
        who.append(f"- **Location:** {merged['location']}")
    if who:
        lines += ["", "## Who they are", "", *who]
    if merged.get("topics"):
        lines += ["", "## Topics", "", *(f"- {t}" for t in merged["topics"])]
    if merged.get("notable_events"):
        lines += ["", "## Timeline", ""]
        for ev in merged["notable_events"]:
            lines.append(f"- **{ev.get('date') or '?'}** — {ev['summary']}")
    contact = [f"- {e}" for e in emails] + [f"- {p}" for p in phones]
    if contact:
        lines += ["", "## Identifiers", "", *contact]
    return "\n".join(lines) + "\n"


def render_singleton(name: str, parent_id: str, slug: str, child_slug: str,
                     emails: list[str], phones: list[str], headline: str) -> str:
    """Thin pointer parent for an UNMERGED person — canonical, links to its one child."""
    lines = [
        "---",
        f"parent_id: {parent_id}",
        f"name: {json.dumps(name, ensure_ascii=False)}",
        f"slug: {slug}",
        "kind: parent",
        "singleton: true",
        f"children: {compose._yaml_list([child_slug])}",
        f"emails: {compose._yaml_list(emails)}",
        f"phones: {compose._yaml_list(phones)}",
        f"generated_at: {now_iso()}",
        "---",
        "",
        f"# {name}",
        "",
        f"Single identity — no duplicates detected. Full context in [[{child_slug}]].",
    ]
    if headline:
        lines += ["", headline]
    return "\n".join(lines) + "\n"


def inject_parent_backref(dossier_dir: Path, child_slug: str, parent_slug: str, parent_name: str) -> None:
    """Add/refresh a 'Part of <parent>' line right after the child's H1."""
    path = dossier_dir / f"{child_slug}.md"
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    out = [ln for ln in lines if PARENT_ANCHOR not in ln]  # strip prior backref
    for i, ln in enumerate(out):
        if ln.startswith("# "):
            backref = f"{PARENT_ANCHOR} _Part of [[{parent_slug}]] **{parent_name}** (proposed merge)_"
            out.insert(i + 1, "")
            out.insert(i + 2, backref)
            break
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


class BuildParentsManifest(StageManifest):
    """The stage's typed manifest payload — same keys as the raw dict it replaces
    (`updated_at` is stamped by the manifest writer)."""
    source: str = "build_parents"
    clusters: int = 0
    parents_written: int = 0
    merged_parents: int = 0
    singleton_parents: int = 0
    owner_excluded: int = 0
    owner_aliases_added: list[str] = []
    orphans_removed: int = 0
    # Slug-migration counters (`migrate_parent_slug_artifacts`).
    parent_slug_keys_migrated: int = 0
    parent_slug_directories_renamed: int = 0
    parent_slug_directory_conflicts: int = 0
    parent_slug_csv_rows_rewritten: int = 0
    parent_slug_jsonl_rows_rewritten: int = 0
    # Canonical graph/worth projection counters (legacy names kept in payload).
    worth_parent_rows: int = 0
    worth_human_migrated: int = 0
    worth_legacy_marks_cleared: int = 0
    worth_stale_parent_rows_removed: int = 0
    parents_dir: str = ""
    elapsed_ms: int = 0


class BuildParents(Node):
    """Builds the canonical parent layer from merge clusters. Deterministic and
    free; owns `index.json`'s `parents` key (compose_dossier owns `slugs`)."""

    name = "deep_parents"
    # All optional: an absent merge CSV / people.csv / index simply contributes no
    # clusters, which is the pre-cluster pipeline state rather than an error.
    inputs = (
        Artifact(path=str(MERGE_CSV), required=False),
        Artifact(path=str(DEFAULT_PEOPLE_CSV), required=False),
        Artifact(path=str(INDEX_JSON), required=False),
        Artifact(path=FACTS_TEMPLATE, required=False),
        Artifact(path=RAW_BUNDLE_TEMPLATE, required=False),
        # Child dossiers are read to inject the parent backref (see the note on
        # outputs below); owner.json is read by the alias fold.
        Artifact(path=DOSSIER_TEMPLATE, required=False),
        Artifact(path=str(OWNER_JSON), required=False),
    )
    # NOT declared: the parent-backref line this stage injects into each CHILD
    # dossier (`inject_parent_backref`). compose_dossier declares `{slug}.md` as a
    # whole-file output, so a second writer would report a two-writer conflict that
    # only the graph's owner can resolve — the annotation is an anchored line edit,
    # not a rewrite, and is called out here rather than declared silently.
    # ALSO not declared: `fold_owner_aliases` appends the owner's alias addresses
    # to owner.json's `emails` key (module constant, not rebindable, no-op unless
    # owner slugs exist). build_owner rewrites the whole file, so declaring this
    # annotate-write would pin a permanent two-writer conflict AND a
    # cluster->parents->cluster cycle over a known, deliberate product behavior
    # (`bin/deep-context owner --force` dropping folded aliases). Same treatment
    # as the child-dossier annotation above: documented here, not declared.
    outputs = (
        Artifact(path=PARENT_TEMPLATE, writes="full_rewrite", required=False),
        # `feedback=True`: within one pass cluster runs BEFORE parents, so
        # cluster's read of the index `parents` key is always the PREVIOUS
        # round's fold state — a cross-iteration edge, like persist's
        # directory.csv slice. Same-run consumers (reconcile) read the parent
        # dossiers themselves, which stay a forward edge.
        Artifact(path=str(INDEX_JSON), writes="upsert", owns_columns=("parents",), feedback=True),
    )
    payload = BuildParentsManifest
    manifest = str(PARENTS_MANIFEST)

    def __init__(
        self,
        *,
        db: Db,
        merge_csv: Path | None = None,
        people_csv: Path | None = None,
        index_json: Path | None = None,
        dossier_dir: Path | None = None,
        facts_dir: Path | None = None,
        raw_dir: Path | None = None,
        parents_dir: Path | None = None,
        review_csv: Path | str | None = LINKEDIN_OVERRIDES_CSV,
        confirm_threshold: float = 0.85,
    ) -> None:
        self.db = db
        self.merge_csv = Path(merge_csv or MERGE_CSV)
        self.people_csv = Path(people_csv or DEFAULT_PEOPLE_CSV)
        self.index_json = Path(index_json or INDEX_JSON)
        self.dossier_dir = Path(dossier_dir or DOSSIER_DIR)
        self.facts_dir = Path(facts_dir or FACTS_DIR)
        self.raw_dir = Path(raw_dir or RAW_DIR)
        self.parents_dir = Path(parents_dir or PARENTS_DIR)
        # Retained as an accepted legacy CLI option; review.csv is an export
        # baton and does not participate in canonical parent construction.
        review_value = str(review_csv or "").strip()
        self.review_csv = Path(review_value) if review_value else None
        # Kept because `--confirm-threshold` is a public CLI flag; no longer read —
        # every clustered member is folded in as a child (see the comment in the
        # cluster loop), so there is no needs-review split left to threshold.
        self.confirm_threshold = confirm_threshold

    def bindings(self) -> dict[str, str]:
        return {
            str(MERGE_CSV): str(self.merge_csv),
            str(DEFAULT_PEOPLE_CSV): str(self.people_csv),
            str(INDEX_JSON): str(self.index_json),
            FACTS_TEMPLATE: str(self.facts_dir / "{person_id}.jsonl"),
            RAW_BUNDLE_TEMPLATE: str(self.raw_dir / "{person_id}.json"),
            DOSSIER_TEMPLATE: str(self.dossier_dir / "{slug}.md"),
            PARENT_TEMPLATE: str(self.parents_dir / "{slug}.md"),
            self.manifest: str(self.parents_dir / "manifest.json"),
        }

    def execute(self) -> BuildParentsManifest:
        started = time.monotonic()
        index = load_index(self.index_json)
        old_parents = dict(index.get("parents") or {})
        slugs_info = index.get("slugs", {})
        pairs = load_pairs(self.merge_csv)
        pairs += superseded_pairs(self.people_csv, slugs_info)
        score_by_pair = {tuple(sorted((p["slug_a"], p["slug_b"]))): p for p in pairs}
        clusters = clusters_from_pairs(pairs)

        parents_dir = self.parents_dir
        parents_dir.mkdir(parents=True, exist_ok=True)
        dossier_dir = self.dossier_dir
        facts_dir = self.facts_dir
        raw_dir = self.raw_dir

        index["parents"] = {}  # authoritative for this run (don't accumulate stale clusters)
        written = 0
        singletons = 0
        written_slugs: set[str] = set()
        clustered_slugs: set[str] = set()
        # The mailbox owner shows up as a "contact" when they email from another address (synthesis
        # flags it is_owner). They are YOU, not a contact — never make them a parent.
        owner_slugs = {slug for slug, info in slugs_info.items()
                       if _is_owner(info.get("person_id", ""), facts_dir)}
        owner_aliases_added = fold_owner_aliases(owner_slugs, slugs_info, raw_dir) if owner_slugs else []
        owner_excluded = 0
        projected_parents: list[ParentRow] = []
        projected_people: list[PersonRow] = []
        projected_identifiers: dict[tuple[str, str, str], PersonIdentifierRow] = {}
        projected_sources: dict[tuple[str, str], PersonSourceRow] = {}
        existing_people = {
            row["person_id"]: row
            for row in self.db.query("SELECT * FROM people")
        }

        def project_member(
            child_slug: str, parent_id: str, parent_slug: str, *, is_owner: bool = False,
        ) -> None:
            info = slugs_info[child_slug]
            person_id = str(info.get("person_id") or "").strip().lower()
            prior = existing_people.get(person_id)
            projected_people.append(PersonRow(
                person_id, parent_id, child_slug, parent_slug,
                str(info.get("name") or info.get("full_name") or child_slug),
                int(is_owner or (prior["is_owner"] if prior else 0)),
                int(prior["is_ghost"] if prior else 0),
                updated_at=now_iso(),
            ))
            for kind, values, normalize in (
                (IdentifierKind.EMAIL.value, info.get("emails") or [], normalize_email),
                (IdentifierKind.PHONE.value, info.get("phones") or [], normalize_phone),
            ):
                for value in values:
                    display = str(value or "").strip()
                    normalized = normalize(display)
                    if normalized:
                        projected_identifiers[(person_id, kind, normalized)] = PersonIdentifierRow(
                            person_id, kind, normalized, display,
                        )
            bundle = _read_json(raw_dir / f"{person_id}.json")
            for source in bundle.get("source_channels") or []:
                source = str(source or "").strip()
                if source:
                    projected_sources[(person_id, source)] = PersonSourceRow(person_id, source)

        def _pscore(row: dict[str, Any]) -> float:
            return float(row.get("confidence") or row.get("score") or 0)

        for cluster in clusters:
            members = [s for s in cluster if s in slugs_info and s not in owner_slugs]
            if len(members) < 2:
                continue

            # Best judge confidence linking each member to the rest of the cluster.
            def best_conf(slug: str) -> float:
                return max((_pscore(score_by_pair[tuple(sorted((slug, o)))])
                            for o in members if o != slug and tuple(sorted((slug, o))) in score_by_pair), default=0.0)

            member_conf = {s: best_conf(s) for s in members}
            # No needs_review limbo. Every clustered member is folded into the parent as a child
            # (defaulted in), carrying its merge confidence — a human rejects the rare wrong one in
            # the review UI. The old split hid low-confidence members entirely: they appeared in no
            # parent's children, so reconcile never judged them and they vanished from the UI.
            confirmed_slugs = list(members)
            review_slugs: list[str] = []

            def child_entry(slug: str, status: str) -> dict[str, Any]:
                info = slugs_info[slug]
                bundle = _read_json(raw_dir / f"{info['person_id']}.json")
                reason = next((score_by_pair[tuple(sorted((slug, o)))]["reason"]
                               for o in members if o != slug and tuple(sorted((slug, o))) in score_by_pair), "")
                return {"slug": slug, "name": info.get("name", slug), "score": member_conf[slug],
                        "reason": reason, "channels": bundle.get("source_channels") or [], "status": status,
                        "pid": info["person_id"]}

            confirmed = [child_entry(s, "confirmed") for s in confirmed_slugs]
            review = [child_entry(s, "needs_review") for s in review_slugs]

            # Merge facts from CONFIRMED children only; needs-review are listed, not merged.
            # Identity comes from the children's index records (one projection, shared with
            # the derived lookup maps), so the parent dossier and index never disagree.
            all_records: list[dict[str, Any]] = []
            child_pids: list[str] = []
            for c in confirmed:
                child_pids.append(c["pid"])
                all_records.extend(read_jsonl(facts_dir / f"{c['pid']}.jsonl"))
            emails, phones = parent_identifiers(index, [c["slug"] for c in confirmed])

            merged = compose.merge_facts(all_records)
            name = merged.get("canonical_name") or confirmed[0]["name"]
            parent_id = parent_id_for(child_pids)
            slug = slugify(name, parent_id)
            projected_parents.append(ParentRow(
                parent_id, f"parent-worth:{parent_id}", name, slug,
                source=ReviewSource.PARENT_WORTH.value, updated_at=now_iso(),
            ))
            for child in confirmed:
                project_member(child["slug"], parent_id, slug)
            (parents_dir / f"{slug}.md").write_text(
                render_parent(name, parent_id, slug, emails, phones, merged, confirmed, review), encoding="utf-8")
            written += 1
            written_slugs.add(slug)

            for c in confirmed + review:
                inject_parent_backref(dossier_dir, c["slug"], slug, name)
                clustered_slugs.add(c["slug"])

            index["parents"][slug] = {"parent_id": parent_id, "name": name, "path": f"parents/{slug}.md",
                                      "children": [c["slug"] for c in confirmed],
                                      "needs_review": [c["slug"] for c in review]}

        # Promote every UNMERGED person to a thin singleton parent (a pointer to its one
        # child), so `parents/` is ALWAYS the COMPLETE canonical layer: exactly one parent
        # per real person. Idempotent — singleton parent_id is a stable hash of [person_id].
        for child_slug, info in slugs_info.items():
            if child_slug in clustered_slugs:
                continue
            if child_slug in owner_slugs:   # you on another email — not a contact
                owner_excluded += 1
                continue
            pid = info["person_id"]
            name = info.get("name", child_slug)
            emails, phones = parent_identifiers(index, [child_slug])
            parent_id = parent_id_for([pid])
            pslug = slugify(name, parent_id)
            projected_parents.append(ParentRow(
                parent_id, f"parent-worth:{parent_id}", name, pslug,
                source=ReviewSource.PARENT_WORTH.value, updated_at=now_iso(),
            ))
            project_member(child_slug, parent_id, pslug)
            (parents_dir / f"{pslug}.md").write_text(
                render_singleton(name, parent_id, pslug, child_slug, emails, phones, info.get("headline", "")),
                encoding="utf-8")
            written += 1
            singletons += 1
            written_slugs.add(pslug)
            inject_parent_backref(dossier_dir, child_slug, pslug, name)
            index["parents"][pslug] = {"parent_id": parent_id, "name": name, "path": f"parents/{pslug}.md",
                                       "children": [child_slug], "needs_review": [], "singleton": True}

        # Owner aliases remain absent from the user-facing parent files/index,
        # but SQLite's canonical graph must still own every projected person so
        # existing facts and artifacts retain valid foreign keys.
        for child_slug in sorted(owner_slugs):
            info = slugs_info[child_slug]
            person_id = info["person_id"]
            name = info.get("name", child_slug)
            parent_id = parent_id_for([person_id])
            parent_slug = slugify(name, parent_id)
            projected_parents.append(ParentRow(
                parent_id, f"parent-worth:{parent_id}", name, parent_slug,
                source=ReviewSource.PARENT_WORTH.value, updated_at=now_iso(),
            ))
            project_member(child_slug, parent_id, parent_slug, is_owner=True)

        active_real = {row.person_id for row in projected_people}
        new_parent_by_old: dict[str, set[str]] = {}
        for person in projected_people:
            prior = existing_people.get(person.person_id)
            if prior:
                new_parent_by_old.setdefault(prior["parent_id"], set()).add(person.parent_id)
        projected_parent_ids = {row.parent_id for row in projected_parents}
        projected_parent_slugs = {row.parent_id: row.display_slug for row in projected_parents}
        old_parents_by_id = {
            row["parent_id"]: row for row in self.db.query("SELECT * FROM parents")
        }
        for person_id, prior in sorted(existing_people.items()):
            if not prior["is_ghost"] or person_id in active_real:
                continue
            targets = new_parent_by_old.get(prior["parent_id"], set())
            if len(targets) == 1:
                target = next(iter(targets))
            else:
                target = prior["parent_id"]
                if target not in projected_parent_ids:
                    old_parent = old_parents_by_id[target]
                    projected_parents.append(ParentRow(
                        target, old_parent["public_identifier"], old_parent["display_name"],
                        old_parent["display_slug"], old_parent["machine_worth"],
                        old_parent["machine_worth_reason"], old_parent["source"], now_iso(),
                    ))
                    projected_parent_ids.add(target)
                    projected_parent_slugs[target] = old_parent["display_slug"]
            projected_people.append(PersonRow(
                person_id, target, prior["child_slug"], projected_parent_slugs.get(target),
                prior["display_name"], prior["is_owner"], 1,
                prior["facts_json"], prior["confidence"], now_iso(),
            ))

        # Remove orphan parent files from earlier cluster runs (slug set changes when
        # clusters change); the dossier compose does the same for child dossiers.
        orphans = 0
        for md in parents_dir.glob("*.md"):
            if md.stem not in written_slugs:
                md.unlink()
                orphans += 1

        # Migrate exact slug-keyed artifacts BEFORE the index replacement, so an
        # unchanged parent_id keeps its paid deep-research directory and its rows.
        # Cope-with-old-installs code lives in ONE place (`common/legacy.py`),
        # dated with its removal condition; every path is passed explicitly.
        slug_migration = migrate_parent_slug_artifacts(
            parent_slug_migrations(old_parents, index["parents"]),
            deep_research_dir=DEEP_RESEARCH_DIR,
            verdicts_jsonl=VERDICTS_JSONL,
            verdicts_csv=VERDICTS_CSV,
            applied_csv=RECONCILE_DIR / "applied.csv",
            synthetic_people_csv=SYNTHETIC_PEOPLE_CSV,
        )
        write_index(self.index_json, index)
        active_people = {row.person_id for row in projected_people}
        for row in self.db.query(
            "SELECT person_id, kind, normalized_value, display_value "
            "FROM person_identifiers ORDER BY person_id, kind, normalized_value"
        ):
            key = (row["person_id"], row["kind"], row["normalized_value"])
            if row["person_id"] in active_people:
                projected_identifiers.setdefault(key, PersonIdentifierRow(*row))
        for row in self.db.query(
            "SELECT person_id, source FROM person_sources ORDER BY person_id, source"
        ):
            key = (row["person_id"], row["source"])
            if row["person_id"] in active_people:
                projected_sources.setdefault(key, PersonSourceRow(*row))
        prior_human_parents = {
            row["parent_id"] for row in self.db.query(
                "SELECT parent_id FROM parents WHERE human_worth IS NOT NULL"
            )
        }
        parents_removed = 0
        if slugs_info or not existing_people:
            graph_counts = self.db.replace_canonical_graph(CanonicalGraphProjection(
                parents=tuple(projected_parents),
                people=tuple(projected_people),
                identifiers=tuple(
                    projected_identifiers[key] for key in sorted(projected_identifiers)
                ),
                sources=tuple(projected_sources[key] for key in sorted(projected_sources)),
            ))
            parents_removed = graph_counts.parents_removed
        human_migrated = sum(
            row["parent_id"] not in prior_human_parents
            for row in self.db.query(
                "SELECT parent_id FROM parents WHERE human_worth IS NOT NULL"
            )
        )
        worth_sync = {
            "parent_rows": len(views.worth_rows(self.db)),
            "human_migrated": human_migrated,
            "legacy_marks_cleared": 0,
            "stale_parent_rows_removed": parents_removed,
        }
        return BuildParentsManifest(
            status="completed",
            clusters=len(clusters),
            parents_written=written,
            merged_parents=written - singletons,
            singleton_parents=singletons,
            owner_excluded=owner_excluded,
            owner_aliases_added=owner_aliases_added,
            orphans_removed=orphans,
            parent_slug_keys_migrated=slug_migration["keys"],
            parent_slug_directories_renamed=slug_migration["directories_renamed"],
            parent_slug_directory_conflicts=slug_migration["directory_conflicts"],
            parent_slug_csv_rows_rewritten=slug_migration["csv_rows_rewritten"],
            parent_slug_jsonl_rows_rewritten=slug_migration["jsonl_rows_rewritten"],
            worth_parent_rows=worth_sync["parent_rows"],
            worth_human_migrated=worth_sync["human_migrated"],
            worth_legacy_marks_cleared=worth_sync["legacy_marks_cleared"],
            worth_stale_parent_rows_removed=worth_sync["stale_parent_rows_removed"],
            parents_dir=str(parents_dir),
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )


def _is_owner(person_id: str, facts_dir: Path) -> bool:
    """True if synthesis flagged this person as the mailbox owner on another email address."""
    if not person_id:
        return False
    return any((r.get("facts") or {}).get("is_owner")
               for r in read_jsonl(facts_dir / f"{person_id}.jsonl"))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build parent canonical dossiers from merge clusters.")
    p.add_argument("--merge-csv", default=str(MERGE_CSV))
    p.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV),
                   help="Merged people.csv; superseded_person_ids rows fold pre-match identities")
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--parents-dir", default=str(PARENTS_DIR))
    p.add_argument("--db", default=str(CANONICAL_DB),
                   help="Canonical Deep Context SQLite database")
    p.add_argument("--review-csv", default=str(LINKEDIN_OVERRIDES_CSV))
    p.add_argument("--confirm-threshold", type=float, default=0.85,
                   help="Min judge confidence to merge a child into the parent (else listed as needs-review)")
    return p


def main(argv: list[str] | None = None) -> int:
    ensure_no_review_session("build_parents")
    args = build_parser().parse_args(argv)
    payload = BuildParents(
        db=Db(Path(args.db)),
        merge_csv=Path(args.merge_csv),
        people_csv=Path(args.people_csv),
        index_json=Path(args.index_json),
        dossier_dir=Path(args.dossier_dir),
        facts_dir=Path(args.facts_dir),
        raw_dir=Path(args.raw_dir),
        parents_dir=Path(args.parents_dir),
        review_csv=args.review_csv,
        confirm_threshold=args.confirm_threshold,
    ).run()
    emit(payload.to_payload())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

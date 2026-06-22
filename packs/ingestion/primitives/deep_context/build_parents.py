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
  (backrefs injected into each child dossier; parents added to index.json)
  parents/manifest.json
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
    DOSSIER_DIR,
    FACTS_DIR,
    INDEX_JSON,
    MERGE_CSV,
    PARENTS_DIR,
    RAW_DIR,
    emit,
    normalize_name,
    now_iso,
    phone_digits,
    slugify,
    write_json,
)

PARENT_ANCHOR = "<!-- parent-link -->"


def load_pairs(csv_path: Path) -> list[dict[str, Any]]:
    if not csv_path.exists():
        return []
    with csv_path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


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
        f"# {name} (canonical)",
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    index = json.loads(Path(args.index_json).read_text(encoding="utf-8")) if Path(args.index_json).exists() else {}
    slugs_info = index.get("slugs", {})
    pairs = load_pairs(Path(args.merge_csv))
    score_by_pair = {tuple(sorted((p["slug_a"], p["slug_b"]))): p for p in pairs}
    clusters = clusters_from_pairs(pairs)

    parents_dir = Path(args.parents_dir)
    parents_dir.mkdir(parents=True, exist_ok=True)
    dossier_dir = Path(args.dossier_dir)
    facts_dir = Path(args.facts_dir)
    raw_dir = Path(args.raw_dir)

    index.setdefault("parents", {})
    written = 0
    written_slugs: set[str] = set()
    def _pscore(row: dict[str, Any]) -> float:
        return float(row.get("confidence") or row.get("score") or 0)

    for cluster in clusters:
        members = [s for s in cluster if s in slugs_info]
        if len(members) < 2:
            continue

        # Best judge confidence linking each member to the rest of the cluster.
        def best_conf(slug: str) -> float:
            return max((_pscore(score_by_pair[tuple(sorted((slug, o)))])
                        for o in members if o != slug and tuple(sorted((slug, o))) in score_by_pair), default=0.0)

        member_conf = {s: best_conf(s) for s in members}
        confirmed_slugs = [s for s in members if member_conf[s] >= args.confirm_threshold]
        if len(confirmed_slugs) < 2:  # not enough strong links -> treat the cluster as the core
            confirmed_slugs = list(members)
        review_slugs = [s for s in members if s not in confirmed_slugs]

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

        # Merge facts + identity from CONFIRMED children only; needs-review are listed, not merged.
        all_records: list[dict[str, Any]] = []
        emails: list[str] = []
        phones: list[str] = []
        child_pids: list[str] = []
        for c in confirmed:
            child_pids.append(c["pid"])
            all_records.extend(_read_jsonl(facts_dir / f"{c['pid']}.jsonl"))
            bundle = _read_json(raw_dir / f"{c['pid']}.json")
            for e in bundle.get("emails") or []:
                if e not in emails:
                    emails.append(e)
            for ph in bundle.get("phones") or []:
                if ph not in phones:
                    phones.append(ph)

        merged = compose.merge_facts(all_records)
        name = merged.get("canonical_name") or confirmed[0]["name"]
        parent_id = parent_id_for(child_pids)
        slug = slugify(name, parent_id)
        (parents_dir / f"{slug}.md").write_text(
            render_parent(name, parent_id, slug, emails, phones, merged, confirmed, review), encoding="utf-8")
        written += 1
        written_slugs.add(slug)

        for c in confirmed + review:
            inject_parent_backref(dossier_dir, c["slug"], slug, name)

        rel_path = f"parents/{slug}.md"
        index["parents"][slug] = {"parent_id": parent_id, "name": name, "path": rel_path,
                                  "children": [c["slug"] for c in confirmed],
                                  "needs_review": [c["slug"] for c in review]}
        # Make parents resolvable by lookup too.
        for e in emails:
            index.setdefault("by_email", {}).setdefault(e.lower(), [])
            if slug not in index["by_email"][e.lower()]:
                index["by_email"][e.lower()].append(slug)
        for ph in phones:
            d = phone_digits(ph)
            if d:
                index.setdefault("by_phone", {}).setdefault(d, [])
                if slug not in index["by_phone"][d]:
                    index["by_phone"][d].append(slug)
        nk = normalize_name(name)
        if nk:
            index.setdefault("by_name", {}).setdefault(nk, [])
            if slug not in index["by_name"][nk]:
                index["by_name"][nk].append(slug)

    write_json(Path(args.index_json), index)
    manifest = {
        "source": "build_parents",
        "status": "completed",
        "clusters": len(clusters),
        "parents_written": written,
        "parents_dir": str(parents_dir),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }
    write_json(parents_dir / "manifest.json", manifest)
    return manifest


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


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
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--parents-dir", default=str(PARENTS_DIR))
    p.add_argument("--confirm-threshold", type=float, default=0.85,
                   help="Min judge confidence to merge a child into the parent (else listed as needs-review)")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

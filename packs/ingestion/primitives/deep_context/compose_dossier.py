"""[3/4] Compose per-person markdown dossiers + the lookup index (reduce step).

Merges each person's per-chunk facts (from ``synthesize_person_context``) into one
markdown dossier and writes the name/phone/email lookup index. This step is
deterministic and LLM-free: the expensive reasoning already happened in the map
step, so the reduce is a cheap, testable fact-merge + template — keeping local
CPU/memory trivial. The orchestrating agent (or a Claude sub-agent) may enrich
the ``## Summary`` prose afterward; the structured sections below stand on their own.

Outputs:
  <dossier-dir>/<slug>.md   one dossier per person
  index.json                lookup map (phone digits / email / name -> slug)
  index.md                  human-readable catalog
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    FACTS_DIR,
    INDEX_JSON,
    INDEX_MD,
    RAW_DIR,
    emit,
    normalize_name,
    now_iso,
    phone_digits,
    slugify,
    write_json,
)

MAX_TOPICS = 25


def merge_facts(chunks: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine a person's per-chunk facts into one record (no LLM)."""
    facts = [c.get("facts") or {} for c in chunks if c.get("facts")]
    if not facts:
        return {}

    def best_scalar(field: str) -> str:
        """Highest-confidence non-empty value (ties -> longest, then first)."""
        candidates = [
            (f.get("confidence") or 0.0, len(str(f.get(field) or "")), str(f.get(field) or "").strip())
            for f in facts if str(f.get(field) or "").strip()
        ]
        return max(candidates)[2] if candidates else ""

    names = [str(f.get("canonical_name") or "").strip() for f in facts if f.get("canonical_name")]
    canonical = Counter(names).most_common(1)[0][0] if names else ""

    employers: dict[str, dict[str, str]] = {}
    status_rank = {"current": 2, "past": 1, "unknown": 0}
    for f in facts:
        for emp in f.get("employers") or []:
            name = str(emp.get("name") or "").strip()
            if not name:
                continue
            key = name.lower()
            incumbent = employers.get(key)
            cand = {"name": name, "role": str(emp.get("role") or "").strip(), "status": str(emp.get("status") or "unknown")}
            if incumbent is None:
                employers[key] = cand
                continue
            # Keep the strongest status and the most specific role across mentions.
            if status_rank.get(cand["status"], 0) > status_rank.get(incumbent["status"], 0):
                incumbent["status"] = cand["status"]
            if not incumbent["role"] and cand["role"]:
                incumbent["role"] = cand["role"]

    aliases: list[str] = []
    topics: list[str] = []
    identifiers: list[str] = []
    for f in facts:
        for value in f.get("aliases") or []:
            v = str(value).strip()
            if v and v != canonical and v not in aliases:
                aliases.append(v)
        for value in f.get("topics") or []:
            v = str(value).strip()
            if v and v.lower() not in {t.lower() for t in topics}:
                topics.append(v)
        for value in f.get("identifiers") or []:
            v = str(value).strip()
            if v and v.lower() not in {i.lower() for i in identifiers}:
                identifiers.append(v)

    events: dict[tuple[str, str], dict[str, str]] = {}
    for f in facts:
        for ev in f.get("notable_events") or []:
            summary = str(ev.get("summary") or "").strip()
            if not summary:
                continue
            date = str(ev.get("date") or "").strip()
            events[(date, summary.lower())] = {"date": date, "summary": summary}

    relationships = [str(f.get("relationship_to_owner") or "").strip() for f in facts]
    relationship = max((r for r in relationships if r), key=len, default="")

    shared: dict[str, dict[str, str]] = {}
    for f in facts:
        for sc in f.get("shared_context") or []:
            detail = str(sc.get("detail") or "").strip()
            if detail:
                shared[detail.lower()] = {
                    "overlap": str(sc.get("overlap") or "other"),
                    "detail": detail,
                    "evidence": str(sc.get("evidence") or "").strip(),
                }

    return {
        "canonical_name": canonical,
        "aliases": aliases,
        "employers": list(employers.values()),
        "title": best_scalar("title"),
        "school": best_scalar("school"),
        "field_of_study": best_scalar("field_of_study"),
        "location": best_scalar("location"),
        "relationship_to_owner": relationship,
        "topics": topics[:MAX_TOPICS],
        "notable_events": sorted(events.values(), key=lambda e: e["date"] or "9999"),
        "identifiers": identifiers,
        "shared_context": list(shared.values()),
        "confidence": max((f.get("confidence") or 0.0 for f in facts), default=0.0),
    }


def headline(merged: dict[str, Any]) -> str:
    """One-line role @ employer summary for the index + frontmatter."""
    title = merged.get("title") or ""
    employers = merged.get("employers") or []
    current = next((e for e in employers if e.get("status") == "current"), employers[0] if employers else None)
    company = current.get("name") if current else ""
    if title and company:
        return f"{title} at {company}"
    if title or company:
        return title or company
    # Fall back to the relationship line. Trim to a WORD boundary (never mid-word) and mark it
    # with an ellipsis; the full text is preserved verbatim in the "## Relationship & cadence"
    # section, so this is a compact one-liner, not lost content.
    rel = (merged.get("relationship_to_owner") or "").strip()
    if len(rel) <= 80:
        return rel
    head = rel[:80].rsplit(" ", 1)[0].rstrip(",;:")
    return f"{head}…"


def _yaml_list(values: list[str]) -> str:
    return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + "]"


def render_dossier(meta: dict[str, Any], merged: dict[str, Any], depth: dict[str, Any] | None = None) -> str:
    name = merged.get("canonical_name") or meta.get("full_name") or "(unknown)"
    depth = depth or {}
    msgs = meta.get("messages") or []
    last_at = max((m.get("at") or "" for m in msgs), default="")
    lines = [
        "---",
        f"person_id: {meta.get('person_id')}",
        f"name: {json.dumps(name, ensure_ascii=False)}",
        f"slug: {slugify(name, str(meta.get('person_id')))}",
        f"emails: {_yaml_list(meta.get('emails') or [])}",
        f"phones: {_yaml_list(meta.get('phones') or [])}",
        f"source_channels: {_yaml_list(meta.get('source_channels') or [])}",
        f"message_count: {len(msgs)}",
        f"last_interaction: {json.dumps(last_at, ensure_ascii=False)}",
        f"confidence: {round(float(merged.get('confidence') or 0.0), 2)}",
        f"generated_at: {now_iso()}",
        "---",
        "",
        f"# {name}",
        "",
        "## Summary",
        "",
        headline(merged) or "_No summary yet._",
    ]
    rel = merged.get("relationship_to_owner")
    if rel:
        used = depth.get("messages_used", len(msgs))
        avail = depth.get("messages_available", len(msgs))
        chans = ", ".join(meta.get("source_channels") or []) or "unknown channels"
        note = f"_grokked {used} of {avail} messages"
        if depth.get("batches_used"):
            note += f" over {depth['batches_used']} batch(es)"
        note += f" across {chans}; last on {last_at[:10] or 'n/a'}"
        note += f" (stopped: {depth['stop_reason']})._" if depth.get("stop_reason") else "._"
        lines += ["", "## Relationship & cadence", "", rel, "", note]

    shared = merged.get("shared_context") or []
    if shared:
        lines += ["", "## Shared context with you", ""]
        for sc in shared:
            ev = f" — _{sc['evidence']}_" if sc.get("evidence") else ""
            lines.append(f"- **{sc.get('overlap', 'other')}:** {sc['detail']}{ev}")

    who: list[str] = []
    if merged.get("title"):
        who.append(f"- **Title:** {merged['title']}")
    for emp in merged.get("employers") or []:
        status = emp.get("status") or "unknown"
        role = f" — {emp['role']}" if emp.get("role") else ""
        who.append(f"- **Employer ({status}):** {emp['name']}{role}")
    if merged.get("school"):
        field = f" ({merged['field_of_study']})" if merged.get("field_of_study") else ""
        who.append(f"- **School:** {merged['school']}{field}")
    if merged.get("location"):
        who.append(f"- **Location:** {merged['location']}")
    if who:
        lines += ["", "## Who they are", "", *who]

    if merged.get("topics"):
        lines += ["", "## Topics", "", *(f"- {t}" for t in merged["topics"])]

    if merged.get("notable_events"):
        lines += ["", "## Timeline", ""]
        for ev in merged["notable_events"]:
            date = ev.get("date") or "?"
            lines.append(f"- **{date}** — {ev['summary']}")

    contact_values = [*(meta.get("emails") or []), *(meta.get("phones") or [])]
    known = {v.lower() for v in contact_values}
    known |= {phone_digits(v) for v in contact_values if phone_digits(v)}
    idents = [
        i for i in (merged.get("identifiers") or [])
        if i.lower() not in known and phone_digits(i) not in known
    ]
    contact = [f"- {v}" for v in contact_values]
    if idents or contact:
        lines += ["", "## Identifiers", "", *contact, *(f"- {i}" for i in idents)]

    # Filled by cluster_merge_candidates; kept as a stable anchor so re-runs update it.
    lines += ["", "## Possible same person", "", "_None detected yet._", ""]
    return "\n".join(lines)


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    raw_dir = Path(args.raw_dir)
    facts_dir = Path(args.facts_dir)
    dossier_dir = Path(args.dossier_dir)
    dossier_dir.mkdir(parents=True, exist_ok=True)

    index = {"slugs": {}, "by_phone": {}, "by_email": {}, "by_name": {}}
    catalog: list[tuple[str, str, str]] = []  # (name, headline, slug)
    written_slugs: set[str] = set()
    written = 0

    for facts_path in sorted(facts_dir.glob("*.jsonl")):
        if facts_path.name == "manifest.json":
            continue
        person_id = facts_path.stem
        if args.person and person_id != args.person:
            continue
        raw_path = raw_dir / f"{person_id}.json"
        if not raw_path.exists():
            continue
        meta = json.loads(raw_path.read_text(encoding="utf-8"))
        chunks = list(_read_jsonl(facts_path))
        merged = merge_facts(chunks)
        if not merged:
            continue
        depth = chunks[-1] if chunks else {}  # incremental synth writes one record with depth meta
        name = merged.get("canonical_name") or meta.get("full_name") or "person"
        slug = slugify(name, person_id)
        (dossier_dir / f"{slug}.md").write_text(render_dossier(meta, merged, depth), encoding="utf-8")
        written_slugs.add(slug)
        written += 1

        rel_path = f"dossiers/{slug}.md"
        index["slugs"][slug] = {"person_id": person_id, "name": name, "path": rel_path, "headline": headline(merged)}
        for email in meta.get("emails") or []:
            index["by_email"].setdefault(email.lower(), []).append(slug)
        for phone in meta.get("phones") or []:
            digits = phone_digits(phone)
            if digits:
                index["by_phone"].setdefault(digits, []).append(slug)
        for nm in {normalize_name(name), normalize_name(meta.get("full_name") or "")}:
            if nm:
                index["by_name"].setdefault(nm, []).append(slug)
        catalog.append((name, headline(merged), slug))

    # Remove orphan dossiers from earlier runs (a changed canonical_name yields a
    # new slug; the old file would otherwise linger). Skip when scoped to --person.
    orphans = 0
    if not args.person:
        for md in dossier_dir.glob("*.md"):
            if md.stem not in written_slugs:
                md.unlink()
                orphans += 1

    write_json(Path(args.index_json), index)
    _write_catalog(Path(args.index_md), catalog)

    manifest = {
        "source": "compose_dossier",
        "status": "completed",
        "dossiers_written": written,
        "orphans_removed": orphans,
        "dossier_dir": str(dossier_dir),
        "index_json": str(args.index_json),
        "index_md": str(args.index_md),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }
    write_json(dossier_dir / "manifest.json", manifest)
    return manifest


def _read_jsonl(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _write_catalog(path: Path, catalog: list[tuple[str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Deep-context dossiers ({len(catalog)})", "", f"_Generated {now_iso()}._", ""]
    for name, head, slug in sorted(catalog, key=lambda c: c[0].lower()):
        suffix = f" — {head}" if head else ""
        lines.append(f"- [[{slug}]] **{name}**{suffix}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Compose markdown dossiers + lookup index from synthesized facts.")
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--index-md", default=str(INDEX_MD))
    p.add_argument("--person", default="", help="Only this person id")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    emit(run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

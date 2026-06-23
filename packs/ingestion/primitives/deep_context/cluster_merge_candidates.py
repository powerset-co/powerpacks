"""[4/4] Detect same-person / merge candidates via a high-reasoning LLM judge.

Clustering REQUIRES LLM reasoning — deterministic name/email/phone scoring is only
the recall net, never the decision. Pipeline:

  1. Blocking + a name-similarity gate produce candidate pairs cheaply (so we never
     LLM every "Chen", only genuinely ambiguous same/similar-name pairs).
  2. A high-reasoning LLM judge decides SAME / DIFFERENT per pair by weighing ALL
     evidence HOLISTICALLY — identity (name/nickname, employer, school, location,
     emails), the role each plays in my life, content & behavior (e.g. forwarding
     household receipts = family behavior), and tone/register where available. No
     single signal dominates; tone is just one input and is skipped when a record
     has no messages from me.
  3. Only judge-confirmed pairs become edges -> connected components -> clusters.

Writes a full verdict log (merge-verdicts.csv) incl. rejections for auditability.
``--no-llm`` falls back to deterministic scoring (offline/tests only).

Outputs: merge-candidates.csv / .md + a "Possible same person" section per dossier.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from pathlib import Path
from typing import Any

from packs.indexing.lib.llm_config import DEFAULT_MODEL
from packs.indexing.lib.openai_stream import drain_pool
from packs.indexing.lib.openai_usage_tiers import env_or_profile_int
from packs.indexing.lib.openai_responses import (
    estimate_cost_usd,
    is_retryable,
    make_async_client,
    parse_json_response,
    reasoning_effort,
    responses_kwargs,
    usage_tokens,
)
from packs.ingestion.primitives.deep_context import compose_dossier as compose
from packs.ingestion.primitives.deep_context.common import (
    DOSSIER_DIR,
    FACTS_DIR,
    INDEX_JSON,
    MERGE_CSV,
    MERGE_MD,
    RAW_DIR,
    emit,
    load_env,
    normalize_name,
    now_iso,
    write_json,
)

DEFAULT_CONFIDENCE = 0.7   # judge must be at least this confident to merge
GATE_NAME_SIM = 0.85       # below this (and no shared contact) a pair isn't worth a call
SECTION_ANCHOR = "## Possible same person"
SAMPLE_PER_DIRECTION = 6
SAMPLE_CHARS = 200

JUDGE_SYSTEM = (
    "You decide whether two contact records (A and B) are the SAME PERSON, so they can be "
    "merged. Reason HOLISTICALLY over ALL the evidence — no single signal dominates. You are "
    "given each contact's name, my relationship to them, key identity facts, what we talk "
    "about, and sample messages (how I talk to them and how they talk to me).\n\n"
    "Weigh these together with careful reasoning:\n"
    "- IDENTITY: name including nicknames/short forms (e.g. Annmay vs Ann), middle initials, "
    "employer, school, location, emails/handles, and any hard CONTRADICTIONS.\n"
    "- ROLE IN MY LIFE: two records that play the same role (romantic partner, specific "
    "coworker, a particular vendor) are more likely the same person.\n"
    "- CONTENT & BEHAVIOR: what we actually do — e.g. forwarding household receipts, "
    "reservations, or logistics to me is intimate/family behavior; coordinating deals is "
    "professional. Behavior that fits the same relationship is strong evidence.\n"
    "- TONE/REGISTER, only WHEN available: consistent register supports same person; a clear "
    "formal-vs-intimate mismatch can indicate different people. If one record has NO messages "
    "from me, you simply cannot use tone — treat its absence as neutral, never as evidence.\n\n"
    "A shared or similar NAME ALONE is not enough — but a similar name PLUS aligned "
    "role/identity/behavior is strong. Set same_person=true only when the COMBINED evidence "
    "supports it; otherwise false."
)

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "same_person": {"type": "boolean"},
        "confidence": {"type": "number"},
        "tone_toward_a": {"type": "string", "description": "How I address contact A (e.g. casual, formal)"},
        "tone_toward_b": {"type": "string"},
        "tone_consistent": {"type": "boolean"},
        "reason": {"type": "string", "description": "One-line rationale, citing tone."},
    },
    "required": ["same_person", "confidence", "tone_toward_a", "tone_toward_b", "tone_consistent", "reason"],
}


# --- Jaro-Winkler (stdlib only, recall gate + --no-llm path) ----------------

def jaro(s1: str, s2: str) -> float:
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    match_dist = max(len(s1), len(s2)) // 2 - 1
    s1_matches = [False] * len(s1)
    s2_matches = [False] * len(s2)
    matches = 0
    for i, ch in enumerate(s1):
        lo = max(0, i - match_dist)
        hi = min(i + match_dist + 1, len(s2))
        for j in range(lo, hi):
            if not s2_matches[j] and s2[j] == ch:
                s1_matches[i] = s2_matches[j] = True
                matches += 1
                break
    if not matches:
        return 0.0
    transpositions = 0
    k = 0
    for i, matched in enumerate(s1_matches):
        if matched:
            while not s2_matches[k]:
                k += 1
            if s1[i] != s2[k]:
                transpositions += 1
            k += 1
    transpositions //= 2
    return (matches / len(s1) + matches / len(s2) + (matches - transpositions) / matches) / 3


def jaro_winkler(s1: str, s2: str, prefix_weight: float = 0.1) -> float:
    base = jaro(s1, s2)
    prefix = 0
    for a, b in zip(s1, s2):
        if a == b and prefix < 4:
            prefix += 1
        else:
            break
    return base + prefix * prefix_weight * (1 - base)


# --- loading (dossier identity + message samples + relationship) ------------

def parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    meta: dict[str, Any] = {}
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        raw = raw.strip()
        try:
            meta[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            meta[key.strip()] = raw.strip('"')
    return meta


def _sample(messages: list[dict[str, Any]], direction: str) -> list[str]:
    out: list[str] = []
    for m in sorted(messages, key=lambda m: m.get("at") or "", reverse=True):
        if m.get("direction") != direction:
            continue
        text = (m.get("text") or "").strip()
        if text:
            out.append(text[:SAMPLE_CHARS])
        if len(out) >= SAMPLE_PER_DIRECTION:
            break
    return out


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _profile(facts_path: Path) -> dict[str, Any]:
    """Compact identity view (relationship + key facts + topics) for the judge."""
    if not facts_path.exists():
        return {}
    recs = [json.loads(l) for l in facts_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    fa = compose.merge_facts(recs) if recs else {}
    if not fa:
        return {}
    return {
        "relationship": str(fa.get("relationship_to_owner") or ""),
        "title": str(fa.get("title") or ""),
        "employers": [e.get("name", "") for e in (fa.get("employers") or []) if e.get("name")],
        "school": str(fa.get("school") or ""),
        "location": str(fa.get("location") or ""),
        "topics": list(fa.get("topics") or [])[:8],
    }


def load_people(index: dict[str, Any], dossier_dir: Path, raw_dir: Path, facts_dir: Path) -> list[dict[str, Any]]:
    by_phone = index.get("by_phone", {})
    people: list[dict[str, Any]] = []
    for slug, info in index.get("slugs", {}).items():
        path = dossier_dir / f"{slug}.md"
        if not path.exists():
            continue
        meta = parse_frontmatter(path.read_text(encoding="utf-8"))
        pid = info.get("person_id", "")
        bundle = _read_json(raw_dir / f"{pid}.json")
        msgs = bundle.get("messages") or []
        people.append({
            "slug": slug,
            "person_id": pid,
            "name": meta.get("name") or info.get("name") or "",
            "name_key": normalize_name(meta.get("name") or info.get("name") or ""),
            "emails": [e.lower() for e in (meta.get("emails") or [])],
            "phone_digits": [d for d, slugs in by_phone.items() if slug in slugs],
            "profile": _profile(facts_dir / f"{pid}.jsonl"),
            "from_me": _sample(msgs, "from_me"),
            "from_them": _sample(msgs, "from_them"),
        })
    return people


def email_localparts(emails: list[str]) -> set[str]:
    return {e.split("@", 1)[0] for e in emails if "@" in e}


# --- blocking + recall gate -------------------------------------------------

def name_tokens(name_key: str) -> set[str]:
    return {t for t in name_key.split() if len(t) > 1}


def generate_pairs(people: list[dict[str, Any]]) -> set[tuple[int, int]]:
    """Blocked pairs that pass a recall gate: shared phone/email/email-localpart,
    or Jaro-Winkler name >= GATE_NAME_SIM. Keeps LLM calls to ambiguous pairs."""
    buckets: dict[str, list[int]] = {}
    for idx, p in enumerate(people):
        keys = {f"email:{e}" for e in p["emails"]}
        keys |= {f"local:{lp}" for lp in email_localparts(p["emails"])}
        keys |= {f"phone:{d}" for d in p["phone_digits"]}
        keys |= {f"tok:{t}" for t in name_tokens(p["name_key"])}
        for key in keys:
            buckets.setdefault(key, []).append(idx)
    cand: set[tuple[int, int]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > 200:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                cand.add((min(members[i], members[j]), max(members[i], members[j])))
    gated: set[tuple[int, int]] = set()
    for a, b in cand:
        pa, pb = people[a], people[b]
        if (set(pa["emails"]) & set(pb["emails"])
                or email_localparts(pa["emails"]) & email_localparts(pb["emails"])
                or set(pa["phone_digits"]) & set(pb["phone_digits"])
                or jaro_winkler(pa["name_key"], pb["name_key"]) >= GATE_NAME_SIM):
            gated.add((a, b))
    return gated


# --- LLM judge --------------------------------------------------------------

def _render_side(label: str, p: dict[str, Any]) -> str:
    pr = p.get("profile") or {}
    facts = []
    if pr.get("relationship"):
        facts.append(f"relationship: {pr['relationship']}")
    if pr.get("title") or pr.get("employers"):
        facts.append(f"work: {pr.get('title', '')} {('@ ' + ', '.join(pr['employers'])) if pr.get('employers') else ''}".strip())
    if pr.get("school"):
        facts.append(f"school: {pr['school']}")
    if pr.get("location"):
        facts.append(f"location: {pr['location']}")
    if pr.get("topics"):
        facts.append(f"we discuss: {', '.join(pr['topics'])}")
    facts_block = "\n".join(f"  {f}" for f in facts) or "  (no extracted facts)"
    me = "\n".join(f"  me→them: {t}" for t in p["from_me"]) or "  (no messages from me — tone unavailable)"
    them = "\n".join(f"  them→me: {t}" for t in p["from_them"]) or "  (no messages from them)"
    emails = ", ".join(p["emails"]) or "none"
    return (f"CONTACT {label} — {p['name']}  [emails: {emails}]\n"
            f"{facts_block}\nMessages:\n{me}\n{them}")


def judge_prompt(pa: dict[str, Any], pb: dict[str, Any]) -> str:
    return f"{_render_side('A', pa)}\n\n{_render_side('B', pb)}\n\nAre A and B the same person?"


async def judge_pair(client: Any, pa: dict[str, Any], pb: dict[str, Any], *, model: str,
                     effort: str, semaphore: asyncio.Semaphore, max_retries: int) -> dict[str, Any]:
    kwargs = responses_kwargs(model, effort=effort, schema=JUDGE_SCHEMA, schema_name="same_person")
    async with semaphore:
        attempt = 0
        while True:
            try:
                response = await client.responses.create(
                    model=model,
                    input=[{"role": "system", "content": JUDGE_SYSTEM},
                           {"role": "user", "content": judge_prompt(pa, pb)}],
                    **kwargs,
                )
                return {"verdict": parse_json_response(response, "judge"), "usage": usage_tokens(response), "error": ""}
            except Exception as exc:  # noqa: BLE001
                attempt += 1
                if is_retryable(exc) and attempt <= max_retries:
                    await asyncio.sleep(min(2 ** attempt, 30))
                    continue
                return {"verdict": {}, "usage": {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                        "error": f"{type(exc).__name__}: {exc}"[:200]}


# --- clustering + output ----------------------------------------------------

def connected_components(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def inject_section(path: Path, body: str) -> None:
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    head = text.split(SECTION_ANCHOR)[0].rstrip()
    path.write_text(f"{head}\n\n{SECTION_ANCHOR}\n\n{body}\n", encoding="utf-8")


def deterministic_verdict(pa: dict[str, Any], pb: dict[str, Any]) -> dict[str, Any]:
    """Offline/tests fallback (--no-llm): shared contact or near-exact name."""
    shared = bool(set(pa["emails"]) & set(pb["emails"])) or bool(set(pa["phone_digits"]) & set(pb["phone_digits"]))
    nsim = jaro_winkler(pa["name_key"], pb["name_key"])
    same = shared or nsim >= 0.97
    return {"same_person": same, "confidence": 0.95 if shared else round(nsim, 2),
            "tone_toward_a": "", "tone_toward_b": "", "tone_consistent": same,
            "reason": "shared contact" if shared else f"name similarity {nsim:.2f}"}


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    dossier_dir = Path(args.dossier_dir)
    index = _read_json(Path(args.index_json))
    people = load_people(index, dossier_dir, Path(args.raw_dir), Path(args.facts_dir))
    pairs = sorted(generate_pairs(people))

    if getattr(args, "dry_run", False):
        # Blocking is free; only the ambiguous pairs below would be judged (small spend).
        per_lo, per_hi = 0.004, 0.02
        return {
            "source": "cluster_merge_candidates", "status": "dry_run",
            "people": len(people), "candidate_pairs_to_judge": len(pairs),
            "estimated_cost_usd_low": round(len(pairs) * per_lo, 2),
            "estimated_cost_usd_high": round(len(pairs) * per_hi, 2),
            "model": args.model, "reasoning_effort": args.reasoning_effort,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "updated_at": now_iso(),
        }

    verdicts: list[dict[str, Any]] = []
    usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0}
    use_llm = not getattr(args, "no_llm", False)

    if use_llm and pairs:
        load_env()
        # Wall-time is bound by per-call high-reasoning latency, not local CPU — parallelize hard.
        concurrency = args.concurrency or env_or_profile_int("POWERPACKS_OPENAI_CONCURRENCY", "openai_concurrency", fallback=64)
        effort = reasoning_effort(args.reasoning_effort)

        async def driver() -> None:
            client = make_async_client(timeout=args.timeout)
            semaphore = asyncio.Semaphore(max(1, concurrency))
            results: dict[int, dict[str, Any]] = {}

            def on_result(item: tuple[int, dict[str, Any]]) -> None:
                results[item[0]] = item[1]

            async def one(i: int, a: int, b: int) -> tuple[int, dict[str, Any]]:
                return i, await judge_pair(client, people[a], people[b], model=args.model,
                                           effort=effort, semaphore=semaphore, max_retries=args.max_retries)
            try:
                await drain_pool([one(i, a, b) for i, (a, b) in enumerate(pairs)], on_result)
            finally:
                await client.close()
            for i, (a, b) in enumerate(pairs):
                res = results.get(i, {"verdict": {}, "usage": {}})
                for k in usage_total:
                    usage_total[k] += res.get("usage", {}).get(k, 0)
                verdicts.append({"a": a, "b": b, **(res["verdict"] or {})})

        asyncio.run(driver())
    else:
        for a, b in pairs:
            verdicts.append({"a": a, "b": b, **deterministic_verdict(people[a], people[b])})

    edges: list[tuple[int, int]] = []
    confirmed: list[dict[str, Any]] = []
    for v in verdicts:
        if v.get("same_person") and float(v.get("confidence") or 0) >= args.confidence:
            a, b = v["a"], v["b"]
            edges.append((a, b))
            confirmed.append({
                "slug_a": people[a]["slug"], "name_a": people[a]["name"],
                "slug_b": people[b]["slug"], "name_b": people[b]["name"],
                "confidence": round(float(v.get("confidence") or 0), 3),
                "tone_consistent": v.get("tone_consistent"),
                "reason": v.get("reason", ""),
            })

    confirmed.sort(key=lambda r: r["confidence"], reverse=True)
    _write_pairs_csv(Path(args.out_csv), confirmed)
    # Full audit log: every judged pair incl. rejections (why a duplicate was NOT merged).
    _write_verdicts_csv(Path(args.out_csv).with_name("merge-verdicts.csv"), people, verdicts)
    clusters = connected_components(len(people), edges)
    _write_clusters_md(Path(args.out_md), people, clusters, confirmed)

    neighbors: dict[str, list[tuple[str, str, float, str]]] = {}
    for r in confirmed:
        neighbors.setdefault(r["slug_a"], []).append((r["slug_b"], r["name_b"], r["confidence"], r["reason"]))
        neighbors.setdefault(r["slug_b"], []).append((r["slug_a"], r["name_a"], r["confidence"], r["reason"]))
    for person in people:
        matches = sorted(neighbors.get(person["slug"], []), key=lambda m: m[2], reverse=True)
        body = "\n".join(f"- [[{s}]] **{n}** (confidence {c:.2f}) — _{why}_" for s, n, c, why in matches) if matches else "_None detected._"
        inject_section(dossier_dir / f"{person['slug']}.md", body)

    billed_output = usage_total["output_tokens"] + usage_total["reasoning_tokens"]
    manifest = {
        "source": "cluster_merge_candidates",
        "status": "completed",
        "judge": "llm" if use_llm else "deterministic",
        "people": len(people),
        "pairs_judged": len(pairs),
        "candidate_pairs": len(confirmed),
        "clusters": len(clusters),
        "confidence_threshold": args.confidence,
        "tokens": usage_total,
        "estimated_cost_usd": estimate_cost_usd(usage_total["input_tokens"], billed_output, args.model),
        "out_csv": str(args.out_csv),
        "out_md": str(args.out_md),
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "updated_at": now_iso(),
    }
    write_json(dossier_dir / "merge_manifest.json", manifest)
    return manifest


def _write_pairs_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["slug_a", "name_a", "slug_b", "name_b", "confidence", "tone_consistent", "reason"])
        writer.writeheader()
        writer.writerows(rows)


def _write_verdicts_csv(path: Path, people: list[dict[str, Any]], verdicts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["name_a", "name_b", "same_person", "confidence", "tone_consistent", "reason"])
        w.writeheader()
        for v in sorted(verdicts, key=lambda v: float(v.get("confidence") or 0), reverse=True):
            w.writerow({
                "name_a": people[v["a"]]["name"], "name_b": people[v["b"]]["name"],
                "same_person": v.get("same_person"), "confidence": v.get("confidence"),
                "tone_consistent": v.get("tone_consistent"), "reason": v.get("reason", ""),
            })


def _write_clusters_md(path: Path, people: list[dict[str, Any]], clusters: list[list[int]], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Merge candidates ({len(clusters)} clusters, {len(rows)} pairs)", "",
             f"_Generated {now_iso()}. LLM-judged on tone + identity. Confirm before merging._", ""]
    for i, group in enumerate(clusters, 1):
        lines.append(f"## Cluster {i}")
        for idx in group:
            lines.append(f"- [[{people[idx]['slug']}]] **{people[idx]['name']}**")
        lines.append("")
    if not clusters:
        lines.append("_No merge candidates confirmed._")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Detect same-person / merge candidates via an LLM tone-aware judge.")
    p.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    p.add_argument("--index-json", default=str(INDEX_JSON))
    p.add_argument("--raw-dir", default=str(RAW_DIR))
    p.add_argument("--facts-dir", default=str(FACTS_DIR))
    p.add_argument("--out-csv", default=str(MERGE_CSV))
    p.add_argument("--out-md", default=str(MERGE_MD))
    p.add_argument("--confidence", type=float, default=DEFAULT_CONFIDENCE, help="Min judge confidence to merge")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--reasoning-effort", default="high", choices=["minimal", "low", "medium", "high"])
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--timeout", type=int, default=120)
    p.add_argument("--max-retries", type=int, default=6)
    p.add_argument("--dry-run", action="store_true", help="Count candidate pairs + estimate cost; no spend")
    p.add_argument("--no-llm", action="store_true", help="Deterministic fallback (offline/tests only)")
    return p


def main(argv: list[str] | None = None) -> int:
    emit(run(build_parser().parse_args(argv)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

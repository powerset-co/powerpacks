"""Signature cache, free survey, CSV receipts, and dossier/cluster rendering."""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packs.ingestion.primitives.common.jsonio import now_iso
from packs.ingestion.primitives.deep_context.merge_candidates.blocking import (
    JUDGE_SLAM_DUNK, connected_components, generate_pairs, slam_dunk_verdict,
)
from packs.ingestion.primitives.deep_context.merge_candidates.judge import JUDGE_LLM, JUDGE_SYSTEM
from packs.ingestion.primitives.deep_context.merge_candidates.models import (
    MergePerson, all_emails, all_phones, load_people, read_json,
)

IDENTITY_CONTRACT_VERSION = "owned-identifiers-v1"
REUSABLE_JUDGES = frozenset({JUDGE_SLAM_DUNK, JUDGE_LLM})
SECTION_ANCHOR = "## Possible same person"
_JUDGE_VERSION = hashlib.sha1(
    f"{IDENTITY_CONTRACT_VERSION}\x1e{JUDGE_SYSTEM}".encode("utf-8")).hexdigest()[:8]


@dataclass(frozen=True)
class PairSurvey:
    people: list[MergePerson]
    pairs: list[tuple[int, int]]
    slam: list[tuple[int, int, dict[str, Any]]]
    reused: list[tuple[int, int, str, dict[str, Any]]]
    to_judge: list[tuple[int, int, str]]


def person_sig(person: MergePerson) -> str:
    profile = person.profile or {}
    return "\x1f".join([
        person.name_key, "|".join(sorted(all_emails(person))),
        "|".join(sorted(all_phones(person))), profile.get("relationship", ""),
        profile.get("title", ""), "|".join(sorted(profile.get("employers") or [])),
        profile.get("school", ""), profile.get("location", ""),
        "|".join(sorted(profile.get("topics") or [])),
    ])


def pair_sig(first: MergePerson, second: MergePerson) -> str:
    left, right = sorted([person_sig(first), person_sig(second)])
    return hashlib.sha1(
        f"{_JUDGE_VERSION}\x1e{left}\x1e{right}".encode("utf-8")).hexdigest()[:16]


def load_cached_verdicts(path: Path) -> dict[frozenset[str], tuple[str, dict[str, Any]]]:
    cache: dict[frozenset[str], tuple[str, dict[str, Any]]] = {}
    if not path.exists():
        return cache
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            first, second = row.get("slug_a") or "", row.get("slug_b") or ""
            signature = row.get("sig") or ""
            if not first or not second or not signature:
                continue
            judge = (row.get("judge") or "").strip().lower() or JUDGE_LLM
            if judge not in REUSABLE_JUDGES:
                continue
            cache[frozenset({first, second})] = (signature, {
                "same_person": (row.get("same_person") or "").strip().lower() == "true",
                "confidence": float(row.get("confidence") or 0),
                "tone_consistent": (row.get("tone_consistent") or "").strip().lower() == "true",
                "reason": row.get("reason", ""), "judge": judge,
            })
    return cache


def split_cached_pairs(pairs: list[tuple[int, int]], people: list[MergePerson],
                       cache: dict[frozenset[str], tuple[str, dict[str, Any]]],
                       ) -> tuple[list[tuple[int, int, str, dict[str, Any]]], list[tuple[int, int, str]]]:
    reused: list[tuple[int, int, str, dict[str, Any]]] = []
    to_judge: list[tuple[int, int, str]] = []
    for left, right in pairs:
        signature = pair_sig(people[left], people[right])
        hit = cache.get(frozenset({people[left].slug, people[right].slug}))
        if hit and hit[0] == signature:
            reused.append((left, right, signature, hit[1]))
        else:
            to_judge.append((left, right, signature))
    return reused, to_judge


def survey_pairs(*, index_json: Path, dossier_dir: Path, raw_dir: Path, facts_dir: Path,
                 verdicts_csv: Path, refresh: bool) -> PairSurvey:
    people = load_people(read_json(index_json), dossier_dir, raw_dir, facts_dir)
    pairs = sorted(generate_pairs(people))
    slam: list[tuple[int, int, dict[str, Any]]] = []
    rest: list[tuple[int, int]] = []
    for left, right in pairs:
        verdict = slam_dunk_verdict(people[left], people[right])
        if verdict:
            slam.append((left, right, verdict))
        else:
            rest.append((left, right))
    cache = {} if refresh else load_cached_verdicts(verdicts_csv)
    reused, to_judge = split_cached_pairs(rest, people, cache)
    return PairSurvey(people, pairs, slam, reused, to_judge)


def inject_section(path: Path, body: str) -> None:
    if not path.exists():
        return
    head = path.read_text(encoding="utf-8").split(SECTION_ANCHOR)[0].rstrip()
    path.write_text(f"{head}\n\n{SECTION_ANCHOR}\n\n{body}\n", encoding="utf-8")


def write_pairs_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["slug_a", "name_a", "slug_b", "name_b",
                                                        "confidence", "tone_consistent", "reason"])
        writer.writeheader()
        writer.writerows(rows)


def write_verdicts_csv(path: Path, people: list[MergePerson], verdicts: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["slug_a", "slug_b", "name_a", "name_b",
            "same_person", "confidence", "tone_consistent", "reason", "sig", "judge"])
        writer.writeheader()
        for verdict in sorted(verdicts, key=lambda value: float(value.get("confidence") or 0), reverse=True):
            left, right = verdict["a"], verdict["b"]
            writer.writerow({
                "slug_a": people[left].slug, "slug_b": people[right].slug,
                "name_a": people[left].name, "name_b": people[right].name,
                "same_person": verdict.get("same_person"), "confidence": verdict.get("confidence"),
                "tone_consistent": verdict.get("tone_consistent"), "reason": verdict.get("reason", ""),
                "sig": verdict.get("sig", ""), "judge": verdict.get("judge", JUDGE_LLM),
            })


def write_clusters_md(path: Path, people: list[MergePerson], clusters: list[list[int]],
                      rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Merge candidates ({len(clusters)} clusters, {len(rows)} pairs)", "",
             f"_Generated {now_iso()}. LLM-judged on tone + identity. Confirm before merging._", ""]
    for number, group in enumerate(clusters, 1):
        lines.append(f"## Cluster {number}")
        lines.extend(f"- [[{people[index].slug}]] **{people[index].name}**" for index in group)
        lines.append("")
    if not clusters:
        lines.append("_No merge candidates confirmed._")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def render_results(*, dossier_dir: Path, out_csv: Path, out_md: Path, verdicts_csv: Path,
                   people: list[MergePerson], verdicts: list[dict[str, Any]],
                   confidence: float) -> tuple[list[dict[str, Any]], list[list[int]]]:
    edges: list[tuple[int, int]] = []
    confirmed: list[dict[str, Any]] = []
    for verdict in verdicts:
        if verdict.get("same_person") and float(verdict.get("confidence") or 0) >= confidence:
            left, right = verdict["a"], verdict["b"]
            edges.append((left, right))
            confirmed.append({
                "slug_a": people[left].slug, "name_a": people[left].name,
                "slug_b": people[right].slug, "name_b": people[right].name,
                "confidence": round(float(verdict.get("confidence") or 0), 3),
                "tone_consistent": verdict.get("tone_consistent"), "reason": verdict.get("reason", ""),
            })
    confirmed.sort(key=lambda row: row["confidence"], reverse=True)
    write_pairs_csv(out_csv, confirmed)
    write_verdicts_csv(verdicts_csv, people, verdicts)
    clusters = connected_components(len(people), edges)
    write_clusters_md(out_md, people, clusters, confirmed)
    neighbors: dict[str, list[tuple[str, str, float, str]]] = {}
    for row in confirmed:
        neighbors.setdefault(row["slug_a"], []).append(
            (row["slug_b"], row["name_b"], row["confidence"], row["reason"]))
        neighbors.setdefault(row["slug_b"], []).append(
            (row["slug_a"], row["name_a"], row["confidence"], row["reason"]))
    for person in people:
        matches = sorted(neighbors.get(person.slug, []), key=lambda match: match[2], reverse=True)
        body = "\n".join(f"- [[{slug}]] **{name}** (confidence {score:.2f}) — _{reason}_"
                         for slug, name, score, reason in matches) if matches else "_None detected._"
        inject_section(dossier_dir / f"{person.slug}.md", body)
    return confirmed, clusters

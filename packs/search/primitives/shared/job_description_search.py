"""Rank people by their best eligible JD-to-position evidence."""

from __future__ import annotations

from typing import Any


def rank_job_description_people(
    job_scores: dict[str, float],
    matches: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
    by_position = {str(row.get("position_id") or row["id"]): row for row in positions}
    best: dict[str, dict[str, Any]] = {}
    for match in matches:
        position_id = str(match["position_id"])
        job_id = str(match["job_description_id"])
        position = by_position.get(position_id)
        score = job_scores.get(job_id, 0.0) * float(match["match_score"])
        if position is None or score <= 0:
            continue
        person_id = str(position.get("person_id") or position.get("base_id") or match["person_id"])
        previous = best.get(person_id)
        if previous and (-score, position_id, job_id) >= (
            -previous["score"], previous["position_id"], previous["job_description_id"],
        ):
            continue
        best[person_id] = {
            **position,
            "person_id": person_id,
            "position_id": position_id,
            "score": score,
            "retrieval_mode": "job_description",
            "job_description_id": job_id,
            "job_description_position_id": position_id,
            "job_description_match_type": match.get("match_type"),
            "job_description_match_score": match["match_score"],
            "job_description_position_gap_days": match.get("posting_position_gap_days"),
        }
    rows = sorted(best.values(), key=lambda row: (-row["score"], row["person_id"]))
    return rows[:top_k] if top_k > 0 else rows

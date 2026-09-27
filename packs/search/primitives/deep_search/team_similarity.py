"""Rank Jev-pass candidates by prior work similarity to stored current staff."""

from __future__ import annotations

import hashlib
import json
import math
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import tiktoken

from packs.search.primitives.shared.openai_client import make_openai_client
from packs.search.primitives.deep_search.results_web.team import (
    fetch_embedded_employees, team_members,
)

MODEL = "text-embedding-3-small"
METHOD = "Titles + descriptions + company names · five jobs"
MAX_TOKENS = 8191
TOP_EMPLOYEES = 3
EMBED_BATCH = 32
ENCODING = tiktoken.get_encoding("cl100k_base")


def prepare_team(domain: str, env_file: Path, run_dir: Path) -> dict:
    """Snapshot the stored roster and API-owned employee embeddings once per run."""
    path = run_dir / "team-embeddings.json"
    fetched = not path.is_file()
    if not fetched:
        snapshot = json.loads(path.read_text())
    else:
        employees, metadata = fetch_embedded_employees(domain, env_file)
        if metadata["company_id_source"] != "coresignal_company" or not metadata["company_id"]:
            raise ValueError("employee roster did not resolve a CoreSignal company")
        if metadata["embedding_model"] != MODEL:
            raise ValueError("employee embedding model differs from candidate model")
        fields = ("provider_employee_id", "full_name", "linkedin_url", "title", "location",
                  "started_on", "embedding_status", "embedding")
        employees = [{field: row.get(field) for field in fields} for row in employees]
        snapshot = {**metadata, "domain": domain, "employees": employees,
                    "fetched_at": datetime.now(timezone.utc).isoformat()}
    team_path = run_dir / "team.json"
    if not team_path.is_file() or not path.is_file():
        team_path.write_text(json.dumps({
            "members": [asdict(member) for member in team_members(snapshot["employees"])],
            "fetched_at": snapshot["fetched_at"]}, indent=2) + "\n")
    if fetched:
        path.write_text(json.dumps(snapshot) + "\n")
    return snapshot


def save_status(run_dir: Path, status: str, reason: str = "", **counts: int) -> None:
    (run_dir / "team-status.json").write_text(json.dumps({
        "status": status, "reason": reason, **counts}, indent=2) + "\n")
    print(f"[team-similarity] {status}{': ' + reason if reason else ''}", file=sys.stderr)


def _date_key(value: object) -> tuple[int, int, int]:
    if isinstance(value, dict):
        return (int(value.get("year") or value.get("yyyy") or 0),
                int(value.get("month") or value.get("mm") or 1),
                int(value.get("day") or value.get("dd") or 1))
    text = str(value or "")
    if "T" in text:
        text = text.split("T", 1)[0]
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y/%m/%d", "%Y/%m", "%B %Y", "%b %Y", "%Y"):
        try:
            date = datetime.strptime(text, fmt)
            return date.year, date.month, date.day
        except ValueError:
            continue
    return 0, 0, 0


def work_text(positions: list[dict]) -> str:
    """Use original titles, employer names and descriptions from five recent jobs."""
    roles = []
    seen = set()
    for position in positions:
        if position.get("deleted"):
            continue
        title = str(position.get("position_title") or position.get("title") or "").strip()
        company = position.get("company_name") or position.get("company") or ""
        if isinstance(company, dict):
            company = company.get("name") or ""
        company = str(company).strip()
        description = str(position.get("description") or "").strip()
        start = position.get("start_date") or position.get("date_from")
        end = position.get("end_date") or position.get("date_to")
        key = (title.casefold(), company.casefold(), _date_key(start), _date_key(end))
        if key in seen or not (title or description):
            continue
        seen.add(key)
        roles.append((bool(position.get("is_current")), _date_key(start),
                      "\n".join(part for part in (title, company, description) if part)))
    roles.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return "\n\n".join(row[2] for row in roles[:5])


def embedding_input(positions: list[dict]) -> tuple[str, str]:
    text = work_text(positions)
    if not text:
        return "", ""
    text = ENCODING.decode(ENCODING.encode(text)[:MAX_TOKENS])
    digest = hashlib.sha256((MODEL + "\n" + text).encode()).hexdigest()
    return digest, text


def embed_candidates(people: list[dict], run_dir: Path) -> dict[str, list[float]]:
    """Persist each exact input immediately so a rerun pays only for missing text."""
    cache = run_dir / "team-similarity-embeddings"
    inputs = {}
    for person in people:
        digest, text = embedding_input(person["positions"])
        if digest:
            inputs[digest] = text
    missing = [(digest, text) for digest, text in inputs.items()
               if not (cache / f"{digest}.json").is_file()]
    if missing:
        cache.mkdir(parents=True, exist_ok=True)
        client = make_openai_client()
        try:
            for start in range(0, len(missing), EMBED_BATCH):
                batch = missing[start:start + EMBED_BATCH]
                response = client.embeddings.create(model=MODEL, input=[text for _, text in batch])
                for item in response.data:
                    digest = batch[item.index][0]
                    (cache / f"{digest}.json").write_text(json.dumps({
                        "model": MODEL, "embedding": item.embedding}) + "\n")
        finally:
            client.close()
    return {digest: json.loads((cache / f"{digest}.json").read_text())["embedding"]
            for digest in inputs}


def _linkedin_key(url: str) -> str:
    path = urlparse(url).path.strip("/").casefold()
    return path if path.startswith("in/") else ""


def _normalized(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else [0.0] * len(vector)


def rank(people: list[dict], employees: list[dict], vectors: dict[str, list[float]]) -> dict[str, dict]:
    """Mean nearest-three cosine, excluding the candidate's own employee row."""
    scored = []
    team = [(row, _normalized(row["embedding"])) for row in employees if row.get("embedding")]
    dimensions = {len(vector) for _, vector in team}
    if len(dimensions) > 1:
        raise ValueError("employee embeddings have different dimensions")
    for person in people:
        digest, _ = embedding_input(person["positions"])
        vector = vectors.get(digest)
        if not vector:
            continue
        vector = _normalized(vector)
        if dimensions and len(vector) not in dimensions:
            raise ValueError("candidate and employee embeddings have different dimensions")
        linkedin = _linkedin_key(person.get("linkedin_url") or "")
        matches = sorted(((sum(a * b for a, b in zip(vector, employee_vector)), row)
                          for row, employee_vector in team
                          if str(row.get("provider_employee_id") or "") != person["person_id"]
                          and (not linkedin or _linkedin_key(row.get("linkedin_url") or "") != linkedin)),
                         key=lambda item: (-item[0], str(item[1].get("provider_employee_id") or "")))
        nearest = matches[:TOP_EMPLOYEES]
        if nearest:
            scored.append((person["person_id"], sum(score for score, _ in nearest) / len(nearest),
                           [row.get("full_name") or "" for _, row in nearest]))
    scored.sort(key=lambda row: (-row[1], row[0]))
    return {person_id: {"rank": index, "candidate_count": len(scored), "score": score,
                        "method": METHOD, "closest_names": names}
            for index, (person_id, score, names) in enumerate(scored, 1)}

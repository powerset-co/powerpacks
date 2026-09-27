"""Snapshot stored current employees for the results viewer; never refresh or score."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import requests

from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import api_base, bearer_token
from .model import TeamMember

PAGE_SIZE = 500


def _fetch_pages(company_id: str, env_file: Path, *, domain: str = "",
                 embed: bool = False) -> tuple[list[dict], dict]:
    headers = {"Authorization": "Bearer " + bearer_token(env_file)}
    employees = []
    metadata = {}
    while True:
        company = ({"company_id": company_id} if company_id else {"domain": domain})
        response = requests.post(api_base(env_file) + "/v2/company/history/employees",
            headers=headers, json={"company_id_source": "coresignal_company", **company,
                "current": True, "is_staff": True, "embed": embed,
                "limit": PAGE_SIZE, "offset": len(employees)}, timeout=120 if embed else 30)
        response.raise_for_status()
        payload = response.json()
        company_id = payload.get("company_id") or company_id
        metadata = {"company_id_source": payload.get("company_id_source") or "coresignal_company",
                    "company_id": company_id, "embedding_model": payload.get("embedding_model"),
                    "embedding_usage_tokens": metadata.get("embedding_usage_tokens", 0)
                    + int(payload.get("embedding_usage_tokens") or 0),
                    "embedding_cache_misses": metadata.get("embedding_cache_misses", 0)
                    + int(payload.get("embedding_cache_misses") or 0)}
        page = payload["employees"]
        employees.extend(page)
        if len(page) < PAGE_SIZE:
            return employees, metadata


def fetch_employees(company_id: str, env_file: Path) -> list[dict]:
    return _fetch_pages(company_id, env_file)[0]


def fetch_embedded_employees(domain: str, env_file: Path) -> tuple[list[dict], dict]:
    return _fetch_pages("", env_file, domain=domain, embed=True)


def team_members(employees: list[dict]) -> tuple[TeamMember, ...]:
    return tuple(TeamMember(name=row.get("full_name") or "",
        title=row.get("title") or "", linkedin_url=row.get("linkedin_url") or "",
        location=row.get("location") or "", started_on=row.get("started_on") or "")
        for row in employees)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    path = args.run_dir / "team.json"
    if path.exists():
        print(json.dumps({"path": str(path), "cached": True}))
        return
    members = team_members(fetch_employees(args.company_id, args.env_file))
    args.run_dir.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        json.dump({"members": [asdict(row) for row in members],
            "fetched_at": datetime.now(timezone.utc).isoformat()}, handle, indent=2)
    print(json.dumps({"path": str(path), "employees": len(members)}))


if __name__ == "__main__":
    main()

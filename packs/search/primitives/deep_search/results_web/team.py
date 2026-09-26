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


def fetch_employees(company_id: str, env_file: Path) -> list[dict]:
    headers = {"Authorization": "Bearer " + bearer_token(env_file)}
    employees = []
    while True:
        response = requests.post(api_base(env_file) + "/v2/company/history/employees",
            headers=headers, json={"company_id_source": "coresignal_company",
                "company_id": company_id, "current": True, "is_staff": True,
                "limit": PAGE_SIZE, "offset": len(employees)}, timeout=30)
        response.raise_for_status()
        page = response.json()["employees"]
        employees.extend(page)
        if len(page) < PAGE_SIZE:
            return employees


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

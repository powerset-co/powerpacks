#!/usr/bin/env python3
"""Save the Powerset team's Taste scores for people and companies in the local search index.

Cloud-only and free: for a signed-in user it sends the index's LinkedIn URLs and
company domains, 100 at a time, to the Powerset API (POST /v2/taste/people and
/v2/taste/companies), which read scores Reporting already saved and never start
a scoring run. People land in `local_person_taste` (one row per scored person and
mode, employee or founder); companies in `local_company_taste` (overall plus
team, momentum, investors, product_market). Nobody scored, no row. Each table is
replaced on every run, so a newly promoted model shows up without a rebuild.

Signed out, nothing to score, or the API refusing (not on the Powerset team, not
deployed, no active model) skips and leaves that table as it was. Never fails
the index build.

Changelog:
  2026-11-16: companies too (local_company_taste).
  2026-11-13: created.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SEARCH_LIB = ROOT / "packs" / "search" / "primitives" / "lib"
if str(SEARCH_LIB) not in sys.path:
    sys.path.insert(0, str(SEARCH_LIB))

import duckdb  # noqa: E402

import postgres_client  # noqa: E402
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys  # noqa: E402

TABLE = "local_person_taste"
COMPANY_TABLE = "local_company_taste"
MODES = ("employee", "founder")
COMPANY_SCORES = ("overall", "team", "momentum", "investors", "product_market")
BATCH = 100

Post = Callable[[str, dict[str, Any]], dict[str, Any]]


def api_post(root: Path) -> Post:
    """POST to the Powerset API as the signed-in user."""
    env_file = root / ".env"
    base = pull_runtime_keys.api_base(env_file)
    token = pull_runtime_keys.bearer_token(env_file)

    def post(path: str, body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            base + path, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    return post


def distinct_values(con: duckdb.DuckDBPyConnection, table: str, columns: tuple[str, ...]) -> list[str]:
    """Non-empty values of the first of `columns` that `table` has; [] without one."""
    present = {row[0] for row in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [table]).fetchall()}
    column = next((name for name in columns if name in present), None)
    if not column:
        return []
    rows = con.execute(
        f'SELECT DISTINCT "{column}" FROM {table} WHERE coalesce("{column}", \'\') <> \'\' ORDER BY 1').fetchall()
    return [row[0] for row in rows]


def batches(values: list[str]):
    for start in range(0, len(values), BATCH):
        yield values[start:start + BATCH]


def fetch_people(urls: list[str], post: Post) -> list[tuple]:
    """(linkedin_url, scoring_mode, score, model_id, scored_at) for every saved score."""
    rows = []
    for mode in MODES:
        for batch in batches(urls):
            answer = post("/v2/taste/people", {"linkedin_urls": batch, "scoring_mode": mode})
            # Reporting keeps request order, so each result belongs to our URL at the same position.
            for url, result in zip(batch, answer.get("results") or []):
                if result.get("score") is not None:
                    rows.append((url, mode, float(result["score"]), str(answer.get("model_id") or ""), result.get("scored_at")))
    return rows


def fetch_companies(domains: list[str], post: Post) -> list[tuple]:
    """(website_domain, company_name, overall, team, momentum, investors, product_market, model_id, scored_at)."""
    rows = []
    for batch in batches(domains):
        answer = post("/v2/taste/companies", {"websites": batch})
        for domain, result in zip(batch, answer.get("results") or []):
            scores = result.get("scores") or {}
            if scores.get("overall") is None:
                continue
            rows.append((domain, result.get("company_name"), *(scores.get(name) for name in COMPANY_SCORES),
                         str(answer.get("model_id") or ""), result.get("scored_at")))
    return rows


def save(con: duckdb.DuckDBPyConnection, table: str, columns: str, rows: list[tuple]) -> None:
    con.execute(f"CREATE OR REPLACE TABLE {table} ({columns})")
    if rows:
        con.executemany(f"INSERT INTO {table} VALUES ({', '.join('?' * len(rows[0]))})", rows)


def refresh(con: duckdb.DuckDBPyConnection, values: list[str], fetch, post: Post, table: str, columns: str) -> dict[str, Any]:
    """Fetch and replace one table; any failure skips and keeps the previous rows."""
    if not values:
        return {"status": "skipped", "reason": "nothing_to_score"}
    try:
        rows = fetch(values, post)
    except urllib.error.HTTPError as error:
        return {"status": "skipped", "reason": f"api_{error.code}"}
    except Exception as error:  # an offline laptop or a sign-in problem never fails the build
        return {"status": "skipped", "reason": type(error).__name__}
    save(con, table, columns, rows)
    return {"status": "completed", "asked": len(values), "scored": len(rows)}


def run(root: Path, db_path: Path, post: Post | None = None) -> dict[str, Any]:
    if post is None and not postgres_client.credentials_subject():
        return {"status": "skipped", "reason": "signed_out"}
    if not db_path.is_file():
        return {"status": "skipped", "reason": "missing_duckdb"}
    with duckdb.connect(str(db_path)) as con:
        urls = distinct_values(con, "local_person_profiles", ("linkedin_url",))
        domains = distinct_values(con, "local_companies", ("website_domain", "domain"))
        if not urls and not domains:
            return {"status": "skipped", "reason": "nothing_to_score"}
        try:
            post = post or api_post(root)
        except Exception as error:
            return {"status": "skipped", "reason": type(error).__name__}
        return {
            "status": "completed",
            "people": refresh(con, urls, fetch_people, post, TABLE,
                              "linkedin_url VARCHAR, scoring_mode VARCHAR, score DOUBLE, model_id VARCHAR, scored_at VARCHAR"),
            "companies": refresh(con, domains, fetch_companies, post, COMPANY_TABLE,
                                 "website_domain VARCHAR, company_name VARCHAR, overall DOUBLE, team DOUBLE, momentum DOUBLE, "
                                 "investors DOUBLE, product_market DOUBLE, model_id VARCHAR, scored_at VARCHAR"),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    print(json.dumps(run(root, root / args.db)))


if __name__ == "__main__":
    main()

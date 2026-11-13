#!/usr/bin/env python3
"""Save the Powerset team's Taste scores for people in the local search index.

Cloud-only and free: for a signed-in user it sends the index's LinkedIn URLs, 100
at a time, to the Powerset API (POST /v2/taste/people), which reads scores
Reporting already saved and never starts a scoring run. Both scoring modes
(employee, founder) land in their own table, `local_person_taste`, one row per
scored person and mode; people nobody scored get no row. The table is replaced
on every run, so a newly promoted model shows up without rebuilding the index.

Signed out, no LinkedIn URLs, or the API refusing (not on the Powerset team, not
deployed, no active model) all skip and leave the table as it was. Never fails
the index build.

Changelog:
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
MODES = ("employee", "founder")
BATCH = 100

Post = Callable[[dict[str, Any]], dict[str, Any]]


def api_post(root: Path) -> Post:
    """POST /v2/taste/people as the signed-in user."""
    env_file = root / ".env"
    url = pull_runtime_keys.api_base(env_file) + "/v2/taste/people"
    token = pull_runtime_keys.bearer_token(env_file)

    def post(body: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            url, data=json.dumps(body).encode(), method="POST",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())

    return post


def linkedin_urls(con: duckdb.DuckDBPyConnection) -> list[str]:
    exists = con.execute(
        "SELECT 1 FROM information_schema.columns WHERE table_name = 'local_person_profiles' AND column_name = 'linkedin_url'"
    ).fetchone()
    if not exists:
        return []
    rows = con.execute(
        "SELECT DISTINCT linkedin_url FROM local_person_profiles WHERE coalesce(linkedin_url, '') <> '' ORDER BY 1"
    ).fetchall()
    return [row[0] for row in rows]


def fetch(urls: list[str], post: Post) -> list[tuple[str, str, float, str, str | None]]:
    """(linkedin_url, scoring_mode, score, model_id, scored_at) for every saved score."""
    rows = []
    for mode in MODES:
        for start in range(0, len(urls), BATCH):
            batch = urls[start:start + BATCH]
            answer = post({"linkedin_urls": batch, "scoring_mode": mode})
            # Reporting keeps request order, so each result belongs to our URL at the same position.
            for url, result in zip(batch, answer.get("results") or []):
                if result.get("score") is not None:
                    rows.append((url, mode, float(result["score"]), str(answer.get("model_id") or ""), result.get("scored_at")))
    return rows


def run(root: Path, db_path: Path, post: Post | None = None) -> dict[str, Any]:
    if post is None and not postgres_client.credentials_subject():
        return {"status": "skipped", "reason": "signed_out"}
    if not db_path.is_file():
        return {"status": "skipped", "reason": "missing_duckdb"}
    with duckdb.connect(str(db_path)) as con:
        urls = linkedin_urls(con)
        if not urls:
            return {"status": "skipped", "reason": "no_linkedin_urls"}
        try:
            rows = fetch(urls, post or api_post(root))
        except urllib.error.HTTPError as error:
            return {"status": "skipped", "reason": f"api_{error.code}"}
        except Exception as error:  # an offline laptop or a sign-in problem never fails the build
            return {"status": "skipped", "reason": type(error).__name__}
        con.execute(
            f"CREATE OR REPLACE TABLE {TABLE} (linkedin_url VARCHAR, scoring_mode VARCHAR, score DOUBLE, model_id VARCHAR, scored_at VARCHAR)"
        )
        if rows:
            con.executemany(f"INSERT INTO {TABLE} VALUES (?, ?, ?, ?, ?)", rows)
    return {"status": "completed", "people": len(urls), "scores": len(rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=".powerpacks/search-index/local-search.duckdb")
    parser.add_argument("--root", default=".")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    print(json.dumps(run(root, root / args.db)))


if __name__ == "__main__":
    main()

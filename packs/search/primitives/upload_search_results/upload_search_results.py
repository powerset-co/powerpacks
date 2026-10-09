#!/usr/bin/env python3
"""Upload a read-only search snapshot and optionally ask the set about its pinned candidates.

The API stores the renderer's data privately and upserts by owner/run ID.
Rerunning refreshes the snapshot without altering local results or labels.
The body is sent gzip-encoded: each pond adds ~15 MB of JSON, and the API caps
the wire size at 25 MiB.

Changelog:
- 2026-10-08: `--ask` sends the pinned candidates to the set and saves the returned ask for status checks.
- 2026-10-01: gzip the upload body (multi-pond runs exceeded the 25 MiB cap).
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "packs/search/primitives/lib"))

import postgres_client as pg
from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.schemas.people_schema import extract_public_identifier
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth
from packs.search.primitives.deep_search.results_web import snapshot

UPLOAD_PATH = "/v2/local-searches"
UPLOAD_TIMEOUT_SECONDS = 120
HTTP_UNAUTHORIZED = 401
GZIP_LEVEL = 6


def post_gzip_json(base: str, path: str, token: str, body: dict[str, Any], *,
                   timeout: int) -> dict[str, Any]:
    """POST one gzip-encoded JSON body; returns the parsed response."""
    request = urllib.request.Request(
        base + path,
        data=gzip.compress(json.dumps(body, ensure_ascii=False).encode("utf-8"), GZIP_LEVEL),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                 "Content-Encoding": "gzip", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


class UploadSearchResults:
    def __init__(self, run_dir: Path, *, env_file: Path | None = None,
                 ask: str | None = None) -> None:
        self.run_dir = run_dir
        self.env_file = env_file
        self.ask = ask

    def run(self) -> dict[str, Any]:
        try:
            token = auth.bearer_token(self.env_file)
        except SystemExit:
            return {"status": "needs_auth"}

        try:
            rendered = snapshot.export_snapshot(self.run_dir)
        except (OSError, ValueError) as exc:
            return {"status": "failed", "error": f"Cannot export search: {exc}"}
        body = {"source_run_id": rendered["search"]["run_id"], "snapshot": rendered}
        if self.ask is not None:
            pinned = {person for person, labels in rendered["tags"]["assignments"].items()
                      if any(label.casefold() == "pinned" for label in labels)}
            candidates = []
            skipped = 0
            for rank, row in enumerate(rendered["search"]["candidates"], start=1):
                if row["person_id"] not in pinned:
                    continue
                public_identifier = extract_public_identifier(row["linkedin_url"])
                if not public_identifier:
                    skipped += 1
                    continue
                candidates.append({"public_identifier": public_identifier,
                                   "linkedin_url": row["linkedin_url"], "name": row["name"],
                                   "local_rank": rank})
            set_id = pg.fetch_default_set_id(env_file=self.env_file)["set_id"]
            if not set_id:
                return {"status": "failed", "error": "No default set could be resolved"}
            body["ask"] = {"question": self.ask, "set_id": set_id, "candidates": candidates}
        try:
            response = post_gzip_json(auth.api_base(self.env_file), UPLOAD_PATH, token,
                                      body, timeout=UPLOAD_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            if exc.code == HTTP_UNAUTHORIZED:
                return {"status": "needs_auth"}
            return {"status": "failed", "http_status": exc.code,
                    "error": "Powerset could not store the search snapshot"}
        except OSError:
            return {"status": "failed", "error": "Search upload connection failed; rerun to retry"}
        if not response.get("id") or not response.get("url"):
            return {"status": "failed", "error": "Powerset returned no hosted search URL"}
        if self.ask is not None:
            ask = {**response["ask"], "question": self.ask}
            write_json(self.run_dir / "ask.json", ask)
            owners_found = sum(bool(candidate["owners"]) for candidate in ask["candidates"])
            print(f"asked {len(candidates)} pinned candidates, {skipped} pinned without LinkedIn skipped, "
                  f"owners found for {owners_found}", file=sys.stderr)
        return {"status": "uploaded", "id": response["id"], "url": response["url"],
                "sharing_enabled": response["sharing_enabled"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    parser.add_argument("--ask", help="Ask the set this question about the pinned candidates")
    args = parser.parse_args(argv)
    payload = UploadSearchResults(args.run_dir, env_file=args.env_file, ask=args.ask).run()
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] in ("uploaded", "needs_auth") else 1


if __name__ == "__main__":
    raise SystemExit(main())

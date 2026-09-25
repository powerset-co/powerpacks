#!/usr/bin/env python3
"""Upload a read-only search snapshot using the current Powerset login.

The API stores the renderer's data privately and upserts by owner/run ID.
Rerunning refreshes the snapshot without altering local results or labels.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth
from packs.powerset.primitives.send_feedback import send_feedback as http
from packs.search.primitives.deep_search.results_web import snapshot

UPLOAD_PATH = "/v2/local-searches"
UPLOAD_TIMEOUT_SECONDS = 120
HTTP_UNAUTHORIZED = 401


class UploadSearchResults:
    def __init__(self, run_dir: Path, *, env_file: Path | None = None) -> None:
        self.run_dir = run_dir
        self.env_file = env_file

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
        try:
            _, response = http.post_json(auth.api_base(self.env_file), UPLOAD_PATH, token,
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
        return {"status": "uploaded", "id": response["id"], "url": response["url"],
                "sharing_enabled": response["sharing_enabled"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    payload = UploadSearchResults(args.run_dir, env_file=args.env_file).run()
    print(json.dumps(payload, indent=2))
    return 0 if payload["status"] in ("uploaded", "needs_auth") else 1


if __name__ == "__main__":
    raise SystemExit(main())

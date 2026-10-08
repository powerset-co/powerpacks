#!/usr/bin/env python3
"""Fetch an ask's answers, save them locally, and print each candidate's owners.

Changelog:
- 2026-10-08: add the ask status primitive and CLI.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import write_json
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth

ASK_STATUS_TIMEOUT_SECONDS = 30


def _owner_status(owner: dict[str, Any]) -> str:
    if owner["status"] == "answered":
        return f"answered {owner['answer']['verdict']}"
    return "pending" if owner["awake"] else "offline"


def run(run_dir: Path, *, env_file: Path) -> dict:
    ask = json.loads((run_dir / "ask.json").read_text(encoding="utf-8"))
    token = auth.bearer_token(env_file)
    request = urllib.request.Request(
        f"{auth.api_base(env_file)}/v2/asks/{ask['ask_id']}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=ASK_STATUS_TIMEOUT_SECONDS) as response:
        body = json.loads(response.read().decode("utf-8"))
    write_json(run_dir / "ask-answers.json", body)

    for candidate in body["candidates"]:
        owners = [f"{owner['name']}: {_owner_status(owner)}" for owner in candidate["owners"]]
        print(candidate["name"] + (": " + ", ".join(owners) if owners else ""))
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    args = parser.parse_args(argv)
    run(args.run_dir, env_file=args.env_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

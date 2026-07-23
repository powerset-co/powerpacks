"""Command-line parsing and dispatch for the review UI."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from packs.ingestion.primitives.deep_context.common import (
    DEFAULT_PEOPLE_CSV,
    DOSSIER_DIR,
    ENRICH_MANIFEST,
    FACTS_DIR,
    LINKEDIN_OVERRIDES_CSV,
    PARENTS_DIR,
    PROFILE_CACHE_DIR,
    REVIEW_MANIFEST,
    VERDICTS_JSONL,
)
from packs.ingestion.primitives.deep_context.reconcile_linkedin import (
    DEFAULT_CONFIRM,
    DEFAULT_DETACH,
)

from .model import AVATAR_DIR, SYNTHETIC_PEOPLE_CSV
from .server import AGENT_ACTIONS, cmd_serve, workflow_status

def cmd_status(args: argparse.Namespace) -> None:
    """Print the next-action contract; ``--wait`` blocks until it is an AGENT
    action (or the timeout passes), then prints and exits.

    This is the whole agent-handoff mechanism — deliberately primitive so it
    always works: stat six local files once a second, recompute the contract
    only when one changed. No sockets, no daemons, no thread ids, no coupling
    to any harness. On timeout the payload carries ``status: waiting`` and the
    caller simply runs the command again."""
    paths = dict(
        review_path=Path(args.review), verdicts_path=Path(args.verdicts),
        synthetic_path=Path(args.synthetic_people), facts_dir=Path(args.facts_dir),
        people_csv=Path(args.people_csv), manifest_path=Path(args.manifest),
        enrichment_manifest_path=Path(args.enrichment_manifest),
    )
    watched = (paths["review_path"], paths["verdicts_path"], paths["synthetic_path"],
               paths["people_csv"], paths["manifest_path"],
               paths["enrichment_manifest_path"])

    def file_signature() -> tuple[tuple[int, int], ...]:
        values = []
        for path in watched:
            try:
                stat = path.stat()
                values.append((stat.st_mtime_ns, stat.st_size))
            except OSError:
                values.append((0, 0))
        return tuple(values)

    status = workflow_status(**paths)
    if getattr(args, "wait", False):
        started = time.monotonic()
        deadline = started + max(1, int(args.timeout))
        signature = file_signature()
        while (status["next_action"] not in AGENT_ACTIONS
               and time.monotonic() < deadline):
            time.sleep(1)
            current = file_signature()
            if current == signature:
                continue
            signature = current
            status = workflow_status(**paths)
        status["waited_seconds"] = int(time.monotonic() - started)
        if status["next_action"] not in AGENT_ACTIONS:
            status["status"] = "waiting"  # still the human's move — run me again
    print(json.dumps(status, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve the staged deep-context people review UI.")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve")
    serve.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    serve.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    serve.add_argument("--parents-dir", default=str(PARENTS_DIR))
    serve.add_argument("--dossier-dir", default=str(DOSSIER_DIR))
    serve.add_argument("--facts-dir", default=str(FACTS_DIR))
    serve.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    serve.add_argument("--synthetic-people", default=str(SYNTHETIC_PEOPLE_CSV))
    serve.add_argument("--manifest", default=str(REVIEW_MANIFEST))
    serve.add_argument("--enrichment-manifest", default=str(ENRICH_MANIFEST))
    serve.add_argument("--profile-cache-dir", default=str(PROFILE_CACHE_DIR))
    serve.add_argument("--avatar-dir", default=str(AVATAR_DIR))
    serve.add_argument("--confirm-threshold", type=float, default=DEFAULT_CONFIRM)
    serve.add_argument("--detach-threshold", type=float, default=DEFAULT_DETACH)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--stage", choices=("worth", "enrich", "linkedin", "done"))
    serve.add_argument("--fresh", action="store_true",
                       help="Begin a new People-review revision even when reusing a live server")
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)
    status = sub.add_parser("status", help="Read files and print the exact next workflow action")
    status.add_argument("--review", default=str(LINKEDIN_OVERRIDES_CSV))
    status.add_argument("--verdicts", default=str(VERDICTS_JSONL))
    status.add_argument("--facts-dir", default=str(FACTS_DIR))
    status.add_argument("--people-csv", default=str(DEFAULT_PEOPLE_CSV))
    status.add_argument("--synthetic-people", default=str(SYNTHETIC_PEOPLE_CSV))
    status.add_argument("--manifest", default=str(REVIEW_MANIFEST))
    status.add_argument("--enrichment-manifest", default=str(ENRICH_MANIFEST))
    status.add_argument("--wait", action="store_true",
                        help="block until next_action is an AGENT action "
                             "(or --timeout passes), then print and exit")
    status.add_argument("--timeout", type=int, default=900,
                        help="max seconds to --wait before returning "
                             "status=waiting (default 900)")
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not getattr(args, "func", None):
        args = build_parser().parse_args(["serve", *(argv or [])])
    args.func(args)
    return 0

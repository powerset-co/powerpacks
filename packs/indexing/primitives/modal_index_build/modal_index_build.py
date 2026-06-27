#!/usr/bin/env python3
"""Optional Modal Volume wrapper for Powerpacks search-index builds.

This command intentionally keeps Modal optional. Local indexing modules and tests do
not import Modal; live remote execution should be invoked with ``uv run --with modal``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from packs.indexing.lib.fingerprints import build_fingerprints  # noqa: E402
from packs.indexing.lib.manifest import DUCKDB_NAME, DUCKDB_SHA_NAME  # noqa: E402

APP_NAME = "powerpacks-indexing"
DEFAULT_VOLUME_NAME = "powerpacks-search-index"
VOLUME_ROOT = "/mnt/powerpacks"


def fallback_command(args: argparse.Namespace) -> str:
    parts = [
        "uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run",
        f"--input {args.input}",
        f"--output-dir {args.output_dir}",
        f"--run-id {args.run_id}",
    ]
    if getattr(args, "default_operator_id", None):
        parts.append(f"--default-operator-id {args.default_operator_id}")
    if getattr(args, "limit", None):
        parts.append(f"--limit {args.limit}")
    return " ".join(parts)


def modal_unavailable_payload(args: argparse.Namespace, kind: str, message: str) -> dict[str, Any]:
    return {"status": "error", "kind": kind, "message": message, "fallback_command": fallback_command(args)}


def planned_paths(args: argparse.Namespace) -> dict[str, Any]:
    fps = build_fingerprints(args.input, operator_id=args.operator_id, default_operator_id=args.default_operator_id, limit=args.limit)
    scope = fps["operator_scope_slug"]
    cache_path = f"{VOLUME_ROOT}/operators/{scope}/search-index/cache/{fps['combined']}"
    run_path = f"{VOLUME_ROOT}/operators/{scope}/search-index/runs/{args.run_id}"
    local_run = Path(args.output_dir) / args.run_id
    return {
        "status": "planned",
        "app_name": APP_NAME,
        "run_id": args.run_id,
        "operator_id": args.operator_id,
        "cache": {"policy": args.cache_policy, "fingerprint": fps["combined"], "operator_scope": scope},
        "volume": {"name": args.volume_name, "run_path": run_path, "cache_path": cache_path},
        "artifacts": {
            "manifest": str(local_run / "index-manifest.json"),
            "duckdb": str(local_run / DUCKDB_NAME),
            "duckdb_sha256": str(local_run / DUCKDB_SHA_NAME),
        },
        "pull": {"duckdb": bool(args.pull_duckdb), "materialize_compat_artifacts": bool(args.materialize_compat_artifacts)},
    }


def build_modal_app(volume_name: str = DEFAULT_VOLUME_NAME):
    """Return a lazily constructed Modal app/function tuple.

    Kept in a function so importing this module never requires Modal.
    """

    import modal  # type: ignore

    app = modal.App(APP_NAME)
    image = modal.Image.debian_slim(python_version="3.12").uv_sync()
    volume = modal.Volume.from_name(volume_name, create_if_missing=True)

    @app.function(image=image, volumes={VOLUME_ROOT: volume}, timeout=60 * 60 * 2)
    def run_remote_build(request: dict[str, Any]) -> dict[str, Any]:
        # The live implementation delegates to the repo-local deterministic CLI in
        # the Modal image. The plain request/response shape is intentionally kept
        # simple so static tests do not need Modal wrappers.
        import subprocess
        from pathlib import Path as _Path

        volume_run = _Path(request["volume_run_path"])
        volume_run.parent.mkdir(parents=True, exist_ok=True)
        input_path = volume_run / "people.csv"
        input_path.write_text(request["people_csv_text"], encoding="utf-8")
        cmd = [
            sys.executable,
            "packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py",
            "run",
            "--input",
            str(input_path),
            "--output-dir",
            str(volume_run.parent),
            "--run-id",
            request["run_id"],
            "--default-operator-id",
            request.get("default_operator_id") or request.get("operator_id") or "local:user",
        ]
        if request.get("limit") is not None:
            cmd.extend(["--limit", str(request["limit"])])
        proc = subprocess.run(cmd, cwd="/root", capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            return {"status": "error", "kind": "remote_build_failed", "stderr": proc.stderr, "stdout": proc.stdout}
        volume.commit()
        return json.loads(proc.stdout)

    return app, run_remote_build


def cmd_plan(args: argparse.Namespace) -> dict[str, Any]:
    return planned_paths(args)


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    plan = planned_paths(args)
    if not args.allow_unverified_live_run:
        return {
            **plan,
            "status": "error",
            "kind": "modal_live_run_unverified",
            "message": "Modal live execution is a static scaffold until repo-source packaging and Volume behavior are verified by an explicit smoke. Re-run with --allow-unverified-live-run only for that opt-in smoke, or use the fallback local command.",
            "fallback_command": fallback_command(args),
        }
    try:
        _app, remote = build_modal_app(args.volume_name)
    except ModuleNotFoundError:
        return modal_unavailable_payload(args, "modal_unavailable", "Modal SDK is not installed. Re-run with `uv run --with modal ...` for remote builds.")
    except Exception as exc:
        return modal_unavailable_payload(args, "modal_unavailable", f"Modal app could not be initialized: {exc}")

    try:
        people_text = Path(args.input).read_text(encoding="utf-8-sig", errors="replace")
        response = remote.remote({
            "run_id": args.run_id,
            "operator_id": args.operator_id,
            "default_operator_id": args.default_operator_id,
            "limit": args.limit,
            "people_csv_text": people_text,
            "volume_run_path": plan["volume"]["run_path"],
        })
    except Exception as exc:
        return modal_unavailable_payload(args, "modal_execution_failed", f"Modal remote execution failed: {exc}")

    if args.pull_duckdb and response.get("status") == "completed":
        # Modal client-side file transfer is intentionally conservative here. A
        # future live smoke can replace this with Modal filesystem APIs if needed.
        response.setdefault("pull", {})["message"] = "Remote build completed; pull final artifacts from the Modal Volume if needed."
    return {**plan, **response, "status": response.get("status", "completed")}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    for name in ["plan", "run"]:
        cmd = sub.add_parser(name)
        cmd.add_argument("--input", required=True)
        cmd.add_argument("--output-dir", required=True)
        cmd.add_argument("--run-id", required=True)
        cmd.add_argument("--operator-id", required=True)
        cmd.add_argument("--default-operator-id")
        cmd.add_argument("--limit", type=int)
        cmd.add_argument("--cache-policy", choices=["reuse", "refresh", "off"], default="reuse")
        cmd.add_argument("--volume-name", default=DEFAULT_VOLUME_NAME)
        cmd.add_argument("--pull-duckdb", action="store_true")
        cmd.add_argument("--materialize-compat-artifacts", action="store_true")
        cmd.add_argument("--allow-unverified-live-run", action="store_true", help="Opt in to the unverified live Modal path for a smoke test")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.cmd == "plan":
        print(json.dumps(cmd_plan(args), sort_keys=True))
        return
    if args.cmd == "run":
        print(json.dumps(cmd_run(args), sort_keys=True))
        return
    build_parser().error("subcommand required")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Send a typed GTM plan to Powerset and write its result artifacts."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import sys
import urllib.error
from typing import Literal

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pydantic import TypeAdapter, ValidationError

from packs.gtm.primitives.discover.boundary import Operation, post, save_request
from packs.gtm.primitives.discover.models import Artifacts, DiscoverRequest, Manifest, Result, SortRequest
from packs.ingestion.primitives.common.jsonio import emit
from packs.ingestion.primitives.common.manifests import write_stage_manifest
from packs.powerset.primitives.pull_runtime_keys import pull_runtime_keys as auth


class GtmDiscovery:
    def __init__(self, *, request_file: Path, operation: Operation = "discover",
                 env_file: Path = _REPO_ROOT / ".env", out_dir: Path | None = None) -> None:
        self.request_text = request_file.read_text(encoding="utf-8")
        if operation == "sort":
            TypeAdapter(SortRequest).validate_json(self.request_text)
        else:
            TypeAdapter(DiscoverRequest).validate_json(self.request_text)
        self.operation = operation
        self.base = auth.api_base(env_file)
        self.env_file = env_file
        self.out_dir = out_dir or _REPO_ROOT / ".powerpacks/gtm" / operation
        self.request_path = self.out_dir / "request.json"
        self.response_path = self.out_dir / "response.json"
        self.candidates_path = self.out_dir / "candidates.jsonl"
        self.manifest_path = self.out_dir / "manifest.json"

    def run(self) -> Manifest:
        token = auth.bearer_token(self.env_file)
        save_request(self.request_path, self.request_text)
        result = post(base=self.base, token=token, operation=self.operation,
                      request_text=self.request_text, response_path=self.response_path,
                      candidates_path=self.candidates_path)
        if isinstance(result, Result):
            candidates = result.candidates
            coverage = result.coverage
            sources = result.sources
            query, filters = result.query, result.filters
            warnings, elapsed_ms = result.warnings, result.elapsed_ms
            timings = result.stage_timings
        else:
            candidates, coverage = result, ()
            sources = tuple(sorted({source for row in result for source in row.fit.candidate.sources}))
            query, filters = "", None
            warnings, elapsed_ms, timings = (), 0, ()
        status: Literal["complete", "partial", "failed"] = "partial" if any(item.status != "complete" or item.error or item.has_more for item in coverage) else "complete"
        manifest = Manifest(status=status, operation=self.operation, query=query, filters=filters,
                            count=len(candidates), qualified=sum(row.fit.status == "qualified" for row in candidates),
                            sources=sources, coverage=coverage, warnings=warnings, elapsed_ms=elapsed_ms,
                            artifacts=Artifacts(str(self.request_path), str(self.response_path), str(self.candidates_path)),
                            stage_timings=timings)
        write_stage_manifest(self.manifest_path, asdict(manifest))
        return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", nargs="?", choices=["discover", "refine", "sort"], default="discover")
    parser.add_argument("--request", type=Path, required=True, help="JSON request with explicit filters, sources, and caps")
    parser.add_argument("--env-file", type=Path, default=_REPO_ROOT / ".env")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        result = GtmDiscovery(request_file=args.request, operation=args.operation,
                              env_file=args.env_file, out_dir=args.output_dir).run()
    except (OSError, ValueError, ValidationError, urllib.error.URLError) as error:
        emit({"status": "failed", "error": str(error)})
        return 1
    emit(asdict(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

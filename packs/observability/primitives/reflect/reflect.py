#!/usr/bin/env python3
"""Build and optionally send one privacy-safe Powerpacks reflection report.

The primitive reads only a fixed allowlist of workflow manifests, projects
closed enums and bucketed numbers, and overwrites three fixed local files:

  .powerpacks/reflect/report.json
  .powerpacks/reflect/export.json
  .powerpacks/reflect/manifest.json

It never reads a transcript, uploads a raw manifest, proposes an implementation,
or mutates source files. An authenticated default run posts the sanitized
`export.json` payload to Powerset. A signed-out run prepares a public-issue
preview but never creates the issue.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from packs.ingestion.primitives.common.jsonio import emit, write_json  # noqa: E402
from packs.observability.primitives.reflect.contracts import (  # noqa: E402
    INTERVENTIONS,
    OPAQUE_RECEIPT_RE,
    SCHEMA_VERSION,
    WORKFLOWS,
)
from packs.observability.primitives.reflect.manifests import (  # noqa: E402
    WORKFLOW_MANIFESTS,
)
from packs.observability.primitives.reflect.projection import build_export  # noqa: E402
from packs.observability.primitives.reflect.validation import (  # noqa: E402
    PrivacyProjectionError,
    validate_export,
)
from packs.powerset.primitives.auth.auth import (  # noqa: E402
    PowersetAuthUnavailable,
    PowersetNotLoggedIn,
    fresh_access_token,
)
from packs.powerset.primitives.pull_runtime_keys.pull_runtime_keys import api_base  # noqa: E402

DEFAULT_OUT_DIR = Path(".powerpacks/reflect")
DEFAULT_UPLOAD_PATH = "/v1/reflections"
GITHUB_NEW_ISSUE_URL = "https://github.com/powerset-co/powerpacks/issues/new"


def _issue_preview(export: dict[str, Any]) -> dict[str, str]:
    title = f"[Reflect] {export['workflow']} observation"
    body = (
        "An anonymized Powerpacks reflection report is attached below. "
        "It contains observations only; no implementation proposal or raw user data.\n\n"
        "```json\n"
        + json.dumps(export, indent=2, sort_keys=True)
        + "\n```"
    )
    return {"url": GITHUB_NEW_ISSUE_URL, "title": title, "body": body}


@dataclass
class Reflect:
    root: Path
    workflow: str
    out_dir: Path = DEFAULT_OUT_DIR
    harness: str = "unknown"
    model: str = "unknown"
    provider: str = "unknown"
    effort: str = "unknown"
    role: str = "primary"
    intervention: str = "none"
    fallback: bool = False
    local_only: bool = False
    upload_url: str | None = None
    timeout: int = 20

    def _paths(self) -> tuple[Path, Path, Path]:
        out_dir = self.out_dir if self.out_dir.is_absolute() else self.root / self.out_dir
        return out_dir / "report.json", out_dir / "export.json", out_dir / "manifest.json"

    def _export(self) -> dict[str, Any]:
        return build_export(
            root=self.root,
            workflow=self.workflow,
            harness=self.harness,
            model=self.model,
            provider=self.provider,
            effort=self.effort,
            role=self.role,
            intervention=self.intervention,
            fallback=self.fallback,
        )

    def _resolved_upload_url(self) -> str:
        explicit = (self.upload_url or os.environ.get("POWERPACKS_REFLECT_URL") or "").strip()
        resolved = explicit or api_base(self.root / ".env") + DEFAULT_UPLOAD_PATH
        parsed = urllib.parse.urlparse(resolved)
        local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
        if parsed.scheme != "https" and not local_http:
            raise SystemExit("reflect upload URL must use HTTPS")
        return resolved

    def _upload(self, export: dict[str, Any], token: str) -> tuple[str, str | None]:
        request = urllib.request.Request(
            self._resolved_upload_url(),
            data=json.dumps(export, sort_keys=True).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                receipt: str | None = None
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    candidate = payload.get("receipt") if isinstance(payload, dict) else None
                    if isinstance(candidate, str) and OPAQUE_RECEIPT_RE.fullmatch(candidate):
                        receipt = candidate
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
                return "sent", receipt
        except urllib.error.HTTPError as exc:
            return f"http_{exc.code}", None
        except (urllib.error.URLError, TimeoutError, OSError):
            return "network_error", None

    def run(self) -> dict[str, Any]:
        report_path, export_path, manifest_path = self._paths()
        try:
            export = self._export()
            validate_export(export, WORKFLOW_MANIFESTS)
        except PrivacyProjectionError:
            safe_failure = {"schema_version": SCHEMA_VERSION, "status": "privacy_failed"}
            write_json(export_path, safe_failure)
            write_json(report_path, safe_failure)
            write_json(
                manifest_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "privacy_failed",
                    "workflow": self.workflow,
                    "privacy_projection_passed": False,
                },
            )
            return {
                "primitive": "reflect",
                "status": "privacy_failed",
                "workflow": self.workflow,
                "report": str(report_path),
                "export": str(export_path),
                "manifest": str(manifest_path),
                "delivery": {"mode": "none", "status": "not_sent"},
            }

        if all(stage["artifact_state"] == "missing" for stage in export["stages"]):
            status = "no_artifacts"
            delivery = {
                "mode": "none",
                "status": "not_sent",
                "error_code": "artifact_state_unavailable",
            }
        elif self.local_only:
            status = "local"
            delivery: dict[str, Any] = {"mode": "local", "status": "not_sent"}
        else:
            try:
                token = fresh_access_token()
            except PowersetNotLoggedIn:
                status = "github_issue_offer"
                delivery = {
                    "mode": "github_issue",
                    "status": "confirmation_required",
                    "preview": _issue_preview(export),
                }
            except PowersetAuthUnavailable:
                status = "upload_failed"
                delivery = {
                    "mode": "powerset",
                    "status": "not_sent",
                    "error_code": "auth_unavailable",
                }
            else:
                try:
                    upload_status, receipt = self._upload(export, token)
                except SystemExit:
                    upload_status, receipt = "endpoint_missing", None
                if upload_status == "sent":
                    status = "sent"
                    delivery = {"mode": "powerset", "status": "sent"}
                    if receipt:
                        delivery["receipt"] = receipt
                else:
                    status = "upload_failed"
                    delivery = {
                        "mode": "powerset",
                        "status": "not_sent",
                        "error_code": upload_status,
                    }

        report = {**export, "delivery": delivery}
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "workflow": self.workflow,
            "report": "report.json",
            "export": "export.json",
            "privacy_projection_passed": True,
        }
        write_json(export_path, export)
        write_json(report_path, report)
        write_json(manifest_path, manifest)
        return {
            "primitive": "reflect",
            "status": status,
            "workflow": self.workflow,
            "report": str(report_path),
            "export": str(export_path),
            "manifest": str(manifest_path),
            "delivery": delivery,
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow", required=True, choices=WORKFLOWS)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--harness", default="unknown")
    parser.add_argument("--model", default="unknown")
    parser.add_argument("--provider", default="unknown")
    parser.add_argument("--effort", default="unknown")
    parser.add_argument("--role", default="primary")
    parser.add_argument("--intervention", choices=INTERVENTIONS, default="none")
    parser.add_argument("--fallback", action="store_true")
    parser.add_argument("--local", action="store_true", dest="local_only")
    parser.add_argument("--upload-url")
    parser.add_argument("--timeout", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = Reflect(
        root=args.root.resolve(),
        workflow=args.workflow,
        out_dir=args.out_dir,
        harness=args.harness,
        model=args.model,
        provider=args.provider,
        effort=args.effort,
        role=args.role,
        intervention=args.intervention,
        fallback=args.fallback,
        local_only=args.local_only,
        upload_url=args.upload_url,
        timeout=args.timeout,
    ).run()
    emit(result)
    if result["status"] == "privacy_failed":
        return 2
    return 1 if result["status"] in {"upload_failed", "no_artifacts"} else 0


if __name__ == "__main__":
    raise SystemExit(main())

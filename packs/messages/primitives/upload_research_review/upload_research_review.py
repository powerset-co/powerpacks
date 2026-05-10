#!/usr/bin/env python3
"""Upload a reviewed messages research CSV to Powerset."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


DEFAULT_CSV = ".powerpacks/messages/research_review.csv"
DEFAULT_API_URL = "https://search-api-7wk4uhe77q-uw.a.run.app"
VALID_BUCKETS = {
    "confident": "yes",
    "medium": "maybe",
    "review": "maybe",
    "yes": "yes",
    "maybe": "maybe",
    "no": "no",
}
TRUTHY = {"1", "true", "yes", "y", "on"}
FALSY = {"0", "false", "no", "n", "off"}


class UploadError(RuntimeError):
    pass


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def normalize_bucket(value: str) -> str:
    bucket = VALID_BUCKETS.get((value or "").strip().lower())
    if not bucket:
        raise UploadError("CSV must include bucket values yes|maybe|no or legacy confident|medium|review")
    return bucket


def normalize_exclude(value: str) -> bool | None:
    """Return explicit approved state from legacy `exclude` column.

    `exclude=no` means approved. `exclude=yes` means not approved. The rest of
    the upload path speaks in terms of approved/unapproved only; yes is just the
    server artifact bucket used for approved rows.
    """
    raw = (value or "").strip().lower()
    if raw in TRUTHY:
        return False
    if raw in FALSY:
        return True
    return None


def approved_for_row(row: dict[str, str]) -> bool:
    explicit = normalize_exclude(row.get("exclude", ""))
    if explicit is not None:
        return explicit
    return normalize_bucket(row.get("bucket", "")) == "yes"


def load_review_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not csv_path.exists():
        raise UploadError(f"review CSV does not exist: {csv_path}")
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [{key: value or "" for key, value in row.items()} for row in reader]
    if not fieldnames:
        raise UploadError("CSV is empty or missing headers")
    if "bucket" not in fieldnames:
        raise UploadError("CSV must include a bucket column")
    if "top_title_company_pairs" not in fieldnames:
        raise UploadError("CSV must include top_title_company_pairs")
    return fieldnames, rows


def prepare_upload_csv(csv_path: Path) -> tuple[bytes, dict[str, Any]]:
    fieldnames, rows = load_review_rows(csv_path)
    output_fields = list(fieldnames)
    for extra in ("source_bucket", "approved"):
        if extra not in output_fields:
            output_fields.append(extra)

    prepared: list[dict[str, str]] = []
    source_counts = {"approved": 0, "unapproved": 0}
    explicit = {"approved": 0, "unapproved": 0, "blank": 0}
    for row in rows:
        original_bucket = normalize_bucket(row.get("bucket", ""))
        explicit_approved = normalize_exclude(row.get("exclude", ""))
        approved = approved_for_row(row)
        source_counts["approved" if approved else "unapproved"] += 1
        if explicit_approved is True:
            explicit["approved"] += 1
        elif explicit_approved is False:
            explicit["unapproved"] += 1
        else:
            explicit["blank"] += 1
        if not approved:
            continue

        next_row = {key: row.get(key, "") for key in output_fields}
        next_row["source_bucket"] = original_bucket
        # Backend artifact compatibility: approved rows are stored in the yes bucket.
        next_row["bucket"] = "yes"
        next_row["approved"] = "true"
        prepared.append(next_row)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=output_fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(prepared)
    summary = {
        "csv": str(csv_path),
        "row_count": len(prepared),
        "approved_count": len(prepared),
        "source_approved_count": source_counts["approved"],
        "source_unapproved_count": source_counts["unapproved"],
        "skipped_unapproved_count": source_counts["unapproved"],
        "explicit_approved_count": explicit["approved"],
        "explicit_unapproved_count": explicit["unapproved"],
        "bucket_default_count": explicit["blank"],
    }
    return buf.getvalue().encode("utf-8"), summary


def auth_token_from_powerpacks() -> str:
    auth_py = repo_root() / "packs/powerset/primitives/auth/auth.py"
    if not auth_py.exists():
        raise UploadError(f"could not find Powerset auth primitive: {auth_py}")
    result = subprocess.run(
        [sys.executable, str(auth_py), "token", "--bearer-only"],
        cwd=repo_root(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout or result.stderr or "").strip()
        raise UploadError(
            "could not get a Powerset access token; run `$powerset login` first"
            + (f": {detail}" if detail else "")
        )
    token = result.stdout.strip()
    if not token:
        raise UploadError("Powerset auth primitive returned an empty token")
    return token


def build_multipart_body(*, field_name: str, filename: str, content_type: str, data: bytes) -> tuple[bytes, str]:
    boundary = f"powerpacks-{uuid.uuid4().hex}"
    chunks = [
        f"--{boundary}\r\n".encode("ascii"),
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8"),
        f"Content-Type: {content_type}\r\n\r\n".encode("ascii"),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode("ascii"),
    ]
    return b"".join(chunks), boundary


def upload_review_csv(*, csv_path: Path, api_url: str, token: str, timeout: int) -> dict[str, Any]:
    prepared_csv, summary = prepare_upload_csv(csv_path)
    body, boundary = build_multipart_body(
        field_name="file",
        filename=csv_path.name,
        content_type="text/csv",
        data=prepared_csv,
    )
    endpoint = f"{api_url.rstrip('/')}/v2/messages-research/artifacts"
    request = urllib.request.Request(
        endpoint,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(len(body)),
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        raise UploadError(f"upload failed (HTTP {exc.code}): {response_text[:500]}") from exc
    except urllib.error.URLError as exc:
        raise UploadError(f"upload failed: {exc}") from exc

    try:
        response_json = json.loads(response_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise UploadError("upload returned non-JSON response") from exc
    return {
        "status_code": status,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "approved_count": summary["approved_count"],
        "prepared_summary": summary,
        "response": response_json,
        "url": endpoint,
    }


def cmd_summarize(args: argparse.Namespace) -> int:
    try:
        _, summary = prepare_upload_csv(Path(args.csv))
    except UploadError as exc:
        emit({"primitive": "upload_research_review", "command": "summarize", "status": "failed", "error": str(exc)})
        return 1
    emit({
        "primitive": "upload_research_review",
        "command": "summarize",
        "status": "ok",
        "approved_count": summary["approved_count"],
        "skipped_unapproved_count": summary["skipped_unapproved_count"],
    })
    return 0


def cmd_prepare(args: argparse.Namespace) -> int:
    try:
        prepared_csv, summary = prepare_upload_csv(Path(args.csv))
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(prepared_csv)
    except UploadError as exc:
        emit({"primitive": "upload_research_review", "command": "prepare", "status": "failed", "error": str(exc)})
        return 1
    emit({
        "primitive": "upload_research_review",
        "command": "prepare",
        "status": "ok",
        "output": str(output),
        **summary,
    })
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    if not args.confirm_upload:
        emit({
            "primitive": "upload_research_review",
            "command": "upload",
            "status": "blocked",
            "error": "pass --confirm-upload after the user explicitly approves uploading approved contacts",
        })
        return 2
    api_url = args.api_url or os.getenv("POWERPACKS_API_URL") or os.getenv("POWERSET_API_URL") or DEFAULT_API_URL
    try:
        token = args.token or auth_token_from_powerpacks()
        result = upload_review_csv(csv_path=Path(args.csv), api_url=api_url, token=token, timeout=args.timeout)
    except UploadError as exc:
        emit({"primitive": "upload_research_review", "command": "upload", "status": "failed", "error": str(exc)})
        return 1
    emit({"primitive": "upload_research_review", "command": "upload", "status": "ok", **result})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a reviewed messages research CSV to Powerset")
    sub = parser.add_subparsers(dest="command", required=True)

    summarize = sub.add_parser("summarize", help="Preview approved upload count")
    summarize.add_argument("--csv", default=DEFAULT_CSV)
    summarize.set_defaults(func=cmd_summarize)

    prepare = sub.add_parser("prepare", help="Write the upload-normalized CSV without sending it")
    prepare.add_argument("--csv", default=DEFAULT_CSV)
    prepare.add_argument("--output", default=".powerpacks/messages/research_review.upload.csv")
    prepare.set_defaults(func=cmd_prepare)

    upload = sub.add_parser("upload", help="Upload approved contacts from the reviewed contacts CSV")
    upload.add_argument("--csv", default=DEFAULT_CSV)
    upload.add_argument("--api-url", default=None)
    upload.add_argument("--token", default=None)
    upload.add_argument("--timeout", type=int, default=120)
    upload.add_argument("--confirm-upload", action="store_true")
    upload.set_defaults(func=cmd_upload)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

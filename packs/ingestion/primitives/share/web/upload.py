"""One in-process upload for the People page, with progress read from its manifest."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from packs.indexing.primitives.upload_powerset import upload_powerset

SAFE_ERRORS = frozenset({
    "Upload requires the powerset_v2 PostgreSQL login",
    "Upload requires a local search index; build the index first",
    "Upload requires a TurboPuffer API key",
    "Upload requires the shared TurboPuffer v3 namespaces",
    "no users row for the current Powerset credentials; run `$powerset login`",
})


class ShareUpload:
    def __init__(self, share_db: Path, people_csv: Path, *,
                 index_db: Path = upload_powerset.DEFAULT_DB,
                 out_dir: Path = upload_powerset.DEFAULT_OUT_DIR) -> None:
        self.share_db = share_db
        self.people_csv = people_csv
        self.index_db = index_db
        self.out_dir = out_dir
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        manifest = self.out_dir / "manifest.json"
        try:
            saved = json.loads(manifest.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            saved = {}
        with self._lock:
            active = self._thread is not None and self._thread.is_alive()
        state = saved.get("status", "idle")
        if saved.get("dry_run") and state == "completed":
            state = "idle"
        if active:
            state = "running"
        elif state == "running":
            state = "interrupted"
        if state not in {"idle", "running", "completed", "failed", "interrupted"}:
            state = "failed"
        progress = saved.get("progress") or {}
        counts = {key: int(progress.get(key) or 0) for key in ("total", "uploaded", "skipped")}
        namespaces = progress.get("namespaces")
        if isinstance(namespaces, dict):
            counts["namespaces"] = {
                name: {key: int(values.get(key) or 0) for key in ("upserted", "patched")}
                for name, values in namespaces.items() if name in {"people", "summaries", "education", "companies", "schools"}
                and isinstance(values, dict)
            }
        payload: dict[str, Any] = {
            "status": state,
            "progress": counts,
        }
        if state == "running" and isinstance(saved.get("stage"), str):
            stage = saved["stage"]
            payload["stage"] = stage
            payload["message"] = {
                "planning": "Checking your shared people and saved uploads…",
                "checking_access": "Connecting to your shared network…",
                "checking_people": "Comparing your people with the shared network…",
                "checking_companies": "Checking companies already in the shared network…",
                "checking_schools": "Checking schools already in the shared network…",
                "checking_changes": "Checking which people have changed since your last upload…",
                "people": "Uploading people…",
                "summaries": "Uploading profiles…",
                "education": "Uploading education…",
                "companies": "Uploading companies…",
                "schools": "Uploading schools…",
                "committing": "Finishing your upload…",
            }.get(stage, "Uploading your network…")
        for key in ("skipped_no_linkedin", "companies_skipped_no_row"):
            count = (saved.get("plan") or {}).get(key)
            if isinstance(count, int):
                payload[key] = count
        if state == "failed":
            error = saved.get("error")
            safe = isinstance(error, str) and (error in SAFE_ERRORS or re.fullmatch(
                r"Upload requires a current local index; \d+ (?:shared people lack profiles|(?:companies|schools) lack rows)", error))
            payload["error"] = error if safe else "Upload failed; retry to resume"
            stage = saved.get("stage")
            if not safe and stage in {"people", "summaries", "education", "companies", "schools"}:
                name = "profiles" if stage == "summaries" else stage
                payload["error"] = f"Could not upload {name}. Retry to resume."
        if state == "completed" and isinstance(saved.get("result"), dict):
            payload["result"] = saved["result"]
        return payload

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                manifest = self.out_dir / "manifest.json"
                try:
                    previous = json.loads(manifest.read_text(encoding="utf-8"))
                except (FileNotFoundError, json.JSONDecodeError):
                    previous = {}
                running = {key: previous[key] for key in
                           ("target", "person_hashes", "owned_people", "pending_upserts") if key in previous}
                running.update(status="running", stage="planning", dry_run=False,
                               progress={"total": 0, "uploaded": 0, "skipped": 0})
                self.out_dir.mkdir(parents=True, exist_ok=True)
                pending = manifest.with_suffix(".tmp")
                pending.write_text(json.dumps(running) + "\n", encoding="utf-8")
                pending.replace(manifest)
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        return self.status()

    def _run(self) -> None:
        try:
            upload_powerset.UploadPowerset(
                db=self.index_db, share_db=self.share_db, people_csv=self.people_csv,
                out_dir=self.out_dir, dry_run=False,
                env_file=Path(os.environ["POWERPACKS_UPLOAD_ENV_FILE"])
                if os.environ.get("POWERPACKS_UPLOAD_ENV_FILE") else None,
            ).run()
        except Exception:
            # Preserve the uploader's manifest and its recovery data, including on startup failure.
            self.out_dir.mkdir(parents=True, exist_ok=True)
            manifest = self.out_dir / "manifest.json"
            try:
                saved = json.loads(manifest.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                saved = {}
            if saved.get("status") != "failed":
                saved.update(status="failed", error="Upload failed; retry to resume")
            saved.setdefault("progress", {"total": 0, "uploaded": 0, "skipped": 0})
            replacement = manifest.with_suffix(".tmp")
            replacement.write_text(json.dumps(saved) + "\n", encoding="utf-8")
            replacement.replace(manifest)

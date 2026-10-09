"""Run one People upload and project its typed manifest to the status route.

Changelog:
  2026-10-09: reading the status writes nothing; a run the last server left running is marked interrupted
    once, when the server starts.
  2026-10-09: the last upload always shows; the confirm is bound by the share list hash alone.
  2026-10-08: share_changed: the share list differs from the last completed upload's.
  2026-10-08: a check of a share list edited since reads idle, so opening checks again.
  2026-09-27: the job reads the server's environment; no second env file.
  2026-09-27: bind confirm to checked decisions, expose the upload status contract.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

from packs.indexing.primitives.upload_powerset import upload_powerset
from packs.indexing.primitives.upload_powerset.errors import log_error, safe_error
from packs.indexing.primitives.upload_powerset.manifest import (
    CHANGED_CHECK, CHECK_FAILED, CHECK_FIRST, INTERRUPTED, RUN_ACTIVE, UPLOAD_FAILED, Stage,
    UploadManifest, share_digest,
)
from packs.ingestion.primitives.share.store import share_rows

STAGE_MESSAGES: dict[str, str] = {
    Stage.PLANNING: "Checking your shared people and saved uploads…",
    Stage.CHECKING_ACCESS: "Connecting to your shared network…",
    Stage.CHECKING_PEOPLE: "Comparing your people with the shared network…",
    Stage.CHECKING_COMPANIES: "Checking companies already in the shared network…",
    Stage.CHECKING_SCHOOLS: "Checking schools already in the shared network…",
    Stage.CHECKING_CHANGES: "Checking which people have changed since your last upload…",
    Stage.WRITING_PEOPLE: "Writing people and access to the shared network…",
    Stage.PEOPLE: "Uploading people…",
    Stage.SUMMARIES: "Uploading profiles…",
    Stage.EDUCATION: "Uploading education…",
    Stage.COMPANIES: "Uploading companies…",
    Stage.SCHOOLS: "Uploading schools…",
    Stage.COMMITTING: "Finishing your upload…",
}


class ShareUpload:
    def __init__(self, share_db: Path, people_csv: Path, *,
                 index_db: Path = upload_powerset.DEFAULT_DB,
                 out_dir: Path = upload_powerset.DEFAULT_OUT_DIR) -> None:
        self.share_db = share_db
        self.people_csv = people_csv
        self.index_db = index_db
        self.out_dir = out_dir
        self.manifest_path = out_dir / "manifest.json"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._mark_interrupted()

    def _mark_interrupted(self) -> None:
        """A manifest still running when this server starts belongs to a run the last server never
        finished."""
        saved = self._saved()
        if saved.status != "running":
            return
        interrupted = replace(saved, status="interrupted", error=INTERRUPTED, finished_at=upload_powerset.now_iso())
        if not saved.dry_run:
            interrupted = replace(interrupted, last_upload={
                "finished_at": interrupted.finished_at, "status": "interrupted",
                "uploaded": saved.progress["uploaded"], "skipped": saved.progress["skipped"]})
        interrupted.write(self.manifest_path)

    def _saved(self) -> UploadManifest:
        return UploadManifest.read(self.manifest_path)

    def _current_share_digest(self) -> str:
        return share_digest(share_rows(self.share_db))

    def status(self) -> dict[str, Any]:
        """The saved manifest as the page reads it; reading writes nothing."""
        saved = self._saved()
        current_digest = self._current_share_digest()
        if saved.status == "running":
            state = "checking" if saved.dry_run else "uploading"
        elif saved.status == "completed" and saved.dry_run:
            # A check of a share list edited since (or from before the binding, with no digest) is no
            # check: opening checks again rather than showing the old plan.
            state = "ready" if saved.share_digest == current_digest else "idle"
        else:
            state = saved.status
        stage = saved.stage if state in {"checking", "uploading"} else None
        progress = saved.progress
        namespaces = {name: {"upserted": value["upserted"], "patched": value["patched"]}
                      for name, value in progress["namespaces"].items()
                      if name in {"people", "summaries", "education", "companies", "schools"}}
        plan = None if state == "idle" else saved.plan
        plan_counts = None if plan is None else {key: plan.get(key, 0) for key in (
            "marked_share", "with_linkedin", "without_linkedin", "new_to_cloud", "changed",
            "already_shared", "already_in_cloud", "losing_access", "companies_missing")}
        last_upload = saved.last_upload
        error = None
        if state == "interrupted":
            error = INTERRUPTED
        elif state == "failed":
            error = safe_error(RuntimeError(saved.error or ""), CHECK_FAILED if saved.dry_run else UPLOAD_FAILED)
        return {
            "status": state,
            "stage": stage,
            "message": STAGE_MESSAGES.get(stage) if stage else None,
            "progress": {"total": progress["total"], "uploaded": progress["uploaded"],
                         "skipped": progress["skipped"], "namespaces": namespaces},
            "plan": plan_counts,
            "checked": saved.share_digest if state == "ready" else None,
            "last_upload": last_upload,
            # The share list differs from what the last completed upload sent: the page offers an update.
            "share_changed": last_upload is not None and last_upload.get("share_digest") != current_digest,
            "failed_action": ("check" if saved.dry_run else "upload") if state in {"failed", "interrupted"} else None,
            "error": error,
        }

    def start(self, *, dry_run: bool, checked: str | None = None) -> dict[str, Any]:
        """Start a check, or the real upload of the check whose digest the browser displayed."""
        with self._lock:
            active = self._thread is not None and self._thread.is_alive()
            if active and not dry_run:
                raise ValueError(RUN_ACTIVE)
            if not active:
                previous = self._saved()
                if not dry_run:
                    if previous.status != "completed" or not previous.dry_run or not previous.share_digest:
                        raise ValueError(CHECK_FIRST)
                    if checked != previous.share_digest:
                        raise ValueError(CHANGED_CHECK)
                    try:
                        current_digest = self._current_share_digest()
                    except BaseException as exc:
                        if isinstance(exc, KeyboardInterrupt):
                            raise
                        log_error(self.out_dir, Stage.PLANNING, exc)
                        raise ValueError(CHECK_FAILED) from exc
                    if previous.share_digest != current_digest:
                        raise ValueError(CHANGED_CHECK)
                running = replace(previous, status="running", stage=Stage.PLANNING,
                                  dry_run=dry_run, progress=UploadManifest().progress,
                                  error=None, error_type=None)
                running.write(self.manifest_path)
                self._thread = threading.Thread(target=self._run, args=(dry_run,), daemon=True)
                self._thread.start()
        return self.status()

    def _run(self, dry_run: bool) -> None:
        try:
            upload_powerset.UploadPowerset(
                db=self.index_db, share_db=self.share_db, people_csv=self.people_csv,
                out_dir=self.out_dir, dry_run=dry_run,
            ).run()
        except BaseException as exc:
            if isinstance(exc, KeyboardInterrupt):
                raise
            saved = self._saved()
            if saved.status != "failed":
                log_error(self.out_dir, saved.stage or Stage.PLANNING, exc)
                failed = replace(saved, status="failed", error=safe_error(
                    exc, CHECK_FAILED if dry_run else UPLOAD_FAILED))
                if not dry_run:
                    failed = replace(failed, last_upload={
                        "finished_at": upload_powerset.now_iso(), "status": "failed",
                        "uploaded": failed.progress["uploaded"], "skipped": failed.progress["skipped"],
                    })
                failed.write(self.manifest_path)

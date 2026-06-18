#!/usr/bin/env python3
"""LinkedIn -> searchable index pipeline on Modal sandboxes.

Local machine = dispatcher/aggregator: upload inputs to the team Modal Volume,
dispatch per-vertical sandboxes, mirror progress to the onboarding status
files, download the finished local-search.duckdb.

Run via the repo environment (modal is a project dependency, and the Modal
token comes from .env via `$powerset env pull` — no `modal token set` needed):
  uv run --project . python packs/indexing/modal/linkedin_modal_pipeline.py <cmd> ...

Volume layout (shared workspace volume, multi-operator):
  /data/cache/...                      shared caches: enrichment artifacts
                                       (key-union merged after every indexing
                                       run) + profile_cache_v2 (file-per-slug)
  /data/operators/<operator-id>/input  this operator's connections.csv + people.csv
  /data/operators/<operator-id>/runs   this operator's run outputs
  /data/synthetic                      benchmark fixture (amplify)

Commands:
  pipeline  the one-shot: --csv Connections.csv -> Importing (run_linkedin.py,
            RapidAPI always approved) -> Indexing (run_indexing.py) ->
            auto-download. Emits onboarding-v2-format progress to
            .powerpacks/runs/setup-linkedin-modal/. Re-dropping an unchanged
            csv is a no-op.
  upload    push this operator's people.csv (--seed-cache bootstraps /data/cache)
  amplify   build the synthetic Jake-scale dataset in-sandbox (no paid calls)
  run       client-driven benchmark run (streams per-phase, exec per step)
  process   indexing only: server-side run (survives disconnects) + watch
            + auto-download; refreshes enrichment caches on the volume
  download  pull local-search.duckdb + manifest.json for a run label
            (--wait polls status.json until the run finishes)

Provider keys (powerset-rapidapi, powerset-openai) are workspace Modal
Secrets mounted server-side; they never exist on the laptop.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

_REPO_FOR_ENV = Path(__file__).resolve().parents[3]
# MODAL_TOKEN_ID / MODAL_TOKEN_SECRET land in .env via `$powerset env pull`;
# the modal SDK reads them from the environment, so load before importing.
load_dotenv(_REPO_FOR_ENV / ".env", override=False)

# The driver runs locally, so it can mark accounts.json from the repo root.
# `python <script>` only puts the script dir on sys.path, so add the repo root
# for the `packs.*` import below.
sys.path.insert(0, str(_REPO_FOR_ENV))

from packs.ingestion.accounts import update_channel  # noqa: E402
from packs.shared.csv_io import CsvIO  # noqa: E402
import modal  # noqa: E402


def require_modal_credentials() -> None:
    """Fail with actionable guidance instead of an SDK auth traceback.

    We cannot provision Modal credentials locally, but we can say exactly what
    to run.
    """
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return
    if (Path.home() / ".modal.toml").exists():
        return
    raise SystemExit(
        "No Modal credentials found.\n"
        "Fix: run `$powerset login` (or `$powerset env pull`) to write MODAL_TOKEN_ID /\n"
        "MODAL_TOKEN_SECRET into .env for provisioned Powerset users. Then re-run this command."
    )


APP_NAME = os.environ.get("POWERPACKS_MODAL_APP", "powerset-indexing")
# Modal Volumes are workspace-scoped: anyone with a powerset-co token shares
# this default volume; outsiders cannot reach it. Set POWERPACKS_MODAL_VOLUME
# for an isolated volume (recommended once multiple operators run concurrently,
# since input/ and runs/ paths are not yet per-operator prefixed).
VOLUME_NAME = os.environ.get("POWERPACKS_MODAL_VOLUME", "powerset-indexing")
DEFAULT_OPERATOR_ID = os.environ.get("POWERPACKS_OPERATOR_ID", "e33a648a-ae5f-432e-83ce-b90d75546ada")

REPO = Path(__file__).resolve().parents[3]
# .powerpacks lives at the main checkout root; walk up when running from a
# worktree, and require the merged people.csv so a stray sibling .powerpacks
# (e.g. created by local test runs) is not mistaken for the real one.
LOCAL_POWERPACKS = next(
    (
        p / ".powerpacks"
        for p in [REPO, *REPO.parents]
        if (p / ".powerpacks/network-import/merged/people.csv").is_file()
    ),
    REPO / ".powerpacks",
)
PIPELINE = "/repo/packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
DUCKDB_SHIM = "/repo/scripts/build-local-duckdb-shim.py"
BENCH = "/repo/packs/indexing/modal/bench_wrapper.py"
AMPLIFY = "/repo/packs/indexing/modal/amplify_dataset.py"

# Multi-operator volume layout: enrichment caches are shared by every operator
# (keys are content-derived, so overlap across networks = free cache hits);
# inputs and run outputs are per-operator so concurrent operators never
# collide. The sandbox merges run outputs back into the shared cache by key
# union after each successful run.
CACHE_ROOT = "/data/cache"
SYNTHETIC_ROOT = "/data/synthetic"
OPERATOR_ROOT = f"/data/operators/{DEFAULT_OPERATOR_ID}"


def dataset_paths(dataset: str) -> tuple[str, str]:
    """Return (people_csv, cache_root) inside the sandbox for a dataset."""
    if dataset == "synthetic":
        # the amplifier writes a self-contained fixture: people.csv + artifacts/ + seeds/
        return f"{SYNTHETIC_ROOT}/people.csv", SYNTHETIC_ROOT
    return f"{OPERATOR_ROOT}/input/people.csv", CACHE_ROOT


def run_vol_path(label: str) -> str:
    return f"{OPERATOR_ROOT}/runs/{label}"


def operator_volume_prefix() -> str:
    """Volume-relative operator prefix; sandbox paths mount the same key at /data."""
    return OPERATOR_ROOT.removeprefix("/data/").lstrip("/")

# local artifact path (relative to .powerpacks/search-index) -> volume artifact name
REAL_ARTIFACTS = {
    "roles/roles_with_dense_text.jsonl": "roles_with_dense_text.jsonl",
    "roles/roles_with_embeddings.jsonl": "roles_with_embeddings.jsonl",
    "company/companies_corpus_v3.jsonl": "companies_corpus_v3.jsonl",
    "company/company_embeddings_v3.jsonl": "company_embeddings_v3.jsonl",
    "unified/summary_embeddings.jsonl": "summary_embeddings.jsonl",
    "unified/person_tech_skills.jsonl": "person_tech_skills.jsonl",
}
REAL_SEEDS = {
    "unified/roles/founder_enrichment.jsonl": "founder_enrichment.jsonl",
    "unified/inferred_ages.jsonl": "inferred_ages.jsonl",
}


def build_image() -> modal.Image:
    return (
        modal.Image.debian_slim(python_version="3.12")
        .pip_install(
            # Pin to the repo uv.lock versions: output determinism depends on it
            # (snowballstemmer 3.1.1 stems differently than 3.0.1, which changes
            # phrase_tokens and record hashes).
            "duckdb==1.5.2",
            "openai==2.33.0",
            "python-dotenv==1.2.2",
            "pyyaml==6.0.3",
            "snowballstemmer==3.0.1",
            "tiktoken==0.13.0",
        )
        .add_local_dir(REPO / "packs", "/repo/packs")
        .add_local_dir(REPO / "scripts", "/repo/scripts")
    )


def get_volume() -> modal.Volume:
    return modal.Volume.from_name(VOLUME_NAME, create_if_missing=True)


def rapidapi_secret() -> modal.Secret:
    """RapidAPI key for the sandboxes. Prefer a local RAPIDAPI_LINKEDIN_KEY_BACKUP
    from .env so we can swap keys (e.g. when the workspace key hits its quota)
    without touching the shared powerset-rapidapi secret; fall back to it."""
    backup = os.environ.get("RAPIDAPI_LINKEDIN_KEY_BACKUP", "").strip()
    if backup:
        return modal.Secret.from_dict({"RAPIDAPI_LINKEDIN_KEY": backup})
    return modal.Secret.from_name("powerset-rapidapi")


def make_sandbox(cpu: float, memory_mib: int, timeout: int) -> modal.Sandbox:
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    return modal.Sandbox.create(
        app=app,
        image=build_image(),
        volumes={"/data": get_volume()},
        cpu=cpu,
        memory=memory_mib,
        timeout=timeout,
    )


def sb_exec(sb: modal.Sandbox, *cmd: str, stream: bool = True) -> tuple[int, str]:
    proc = sb.exec(*cmd)
    captured: list[str] = []
    for line in proc.stdout:
        captured.append(line)
        if stream:
            print(line, end="", flush=True)
    code = proc.wait()
    if code != 0 and stream:
        for line in proc.stderr:
            print(line, end="", flush=True)
    return code, "".join(captured)


def sb_read_json(sb: modal.Sandbox, path: str) -> dict | None:
    code, out = sb_exec(sb, "cat", path, stream=False)
    if code != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def cmd_upload(args: argparse.Namespace) -> int:
    """Push this operator's people.csv (always) and optionally seed the shared cache.

    --seed-cache bootstraps /data/cache from local enrichment artifacts and is
    an OVERWRITE - use it on an empty/new volume. Day-to-day, the cache grows
    server-side via the post-run key-union merge, so re-seeding is not needed
    (and would discard rows other operators contributed since your local copy).
    """
    people_csv = LOCAL_POWERPACKS / "network-import/merged/people.csv"
    vol = get_volume()
    op_prefix = operator_volume_prefix()
    total_mb = people_csv.stat().st_size / 1e6
    uploaded = 1
    with vol.batch_upload(force=True) as batch:
        batch.put_file(people_csv, f"{op_prefix}/input/people.csv")
        if args.seed_cache:
            search_index = LOCAL_POWERPACKS / "search-index"
            for rel, name in REAL_ARTIFACTS.items():
                src = search_index / rel
                total_mb += src.stat().st_size / 1e6
                uploaded += 1
                batch.put_file(src, f"cache/artifacts/{name}")
            for rel, name in REAL_SEEDS.items():
                src = search_index / rel
                total_mb += src.stat().st_size / 1e6
                uploaded += 1
                batch.put_file(src, f"cache/seeds/{name}")
            profile_cache = LOCAL_POWERPACKS / "network-import/profile_cache_v2"
            if profile_cache.is_dir():
                batch.put_directory(profile_cache, "cache/profile_cache_v2")
                total_mb += sum(f.stat().st_size for f in profile_cache.iterdir() if f.is_file()) / 1e6
                uploaded += 1
    print(f"uploaded {uploaded} files ({total_mb:.0f} MB) to volume {VOLUME_NAME} "
          f"(people.csv -> {op_prefix}/input/{', cache seeded' if args.seed_cache else ''})")
    return 0


PIPELINE_VERTICAL = "linkedin_modal"
PIPELINE_STAGES = [
    {"id": "importing", "label": "Importing contacts"},
    {"id": "indexing", "label": "Building search index"},
]
PROGRESS_DIR = LOCAL_POWERPACKS / "runs/setup-linkedin-modal"
IMPORT_LABEL = "linkedin-import"
INDEX_LABEL = "linkedin-index"


class PipelineProgress:
    """Onboarding-v2-format progress files the console polls.

    Same schema as setup_linkedin_csv.RunContext (status.json + events.jsonl,
    atomic writes), collapsed to the two user-facing stages.
    """

    def __init__(self, progress_dir: Path = PROGRESS_DIR, stages: list[dict] = PIPELINE_STAGES,
                 vertical: str = PIPELINE_VERTICAL) -> None:
        self.run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.stages = stages
        self.vertical = vertical
        progress_dir.mkdir(parents=True, exist_ok=True)
        self.status_path = progress_dir / "status.json"
        self.events_path = progress_dir / "events.jsonl"
        self.events_path.write_text("")
        self.status: dict = {
            "schema_version": 1,
            "vertical": vertical,
            "run_id": self.run_id,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "progress": 0.0,
            "current_stage": stages[0]["id"],
            "stages": {},
            "stage_order": stages,
        }
        self._write()

    def _write(self) -> None:
        self.status["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = self.status_path.with_name(self.status_path.name + ".tmp")
        tmp.write_text(json.dumps(self.status, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.status_path)

    def event(self, stage_id: str, message: str, *, status: str = "running",
              progress: float | None = None, payload: dict | None = None) -> None:
        index = next((i for i, s in enumerate(self.stages) if s["id"] == stage_id), 0) + 1
        label = self.stages[index - 1]["label"]
        if progress is None:
            progress = index / len(self.stages) if status == "completed" else (index - 1) / len(self.stages)
        record = {
            "vertical": self.vertical,
            "run_id": self.run_id,
            "stage": stage_id,
            "stage_label": label,
            "stage_index": index,
            "stage_total": len(self.stages),
            "status": status,
            "message": message,
            "progress": round(progress, 3),
            "payload": payload or {},
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
        self.status["current_stage"] = stage_id
        self.status["progress"] = record["progress"]
        self.status["stages"][stage_id] = {
            "status": status, "label": label, "message": message,
            "updated_at": record["updated_at"], "payload": payload or {},
        }
        if status == "failed":
            self.status["status"] = "failed"
        self._write()
        print(f"[{stage_id}] {message}", flush=True)

    def finish(self, result: dict) -> None:
        self.status |= {"status": "completed", "progress": 1.0, "result": result}
        self._write()


def csv_connection_rows(path: Path) -> int:
    """Count data rows in a LinkedIn export (skips the Notes preamble)."""
    count = 0
    seen_header = False
    with path.open(encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            if not seen_header:
                if line.startswith("First Name,"):
                    seen_header = True
                continue
            if line.strip():
                count += 1
    return count


def estimate_seconds(rows: int, misses: int | None = None) -> int:
    """Rough wall-time estimate; misses defaults to rows (cold worst case)."""
    rapid = (misses if misses is not None else rows) / 3.3  # 200/min RapidAPI
    return int(100 + rapid + 0.15 * rows)  # dispatch/download overhead + import + index


def reset_run_status(vol: modal.Volume, label: str) -> None:
    """Write a pending placeholder BEFORE dispatching so a watcher can never
    mistake the previous run's terminal status.json for the new run."""
    payload = json.dumps({"status": "pending", "phase": "dispatch",
                          "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}).encode()
    with vol.batch_upload(force=True) as batch:
        batch.put_file(io.BytesIO(payload), run_vol_path(label).removeprefix("/data/") + "/status.json")


def read_run_status(label: str) -> dict | None:
    vol = get_volume()
    path = run_vol_path(label).removeprefix("/data/") + "/status.json"
    try:
        return json.loads(b"".join(vol.read_file(path)))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def watch_run(label: str, progress: PipelineProgress, stage_id: str, message_prefix: str,
              timeout_s: int = 7200) -> dict | None:
    """Poll a sandbox run's volume status and mirror it into the local stage."""
    deadline = time.time() + timeout_s
    last_phase = None
    while time.time() < deadline:
        payload = read_run_status(label)
        if payload:
            phase = payload.get("phase")
            if phase != last_phase:
                last_phase = phase
                progress.event(stage_id, f"{message_prefix}: {phase}", payload={"phase": phase})
            if payload.get("status") in ("completed", "failed"):
                return payload
        time.sleep(3)
    return None


def mark_linkedin_linked(csv_path: Path) -> None:
    """Flip the linkedin_csv channel to linked in accounts.json on a successful
    import so the console source page and sidebar show LinkedIn connected.

    v2 onboarding writes accounts.json; the v3 Modal path historically did not,
    so a v3-imported LinkedIn looked unlinked. Best-effort: a status-file write
    must never fail an otherwise-successful import.
    """
    try:
        update_channel("linkedin_csv", linked=True, success=True, artifact=str(csv_path))
    except Exception as exc:  # never fail the pipeline on a status write
        print(f"[accounts] could not mark linkedin linked: {exc}", flush=True)


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Drop a Connections.csv -> searchable local-search.duckdb.

    Importing (run_linkedin.py: parse + RapidAPI enrich, always approved) ->
    Indexing (run_indexing.py: cache-replayed processing + duckdb) ->
    auto-download. Re-dropping an unchanged csv is a no-op.
    """
    csv_path = Path(args.csv).expanduser()
    if not csv_path.exists():
        raise SystemExit(f"missing csv: {csv_path}")
    rows = csv_connection_rows(csv_path)
    csv_sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    vol = get_volume()
    op_prefix = f"operators/{DEFAULT_OPERATOR_ID}"
    progress = PipelineProgress()

    # No-op when the same csv was already fully processed for this operator.
    try:
        last_sha = b"".join(vol.read_file(f"{op_prefix}/runs/last-input.sha")).decode().strip()
    except FileNotFoundError:
        last_sha = ""
    if last_sha == csv_sha and not args.force:
        progress.event("importing", "Connections.csv unchanged - already imported", status="completed")
        # Remote is already up to date, but a clean/fresh local workspace may have
        # no local-search.duckdb. Hydrate it from the existing run instead of
        # finishing with empty local artifacts.
        dest = Path(args.dest) if args.dest else LOCAL_POWERPACKS / "search-index"
        local_duckdb = dest / "local-search.duckdb"
        downloaded = False
        if not local_duckdb.exists() or local_duckdb.stat().st_size == 0:
            progress.event("indexing", "Downloading existing search index")
            code = cmd_download(argparse.Namespace(label=INDEX_LABEL, dest=args.dest, wait=False))
            if code != 0:
                progress.event("indexing", "Could not download existing search index", status="failed")
                return code
            downloaded = True
        progress.event(
            "indexing",
            "Search index is ready" if downloaded else "Search index already up to date",
            status="completed",
            progress=1.0,
        )
        mark_linkedin_linked(csv_path)
        result = {"noop": True, "csv": str(csv_path), "connections": rows,
                  "downloaded": downloaded, "duckdb": str(local_duckdb)}
        progress.finish(result)
        print(json.dumps({"status": "noop", "connections": rows, "downloaded": downloaded}))
        return 0

    eta = estimate_seconds(rows)
    progress.event("importing", f"Uploading {csv_path.name} ({rows} connections, ~{eta // 60 + 1} min estimated)",
                   payload={"connections": rows, "estimated_seconds": eta})
    with vol.batch_upload(force=True) as batch:
        batch.put_file(csv_path, f"{op_prefix}/input/connections.csv")

    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    import_vol = run_vol_path(IMPORT_LABEL)
    reset_run_status(vol, IMPORT_LABEL)
    sb = modal.Sandbox.create(
        "python", "/repo/packs/indexing/modal/bench_wrapper.py", f"{import_vol}/bench-import.json",
        "python", "/repo/packs/indexing/modal/run_linkedin.py",
        "--connections-csv", f"{OPERATOR_ROOT}/input/connections.csv",
        "--people-out", f"{OPERATOR_ROOT}/input/people.csv",
        "--cache-root", CACHE_ROOT,
        "--run-vol", import_vol,
        "--operator-id", DEFAULT_OPERATOR_ID,
        "--source-user", args.source_user,
        app=app,
        image=build_image(),
        volumes={"/data": vol},
        secrets=[rapidapi_secret()],
        cpu=2,
        memory=4096,
        timeout=args.timeout,
    )
    progress.event("importing", "Importing and enriching contacts", payload={"sandbox": sb.object_id})
    payload = watch_run(IMPORT_LABEL, progress, "importing", "Importing")
    if not payload or payload.get("status") != "completed":
        progress.event("importing", f"Import failed: {(payload or {}).get('error') or (payload or {}).get('phase')}", status="failed", payload=payload or {})
        return 1
    stats = payload.get("stats") or {}
    progress.event("importing", f"Imported {stats.get('people')} contacts "
                   f"({stats.get('cache_hit_count') or 0} cached, {stats.get('paid_call_count') or 0} fetched)",
                   status="completed", payload=stats)

    # Indexing: same server-side runner as `process`, with paid enrichment
    # allowed for genuinely-new roles/companies (estimate-capped).
    index_vol = run_vol_path(INDEX_LABEL)
    people_csv, cache_root = dataset_paths("real")
    reset_run_status(vol, INDEX_LABEL)
    sb2 = modal.Sandbox.create(
        "python", "/repo/packs/indexing/modal/run_indexing.py",
        "--people-csv", people_csv,
        "--cache-root", cache_root,
        "--run-vol", index_vol,
        "--operator-id", DEFAULT_OPERATOR_ID,
        "--enrich", "--max-usd", str(args.max_usd),
        app=app,
        image=build_image(),
        volumes={"/data": vol},
        # openai for LLM classification; rapidapi to hydrate company details for
        # the long-tail companies not in the corpus (cached on the volume).
        secrets=[modal.Secret.from_name("powerset-openai"), rapidapi_secret()],
        cpu=4,
        memory=16384,
        timeout=args.timeout,
    )
    progress.event("indexing", "Building search records", payload={"sandbox": sb2.object_id})
    payload = watch_run(INDEX_LABEL, progress, "indexing", "Indexing")
    if not payload or payload.get("status") != "completed":
        progress.event("indexing", f"Indexing failed: {(payload or {}).get('error') or (payload or {}).get('phase')}", status="failed", payload=payload or {})
        return 1

    dl = argparse.Namespace(label=INDEX_LABEL, dest=args.dest, wait=False)
    code = cmd_download(dl)
    if code != 0:
        progress.event("indexing", "Download failed", status="failed")
        return code
    with vol.batch_upload(force=True) as batch:
        batch.put_file(io.BytesIO(csv_sha.encode()), f"{op_prefix}/runs/last-input.sha")
    dest = Path(args.dest) if args.dest else LOCAL_POWERPACKS / "search-index"
    result = {"csv": str(csv_path), "connections": rows, "import": stats,
              "duckdb": str(dest / "local-search.duckdb")}
    progress.event("indexing", "Search index is ready", status="completed", progress=1.0, payload=result)
    mark_linkedin_linked(csv_path)
    progress.finish(result)
    print(json.dumps({"status": "completed", **result}))
    return 0


GMAIL_VERTICAL = "gmail_modal"
GMAIL_STAGES = [
    {"id": "enriching", "label": "Enriching contacts"},
    {"id": "importing", "label": "Loading enriched contacts"},
    {"id": "indexing", "label": "Building search index"},
]
GMAIL_PROGRESS_DIR = LOCAL_POWERPACKS / "runs/setup-gmail-modal"
GMAIL_INDEX_LABEL = "gmail-index"


def people_csv_rows(path: Path) -> int:
    """Count data rows (real CSV records, not lines).

    people.csv carries multi-line rapidapi_response JSON, so a plain line count
    overcounts badly; parse with csv to count one row per actual person.
    """
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        reader = CsvIO.reader(handle)
        if next(reader, None) is None:  # header
            return 0
        return sum(1 for _ in reader)


def cmd_index_people(args: argparse.Namespace) -> int:
    """Index an already-enriched people.csv on Modal (no RapidAPI import stage).

    Gmail enriches locally (Parallel.ai email+context); this ships the resulting
    people.csv to the volume and runs the SAME indexing sandbox the LinkedIn
    pipeline uses. Use it for any source whose enrichment already happened
    locally, so Modal only does role/company/age classification + duckdb.
    """
    people_path = Path(args.people_csv).expanduser()
    if not people_path.exists():
        raise SystemExit(f"missing people.csv: {people_path}")
    rows = people_csv_rows(people_path)
    progress = PipelineProgress(GMAIL_PROGRESS_DIR, GMAIL_STAGES, GMAIL_VERTICAL)
    # Enrich already happened locally (Parallel) before this command runs.
    progress.event("enriching", "Enriched contacts locally", status="completed", payload={"contacts": rows})

    vol = get_volume()
    progress.event("importing", f"Uploading {rows} enriched contacts", payload={"contacts": rows})
    with vol.batch_upload(force=True) as batch:
        batch.put_file(people_path, f"{operator_volume_prefix()}/input/people.csv")
    progress.event("importing", f"Loaded {rows} contacts", status="completed", payload={"contacts": rows})

    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    index_vol = run_vol_path(GMAIL_INDEX_LABEL)
    people_csv, cache_root = dataset_paths("real")
    reset_run_status(vol, GMAIL_INDEX_LABEL)
    sb = modal.Sandbox.create(
        "python", "/repo/packs/indexing/modal/run_indexing.py",
        "--people-csv", people_csv,
        "--cache-root", cache_root,
        "--run-vol", index_vol,
        "--operator-id", DEFAULT_OPERATOR_ID,
        "--enrich", "--max-usd", str(args.max_usd),
        app=app,
        image=build_image(),
        volumes={"/data": vol},
        secrets=[modal.Secret.from_name("powerset-openai"), rapidapi_secret()],
        cpu=4,
        memory=16384,
        timeout=args.timeout,
    )
    progress.event("indexing", "Building search records", payload={"sandbox": sb.object_id})
    payload = watch_run(GMAIL_INDEX_LABEL, progress, "indexing", "Indexing")
    if not payload or payload.get("status") != "completed":
        progress.event("indexing", f"Indexing failed: {(payload or {}).get('error') or (payload or {}).get('phase')}", status="failed", payload=payload or {})
        return 1

    dl = argparse.Namespace(label=GMAIL_INDEX_LABEL, dest=args.dest, wait=False)
    code = cmd_download(dl)
    if code != 0:
        progress.event("indexing", "Download failed", status="failed")
        return code
    dest = Path(args.dest) if args.dest else LOCAL_POWERPACKS / "search-index"
    result = {"people_csv": str(people_path), "contacts": rows,
              "duckdb": str(dest / "local-search.duckdb")}
    progress.event("indexing", "Search index is ready", status="completed", progress=1.0, payload=result)
    progress.finish(result)
    print(json.dumps({"status": "completed", **result}))
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    """Fully automatic: dispatch a server-side run, watch it, download results.

    The sandbox entrypoint (run_indexing.py) owns seed -> pipeline -> duckdb
    -> persist -> status.json, so the cloud run completes and persists even if
    this driver disconnects; re-attach later with `download --wait`.
    """
    label = args.label or f"{args.dataset}-process"
    people_csv, cache_root = dataset_paths(args.dataset)
    run_vol = run_vol_path(label)
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    entrypoint = [
        "python", "/repo/packs/indexing/modal/run_indexing.py",
        "--people-csv", people_csv,
        "--cache-root", cache_root,
        "--run-vol", run_vol,
        "--operator-id", DEFAULT_OPERATOR_ID,
    ]
    if args.persist_artifacts:
        entrypoint.append("--persist-artifacts")
    if args.dataset == "synthetic":
        entrypoint.append("--no-refresh-cache")
    secrets: list[modal.Secret] = []
    if getattr(args, "enrich", False):
        # Workspace-scoped secret (powerset-co members only). Only mounted for
        # --enrich runs; default runs stay replay-only with no key in the
        # sandbox, so they cannot spend.
        secrets.append(modal.Secret.from_name("powerset-openai"))
        # rapidapi hydrates company details (by id and by slug) for companies not
        # in the corpus, so the LLM classifies them with real context; the cache
        # lands on the volume for reuse.
        secrets.append(rapidapi_secret())
        entrypoint += ["--enrich", "--max-usd", str(args.max_usd)]
    started = time.time()
    sb = modal.Sandbox.create(
        *entrypoint,
        app=app,
        image=build_image(),
        volumes={"/data": get_volume()},
        secrets=secrets,
        cpu=args.cpu,
        memory=args.memory_mib,
        timeout=args.timeout,
    )
    print(f"dispatched sandbox {sb.object_id} (cpu={args.cpu} mem={args.memory_mib}MiB) run={label}")
    print("safe to disconnect: the run persists server-side; re-attach with "
          f"`download --wait --label {label}`")
    for line in sb.stdout:
        print(line, end="", flush=True)
    sb.wait()
    print(f"sandbox finished after {time.time() - started:.0f}s")
    # The server-side status.json is the source of truth for run outcome
    # (sb.wait() returns None on success in modal 1.x); download --wait
    # verifies it says completed before pulling artifacts.
    args.label = label
    args.dest = getattr(args, "dest", None)
    args.wait = True
    return cmd_download(args)


def wait_for_status(label: str, timeout_s: int = 7200) -> dict | None:
    vol = get_volume()
    # volume reads are relative to the volume root (no /data prefix)
    status_path = run_vol_path(label).removeprefix("/data/") + "/status.json"
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            data = b"".join(vol.read_file(status_path))
            payload = json.loads(data)
        except (FileNotFoundError, json.JSONDecodeError):
            payload = None
        if payload and payload.get("status") in ("completed", "failed"):
            return payload
        if payload:
            print(f"  status={payload.get('status')} phase={payload.get('phase')} ({payload.get('updated_at')})")
        time.sleep(30)
    return None


def pull_volume_file(vol: modal.Volume, remote_key: str, target: Path) -> int:
    """Stream one volume file to a local path (atomic via .tmp; prior copy -> .bkup).

    Shared by cmd_download (search artifacts) and cmd_import_linkedin (the enriched
    people.csv). Returns bytes written, or -1 if the remote file is missing.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        backup = target.with_name(target.name + ".bkup")
        target.replace(backup)
        print(f"existing {target.name} renamed to {backup.name}")
    tmp = target.with_name(target.name + ".tmp")
    written = 0
    try:
        with tmp.open("wb") as handle:
            for chunk in vol.read_file(remote_key):
                handle.write(chunk)
                written += len(chunk)
    except FileNotFoundError:
        tmp.unlink(missing_ok=True)
        return -1
    tmp.replace(target)
    return written


def cmd_download(args: argparse.Namespace) -> int:
    """Pull the search artifacts a local machine actually consumes.

    The volume stays the durable home for ledger/records/enrichment caches
    (resume + incremental state); local search only needs local-search.duckdb
    plus manifest.json.
    """
    run_prefix = run_vol_path(args.label).removeprefix("/data/")
    if getattr(args, "wait", False):
        print(f"waiting for {run_prefix}/status.json ...")
        payload = wait_for_status(args.label)
        if not payload:
            print("timed out waiting for run status")
            return 1
        if payload.get("status") != "completed":
            print(f"run finished with status={payload.get('status')} phase={payload.get('phase')}")
            return 1
    vol = get_volume()
    dest = Path(args.dest) if args.dest else LOCAL_POWERPACKS / "search-index"
    dest.mkdir(parents=True, exist_ok=True)
    started = time.time()
    for name in ("local-search.duckdb", "manifest.json"):
        remote = f"{run_prefix}/{name}"
        target = dest / name
        written = pull_volume_file(vol, remote, target)
        if written < 0:
            print(f"missing on volume (was the run made with --persist-artifacts?): {remote}")
            return 1
        elapsed = max(time.time() - started, 0.001)
        print(f"downloaded {remote} -> {target} ({written / 1e6:.0f} MB, {written / 1e6 / elapsed:.0f} MB/s)")
        started = time.time()
    return 0


def cmd_import_linkedin(args: argparse.Namespace) -> int:
    """Connections.csv -> enriched people.csv on Modal (import/enrich only).

    Runs the SAME run_linkedin.py RapidAPI enrichment sandbox as `pipeline`'s
    first stage, but stops before indexing and downloads the enriched people.csv
    so it can be merged with other sources (e.g. Gmail) ahead of a single
    `index-people` pass. Default dest is the canonical LinkedIn import path the
    fan-in merge reads. Enrichment caches on the shared volume keep reruns cheap.
    """
    csv_path = Path(args.csv).expanduser()
    if not csv_path.exists():
        raise SystemExit(f"missing csv: {csv_path}")
    rows = csv_connection_rows(csv_path)
    vol = get_volume()
    op_prefix = f"operators/{DEFAULT_OPERATOR_ID}"
    progress = PipelineProgress()

    eta = estimate_seconds(rows)
    progress.event("importing", f"Uploading {csv_path.name} ({rows} connections, ~{eta // 60 + 1} min estimated)",
                   payload={"connections": rows, "estimated_seconds": eta})
    with vol.batch_upload(force=True) as batch:
        batch.put_file(csv_path, f"{op_prefix}/input/connections.csv")

    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    import_vol = run_vol_path(IMPORT_LABEL)
    reset_run_status(vol, IMPORT_LABEL)
    sb = modal.Sandbox.create(
        "python", "/repo/packs/indexing/modal/bench_wrapper.py", f"{import_vol}/bench-import.json",
        "python", "/repo/packs/indexing/modal/run_linkedin.py",
        "--connections-csv", f"{OPERATOR_ROOT}/input/connections.csv",
        "--people-out", f"{OPERATOR_ROOT}/input/people.csv",
        "--cache-root", CACHE_ROOT,
        "--run-vol", import_vol,
        "--operator-id", DEFAULT_OPERATOR_ID,
        "--source-user", args.source_user,
        app=app,
        image=build_image(),
        volumes={"/data": vol},
        secrets=[rapidapi_secret()],
        cpu=2,
        memory=4096,
        timeout=args.timeout,
    )
    progress.event("importing", "Importing and enriching contacts", payload={"sandbox": sb.object_id})
    payload = watch_run(IMPORT_LABEL, progress, "importing", "Importing")
    if not payload or payload.get("status") != "completed":
        progress.event("importing", f"Import failed: {(payload or {}).get('error') or (payload or {}).get('phase')}", status="failed", payload=payload or {})
        return 1
    stats = payload.get("stats") or {}
    progress.event("importing", f"Imported {stats.get('people')} contacts "
                   f"({stats.get('cache_hit_count') or 0} cached, {stats.get('paid_call_count') or 0} fetched)",
                   payload=stats)

    # Pull the enriched people.csv to the canonical LinkedIn import path so the
    # fan-in merge can pick it up (reuses the shared volume->local file pull).
    dest = Path(args.dest).expanduser() if args.dest else LOCAL_POWERPACKS / "network-import/import/linkedin/people.csv"
    written = pull_volume_file(vol, f"{op_prefix}/input/people.csv", dest)
    if written < 0:
        progress.event("importing", "Enriched people.csv missing on volume", status="failed")
        print(json.dumps({"status": "failed", "error": f"people.csv missing on volume for {op_prefix}"}))
        return 1
    progress.event("importing", "Enriched people.csv downloaded", status="completed", progress=1.0,
                   payload={"people_csv": str(dest), "bytes": written})
    result = {"status": "completed", "csv": str(csv_path), "connections": rows,
              "people": stats.get("people"), "people_csv": str(dest), "bytes": written}
    progress.finish(result)
    print(json.dumps(result))
    return 0


def cmd_preload(args: argparse.Namespace) -> int:
    """Union-merge local cache payloads into the shared volume cache.

    Only the LLM-classification artifacts are worth shipping (embeddings are
    re-computable for pennies). Profile caches upload as one tarball (one
    sequential transfer instead of tens of thousands of small file ops) and
    are merged copy-if-absent server-side.
    """
    vol = get_volume()
    uploads: list[tuple[Path, str]] = []
    for flag, name in (
        ("role_classifications", "roles_with_dense_text.jsonl"),
        ("role_embeddings", "roles_with_embeddings.jsonl"),
        ("company_classifications", "companies_corpus_v3.jsonl"),
        ("company_embeddings", "company_embeddings_v3.jsonl"),
        ("summary_embeddings", "summary_embeddings.jsonl"),
    ):
        value = getattr(args, flag, None)
        if value:
            src = Path(value).expanduser()
            if not src.exists():
                raise SystemExit(f"missing: {src}")
            uploads.append((src, f"incoming/{name}"))

    tarball: Path | None = None
    if args.profile_cache:
        cache_dir = Path(args.profile_cache).expanduser()
        if not cache_dir.is_dir():
            raise SystemExit(f"missing profile cache dir: {cache_dir}")
        tarball = Path(tempfile.mkdtemp()) / "profile_cache_v2.tar.gz"
        print(f"packing {cache_dir} ...")
        subprocess.run(["tar", "-czf", str(tarball), "-C", str(cache_dir), "."], check=True)
        uploads.append((tarball, "incoming/profile_cache_v2.tar.gz"))

    if not uploads:
        raise SystemExit("nothing to preload; pass at least one payload flag")
    total_mb = sum(src.stat().st_size for src, _ in uploads) / 1e6
    print(f"uploading {len(uploads)} payloads ({total_mb:.0f} MB) ...")
    started = time.time()
    with vol.batch_upload(force=True) as batch:
        for src, remote in uploads:
            batch.put_file(src, remote)
    print(f"uploaded in {time.time() - started:.0f}s; merging server-side ...")
    if tarball:
        tarball.unlink(missing_ok=True)

    sb = make_sandbox(cpu=2, memory_mib=4096, timeout=1800)
    try:
        code, _ = sb_exec(sb, "python", "/repo/packs/indexing/modal/merge_incoming.py")
        return code
    finally:
        sb.terminate()


def cmd_amplify(args: argparse.Namespace) -> int:
    sb = make_sandbox(cpu=args.cpu, memory_mib=args.memory_mib, timeout=args.timeout)
    print(f"sandbox {sb.object_id} (cpu={args.cpu} mem={args.memory_mib}MiB)")
    try:
        code, _ = sb_exec(
            sb, "python", AMPLIFY,
            "--people-csv", f"{OPERATOR_ROOT}/input/people.csv",
            "--artifacts-dir", f"{CACHE_ROOT}/artifacts",
            "--output-dir", SYNTHETIC_ROOT,
            "--target-people", str(args.target_people),
            "--target-roles", str(args.target_roles),
            "--target-companies", str(args.target_companies),
        )
        return code
    finally:
        sb.terminate()


def pipeline_cmd(people_csv: str, out_dir: str, artifacts: str) -> list[str]:
    return [
        "python", PIPELINE, "run",
        "--input", people_csv,
        "--output-dir", out_dir,
        "--default-operator-id", DEFAULT_OPERATOR_ID,
        "--role-input-classifications", f"{artifacts}/roles_with_dense_text.jsonl",
        "--role-input-embeddings", f"{artifacts}/roles_with_embeddings.jsonl",
        "--company-input-classifications", f"{artifacts}/companies_corpus_v3.jsonl",
        "--company-input-embeddings", f"{artifacts}/company_embeddings_v3.jsonl",
        "--summary-input-embeddings", f"{artifacts}/summary_embeddings.jsonl",
        "--person-tech-skills-input", f"{artifacts}/person_tech_skills.jsonl",
    ]


def step_durations(ledger: dict) -> list[tuple[str, float | None]]:
    rows: list[tuple[str, float | None]] = []
    prev = None
    for step in ledger.get("steps", []):
        ts = step.get("updated_at")
        dur = None
        if ts and prev:
            try:
                dur = (time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
                       - time.mktime(time.strptime(prev, "%Y-%m-%dT%H:%M:%SZ")))
            except ValueError:
                dur = None
        rows.append((step.get("id", "?"), dur))
        if ts:
            prev = ts
    return rows


def cmd_run(args: argparse.Namespace) -> int:
    people_csv, cache_root = dataset_paths(args.dataset)
    artifacts = f"{cache_root}/artifacts"
    seeds = f"{cache_root}/seeds"
    label = args.label or f"{args.dataset}-{int(args.cpu)}cpu-{args.memory_mib}mib"
    run_vol = run_vol_path(label)
    work = "/tmp/run/search-index"  # container-local disk; results copied to volume after

    sb = make_sandbox(cpu=args.cpu, memory_mib=args.memory_mib, timeout=args.timeout)
    print(f"sandbox {sb.object_id} (cpu={args.cpu} mem={args.memory_mib}MiB) run={label}")
    try:
        sb_exec(sb, "bash", "-c",
                f"mkdir -p {work}/unified/roles {run_vol} && "
                f"cp {seeds}/founder_enrichment.jsonl {work}/unified/roles/founder_enrichment.jsonl && "
                f"cp {seeds}/inferred_ages.jsonl {work}/unified/inferred_ages.jsonl")

        print("--- pipeline ---", flush=True)
        code, _ = sb_exec(sb, "python", BENCH, f"{run_vol}/bench-pipeline.json",
                          *pipeline_cmd(people_csv, work, artifacts))
        pipeline_ok = code == 0

        duckdb_ok = False
        if pipeline_ok:
            print("--- duckdb build ---", flush=True)
            code, _ = sb_exec(sb, "python", BENCH, f"{run_vol}/bench-duckdb.json",
                              "python", DUCKDB_SHIM,
                              "--records-dir", work,
                              "--output-dir", work,
                              "--operator-id", DEFAULT_OPERATOR_ID,
                              "--force")
            duckdb_ok = code == 0

        # persist reports + small metadata to the volume (artifacts optionally)
        keep = "ledger.json manifest.json stats local-search.duckdb" if args.persist_artifacts else "ledger.json manifest.json stats"
        sb_exec(sb, "bash", "-c",
                f"cd {work} 2>/dev/null && for f in {keep}; do [ -e $f ] && cp -r $f {run_vol}/; done; "
                f"du -sh {work} {work}/local-search.duckdb 2>/dev/null | tee {run_vol}/sizes.txt; true")
        if args.persist_artifacts:
            sb_exec(sb, "bash", "-c", f"cp -r {work}/records {run_vol}/ 2>/dev/null; true")

        bench = sb_read_json(sb, f"{run_vol}/bench-pipeline.json") or {}
        bench_db = sb_read_json(sb, f"{run_vol}/bench-duckdb.json") or {}
        ledger = sb_read_json(sb, f"{work}/ledger.json") or {}

        print(f"\n=== {label} ===")
        print(f"pipeline: ok={pipeline_ok} wall={bench.get('wall_seconds')}s peak_rss={bench.get('max_rss_mb')}MB "
              f"(sampled {bench.get('sampled_peak_rss_mb')}MB)")
        print(f"duckdb:   ok={duckdb_ok} wall={bench_db.get('wall_seconds')}s peak_rss={bench_db.get('max_rss_mb')}MB")
        print("steps (gap between ledger updated_at stamps):")
        for sid, dur in step_durations(ledger):
            print(f"  {sid:<24} {'' if dur is None else f'{dur:>6.0f}s'}")
        for step in ledger.get("steps", []):
            if step.get("id") == "validate_contracts":
                validation = (step.get("stats") or {}).get("validation") or {}
                bad = {k: v.get("errors") for k, v in validation.items() if "ok" in v and not v.get("ok")}
                print(f"validate_contracts: {'ALL OK' if not bad else bad}")
        return 0 if (pipeline_ok and duckdb_ok) else 1
    finally:
        sb.terminate()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    pipe = sub.add_parser("pipeline", help="Connections.csv -> searchable duckdb (Importing -> Indexing)")
    pipe.add_argument("--csv", required=True, help="path to the LinkedIn Connections.csv export")
    pipe.add_argument("--source-user", default="linkedin")
    pipe.add_argument("--dest", help="download destination; defaults to .powerpacks/search-index")
    pipe.add_argument("--timeout", type=int, default=7200)
    pipe.add_argument("--max-usd", type=float, default=0.0,
                      help="0 (default) = uncapped, no estimate pass (internal team default); >0 adds a dry-run estimate gate")
    pipe.add_argument("--force", action="store_true", help="reprocess even if the csv is unchanged")

    imp = sub.add_parser("import-linkedin", help="Connections.csv -> enriched people.csv on Modal (import/enrich only; downloads people.csv for source merge)")
    imp.add_argument("--csv", required=True, help="path to the LinkedIn Connections.csv export")
    imp.add_argument("--source-user", default="linkedin")
    imp.add_argument("--dest", help="download dest for the enriched people.csv; defaults to .powerpacks/network-import/import/linkedin/people.csv")
    imp.add_argument("--timeout", type=int, default=7200)

    idx = sub.add_parser("index-people", help="index an already-enriched people.csv (no import stage) -> searchable duckdb")
    idx.add_argument("--people-csv", required=True, help="path to an enriched people.csv (e.g. the Gmail merged people.csv)")
    idx.add_argument("--dest", help="download destination; defaults to .powerpacks/search-index")
    idx.add_argument("--timeout", type=int, default=7200)
    idx.add_argument("--max-usd", type=float, default=0.0,
                     help="0 (default) = uncapped, no estimate pass; >0 adds a dry-run estimate gate")

    pre = sub.add_parser("preload", help="union-merge local cache payloads into the shared volume cache")
    pre.add_argument("--role-classifications")
    pre.add_argument("--role-embeddings")
    pre.add_argument("--company-classifications")
    pre.add_argument("--company-embeddings")
    pre.add_argument("--summary-embeddings")
    pre.add_argument("--profile-cache", help="directory of slug.json profiles (uploaded as one tarball)")

    up = sub.add_parser("upload")
    up.add_argument("--seed-cache", action="store_true",
                    help="bootstrap the shared /data/cache from local artifacts (overwrite; new/empty volumes only)")

    amp = sub.add_parser("amplify")
    amp.add_argument("--cpu", type=float, default=4)
    amp.add_argument("--memory-mib", type=int, default=16384)
    amp.add_argument("--timeout", type=int, default=3600)
    amp.add_argument("--target-people", type=int, default=6200)
    amp.add_argument("--target-roles", type=int, default=39400)
    amp.add_argument("--target-companies", type=int, default=28800)

    run = sub.add_parser("run")
    run.add_argument("--dataset", choices=["real", "synthetic"], required=True)
    run.add_argument("--cpu", type=float, default=4)
    run.add_argument("--memory-mib", type=int, default=16384)
    run.add_argument("--timeout", type=int, default=7200)
    run.add_argument("--label")
    run.add_argument("--persist-artifacts", action="store_true")

    proc = sub.add_parser("process", help="dispatch server-side run, watch, auto-download")
    proc.add_argument("--dataset", choices=["real", "synthetic"], required=True)
    proc.add_argument("--cpu", type=float, default=4)
    proc.add_argument("--memory-mib", type=int, default=16384)
    proc.add_argument("--timeout", type=int, default=7200)
    proc.add_argument("--label")
    proc.add_argument("--persist-artifacts", action="store_true")
    proc.add_argument("--dest", help="download destination; defaults to .powerpacks/search-index")
    proc.add_argument("--enrich", action="store_true",
                      help="allow paid OpenAI calls for cache misses (mounts the powerset-openai secret; dry-run estimate gated by --max-usd)")
    proc.add_argument("--max-usd", type=float, default=25.0)

    dl = sub.add_parser("download")
    dl.add_argument("--label", required=True, help="run label to pull, e.g. real-1x")
    dl.add_argument("--dest", help="destination dir; defaults to .powerpacks/search-index")
    dl.add_argument("--wait", action="store_true", help="poll runs/<label>/status.json until the run finishes")

    args = ap.parse_args()
    require_modal_credentials()
    return {"pipeline": cmd_pipeline, "import-linkedin": cmd_import_linkedin, "index-people": cmd_index_people, "preload": cmd_preload, "upload": cmd_upload, "amplify": cmd_amplify, "run": cmd_run, "download": cmd_download, "process": cmd_process}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

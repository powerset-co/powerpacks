#!/usr/bin/env python3
"""Modal PoC driver: run the Powerpacks indexing/processing pipeline in a cloud sandbox.

Local machine = dispatcher/aggregator: upload inputs to a Modal Volume,
dispatch the unmodified repo pipeline into a Sandbox, download results.

Run with the modal CLI's python (has the modal SDK):
  ~/.local/share/uv/tools/modal/bin/python packs/indexing/modal/modal_indexing_poc.py <cmd> ...

Commands:
  upload    push local people.csv + precomputed artifacts to the Volume
  amplify   build the synthetic Jake-scale dataset in-sandbox (no paid calls)
  run       client-driven benchmark run (streams per-phase, exec per step)
  process   fully automatic: server-side run (survives disconnects) + watch
            + auto-download; refreshes enrichment caches on the volume
  download  pull local-search.duckdb + manifest.json for a run label
            (--wait polls status.json until the run finishes)

Nothing here makes OpenAI calls: every paid stage is covered by precomputed
artifacts or pre-seeded checkpoints, and no OPENAI_API_KEY is set in the
sandbox.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import modal

APP_NAME = "powerpacks-indexing-poc"
VOLUME_NAME = "powerpacks-indexing-poc"
DEFAULT_OPERATOR_ID = "e33a648a-ae5f-432e-83ce-b90d75546ada"

REPO = Path(__file__).resolve().parents[3]
# .powerpacks lives at the main checkout root; walk up when running from a worktree
LOCAL_POWERPACKS = next(
    (p / ".powerpacks" for p in [REPO, *REPO.parents] if (p / ".powerpacks").is_dir()),
    REPO / ".powerpacks",
)
PIPELINE = "/repo/packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py"
DUCKDB_SHIM = "/repo/scripts/build-local-duckdb-shim.py"
BENCH = "/repo/packs/indexing/modal/bench_wrapper.py"
AMPLIFY = "/repo/packs/indexing/modal/amplify_dataset.py"

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
    search_index = LOCAL_POWERPACKS / "search-index"
    people_csv = LOCAL_POWERPACKS / "network-import/merged/people.csv"
    vol = get_volume()
    total_mb = people_csv.stat().st_size / 1e6
    with vol.batch_upload(force=True) as batch:
        batch.put_file(people_csv, "input/real/people.csv")
        for rel, name in REAL_ARTIFACTS.items():
            src = search_index / rel
            total_mb += src.stat().st_size / 1e6
            batch.put_file(src, f"input/real/artifacts/{name}")
        for rel, name in REAL_SEEDS.items():
            src = search_index / rel
            total_mb += src.stat().st_size / 1e6
            batch.put_file(src, f"input/real/seeds/{name}")
    print(f"uploaded people.csv + {len(REAL_ARTIFACTS) + len(REAL_SEEDS)} artifacts ({total_mb:.0f} MB) to volume {VOLUME_NAME}")
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    """Fully automatic: dispatch a server-side run, watch it, download results.

    The sandbox entrypoint (run_in_sandbox.py) owns seed -> pipeline -> duckdb
    -> persist -> status.json, so the cloud run completes and persists even if
    this driver disconnects; re-attach later with `download --wait`.
    """
    label = args.label or f"{args.dataset}-process"
    input_root = f"/data/input/{args.dataset}"
    run_vol = f"/data/runs/{label}"
    app = modal.App.lookup(APP_NAME, create_if_missing=True)
    entrypoint = [
        "python", "/repo/packs/indexing/modal/run_in_sandbox.py",
        "--input-root", input_root,
        "--run-vol", run_vol,
        "--operator-id", DEFAULT_OPERATOR_ID,
    ]
    if args.persist_artifacts:
        entrypoint.append("--persist-artifacts")
    started = time.time()
    sb = modal.Sandbox.create(
        *entrypoint,
        app=app,
        image=build_image(),
        volumes={"/data": get_volume()},
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
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            data = b"".join(vol.read_file(f"runs/{label}/status.json"))
            payload = json.loads(data)
        except (FileNotFoundError, json.JSONDecodeError):
            payload = None
        if payload and payload.get("status") in ("completed", "failed"):
            return payload
        if payload:
            print(f"  status={payload.get('status')} phase={payload.get('phase')} ({payload.get('updated_at')})")
        time.sleep(30)
    return None


def cmd_download(args: argparse.Namespace) -> int:
    """Pull the search artifacts a local machine actually consumes.

    The volume stays the durable home for ledger/records/enrichment caches
    (resume + incremental state); local search only needs local-search.duckdb
    plus manifest.json.
    """
    if getattr(args, "wait", False):
        print(f"waiting for runs/{args.label}/status.json ...")
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
        remote = f"runs/{args.label}/{name}"
        target = dest / name
        if target.exists():
            backup = target.with_name(target.name + ".bkup")
            target.replace(backup)
            print(f"existing {target.name} renamed to {backup.name}")
        tmp = target.with_name(target.name + ".tmp")
        written = 0
        try:
            with tmp.open("wb") as handle:
                for chunk in vol.read_file(remote):
                    handle.write(chunk)
                    written += len(chunk)
        except FileNotFoundError:
            tmp.unlink(missing_ok=True)
            print(f"missing on volume (was the run made with --persist-artifacts?): {remote}")
            return 1
        tmp.replace(target)
        elapsed = max(time.time() - started, 0.001)
        print(f"downloaded {remote} -> {target} ({written / 1e6:.0f} MB, {written / 1e6 / elapsed:.0f} MB/s)")
        started = time.time()
    return 0


def cmd_amplify(args: argparse.Namespace) -> int:
    sb = make_sandbox(cpu=args.cpu, memory_mib=args.memory_mib, timeout=args.timeout)
    print(f"sandbox {sb.object_id} (cpu={args.cpu} mem={args.memory_mib}MiB)")
    try:
        code, _ = sb_exec(
            sb, "python", AMPLIFY,
            "--people-csv", "/data/input/real/people.csv",
            "--artifacts-dir", "/data/input/real/artifacts",
            "--output-dir", "/data/input/synthetic",
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
    input_root = f"/data/input/{args.dataset}"
    people_csv = f"{input_root}/people.csv"
    artifacts = f"{input_root}/artifacts"
    seeds = f"{input_root}/seeds"
    label = args.label or f"{args.dataset}-{int(args.cpu)}cpu-{args.memory_mib}mib"
    run_vol = f"/data/runs/{label}"
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

    sub.add_parser("upload")

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

    dl = sub.add_parser("download")
    dl.add_argument("--label", required=True, help="run label to pull, e.g. real-1x")
    dl.add_argument("--dest", help="destination dir; defaults to .powerpacks/search-index")
    dl.add_argument("--wait", action="store_true", help="poll runs/<label>/status.json until the run finishes")

    args = ap.parse_args()
    return {"upload": cmd_upload, "amplify": cmd_amplify, "run": cmd_run, "download": cmd_download, "process": cmd_process}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())

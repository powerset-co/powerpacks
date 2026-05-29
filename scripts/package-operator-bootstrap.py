#!/usr/bin/env python3
"""Package per-operator Powerpacks bootstrap bundles.

This is an operator-scoped migration helper for bootstrapping sync/import/
enrich/processing state from existing Aleph/Powerpacks artifacts. It packages
the reusable outputs/checkpoints needed to resume local ingestion
without bundling raw msgvault databases, raw mail, message bodies, secrets, or
provider credentials.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = Path(".powerpacks/operator-bootstrap")
OPERATOR_ACCESS_COLUMNS = ["operator_id", "person_id", "operator_slug", "operator_email"]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def slugify(value: str) -> str:
    text = clean(value).lower()
    text = re.sub(r"[^a-z0-9._@+-]+", "-", text).strip("-")
    return text or "unknown"


def default_seed() -> Path:
    candidates = [
        ROOT / ".powerpacks/aleph-seed/2026-05-08",
        ROOT.parent / "aleph-mvp",
    ]
    for candidate in candidates:
        if (candidate / "pipeline_output/unified/flattened_people.jsonl").exists():
            return candidate
    return candidates[0]


def default_operator_mapping() -> str:
    for candidate in [ROOT.parent / "aleph-mvp/operator_mapping.json", ROOT / "operator_mapping.json"]:
        if candidate.exists():
            return str(candidate)
    return str(ROOT.parent / "aleph-mvp/operator_mapping.json")


def default_linkedin_csv() -> str:
    candidates = [
        ROOT.parent / "Connections.csv",
        Path.home() / "Downloads/Complete_LinkedInDataExport_05-16-2026.zip/Connections.csv",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return ""


def load_operator_mapping(path: Path) -> dict[str, dict[str, Any]]:
    mapping = read_json(path)
    users = mapping.get("_users") or {}
    if not isinstance(users, dict):
        raise SystemExit(f"operator mapping is missing _users: {path}")
    operators: dict[str, dict[str, Any]] = {}
    for operator_id, slug in users.items():
        operator_id = str(operator_id)
        slug = str(slug)
        operators[slug] = {
            "slug": slug,
            "operator_id": operator_id,
            "operator_short": operator_id.split("-", 1)[0],
            "token_ids": list(mapping.get(operator_id) or []),
        }
    return operators


def select_operators(args: argparse.Namespace, operators: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    raw = args.operators or ",".join(sorted(operators))
    selected = [slug.strip() for slug in raw.split(",") if slug.strip()]
    missing = [slug for slug in selected if slug not in operators]
    if missing:
        raise SystemExit(f"unknown operators in {args.operator_mapping}: {', '.join(missing)}")
    return [operators[slug] for slug in selected]


def run_command(cmd: list[str], *, cwd: Path = ROOT) -> str:
    completed = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, file=sys.stderr, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        raise SystemExit(completed.returncode)
    return completed.stdout


def db_url_from_env(args: argparse.Namespace) -> str:
    load_dotenv()
    return clean(args.database_url or os.getenv("DATABASE_URL"))


def export_operator_access_from_db(args: argparse.Namespace, operators: list[dict[str, Any]], out_path: Path) -> dict[str, Any]:
    database_url = db_url_from_env(args)
    if not database_url:
        raise SystemExit("no --operator-access supplied and DATABASE_URL is missing")
    try:
        import psycopg2  # type: ignore
    except Exception as exc:
        raise SystemExit(f"psycopg2 is required to export operator access from DATABASE_URL: {exc}") from exc

    operator_ids = [operator["operator_id"] for operator in operators]
    slug_by_id = {operator["operator_id"]: operator["slug"] for operator in operators}
    rows: list[dict[str, Any]] = []
    conn = psycopg2.connect(database_url, connect_timeout=15)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT operator_id::text, person_id::text
                FROM public.operator_person_sources
                WHERE person_id IS NOT NULL
                  AND operator_id IS NOT NULL
                  AND operator_id::text = ANY(%s)
                GROUP BY operator_id::text, person_id::text
                ORDER BY operator_id::text, person_id::text
                """,
                (operator_ids,),
            )
            for operator_id, person_id in cur.fetchall():
                rows.append(
                    {
                        "operator_id": operator_id,
                        "person_id": person_id,
                        "operator_slug": slug_by_id.get(operator_id, ""),
                        "operator_email": "",
                    }
                )
    finally:
        conn.close()

    write_csv(out_path, OPERATOR_ACCESS_COLUMNS, rows)
    counts: dict[str, int] = {}
    for row in rows:
        operator_id = str(row["operator_id"])
        counts[operator_id] = counts.get(operator_id, 0) + 1
    return {"status": "ok", "path": str(out_path), "rows": len(rows), "operator_counts": counts, "source": "postgres"}


def operator_access_stats(path: Path, operators: list[dict[str, Any]]) -> dict[str, Any]:
    wanted = {operator["operator_id"] for operator in operators}
    counts = {operator["operator_id"]: 0 for operator in operators}
    with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
        for row in csv.DictReader(handle):
            operator_id = clean(row.get("operator_id"))
            person_id = clean(row.get("person_id") or row.get("base_person_id"))
            if operator_id in wanted and person_id:
                counts[operator_id] += 1
    return {"status": "ok", "path": str(path), "rows": sum(counts.values()), "operator_counts": counts, "source": "file"}


def ensure_operator_access(args: argparse.Namespace, operators: list[dict[str, Any]], output_root: Path) -> tuple[Path, dict[str, Any]]:
    if args.operator_access:
        path = Path(args.operator_access)
        if not path.exists():
            raise SystemExit(f"operator access file not found: {path}")
        return path, operator_access_stats(path, operators)
    path = output_root / "operator_access/operator-access.csv"
    if path.exists() and args.reuse_operator_access:
        stats = operator_access_stats(path, operators)
        counts = stats.get("operator_counts") or {}
        if all(int(counts.get(operator["operator_id"], 0)) > 0 for operator in operators):
            return path, stats
    return path, export_operator_access_from_db(args, operators, path)


def resolve_source_dir(args: argparse.Namespace) -> Path:
    if args.source_dir:
        return Path(args.source_dir)
    return Path(args.seed) / "pipeline_output/unified/contact"


def run_network_bootstrap(args: argparse.Namespace, operators: list[dict[str, Any]], output_root: Path) -> tuple[Path, dict[str, Any]]:
    root = Path(args.network_bootstrap_root) if args.network_bootstrap_root else output_root / "work/network-bootstrap"
    missing = [operator["slug"] for operator in operators if not (root / "operators" / operator["slug"] / "manifest.json").exists()]
    if args.reuse_network_bootstrap and not missing:
        summary_path = root / "summary.json"
        summary = read_json(summary_path) if summary_path.exists() else {"status": "ok", "operators": []}
        return root, {"status": "reused", "root": str(root), "summary": summary}
    source_dir = resolve_source_dir(args)
    if not source_dir.exists():
        raise SystemExit(f"network bootstrap source dir not found: {source_dir}")
    cmd = [
        sys.executable,
        "packs/ingestion/primitives/bootstrap_network_from_exports/bootstrap_network_from_exports.py",
        "generate",
        "--operator-mapping",
        str(Path(args.operator_mapping)),
        "--source-dir",
        str(source_dir),
        "--operators",
        ",".join(operator["slug"] for operator in operators),
        "--output-root",
        str(root),
        "--profile-cache-dir",
        str(output_root / "work/profile_cache_v2"),
        "--force",
    ]
    if args.linkedin_csv:
        cmd.extend(["--linkedin-csv", str(Path(args.linkedin_csv))])
    if args.gmail_account_email:
        cmd.extend(["--gmail-account-email", args.gmail_account_email])
    if args.seed_profile_cache:
        cmd.append("--seed-profile-cache")
    stdout = run_command(cmd)
    try:
        summary = json.loads(stdout)
    except json.JSONDecodeError:
        summary = {"status": "ok", "stdout": stdout}
    return root, {"status": "generated", "root": str(root), "summary": summary}


def copy_dir(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return True


def copy_file(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return True


def link_or_copy_file(src: Path, dst: Path) -> bool:
    if not src.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)
    return True


def copy_stage_path(src: Path, restore_powerpacks_root: Path) -> str | None:
    if not src.exists():
        return None
    try:
        rel = src.resolve().relative_to(ROOT.resolve())
    except ValueError:
        text = str(src)
        if text.startswith(".powerpacks/"):
            rel = Path(text)
        else:
            return None
    if rel.parts and rel.parts[0] == ".powerpacks":
        rel = Path(*rel.parts[1:])
    dst = restore_powerpacks_root / rel
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        copy_file(src, dst)
    return str(Path(".powerpacks") / rel)


def collect_stage_paths(value: Any) -> list[str]:
    paths: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            paths.extend(collect_stage_paths(item))
    elif isinstance(value, list):
        for item in value:
            paths.extend(collect_stage_paths(item))
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith(".powerpacks/"):
            paths.append(text)
    return paths


def read_counts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = read_json(path)
    return payload.get("counts") if isinstance(payload, dict) and isinstance(payload.get("counts"), dict) else {}


def write_sync_manifest(args: argparse.Namespace, operator: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    manifest = {
        "status": "metadata_bootstrap_only",
        "operator": operator["slug"],
        "operator_id": operator["operator_id"],
        "generated_at": now_iso(),
        "gmail_account_email": args.gmail_account_email or "",
        "gmail_query": args.gmail_query or "",
        "included": {
            "raw_msgvault_db": False,
            "raw_mail": False,
            "message_bodies": False,
            "attachments": False,
            "secrets": False,
        },
        "note": "Sync credentials and raw provider archives are intentionally not bundled. The import/enrich sections contain reusable metadata checkpoints only.",
    }
    write_json(out_dir / "sync/manifest.json", manifest)
    return manifest


def build_processing(args: argparse.Namespace, operator_access: Path, operator: dict[str, Any], out_dir: Path) -> dict[str, Any]:
    search_index_dir = out_dir / "processing/search-index"
    if args.skip_processing:
        if not (search_index_dir / "stats/bootstrap_from_aleph.json").exists():
            raise SystemExit(f"--skip-processing requires existing {search_index_dir}")
        return {"status": "skipped", "run_dir": str(search_index_dir)}
    cmd = [
        sys.executable,
        "scripts/bootstrap-local-from-aleph.py",
        "--seed",
        str(Path(args.seed)),
        "--operator-access",
        str(operator_access),
        "--operator-id",
        operator["operator_id"],
        "--operator-email",
        operator["slug"],
        "--output-dir",
        str(search_index_dir),
        "--checkpoint-every",
        str(args.checkpoint_every),
        "--education-limit",
        str(args.education_limit),
        "--force",
    ]
    if args.company_csv:
        cmd.extend(["--company-csv", str(Path(args.company_csv))])
    if args.skip_duckdb:
        cmd.append("--skip-duckdb")
    stdout = run_command(cmd)
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        return {"status": "ok", "stdout": stdout, "run_dir": str(search_index_dir)}


def make_readme(operator: dict[str, Any], manifest: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"Powerpacks operator bootstrap: {operator['slug']}",
            "",
            "Contents:",
            "- sync/: metadata-only sync status; no raw msgvault DB, raw mail, attachments, secrets, or message bodies.",
            "- import/: contact/source metadata needed by import-network.",
            "- enrich/: LinkedIn resolution and local profile cache checkpoints.",
            "- .powerpacks/search-index/records/: restore-ready local search records with vectors.",
            "",
            "Local search:",
            "local-search.duckdb is intentionally not bundled. Rebuild it from .powerpacks/search-index/records/ after restore.",
            "",
            f"Generated: {manifest['generated_at']}",
        ]
    ) + "\n"


PROCESSING_STEPS = [
    "flatten_people",
    "build_roles",
    "embed_role_positions",
    "build_company_corpus",
    "embed_companies",
    "build_education_corpus",
    "build_location_corpus",
    "build_people_records",
    "build_unified_profiles",
    "build_summary_records",
    "embed_summaries",
    "build_vectors",
    "validate_contracts",
]


SEARCH_INDEX_RECORD_FILES = [
    Path("records/people.records.jsonl"),
    Path("records/summaries.records.jsonl"),
    Path("records/education.records.jsonl"),
    Path("records/schools.records.jsonl"),
    Path("records/companies.records.jsonl"),
]

SEARCH_INDEX_METADATA_FILES = [
    Path("manifest.json"),
    Path("stats/bootstrap_from_aleph.json"),
    Path("vectors/checkpoint.json"),
]


def write_processing_restore_ledger(restore_powerpacks_root: Path, operator: dict[str, Any], source_dir: Path) -> None:
    ledger_path = restore_powerpacks_root / "search-index/ledger.json"
    stats_path = source_dir / "stats/bootstrap_from_aleph.json"
    stats = read_json(stats_path) if stats_path.exists() else {}
    records_dir = restore_powerpacks_root / "search-index/records"
    artifacts = {
        "people": ".powerpacks/search-index/records/people.records.jsonl",
        "companies": ".powerpacks/search-index/records/companies.records.jsonl",
        "schools": ".powerpacks/search-index/records/schools.records.jsonl",
        "education": ".powerpacks/search-index/records/education.records.jsonl",
        "summaries": ".powerpacks/search-index/records/summaries.records.jsonl",
        "local_search_db": ".powerpacks/search-index/local-search.duckdb",
    }
    for key, path_text in list(artifacts.items()):
        if not (restore_powerpacks_root / Path(path_text).relative_to(".powerpacks")).exists():
            artifacts.pop(key, None)
    write_json(
        ledger_path,
        {
            "primitive": "build_processing_pipeline",
            "version": 1,
            "status": "completed",
            "run_dir": ".powerpacks/search-index",
            "input": ".powerpacks/network-import/merged/people.csv",
            "default_operator_id": operator["operator_id"],
            "restored_from_operator_bootstrap": True,
            "steps": [{"id": step, "status": "completed"} for step in PROCESSING_STEPS],
            "artifacts": artifacts,
            "bootstrap_stats": stats,
            "records_dir": ".powerpacks/search-index/records",
        },
    )


def copy_processing_restore_payload(source_dir: Path, restore_powerpacks_root: Path) -> list[str]:
    dst = restore_powerpacks_root / "search-index"
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for rel in SEARCH_INDEX_RECORD_FILES:
        if link_or_copy_file(source_dir / rel, dst / rel):
            copied.append(str(Path(".powerpacks/search-index") / rel))
    for rel in SEARCH_INDEX_METADATA_FILES:
        if copy_file(source_dir / rel, dst / rel):
            copied.append(str(Path(".powerpacks/search-index") / rel))
    return copied


def build_restore_payload(operator: dict[str, Any], operator_dir: Path, network_root: Path, output_root: Path) -> dict[str, Any]:
    restore_powerpacks_root = output_root / "restores" / operator["slug"] / ".powerpacks"
    if restore_powerpacks_root.exists():
        shutil.rmtree(restore_powerpacks_root)
    restore_powerpacks_root.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    missing: list[str] = []

    import_ledger_path = operator_dir / "import/outputs/import-network.ledger.json"
    import_ledger = read_json(import_ledger_path) if import_ledger_path.exists() else {}
    run_dir_text = clean(import_ledger.get("run_dir"))
    if import_ledger:
        for path_text in sorted(set(collect_stage_paths(import_ledger))):
            copied_path = copy_stage_path(ROOT / path_text, restore_powerpacks_root)
            if copied_path:
                copied.append(copied_path)
            else:
                missing.append(path_text)
        if run_dir_text:
            restored_ledger_path = restore_powerpacks_root / Path(run_dir_text).relative_to(".powerpacks") / "import-network.ledger.json"
            restored_ledger = dict(import_ledger)
            restored_ledger["ledger"] = str(Path(run_dir_text) / "import-network.ledger.json")
            write_json(restored_ledger_path, restored_ledger)
            copied.append(str(Path(run_dir_text) / "import-network.ledger.json"))
            merged_dir = ROOT / run_dir_text / "merged"
            if merged_dir.exists():
                dst = restore_powerpacks_root / "network-import/merged"
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(merged_dir, dst)
                copied.append(".powerpacks/network-import/merged")

    processing_dir = operator_dir / "processing/search-index"
    if processing_dir.exists():
        copied.extend(copy_processing_restore_payload(processing_dir, restore_powerpacks_root))
        write_processing_restore_ledger(restore_powerpacks_root, operator, processing_dir)
        copied.append(".powerpacks/search-index")

    restore_manifest = {
        "status": "ok",
        "bundle_mode": "restore_records_only",
        "operator": operator["slug"],
        "operator_id": operator["operator_id"],
        "generated_at": now_iso(),
        "restore_root": str(restore_powerpacks_root),
        "normal_pipeline_outputs": copied,
        "missing_referenced_outputs": missing,
        "excluded_heavy_outputs": [
            ".powerpacks/search-index/local-search.duckdb",
            ".powerpacks/search-index/roles",
            ".powerpacks/search-index/company",
            ".powerpacks/search-index/unified",
            ".powerpacks/search-index/roles/embedding_checkpoints",
            ".powerpacks/search-index/company/embedding_checkpoints",
        ],
        "commands": {
            "import_dry_run": f"uv run --project . python packs/ingestion/primitives/import_network_pipeline/import_network_pipeline.py run --ledger {run_dir_text}/import-network.ledger.json --run-id {Path(run_dir_text).name if run_dir_text else 'network-bootstrap'} --dry-run" if run_dir_text else "",
            "processing_dry_run": "uv run --project . python packs/indexing/primitives/build_processing_pipeline/build_processing_pipeline.py run --input .powerpacks/network-import/merged/people.csv --output-dir .powerpacks/search-index --dry-run",
        },
    }
    write_json(restore_powerpacks_root / "operator-bootstrap/restore-manifest.json", restore_manifest)
    return restore_manifest


def package_bundle(operator_dir: Path, restore_powerpacks_root: Path, bundles_dir: Path, slug: str, *, force: bool) -> Path:
    bundles_dir.mkdir(parents=True, exist_ok=True)
    archive_path = bundles_dir / f"{slug}.operator-bootstrap.tar.gz"
    if archive_path.exists():
        if force:
            archive_path.unlink()
        else:
            raise SystemExit(f"bundle exists: {archive_path}. Use --force to replace.")
    env = os.environ.copy()
    env["COPYFILE_DISABLE"] = "1"
    archive_path_abs = archive_path.resolve()
    cmd = ["tar", "-czf", str(archive_path_abs)]
    if restore_powerpacks_root.exists():
        # The restore tree already carries the heavy search-index payload. Keep
        # the operator section lightweight so bundles do not store it twice.
        cmd.extend(["--exclude", f"{operator_dir.name}/processing"])
    cmd.extend(["-C", str(operator_dir.parent.resolve()), operator_dir.name])
    if restore_powerpacks_root.exists():
        cmd.extend(["-C", str(restore_powerpacks_root.parent.resolve()), ".powerpacks"])
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, file=sys.stderr, end="")
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        raise SystemExit(completed.returncode)
    return archive_path


def gcs_destinations(gcs_uri: str, operator: dict[str, Any]) -> dict[str, str]:
    base = gcs_uri.rstrip("/")
    slug = slugify(operator["slug"])
    operator_id = operator["operator_id"]
    return {
        "prefix": f"{base}/users/{slug}/operators/{operator_id}",
        "bundle": f"{base}/users/{slug}/operators/{operator_id}/operator-bootstrap.tar.gz",
        "manifest": f"{base}/users/{slug}/operators/{operator_id}/manifest.json",
    }


def parse_exact_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://") or uri.endswith("/") or "*" in uri:
        raise ValueError(f"not an exact gs:// object URI: {uri}")
    bucket, sep, object_name = uri[5:].partition("/")
    if not bucket or not sep or not object_name:
        raise ValueError(f"not an exact gs:// object URI: {uri}")
    return bucket, object_name


def python_gcs_upload(local_path: Path, destination: str) -> None:
    try:
        from google.cloud import storage  # type: ignore
    except Exception as exc:
        raise SystemExit(f"google-cloud-storage is required for --gcs-upload-backend python; run through uv: {exc}") from exc
    bucket_name, object_name = parse_exact_gcs_uri(destination)
    tmp_key = None
    old_gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    try:
        gac = (old_gac or "").strip()
        if gac.startswith("{"):
            fd, key_path = tempfile.mkstemp(prefix="powerpacks-gcs-key-", suffix=".json", dir="/var/tmp")
            os.close(fd)
            tmp_key = Path(key_path)
            tmp_key.write_text(gac, encoding="utf-8")
            tmp_key.chmod(0o600)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(tmp_key)
        storage.Client().bucket(bucket_name).blob(object_name).upload_from_filename(str(local_path))
    finally:
        if old_gac is None:
            os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
        else:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = old_gac
        if tmp_key:
            tmp_key.unlink(missing_ok=True)


def upload_one(local_path: Path, destination: str, backend: str) -> str:
    if backend == "python":
        python_gcs_upload(local_path, destination)
        return "python-google-cloud-storage"
    if backend == "gcloud":
        run_command(["gcloud", "storage", "cp", str(local_path), destination])
        return "gcloud"
    if backend == "auto":
        if shutil.which("gcloud"):
            try:
                run_command(["gcloud", "storage", "cp", str(local_path), destination])
                return "gcloud"
            except SystemExit:
                python_gcs_upload(local_path, destination)
                return "python-google-cloud-storage"
        python_gcs_upload(local_path, destination)
        return "python-google-cloud-storage"
    raise SystemExit(f"unsupported --gcs-upload-backend: {backend}")


def upload_to_gcs(archive_path: Path, manifest_path: Path, destinations: dict[str, str], *, dry_run: bool, backend: str) -> dict[str, Any]:
    if dry_run:
        return {"status": "dry_run", "upload_backend": backend, **destinations}
    used_bundle_backend = upload_one(archive_path, destinations["bundle"], backend)
    used_manifest_backend = upload_one(manifest_path, destinations["manifest"], backend)
    return {"status": "uploaded", "upload_backend": used_bundle_backend if used_bundle_backend == used_manifest_backend else "mixed", **destinations}


def assemble_operator(
    args: argparse.Namespace,
    operator: dict[str, Any],
    operator_access: Path,
    access_stats: dict[str, Any],
    network_root: Path,
    output_root: Path,
) -> dict[str, Any]:
    operator_dir = output_root / "operators" / operator["slug"]
    if operator_dir.exists() and args.force:
        if args.skip_processing:
            for name in ["sync", "import", "enrich", "README.txt", "manifest.json"]:
                path = operator_dir / name
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
        else:
            shutil.rmtree(operator_dir)
    operator_dir.mkdir(parents=True, exist_ok=True)

    sync_manifest = write_sync_manifest(args, operator, operator_dir)
    network_dir = network_root / "operators" / operator["slug"]
    if not network_dir.exists():
        raise SystemExit(f"network bootstrap output missing for {operator['slug']}: {network_dir}")
    copy_file(network_dir / "manifest.json", operator_dir / "import/network-bootstrap-manifest.json")
    copy_file(network_dir / "outputs/counts.json", operator_dir / "import/counts.json")
    copy_dir(network_dir / "inputs", operator_dir / "import/inputs")
    copy_dir(network_dir / "outputs", operator_dir / "import/outputs")
    copy_dir(network_dir / "resolution", operator_dir / "enrich/resolution")
    copy_dir(network_dir / "enrichment", operator_dir / "enrich/enrichment")

    network_manifest_path = network_dir / "manifest.json"
    network_counts = read_counts(network_manifest_path)
    processing_payload = build_processing(args, operator_access, operator, operator_dir)
    processing_stats_path = operator_dir / "processing/search-index/stats/bootstrap_from_aleph.json"
    processing_stats = read_json(processing_stats_path) if processing_stats_path.exists() else {}
    processing_counts = processing_stats.get("counts") if isinstance(processing_stats.get("counts"), dict) else {}
    duckdb_tables = processing_stats.get("duckdb_tables") if isinstance(processing_stats.get("duckdb_tables"), dict) else {}

    manifest_path = operator_dir / "manifest.json"
    manifest = {
        "status": "ok",
        "schema_version": 1,
        "generated_at": now_iso(),
        "operator": operator["slug"],
        "operator_id": operator["operator_id"],
        "operator_short": operator["operator_short"],
        "token_ids": operator.get("token_ids") or [],
        "sources": {
            "operator_mapping": str(Path(args.operator_mapping)),
            "operator_access": str(operator_access),
            "network_source_dir": str(resolve_source_dir(args)),
            "seed": str(Path(args.seed)),
            "company_csv": str(Path(args.company_csv)) if args.company_csv else str(Path(args.seed) / "data/company_harmonic_all.csv"),
            "linkedin_csv": args.linkedin_csv or "",
        },
        "stages": {
            "sync": sync_manifest,
            "import": {
                "status": "ok",
                "dir": str(operator_dir / "import"),
                "counts": network_counts,
                "commands": str(operator_dir / "import/outputs/commands.txt"),
            },
            "enrich": {
                "status": "ok",
                "dir": str(operator_dir / "enrich"),
                "counts": {
                    "linkedin_resolution_rows": network_counts.get("linkedin_resolution_rows", 0),
                    "linkedin_resolution_cached_rows": network_counts.get("linkedin_resolution_cached_rows", 0),
                    "linkedin_resolution_uncached_rows": network_counts.get("linkedin_resolution_uncached_rows", 0),
                    "profile_cache_files": network_counts.get("profile_cache_files", 0),
                },
            },
            "processing": {
                "status": "ok",
                "dir": str(operator_dir / "processing/search-index"),
                "counts": processing_counts,
                "duckdb_tables": duckdb_tables,
                "duckdb": str(operator_dir / "processing/search-index/local-search.duckdb"),
                "bootstrap": processing_payload,
            },
        },
        "access": {
            "source": access_stats.get("source"),
            "person_count": (access_stats.get("operator_counts") or {}).get(operator["operator_id"], 0),
        },
        "artifacts": {
            "operator_dir": str(operator_dir),
            "bundle": "",
            "manifest": str(manifest_path),
            "local_search_db": str(operator_dir / "processing/search-index/local-search.duckdb"),
        },
        "privacy": {
            "operator_scoped": True,
            "raw_msgvault_db_copied": False,
            "raw_mail_copied": False,
            "message_bodies_copied": False,
            "attachments_copied": False,
            "secrets_copied": False,
        },
    }
    restore_payload = build_restore_payload(operator, operator_dir, network_root, output_root)
    manifest["restore"] = restore_payload
    planned_archive_path = output_root / "bundles" / f"{operator['slug']}.operator-bootstrap.tar.gz"
    manifest["artifacts"]["bundle"] = str(planned_archive_path)
    if args.gcs_uri:
        destinations = gcs_destinations(args.gcs_uri, operator)
        manifest["gcs"] = {"status": "dry_run" if args.gcs_dry_run else "uploaded", **destinations}
    (operator_dir / "README.txt").write_text(make_readme(operator, manifest), encoding="utf-8")
    write_json(manifest_path, manifest)
    archive_path = package_bundle(operator_dir, output_root / "restores" / operator["slug"] / ".powerpacks", output_root / "bundles", operator["slug"], force=True)
    if args.gcs_uri:
        destinations = gcs_destinations(args.gcs_uri, operator)
        manifest["gcs"] = upload_to_gcs(archive_path, manifest_path, destinations, dry_run=args.gcs_dry_run, backend=args.gcs_upload_backend)
    return manifest


def cmd_generate(args: argparse.Namespace) -> int:
    args.seed = str(Path(args.seed or default_seed()))
    if not args.company_csv and (Path(args.seed) / "data/company_harmonic_all.csv").exists():
        args.company_csv = str(Path(args.seed) / "data/company_harmonic_all.csv")
    if not args.linkedin_csv:
        args.linkedin_csv = default_linkedin_csv()
    operators_by_slug = load_operator_mapping(Path(args.operator_mapping))
    operators = select_operators(args, operators_by_slug)
    output_root = Path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)

    operator_access, access_payload = ensure_operator_access(args, operators, output_root)
    network_root, network_payload = run_network_bootstrap(args, operators, output_root)
    manifests = [
        assemble_operator(args, operator, operator_access, access_payload, network_root, output_root)
        for operator in operators
    ]
    summary = {
        "status": "ok",
        "generated_at": now_iso(),
        "output_root": str(output_root),
        "operator_access": access_payload,
        "network_bootstrap": network_payload,
        "operators": [
            {
                "operator": item["operator"],
                "operator_id": item["operator_id"],
                "access_person_count": item["access"]["person_count"],
                "bundle": item["artifacts"]["bundle"],
                "manifest": item["artifacts"]["manifest"],
                "gcs": item.get("gcs"),
                "import_counts": item["stages"]["import"]["counts"],
                "processing_counts": item["stages"]["processing"]["counts"],
                "duckdb_tables": item["stages"]["processing"]["duckdb_tables"],
            }
            for item in manifests
        ],
        "privacy": {
            "raw_msgvault_db_copied": False,
            "raw_mail_copied": False,
            "message_bodies_copied": False,
            "attachments_copied": False,
            "secrets_copied": False,
        },
    }
    write_json(output_root / "summary.json", summary)
    if args.gcs_uri and not args.gcs_dry_run:
        upload_one(output_root / "summary.json", args.gcs_uri.rstrip("/") + "/summary.json", args.gcs_upload_backend)
    emit(summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--operator-mapping", default=default_operator_mapping(), help="JSON mapping with _users and token IDs")
    gen.add_argument("--operators", default="", help="Comma-separated operator slugs. Defaults to all operators in mapping.")
    gen.add_argument("--operator-access", help="CSV/JSONL with operator_id, person_id/base_person_id. If omitted, DATABASE_URL is queried.")
    gen.add_argument("--database-url", default="", help="Postgres URL used only when --operator-access is omitted")
    gen.add_argument("--reuse-operator-access", action="store_true")
    gen.add_argument("--seed", default=str(default_seed()), help="Seed root containing pipeline_output/ and data/")
    gen.add_argument("--company-csv", default="", help="Override company_harmonic_all.csv path")
    gen.add_argument("--source-dir", default="", help="Existing export CSV source dir. Defaults to <seed>/pipeline_output/unified/contact.")
    gen.add_argument("--network-bootstrap-root", default="", help="Existing or generated network bootstrap root")
    gen.add_argument("--reuse-network-bootstrap", action="store_true", help="Reuse existing network bootstrap operator manifests")
    gen.add_argument("--linkedin-csv", default="", help="LinkedIn Connections.csv for cached LinkedIn subset generation")
    gen.add_argument("--gmail-account-email", default="", help="Metadata-only note and generated import command input")
    gen.add_argument("--gmail-query", default="", help="Metadata-only sync query note")
    gen.add_argument("--seed-profile-cache", action="store_true")
    gen.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    gen.add_argument("--checkpoint-every", type=int, default=1000)
    gen.add_argument("--education-limit", type=int, default=5000)
    gen.add_argument("--skip-processing", action="store_true")
    gen.add_argument("--skip-duckdb", action="store_true")
    gen.add_argument("--gcs-uri", default="", help="Optional GCS base URI, e.g. gs://bucket/powerpacks/operator-bootstrap")
    gen.add_argument("--gcs-dry-run", action="store_true")
    gen.add_argument("--gcs-upload-backend", choices=["auto", "gcloud", "python"], default="auto", help="Upload bundles with gcloud storage cp or google-cloud-storage through uv.")
    gen.add_argument("--force", action="store_true")
    gen.set_defaults(func=cmd_generate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

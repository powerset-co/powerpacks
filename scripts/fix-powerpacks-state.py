#!/usr/bin/env python3
"""Doctor/fixer for Powerpacks local state roots.

The script enforces the local-state contract in config/powerpacks-state-paths.json:
Powerpacks product state should live under the canonical non-.codex repo's
.powerpacks/ directory. Legacy .codex checkouts may be scanned as sources, but
should not remain the runtime root.

Default mode applies safe repairs: copy missing/newer managed state into the
canonical repo, adopt an authenticated wacli store, and scrub an unauthenticated
canonical wacli store so the user can reauth cleanly. Use --dry-run to inspect
the plan without changes. Use --quarantine-legacy-state only after review; it
renames legacy .powerpacks directories instead of deleting them.

Changelog:
  2026-07-23 (nuke accounts.json): removed the accounts.json repair/reader
    machinery — repair_gmail_accounts, linked_source_checks, the msgvault/
    account-record helpers (account_records/config_obj/string_list/
    msgvault_accounts/accounts_container), the --no-repair-accounts flag, and
    the stale --accounts command strings. The linked-source registry was
    deleted; gmail selection is now --account-email and messages selection is
    --include-imessage/--include-whatsapp.
  2026-07-23 (audit batch 16): linkedin discovery_command now points at the
    live $setup path (linkedin_modal_pipeline.py import-linkedin); the
    standalone linkedin/discover.py CLI was deleted with the retired
    discover-contacts orchestrator.
  2026-07-23 (audit batch 20A): discover_contacts_pipeline/import_contacts_pipeline
    packages renamed to discover/imports; linkedin import_command now points at the
    live Modal import-linkedin path (the dead local imports/linkedin/importer.py was
    deleted).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packs.shared.csv_io import CsvIO  # noqa: E402

CONFIG = ROOT / "config/powerpacks-state-paths.json"
VERTICAL_STAGE_PATHS = {
    "gmail": {
        "discovery": ".powerpacks/network-import/discover/gmail/manifest.json",
        "import": ".powerpacks/network-import/import/gmail/manifest.json",
        "discovery_command": "uv run --project . python packs/ingestion/primitives/discover/gmail/discover.py discover --account-email <gmail-account-email>",
        "import_command": "uv run --project . python packs/ingestion/primitives/imports/gmail/importer.py run --operator-id <operator-id>",
    },
    "linkedin": {
        "discovery": ".powerpacks/network-import/discover/linkedin/manifest.json",
        "import": ".powerpacks/network-import/import/linkedin/manifest.json",
        "discovery_command": "uv run --env-file .env --project . python packs/indexing/modal/linkedin_modal_pipeline.py import-linkedin --csv .powerpacks/network-import/discover/linkedin/Connections.csv",
        "import_command": "uv run --env-file .env --project . python packs/indexing/modal/linkedin_modal_pipeline.py import-linkedin --csv .powerpacks/network-import/discover/linkedin/Connections.csv",
    },
    "messages": {
        "discovery": ".powerpacks/network-import/discover/messages/manifest.json",
        "import": ".powerpacks/network-import/import/messages/manifest.json",
        "discovery_command": "uv run --project . python packs/ingestion/primitives/discover/messages/discover.py discover --include-imessage --include-whatsapp",
        "import_command": "uv run --project . python packs/ingestion/primitives/imports/messages/importer.py run --operator-id <operator-id>",
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def expand_value(value: str, *, cwd: Path | None = None) -> str:
    out = value
    replacements = {
        "$POWERPACKS_REPO_ROOT": os.environ.get("POWERPACKS_REPO_ROOT", ""),
        "$PWD": str(cwd or Path.cwd()),
    }
    for key, replacement in replacements.items():
        out = out.replace(key, replacement)
    return os.path.expandvars(os.path.expanduser(out))


def expand_path(value: str, *, cwd: Path | None = None) -> Path:
    return Path(expand_value(value, cwd=cwd)).resolve()


def is_codex_path(path: Path) -> bool:
    text = str(path)
    return "/.codex/" in text or text.endswith("/.codex")


def is_repo_root(path: Path) -> bool:
    return (path / "packs").is_dir() and (path / "pyproject.toml").exists()


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def newest_mtime(path: Path) -> float:
    if not path.exists():
        return 0.0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0
    newest = 0.0
    for child in path.rglob("*"):
        try:
            newest = max(newest, child.stat().st_mtime)
        except OSError:
            pass
    return newest


def file_count(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        return 1
    return sum(1 for child in path.rglob("*") if child.is_file() or child.is_symlink())


def csv_count(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="replace") as handle:
            return sum(1 for _ in CsvIO.dict_reader(handle))
    except Exception:
        return 0


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def copy_file_if_newer(src: Path, dst: Path, *, apply: bool, backup: bool) -> dict[str, Any]:
    src_mtime = newest_mtime(src)
    dst_mtime = newest_mtime(dst)
    if not src.exists():
        return {"action": "missing_source"}
    if dst.exists() and dst_mtime >= src_mtime:
        return {"action": "kept_target", "source_mtime": src_mtime, "target_mtime": dst_mtime}
    backup_path = ""
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if backup and dst.exists():
            backup_dst = dst.with_name(f"{dst.name}.backup-{stamp()}")
            shutil.move(str(dst), str(backup_dst))
            backup_path = str(backup_dst)
        shutil.copy2(src, dst)
    return {
        "action": "copied" if apply else "would_copy",
        "source_mtime": src_mtime,
        "target_mtime": dst_mtime,
        "backup": backup_path,
    }


def copy_dir_newer_files(src: Path, dst: Path, *, apply: bool, backup: bool) -> dict[str, Any]:
    if not src.exists():
        return {"action": "missing_source"}
    copied: list[str] = []
    kept: list[str] = []
    backup_root = ""
    if apply:
        dst.mkdir(parents=True, exist_ok=True)
    if backup and apply and dst.exists() and newest_mtime(src) > newest_mtime(dst):
        # Directory backups are intentionally conservative: copy newer files over
        # by default; full directory replacement only happens with this flag.
        backup_path = dst.with_name(f"{dst.name}.backup-{stamp()}")
        shutil.copytree(dst, backup_path, dirs_exist_ok=False)
        backup_root = str(backup_path)
    for source_file in sorted(src.rglob("*")):
        if not (source_file.is_file() or source_file.is_symlink()):
            continue
        rel_path = source_file.relative_to(src)
        target_file = dst / rel_path
        src_mtime = newest_mtime(source_file)
        dst_mtime = newest_mtime(target_file)
        if target_file.exists() and dst_mtime >= src_mtime:
            kept.append(str(rel_path))
            continue
        copied.append(str(rel_path))
        if apply:
            target_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, target_file)
    return {
        "action": "copied" if apply and copied else "would_copy" if copied else "kept_target",
        "copied_files": len(copied),
        "kept_files": len(kept),
        "copied_sample": copied[:20],
        "backup": backup_root,
    }


def find_canonical_repo(config: dict[str, Any], explicit: str = "") -> Path | None:
    if explicit:
        path = expand_path(explicit)
        return path if is_repo_root(path) else None
    for item in config.get("canonical_repo_candidates") or []:
        path = expand_path(str(item), cwd=Path.cwd())
        if path and is_repo_root(path) and not is_codex_path(path):
            return path
    return None


def legacy_repos(config: dict[str, Any], canonical: Path, extras: list[str]) -> list[Path]:
    out: list[Path] = []
    for item in [*(config.get("legacy_state_sources") or []), *extras]:
        path = expand_path(str(item), cwd=Path.cwd())
        if path == canonical:
            continue
        if ((path / ".powerpacks").exists() or (path / ".env").exists()) and path not in out:
            out.append(path)
    return out


def wacli_store_summary(store: Path) -> dict[str, Any]:
    db = store / "wacli.db"
    if not db.exists():
        return {"status": "missing", "store": str(store), "score": 0}
    summary: dict[str, Any] = {"status": "present", "store": str(store), "db": str(db), "score": 0}
    try:
        with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
            tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'").fetchall()}
            for table in ["chats", "contacts", "groups", "messages"]:
                if table in tables:
                    summary[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0] or 0)
            summary["score"] = int(summary.get("messages") or 0) + int(summary.get("contacts") or 0) + int(summary.get("chats") or 0) + int(summary.get("groups") or 0)
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


def run_json_command(cmd: list[str], *, cwd: Path, timeout: int = 90) -> dict[str, Any]:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"returncode": 1, "stdout": "", "stderr": f"{type(exc).__name__}: {exc}", "json": {}}
    payload: dict[str, Any] = {}
    text = proc.stdout or ""
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "json": parsed}
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for idx, char in enumerate(text):
        if char != "{":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[idx:])
            if isinstance(parsed, dict):
                # Fallback for noisy commands: keep the first complete object so
                # nested pretty-printed objects do not overwrite the envelope.
                payload = parsed
                break
        except json.JSONDecodeError:
            continue
    return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr, "json": payload}


def wacli_auth_status(canonical: Path, store: Path) -> dict[str, Any]:
    if not store.exists():
        return {"status": "missing", "authenticated": False, "store": str(store)}
    cmd = [
        sys.executable,
        "packs/ingestion/primitives/discover/messages/whatsapp_wacli.py",
        "status",
        "--store",
        str(store),
    ]
    result = run_json_command(cmd, cwd=canonical, timeout=90)
    payload = result.get("json") if isinstance(result.get("json"), dict) else {}
    auth = payload.get("auth") if isinstance(payload.get("auth"), dict) else {}
    return {
        "status": payload.get("status") or ("ok" if result.get("returncode") == 0 else "failed"),
        "authenticated": bool(auth.get("authenticated")),
        "returncode": result.get("returncode"),
        "store": str(store),
        "auth": auth,
        "doctor_connected": ((payload.get("doctor") or {}).get("data") or {}).get("connected") if isinstance(payload.get("doctor"), dict) else None,
        "error": payload.get("error") or auth.get("error") or (result.get("stderr") or "")[-1000:],
    }


def wacli_candidate(canonical: Path, store: Path) -> dict[str, Any]:
    summary = wacli_store_summary(store)
    auth = wacli_auth_status(canonical, store)
    score = int(summary.get("score") or 0) + (1_000_000_000 if auth.get("authenticated") else 0)
    return {"store": str(store), "summary": summary, "auth_status": auth, "score": score, "newest_mtime": newest_mtime(store)}


def copy_directory_replace(src: Path, dst: Path, *, apply: bool, backup: bool) -> dict[str, Any]:
    backup_path = ""
    if apply:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            backup_path = str(dst.with_name(f"{dst.name}.backup-{stamp()}"))
            if backup:
                shutil.move(str(dst), backup_path)
            else:
                shutil.rmtree(dst)
        shutil.copytree(src, dst)
    return {"action": "copied" if apply else "would_copy", "source": str(src), "target": str(dst), "backup": backup_path}


def repair_wacli_store(canonical: Path, legacy: list[Path], *, apply: bool, backup: bool, scrub_bad: bool) -> dict[str, Any]:
    canonical_store = canonical / ".powerpacks/messages/wacli"
    stores = [canonical_store, *[repo / ".powerpacks/messages/wacli" for repo in legacy]]
    candidates = [wacli_candidate(canonical, store) for store in stores if store.exists()]
    canonical_candidate = next((item for item in candidates if Path(item["store"]).resolve() == canonical_store.resolve()), wacli_candidate(canonical, canonical_store))
    best = max(candidates, key=lambda item: (int(item.get("score") or 0), float(item.get("newest_mtime") or 0)), default=canonical_candidate)
    result: dict[str, Any] = {
        "canonical_store": str(canonical_store),
        "canonical": canonical_candidate,
        "candidates": candidates,
        "best_store": best.get("store"),
        "action": "none",
        "root_cause": "",
    }
    canonical_auth = bool((canonical_candidate.get("auth_status") or {}).get("authenticated"))
    best_auth = bool((best.get("auth_status") or {}).get("authenticated"))
    best_store = Path(str(best.get("store") or ""))
    if best_auth and best_store.exists() and best_store.resolve() != canonical_store.resolve() and not canonical_auth:
        result["root_cause"] = "canonical wacli store is missing or not authenticated, but an authenticated legacy store exists"
        result.update(copy_directory_replace(best_store, canonical_store, apply=apply, backup=True))
        if apply:
            result["post_copy_auth_status"] = wacli_auth_status(canonical, canonical_store)
        return result
    if not canonical_auth and canonical_store.exists() and scrub_bad:
        dest = canonical_store.with_name(f"wacli.stale-{stamp()}")
        result["root_cause"] = "canonical wacli store exists but is not authenticated; no better authenticated legacy store was found"
        result["action"] = "moved_stale" if apply else "would_move_stale"
        result["stale_dest"] = str(dest)
        if apply:
            shutil.move(str(canonical_store), str(dest))
        return result
    if canonical_auth:
        result["root_cause"] = "canonical wacli store is authenticated"
    elif not canonical_store.exists():
        result["root_cause"] = "no canonical wacli store found; user may need WhatsApp reauth if WhatsApp is linked"
    else:
        result["root_cause"] = "canonical wacli store is present but not authenticated; user may need reauth or --scrub-bad-wacli"
    return result


def adopt_state(canonical: Path, legacy: list[Path], config: dict[str, Any], *, apply: bool, backup: bool) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for spec in config.get("managed_paths") or []:
        rel_path = str(spec.get("path") or "")
        if not rel_path:
            continue
        policy = str(spec.get("policy") or "report_only")
        target = canonical / rel_path
        sources = [(repo / rel_path) for repo in legacy if (repo / rel_path).exists()]
        action: dict[str, Any] = {
            "id": spec.get("id"),
            "path": rel_path,
            "policy": policy,
            "target": str(target),
            "target_exists": target.exists(),
            "target_newest_mtime": newest_mtime(target),
            "target_file_count": file_count(target),
            "sources": [
                {"path": str(src), "newest_mtime": newest_mtime(src), "file_count": file_count(src)}
                for src in sources
            ],
        }
        if not sources:
            action["action"] = "no_legacy_source"
            actions.append(action)
            continue
        newest_source = max(sources, key=newest_mtime)
        if policy == "adopt_if_missing":
            action["newest_source"] = str(newest_source)
            if target.exists():
                action["action"] = "kept_existing_target"
                actions.append(action)
                continue
            result = copy_file_if_newer(newest_source, target, apply=apply, backup=False)
            action.update(result)
            actions.append(action)
            continue
        if policy != "adopt_newer":
            action["action"] = "report_only"
            action["newest_source"] = str(newest_source)
            actions.append(action)
            continue
        if newest_mtime(newest_source) <= newest_mtime(target):
            action["action"] = "kept_target"
            action["newest_source"] = str(newest_source)
            actions.append(action)
            continue
        if str(spec.get("kind")) == "directory":
            result = copy_dir_newer_files(newest_source, target, apply=apply, backup=backup)
        else:
            result = copy_file_if_newer(newest_source, target, apply=apply, backup=backup)
        action.update(result)
        action["newest_source"] = str(newest_source)
        actions.append(action)
    return actions


def quarantine_legacy(legacy: list[Path], *, apply: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for repo in legacy:
        state = repo / ".powerpacks"
        if not state.exists():
            continue
        dest = repo / f".powerpacks.stale-{stamp()}"
        out.append({"source": str(state), "dest": str(dest), "action": "moved" if apply else "would_move"})
        if apply:
            shutil.move(str(state), str(dest))
    return out


def summarize_manifest(canonical: Path, rel_path: str) -> dict[str, Any]:
    path = canonical / rel_path
    summary: dict[str, Any] = {
        "path": rel_path,
        "exists": path.exists(),
        "status": "missing",
        "updated_at": "",
    }
    if not path.exists():
        return summary
    data = load_json(path)
    summary.update({
        "status": str(data.get("status") or "unknown"),
        "updated_at": data.get("updated_at") or data.get("completed_at") or "",
        "artifact_dir": data.get("artifact_dir") or str(path.parent.relative_to(canonical)),
        "stats": data.get("stats") if isinstance(data.get("stats"), dict) else {},
    })
    for key in ("contacts_csv", "linkedin_resolution_queue_csv"):
        if data.get(key):
            summary[f"{key}_rows"] = csv_count(canonical / str(data[key]))
    outputs = data.get("outputs") if isinstance(data.get("outputs"), dict) else {}
    if outputs.get("people_csv"):
        summary["people_csv_rows"] = csv_count(canonical / str(outputs["people_csv"]))
    if outputs.get("directory_csv"):
        summary["directory_csv"] = str(outputs["directory_csv"])
    return summary


def vertical_stage_health(canonical: Path) -> dict[str, Any]:
    verticals: dict[str, Any] = {}
    for name, spec in VERTICAL_STAGE_PATHS.items():
        discovery = summarize_manifest(canonical, spec["discovery"])
        import_stage = summarize_manifest(canonical, spec["import"])
        verticals[name] = {
            "discovery": discovery,
            "import": import_stage,
            "commands": {
                "discovery": spec["discovery_command"],
                "import": spec["import_command"],
            },
            "contract": {
                "discovery_manifest": spec["discovery"],
                "import_manifest": spec["import"],
                "notes": "Discovery and import/enrichment are separate direct vertical calls. Rerunning import should reuse existing artifacts where the primitive is idempotent.",
            },
        }
    return {
        "status": "ok",
        "verticals": verticals,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--target", default="", help="Canonical Powerpacks repo root")
    parser.add_argument("--legacy-source", action="append", default=[], help="Additional legacy repo root containing .powerpacks")
    parser.add_argument("--apply", dest="apply", action="store_true", default=True, help="Apply repairs. This is the default.")
    parser.add_argument("--dry-run", dest="apply", action="store_false", help="Inspect the repair plan without changing files.")
    parser.add_argument("--backup", action="store_true", default=True, help="Backup target files/dirs before overwrite/copy where applicable. This is the default for replacements.")
    parser.add_argument("--no-backup", dest="backup", action="store_false", help="Do not create backups before replacement operations.")
    parser.add_argument("--no-repair-wacli", action="store_true", help="Do not copy a better authenticated legacy wacli store into the canonical repo")
    parser.add_argument("--scrub-bad-wacli", dest="scrub_bad_wacli", action="store_true", default=True, help="Move an unauthenticated canonical wacli store aside when no authenticated store exists. This is the default.")
    parser.add_argument("--no-scrub-bad-wacli", dest="scrub_bad_wacli", action="store_false", help="Leave an unauthenticated canonical wacli store in place.")
    parser.add_argument("--quarantine-legacy-state", action="store_true", help="Rename legacy .powerpacks dirs after adoption. Requires --apply.")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    args = parser.parse_args()

    config = load_json(expand_path(args.config))
    canonical = find_canonical_repo(config, args.target)
    if not canonical:
        payload = {"status": "failed", "error": "No canonical non-.codex Powerpacks repo found", "config": str(args.config)}
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2
    legacy = legacy_repos(config, canonical, args.legacy_source)
    status = "ok"
    issues: list[str] = []
    if is_codex_path(Path.cwd().resolve()):
        issues.append("current working directory is under .codex; run product commands from canonical_repo")
        status = "needs_attention"

    actions = adopt_state(canonical, legacy, config, apply=args.apply, backup=args.backup)
    wacli_repair = {"action": "skipped", "reason": "--no-repair-wacli"} if args.no_repair_wacli else repair_wacli_store(canonical, legacy, apply=args.apply, backup=args.backup, scrub_bad=bool(args.scrub_bad_wacli))
    if wacli_repair.get("action") in {"would_copy", "would_move_stale"}:
        status = "needs_attention"
        issues.append(str(wacli_repair.get("root_cause") or "wacli store can be repaired"))
    stages = vertical_stage_health(canonical)

    quarantine = []
    if args.quarantine_legacy_state:
        if not args.apply:
            status = "needs_attention"
            issues.append("--quarantine-legacy-state requires --apply; no legacy state was moved")
        else:
            quarantine = quarantine_legacy(legacy, apply=True)

    payload = {
        "status": status,
        "mode": "apply" if args.apply else "dry_run",
        "generated_at": now_iso(),
        "canonical_repo": str(canonical),
        "current_working_directory": str(Path.cwd().resolve()),
        "legacy_repos": [str(path) for path in legacy],
        "issues": issues,
        "adoption_actions": actions,
        "wacli_store_repair": wacli_repair,
        "source_stage_health": stages,
        "quarantine": quarantine,
        "next": "cd " + str(canonical),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not args.apply:
            print("\nDry run only. Re-run without --dry-run to apply repairs.", file=sys.stderr)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

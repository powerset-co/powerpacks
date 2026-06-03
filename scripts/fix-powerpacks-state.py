#!/usr/bin/env python3
"""Doctor/fixer for Powerpacks local state roots.

The script enforces the local-state contract in config/powerpacks-state-paths.json:
Powerpacks product state should live under the canonical non-.codex repo's
.powerpacks/ directory. Legacy .codex checkouts may be scanned as sources, but
should not remain the runtime root.

Default mode is a dry-run. Use --apply to copy missing/newer managed state into
the canonical repo. Use --quarantine-legacy-state only with --apply after review;
it renames legacy .powerpacks directories instead of deleting them.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/powerpacks-state-paths.json"


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


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
        if (path / ".powerpacks").exists() and path not in out:
            out.append(path)
    return out


def account_records(accounts: dict[str, Any]) -> dict[str, Any]:
    records = accounts.get("accounts") or accounts.get("channels") or accounts.get("sources") or {}
    return records if isinstance(records, dict) else {}


def config_obj(record: dict[str, Any]) -> dict[str, Any]:
    cfg = record.get("config") if isinstance(record.get("config"), dict) else {}
    return cfg if isinstance(cfg, dict) else {}


def string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    return []


def msgvault_accounts(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"status": "missing", "path": str(db_path), "accounts": []}
    try:
        with sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT identifier AS account_email, display_name, COUNT(messages.id) AS message_count
                FROM sources
                LEFT JOIN messages ON messages.source_id = sources.id
                WHERE lower(source_type) = 'gmail' AND identifier IS NOT NULL AND identifier != ''
                GROUP BY sources.id, identifier, display_name
                ORDER BY identifier
                """
            ).fetchall()
        accounts = [
            {
                "account_email": str(row["account_email"]).strip().lower(),
                "display_name": row["display_name"] or "",
                "message_count": int(row["message_count"] or 0),
            }
            for row in rows
            if str(row["account_email"] or "").strip()
        ]
        return {"status": "ok", "path": str(db_path), "accounts": accounts}
    except Exception as exc:
        return {"status": "failed", "path": str(db_path), "error": f"{type(exc).__name__}: {exc}", "accounts": []}


def wacli_store_summary(store: Path) -> dict[str, Any]:
    db = store / "wacli.db"
    if not db.exists():
        return {"status": "missing", "store": str(store)}
    summary: dict[str, Any] = {"status": "present", "store": str(store), "db": str(db)}
    try:
        with sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True) as conn:
            tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'").fetchall()}
            for table in ["chats", "contacts", "groups", "messages"]:
                if table in tables:
                    summary[table] = int(conn.execute(f"select count(*) from {table}").fetchone()[0] or 0)
    except Exception as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
    return summary


def linked_source_checks(canonical: Path, config: dict[str, Any]) -> dict[str, Any]:
    accounts_path = canonical / ".powerpacks/ingestion/accounts.json"
    accounts = load_json(accounts_path)
    records = account_records(accounts)
    checks: dict[str, Any] = {"accounts_path": str(accounts_path), "accounts_exists": accounts_path.exists(), "sources": {}}

    for source, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        if not (record.get("linked") or record.get("status") == "linked"):
            continue
        cfg = config_obj(record)
        item: dict[str, Any] = {"linked": True, "status": record.get("status") or "linked"}
        if source == "gmail":
            db_text = str(cfg.get("msgvault_db") or config.get("external_paths", {}).get("msgvault_default_db") or "~/.msgvault/msgvault.db")
            db = expand_path(db_text)
            discovered = msgvault_accounts(db)
            selected = string_list(cfg.get("selected_accounts")) or string_list(cfg.get("account_emails")) or string_list(record.get("usernames"))
            discovered_emails = {row.get("account_email") for row in discovered.get("accounts", [])}
            missing = [email for email in selected if email.lower() not in discovered_emails]
            item.update({"msgvault": discovered, "selected_accounts": selected, "missing_selected_accounts": missing, "ok": discovered.get("status") == "ok" and not missing})
        elif source == "linkedin_csv":
            csv_path = expand_path(str(cfg.get("csv_path") or "")) if cfg.get("csv_path") else None
            item.update({"csv_path": str(csv_path) if csv_path else "", "ok": bool(csv_path and csv_path.exists())})
        elif source == "messages":
            msg_cfg = config_obj(record)
            whatsapp = msg_cfg.get("whatsapp") if isinstance(msg_cfg.get("whatsapp"), dict) else {}
            imessage = msg_cfg.get("imessage") if isinstance(msg_cfg.get("imessage"), dict) else {}
            item.update({
                "imessage_config": imessage,
                "whatsapp_config": whatsapp,
                "wacli_store": wacli_store_summary(canonical / ".powerpacks/messages/wacli"),
                "ok": True,
            })
        elif source == "twitter":
            item.update({"handle": (string_list(record.get("usernames")) or [cfg.get("handle") or ""])[0], "ok": True})
        else:
            item["ok"] = True
        checks["sources"][source] = item
    return checks


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG))
    parser.add_argument("--target", default="", help="Canonical Powerpacks repo root")
    parser.add_argument("--legacy-source", action="append", default=[], help="Additional legacy repo root containing .powerpacks")
    parser.add_argument("--apply", action="store_true", help="Apply newer-state adoption. Default is dry-run/report only.")
    parser.add_argument("--backup", action="store_true", help="Backup target files/dirs before overwrite/copy where applicable.")
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
    checks = linked_source_checks(canonical, config)
    for source, item in (checks.get("sources") or {}).items():
        if isinstance(item, dict) and item.get("ok") is False:
            status = "needs_attention"
            issues.append(f"linked source check failed: {source}")

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
        "linked_source_checks": checks,
        "quarantine": quarantine,
        "next": "cd " + str(canonical),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))
        if not args.apply:
            print("\nDry run only. Re-run with --apply to copy missing/newer managed state.", file=sys.stderr)
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

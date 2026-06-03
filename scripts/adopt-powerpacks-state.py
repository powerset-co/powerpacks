#!/usr/bin/env python3
"""Adopt local Powerpacks state from a legacy/agent checkout into this repo.

Use this when an agent accidentally ran setup from ~/.codex/powerpacks (or any
other alternate checkout) and created useful local state there. The script copies
that checkout's .powerpacks directory into the canonical Powerpacks install so
future setup/import/index runs all use one repeatable state root.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def expand(path_text: str) -> Path:
    return Path(path_text).expanduser().resolve()


def is_repo_root(path: Path) -> bool:
    return (path / "packs").is_dir() and (path / "scripts").is_dir()


def default_target() -> Path:
    candidates = []
    if os.environ.get("POWERPACKS_REPO_ROOT"):
        candidates.append(expand(os.environ["POWERPACKS_REPO_ROOT"]))
    candidates.extend([
        Path.cwd().resolve(),
        expand("~/powerpacks"),
        expand("~/workspace/powerpacks"),
    ])
    for candidate in candidates:
        if is_repo_root(candidate) and "/.codex/" not in str(candidate):
            return candidate
    for candidate in candidates:
        if is_repo_root(candidate):
            return candidate
    return Path.cwd().resolve()


def iter_paths(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        yield path


def copy_file(src: Path, dst: Path, *, overwrite: bool, dry_run: bool, copied: list[str], skipped: list[str]) -> None:
    if dst.exists() and not overwrite:
        skipped.append(str(dst))
        return
    copied.append(str(dst))
    if dry_run:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def copy_tree_contents(src_root: Path, dst_root: Path, *, overwrite: bool, dry_run: bool) -> dict[str, object]:
    copied: list[str] = []
    skipped: list[str] = []
    dirs_created: list[str] = []
    for src in iter_paths(src_root):
        rel = src.relative_to(src_root)
        dst = dst_root / rel
        if src.is_dir():
            if not dst.exists():
                dirs_created.append(str(dst))
                if not dry_run:
                    dst.mkdir(parents=True, exist_ok=True)
            continue
        if src.is_file() or src.is_symlink():
            copy_file(src, dst, overwrite=overwrite, dry_run=dry_run, copied=copied, skipped=skipped)
    return {
        "copied_files": len(copied),
        "skipped_existing_files": len(skipped),
        "created_dirs": len(dirs_created),
        "copied_sample": copied[:20],
        "skipped_sample": skipped[:20],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="~/.codex/powerpacks", help="Legacy/alternate Powerpacks checkout to copy .powerpacks from")
    parser.add_argument("--target", default="", help="Canonical Powerpacks checkout; defaults to POWERPACKS_REPO_ROOT, cwd, ~/powerpacks, or ~/workspace/powerpacks")
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing target files. Without this, existing files are kept.")
    parser.add_argument("--backup-existing", action="store_true", help="Move existing target .powerpacks aside before copying")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-target-codex", action="store_true", help="Permit target paths under ~/.codex; normally refused")
    args = parser.parse_args()

    source_repo = expand(args.source)
    target_repo = expand(args.target) if args.target else default_target()
    source_state = source_repo / ".powerpacks"
    target_state = target_repo / ".powerpacks"

    if not source_state.exists():
        print(f"error: source .powerpacks not found: {source_state}", file=sys.stderr)
        return 2
    if not is_repo_root(target_repo):
        print(f"error: target is not a Powerpacks repo root: {target_repo}", file=sys.stderr)
        return 2
    if "/.codex/" in str(target_repo) and not args.allow_target_codex:
        print(f"error: refusing to use .codex checkout as canonical target: {target_repo}", file=sys.stderr)
        print("pass --allow-target-codex only for explicit debugging", file=sys.stderr)
        return 2
    if source_state.resolve() == target_state.resolve():
        print(f"status: already_same_state_root\nrepo: {target_repo}\nstate: {target_state}")
        return 0

    backup_path = None
    if target_state.exists() and args.backup_existing:
        backup_path = target_repo / f".powerpacks.backup-{now_stamp()}"
        print(f"backup: {target_state} -> {backup_path}")
        if not args.dry_run:
            shutil.move(str(target_state), str(backup_path))

    if not target_state.exists() and not args.dry_run:
        target_state.mkdir(parents=True, exist_ok=True)

    stats = copy_tree_contents(source_state, target_state, overwrite=args.overwrite, dry_run=args.dry_run)
    print("status: ok" if not args.dry_run else "status: dry_run")
    print(f"source_repo: {source_repo}")
    print(f"target_repo: {target_repo}")
    print(f"source_state: {source_state}")
    print(f"target_state: {target_state}")
    if backup_path:
        print(f"backup: {backup_path}")
    for key, value in stats.items():
        print(f"{key}: {value}")
    print("next: cd " + str(target_repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

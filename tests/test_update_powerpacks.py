from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = ROOT / "bin/update-powerpacks"
SKILL = ROOT / "packs/powerset/skills/update-powerpacks/SKILL.md"


def run(*args: str | Path, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"command failed ({proc.returncode}): {' '.join(str(arg) for arg in args)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def write(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)


class UpdatePowerpacksTests(unittest.TestCase):
    def test_skill_only_dispatches_to_installed_launcher(self) -> None:
        text = SKILL.read_text(encoding="utf-8")

        self.assertLess(len(text.splitlines()), 45)
        self.assertNotIn("git status", text)
        self.assertNotIn("git stash", text)
        self.assertNotIn("git reset", text)
        self.assertIn("skills/update-powerpacks/update-powerpacks", text)
        self.assertIn("Do not run any other Git command", text)

        for adapter in (
            ROOT / "adapters/codex/install.sh",
            ROOT / "adapters/claude-code/install.sh",
            ROOT / "adapters/pi/install.sh",
        ):
            adapter_text = adapter.read_text(encoding="utf-8")
            self.assertIn(
                'install -m 755 "$REPO_ROOT/bin/update-powerpacks" "$SKILLS_DIR/update-powerpacks/update-powerpacks"',
                adapter_text,
            )

        codex_adapter = (ROOT / "adapters/codex/install.sh").read_text(encoding="utf-8")
        self.assertIn('cp -R "$BUNDLE_DIR/.powerpacks" "$tmp/.powerpacks"', codex_adapter)
        self.assertIn('mv "$BUNDLE_DIR" "$backup"', codex_adapter)

    def test_codex_adapter_preserves_installed_bundle_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            codex_home = root / "codex"
            skills_dir = codex_home / "skills"
            bundle_dir = codex_home / "powerpacks"
            write(bundle_dir / ".powerpacks/sentinel", "keep installed state\n")
            write(bundle_dir / ".env", "KEEP_BUNDLE_ENV=yes\n")

            env = os.environ | {
                "CODEX_HOME": str(codex_home),
                "CODEX_POWERPACKS_BUNDLE_DIR": str(bundle_dir),
                "POWERPACKS_SKIP_AGENT_BOOTSTRAP": "1",
            }
            run(ROOT / "adapters/codex/install.sh", skills_dir, cwd=ROOT, env=env)

            self.assertEqual(
                (bundle_dir / ".powerpacks/sentinel").read_text(encoding="utf-8"),
                "keep installed state\n",
            )
            self.assertEqual((bundle_dir / ".env").read_text(encoding="utf-8"), "KEEP_BUNDLE_ENV=yes\n")
            self.assertFalse(Path(f"{bundle_dir}.tmp").exists())
            self.assertFalse(Path(f"{bundle_dir}.backup").exists())
            self.assertTrue((skills_dir / "update-powerpacks/update-powerpacks").is_file())

    def test_script_stashes_dirty_files_resets_main_and_preserves_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            publisher = root / "publisher"
            checkout = root / "checkout"
            home = root / "home"
            install_record = root / "install-record.txt"
            sync_record = root / "sync-record.txt"

            run("git", "init", "--bare", remote, cwd=root)
            run("git", "init", "-b", "main", publisher, cwd=root)
            run("git", "config", "user.email", "test@example.com", cwd=publisher)
            run("git", "config", "user.name", "Powerpacks Test", cwd=publisher)

            write(publisher / ".gitignore", ".powerpacks/\n.env\n")
            write(publisher / "packs/.keep", "")
            write(publisher / "pyproject.toml", "[project]\nname='fixture'\nversion='0'\n")
            write(publisher / "tracked.txt", "remote-original\n")
            write(publisher / "version.txt", "old\n")
            write(publisher / "bin/update-powerpacks", UPDATE_SCRIPT.read_text(encoding="utf-8"), executable=True)
            write(
                publisher / "bin/sync-agent-files.sh",
                '#!/usr/bin/env bash\nset -euo pipefail\nprintf "synced\\n" > "$POWERPACKS_TEST_SYNC_RECORD"\n',
                executable=True,
            )
            write(
                publisher / "install.sh",
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "${POWERPACKS_TEST_INSTALL_FAIL:-}" == "1" ]]; then
  exit 42
fi
printf '%s\n%s\n' "$*" "${POWERPACKS_SKIP_AGENT_BOOTSTRAP:-}" > "$POWERPACKS_TEST_INSTALL_RECORD"
skills_dir="${2:-$HOME/.codex/skills}"
mkdir -p "$skills_dir"
printf '{"repo_root":"%s","commit":"fixture","version":"0","installed_at":"now"}\n' "$PWD" > "$skills_dir/.powerpacks-install.json"
""",
                executable=True,
            )
            run("git", "add", ".", cwd=publisher)
            run("git", "commit", "-m", "initial", cwd=publisher)
            run("git", "remote", "add", "origin", remote, cwd=publisher)
            run("git", "push", "-u", "origin", "main", cwd=publisher)
            run("git", "--git-dir", remote, "symbolic-ref", "HEAD", "refs/heads/main", cwd=root)
            run("git", "clone", remote, checkout, cwd=root)
            run("git", "config", "user.email", "test@example.com", cwd=checkout)
            run("git", "config", "user.name", "Powerpacks Test", cwd=checkout)

            write(publisher / "version.txt", "new\n")
            run("git", "add", "version.txt", cwd=publisher)
            run("git", "commit", "-m", "remote update", cwd=publisher)
            run("git", "push", cwd=publisher)

            run("git", "switch", "-c", "local-work", cwd=checkout)
            write(checkout / "tracked.txt", "dirty tracked work\n")
            write(checkout / "notes.txt", "dirty untracked work\n")
            write(checkout / ".powerpacks/sentinel", "keep powerpacks state\n")
            write(checkout / ".env", "KEEP_ENV=yes\n")

            installed_launcher = home / ".codex/skills/update-powerpacks/update-powerpacks"
            write(installed_launcher, UPDATE_SCRIPT.read_text(encoding="utf-8"), executable=True)
            write(
                home / ".codex/skills/.powerpacks-install.json",
                json.dumps({"repo_root": str(checkout)}) + "\n",
            )

            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(home),
                    "POWERPACKS_TEST_INSTALL_RECORD": str(install_record),
                    "POWERPACKS_TEST_SYNC_RECORD": str(sync_record),
                }
            )
            proc = run(installed_launcher, "codex", cwd=root, env=env)

            self.assertEqual(run("git", "branch", "--show-current", cwd=checkout).stdout.strip(), "main")
            self.assertEqual(
                run("git", "rev-parse", "HEAD", cwd=checkout).stdout,
                run("git", "rev-parse", "origin/main", cwd=checkout).stdout,
            )
            self.assertEqual((checkout / "version.txt").read_text(encoding="utf-8"), "new\n")
            self.assertEqual((checkout / "tracked.txt").read_text(encoding="utf-8"), "remote-original\n")
            self.assertFalse((checkout / "notes.txt").exists())
            self.assertEqual((checkout / ".powerpacks/sentinel").read_text(encoding="utf-8"), "keep powerpacks state\n")
            self.assertEqual((checkout / ".env").read_text(encoding="utf-8"), "KEEP_ENV=yes\n")
            self.assertEqual(run("git", "status", "--porcelain", cwd=checkout).stdout, "")

            stash_files = set(
                run("git", "stash", "show", "--include-untracked", "--name-only", "stash@{0}", cwd=checkout).stdout.splitlines()
            )
            self.assertEqual(stash_files, {"notes.txt", "tracked.txt"})
            self.assertIn("stash_restored=false", proc.stdout)
            self.assertNotIn("stash_commit=none", proc.stdout)
            self.assertIn("stash_ref=stash@{0}", proc.stdout)
            self.assertIn("preserved=.powerpacks,.env", proc.stdout)
            self.assertEqual(install_record.read_text(encoding="utf-8"), "codex\n1\n")
            self.assertEqual(sync_record.read_text(encoding="utf-8"), "synced\n")
            self.assertTrue((home / ".codex/skills/.powerpacks-install.json").is_file())

            write(checkout / "after-reset.txt", "stash me after reset\n")
            failing_env = env | {"POWERPACKS_TEST_INSTALL_FAIL": "1"}
            failed_proc = subprocess.run(
                [str(installed_launcher), "codex"],
                cwd=root,
                env=failing_env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(failed_proc.returncode, 42)
            self.assertIn("recovery_stash_commit=", failed_proc.stderr)
            self.assertIn("recovery_stash_ref=stash@{0}", failed_proc.stderr)
            self.assertFalse((checkout / "after-reset.txt").exists())
            self.assertIn(
                "after-reset.txt",
                run("git", "stash", "show", "--include-untracked", "--name-only", "stash@{0}", cwd=checkout).stdout,
            )

            write(publisher / ".env", "REMOTE_ENV=unsafe\n")
            run("git", "add", "--force", ".env", cwd=publisher)
            run("git", "commit", "-m", "unsafe remote env", cwd=publisher)
            run("git", "push", cwd=publisher)
            unsafe_proc = subprocess.run(
                [str(installed_launcher), "codex"],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(unsafe_proc.returncode, 0)
            self.assertIn("origin/main tracks .env", unsafe_proc.stderr)
            self.assertEqual((checkout / ".env").read_text(encoding="utf-8"), "KEEP_ENV=yes\n")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = ROOT / "bin/update-powerpacks"
CHANNEL_SCRIPT = ROOT / "bin/powerpacks-channel"
STAMP_SCRIPT = ROOT / "bin/powerpacks-install-stamp"
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
        self.assertNotIn("reload", text.lower())
        self.assertNotIn("restart", text.lower())
        self.assertIn("skills/update-powerpacks/update-powerpacks", text)
        self.assertIn("Do not run any other Git command", text)
        self.assertNotIn("restart_required", UPDATE_SCRIPT.read_text(encoding="utf-8"))

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
            write(publisher / ".release-please-manifest.json", '{".": "1.0.0"}\n')
            write(publisher / "tracked.txt", "remote-original\n")
            write(publisher / "version.txt", "old\n")
            write(publisher / "bin/update-powerpacks", UPDATE_SCRIPT.read_text(encoding="utf-8"), executable=True)
            write(publisher / "bin/powerpacks-channel", CHANNEL_SCRIPT.read_text(encoding="utf-8"), executable=True)
            write(publisher / "bin/powerpacks-install-stamp", STAMP_SCRIPT.read_text(encoding="utf-8"), executable=True)
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
            # The only published release. Everything committed after this tag is
            # unreleased main, which a pinned install must NOT pick up.
            run("git", "tag", "powerpacks-v1.0.0", cwd=publisher)
            run("git", "remote", "add", "origin", remote, cwd=publisher)
            run("git", "push", "-u", "origin", "main", cwd=publisher)
            run("git", "push", "--tags", "origin", cwd=publisher)
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

            self.assertEqual(
                run("git", "branch", "--show-current", cwd=checkout).stdout.strip(),
                "powerpacks-stable",
            )
            self.assertEqual(
                run("git", "rev-parse", "HEAD", cwd=checkout).stdout,
                run("git", "rev-parse", "powerpacks-v1.0.0^{commit}", cwd=checkout).stdout,
            )
            # The whole point of the pin: unreleased main moved version.txt to
            # "new", and the install stayed on the released content instead.
            self.assertEqual((checkout / "version.txt").read_text(encoding="utf-8"), "old\n")
            self.assertNotEqual(
                run("git", "rev-parse", "HEAD", cwd=checkout).stdout,
                run("git", "rev-parse", "origin/main", cwd=checkout).stdout,
            )
            self.assertIn("channel=stable", proc.stdout)
            self.assertIn("ref=powerpacks-v1.0.0", proc.stdout)
            self.assertIn("version=1.0.0", proc.stdout)
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

            # The guard now names the release being moved to, so it has to be a
            # tagged release that carries .env — not merely unreleased main.
            write(publisher / ".env", "REMOTE_ENV=unsafe\n")
            run("git", "add", "--force", ".env", cwd=publisher)
            run("git", "commit", "-m", "unsafe remote env", cwd=publisher)
            run("git", "tag", "powerpacks-v1.1.0", cwd=publisher)
            run("git", "push", cwd=publisher)
            run("git", "push", "--tags", "origin", cwd=publisher)
            unsafe_proc = subprocess.run(
                [str(installed_launcher), "codex"],
                cwd=root,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(unsafe_proc.returncode, 0)
            self.assertIn("powerpacks-v1.1.0 tracks .env", unsafe_proc.stderr)
            self.assertEqual((checkout / ".env").read_text(encoding="utf-8"), "KEEP_ENV=yes\n")


class ReferencedPathTests(unittest.TestCase):
    """Every repo path a shell entry point names must actually be in the repo.

    The updater once pointed at a wacli primitive that two renames had moved.
    Nothing failed: the guard around it is `[[ -f ... ]]`, so the step silently
    became a no-op and the update looked healthy for months.
    """

    # Rendered by bin/agent-bootstrap into an ignored file, so it is legitimately
    # absent from a clean checkout.
    GENERATED = {".codex/AGENTS.md"}

    def test_repo_anchored_paths_exist(self) -> None:
        sources = sorted((ROOT / "bin").iterdir())
        sources += [ROOT / f"adapters/{h}/install.sh" for h in ("codex", "claude-code", "pi")]

        pattern = re.compile(r"\$\{?(?:REPO|REPO_ROOT)\}?/([A-Za-z0-9_./-]+)")
        referenced: dict[str, str] = {}
        for source in sources:
            if not source.is_file():
                continue
            for match in pattern.finditer(source.read_text(encoding="utf-8", errors="ignore")):
                referenced.setdefault(match.group(1).rstrip("/"), source.name)

        self.assertGreater(len(referenced), 20, "path scan found suspiciously little")

        missing = []
        for path, source in sorted(referenced.items()):
            if path in self.GENERATED:
                continue
            tracked = subprocess.run(
                ["git", "ls-files", "--", path],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if not tracked:
                missing.append(f"{source} references missing repo path: {path}")

        self.assertEqual(missing, [], "\n".join(missing))


class ReleaseChannelTests(unittest.TestCase):
    """bin/powerpacks-channel decides which ref a release channel names."""

    TAGS = (
        "powerpacks-v0.9.0",
        "powerpacks-v0.10.0",
        "powerpacks-v1.0.0-rc.1",
        "powerpacks-v1.0.0-rc.2",
        "powerpacks-v1.0.0",
        "powerpacks-v1.1.0-rc.1",
        "powerpacks-console-v9.9.9",
    )

    def _repo(self, root: Path, tags: tuple[str, ...]) -> Path:
        remote = root / "remote.git"
        repo = root / "repo"
        run("git", "init", "--bare", remote, cwd=root)
        run("git", "init", "-b", "main", repo, cwd=root)
        run("git", "config", "user.email", "test@example.com", cwd=repo)
        run("git", "config", "user.name", "Powerpacks Test", cwd=repo)
        write(repo / "README.md", "fixture\n")
        run("git", "add", ".", cwd=repo)
        run("git", "commit", "-m", "initial", cwd=repo)
        # One commit per tag, the way release-please actually tags: every release
        # is its own version-bump commit, so no two tags share a commit.
        for tag in tags:
            write(repo / "README.md", f"fixture {tag}\n")
            run("git", "add", ".", cwd=repo)
            run("git", "commit", "-m", f"release {tag}", cwd=repo)
            run("git", "tag", tag, cwd=repo)
        run("git", "remote", "add", "origin", remote, cwd=repo)
        run("git", "push", "-u", "origin", "main", cwd=repo)
        return repo

    def _resolve(self, repo: Path, *args: str, **overrides: str) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("POWERPACKS_")}
        env.update(overrides)
        out = run(CHANNEL_SCRIPT, *args, repo, cwd=repo, env=env).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def test_channels_select_the_right_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp), self.TAGS)

            # stable skips every release candidate and the retired console tags,
            # and orders 0.10.0 above 0.9.0 (a plain string sort would not).
            self.assertEqual(self._resolve(repo)["ref"], "powerpacks-v1.0.0")
            self.assertEqual(self._resolve(repo)["channel"], "stable")

            # rc sees candidates too, so the unreleased 1.1.0 line wins.
            self.assertEqual(
                self._resolve(repo, POWERPACKS_CHANNEL="rc")["ref"],
                "powerpacks-v1.1.0-rc.1",
            )
            self.assertEqual(
                self._resolve(repo, POWERPACKS_CHANNEL="edge")["ref"],
                "origin/main",
            )
            self.assertEqual(
                self._resolve(repo, POWERPACKS_REF="powerpacks-v0.9.0"),
                {
                    "channel": "pinned",
                    "ref": "powerpacks-v0.9.0",
                    "branch": "powerpacks-pinned",
                },
            )

    def test_final_release_outranks_its_own_candidates_on_rc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(
                Path(tmp),
                ("powerpacks-v1.0.0-rc.1", "powerpacks-v1.0.0-rc.2", "powerpacks-v1.0.0"),
            )
            self.assertEqual(
                self._resolve(repo, POWERPACKS_CHANNEL="rc")["ref"],
                "powerpacks-v1.0.0",
            )

    def test_checkout_pins_the_branch_and_channel_sticks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp), self.TAGS)

            self._resolve(repo, "--checkout", POWERPACKS_CHANNEL="rc")
            self.assertEqual(
                run("git", "branch", "--show-current", cwd=repo).stdout.strip(),
                "powerpacks-rc",
            )
            self.assertEqual(
                run("git", "rev-parse", "HEAD", cwd=repo).stdout,
                run("git", "rev-parse", "powerpacks-v1.1.0-rc.1^{commit}", cwd=repo).stdout,
            )
            # The branch name is the channel memory: a later run with no
            # environment at all stays on rc instead of falling back to stable.
            self.assertEqual(self._resolve(repo)["channel"], "rc")

    def test_stale_launcher_hands_off_to_the_shipped_updater(self) -> None:
        """A harness-installed launcher older than the release must not finish the run.

        Everything after the checkout (skill install, pinned-binary refresh)
        depends on the tree's layout, so the release's own updater has to be the
        one that runs it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            remote = root / "remote.git"
            publisher = root / "publisher"
            checkout = root / "checkout"
            home = root / "home"
            handoff_record = root / "handoff.txt"

            real_updater = UPDATE_SCRIPT.read_text(encoding="utf-8")
            # The released copy differs from the installed launcher by one line,
            # which appends a marker whenever THAT copy is the one executing.
            shipped_updater = real_updater.replace(
                "set -euo pipefail",
                'set -euo pipefail\nprintf "handoff\\n" >> "$POWERPACKS_TEST_HANDOFF_RECORD"',
                1,
            )

            run("git", "init", "--bare", remote, cwd=root)
            run("git", "init", "-b", "main", publisher, cwd=root)
            run("git", "config", "user.email", "test@example.com", cwd=publisher)
            run("git", "config", "user.name", "Powerpacks Test", cwd=publisher)
            write(publisher / ".gitignore", ".powerpacks/\n.env\n")
            write(publisher / "packs/.keep", "")
            write(publisher / "pyproject.toml", "[project]\nname='fixture'\nversion='0'\n")
            write(publisher / ".release-please-manifest.json", '{".": "1.0.0"}\n')
            write(publisher / "bin/update-powerpacks", shipped_updater, executable=True)
            write(publisher / "bin/powerpacks-channel", CHANNEL_SCRIPT.read_text(encoding="utf-8"), executable=True)
            write(publisher / "bin/powerpacks-install-stamp", STAMP_SCRIPT.read_text(encoding="utf-8"), executable=True)
            write(
                publisher / "bin/sync-agent-files.sh",
                '#!/usr/bin/env bash\nset -euo pipefail\n',
                executable=True,
            )
            write(
                publisher / "install.sh",
                '#!/usr/bin/env bash\nset -euo pipefail\n'
                'skills_dir="${2:-$HOME/.codex/skills}"\nmkdir -p "$skills_dir"\n',
                executable=True,
            )
            run("git", "add", ".", cwd=publisher)
            run("git", "commit", "-m", "initial", cwd=publisher)
            run("git", "tag", "powerpacks-v1.0.0", cwd=publisher)
            run("git", "remote", "add", "origin", remote, cwd=publisher)
            run("git", "push", "-u", "origin", "main", cwd=publisher)
            run("git", "push", "--tags", "origin", cwd=publisher)
            run("git", "--git-dir", remote, "symbolic-ref", "HEAD", "refs/heads/main", cwd=root)
            run("git", "clone", remote, checkout, cwd=root)

            # The launcher the harness installed is the UNMARKED copy, standing in
            # for a version installed before the release.
            installed_launcher = home / ".codex/skills/update-powerpacks/update-powerpacks"
            write(installed_launcher, real_updater, executable=True)
            write(
                home / ".codex/skills/.powerpacks-install.json",
                json.dumps({"repo_root": str(checkout)}) + "\n",
            )

            env = os.environ | {
                "HOME": str(home),
                "POWERPACKS_TEST_HANDOFF_RECORD": str(handoff_record),
            }
            proc = run(installed_launcher, "codex", cwd=root, env=env)

            # Exactly one marker: the shipped updater ran, and only once.
            self.assertEqual(
                handoff_record.read_text(encoding="utf-8").count("handoff"),
                1,
                "expected exactly one handoff to the shipped updater",
            )
            self.assertIn("handing off to the updater shipped with", proc.stderr)
            self.assertIn("status=ok", proc.stdout)
            self.assertIn("ref=powerpacks-v1.0.0", proc.stdout)

            # Second run: launcher and release now match, so no handoff happens.
            handoff_record.unlink()
            write(installed_launcher, shipped_updater, executable=True)
            proc2 = run(installed_launcher, "codex", cwd=root, env=env)
            self.assertEqual(
                handoff_record.read_text(encoding="utf-8").count("handoff"),
                1,
                "a matching launcher must run once, not hand off to itself",
            )
            self.assertNotIn("handing off", proc2.stderr)

    def test_unknown_channel_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._repo(Path(tmp), self.TAGS)
            env = {k: v for k, v in os.environ.items() if not k.startswith("POWERPACKS_")}
            env["POWERPACKS_CHANNEL"] = "bogus"
            proc = subprocess.run(
                [str(CHANNEL_SCRIPT), str(repo)],
                cwd=repo,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("unknown POWERPACKS_CHANNEL", proc.stderr)

    def test_install_stamp_records_the_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = self._repo(root, self.TAGS)
            write(repo / ".release-please-manifest.json", '{".": "1.0.0"}\n')

            self._resolve(repo, "--checkout")
            dest = root / "skills/.powerpacks-install.json"
            run(STAMP_SCRIPT, repo, "claude-code", dest, cwd=repo)
            stamp = json.loads(dest.read_text(encoding="utf-8"))

            self.assertEqual(stamp["channel"], "stable")
            self.assertEqual(stamp["ref"], "powerpacks-v1.0.0")
            self.assertEqual(stamp["harness"], "claude-code")


if __name__ == "__main__":
    unittest.main()

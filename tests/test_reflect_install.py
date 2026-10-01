from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HOOKS_INSTALL = ROOT / "packs/powerset/primitives/reflect/hooks_install.py"
REFLECT_BIN = ROOT / "bin/reflect"
ADAPTERS = {
    "claude-code": ROOT / "adapters/claude-code/install.sh",
    "codex": ROOT / "adapters/codex/install.sh",
}
CONFIG_FILENAMES = {"claude-code": "settings.json", "codex": "hooks.json"}
TIMEOUTS = {"claude-code": 30, "codex": 3}
MATCHERS = {"claude-code": "", "codex": None}
COMMAND = "/x/bin/reflect hook end"
# Codex's fingerprint of COMMAND's SessionEnd group; pinned so the recipe cannot drift.
COMMAND_TRUSTED_HASH = "sha256:e8f4f81e4d68a4ffa575010850fa89c01b36c4803f31b9d79a168990cba139ff"
FOREIGN_TOML = """model = "gpt-6"

[features]
hooks = true

[projects."/x"]
trust_level = "trusted"
"""


def ours(harness: str, command: str = COMMAND) -> dict:
    return {"type": "command", "command": command, "timeout": TIMEOUTS[harness]}


def our_group(harness: str, command: str = COMMAND) -> dict:
    return {"matcher": MATCHERS[harness], "hooks": [ours(harness, command)]}


def install(harness: str, config_dir: Path, command: str = COMMAND, *, remove: bool = False) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, str(HOOKS_INSTALL), "--harness", harness, "--config-dir", str(config_dir), "--command", command]
    if remove:
        args.append("--remove")
    return subprocess.run(args, check=False, capture_output=True, text=True)


def real_shaped_config() -> dict:
    return {
        "env": {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": "64000"},
        "permissions": {"allow": ["Bash(git status)"], "deny": []},
        "model": "opus",
        "statusLine": {"type": "command", "command": "~/.claude/statusline.sh \u26a1"},
        "enabledPlugins": {"example@market": True},
        "effortLevel": "high",
        "tui": {"theme": "dark"},
        "hooks": {
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.sh"}]}],
            "SessionEnd": [{"matcher": "", "hooks": [{"type": "command", "command": "notify.sh done"}]}],
        },
    }


class HooksInstallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def config_path(self, harness: str) -> Path:
        return self.tmp / harness / CONFIG_FILENAMES[harness]

    def read(self, harness: str) -> dict:
        return json.loads(self.config_path(harness).read_text(encoding="utf-8"))

    def test_register_into_empty_dir_creates_exact_shape(self) -> None:
        for harness in CONFIG_FILENAMES:
            with self.subTest(harness=harness):
                proc = install(harness, self.tmp / harness)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                path = self.config_path(harness)
                self.assertEqual(proc.stdout.strip(), f"registered {harness} SessionEnd hook in {path}")
                self.assertEqual(self.read(harness), {"hooks": {"SessionEnd": [our_group(harness)]}})
                self.assertTrue(path.read_text(encoding="utf-8").endswith("}\n"))

    def test_register_twice_keeps_one_entry(self) -> None:
        for harness in CONFIG_FILENAMES:
            with self.subTest(harness=harness):
                install(harness, self.tmp / harness)
                proc = install(harness, self.tmp / harness)
                self.assertIn("already registered", proc.stdout)
                self.assertEqual(self.read(harness)["hooks"]["SessionEnd"], [our_group(harness)])

    def test_changed_command_replaces_in_place(self) -> None:
        moved = "/new/checkout/bin/reflect hook end"
        for harness in CONFIG_FILENAMES:
            with self.subTest(harness=harness):
                install(harness, self.tmp / harness)
                proc = install(harness, self.tmp / harness, moved)
                self.assertTrue(proc.stdout.startswith("registered "), proc.stdout)
                self.assertEqual(self.read(harness)["hooks"]["SessionEnd"], [our_group(harness, moved)])

    def test_real_shaped_config_is_preserved_and_remove_restores_it(self) -> None:
        for harness in CONFIG_FILENAMES:
            with self.subTest(harness=harness):
                path = self.config_path(harness)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps(real_shaped_config(), indent=2), encoding="utf-8")

                install(harness, self.tmp / harness)
                expected = real_shaped_config()
                groups = expected["hooks"]["SessionEnd"]
                if harness == "codex":
                    groups.insert(0, our_group(harness))
                else:
                    groups.append(our_group(harness))
                self.assertEqual(self.read(harness), expected)

                proc = install(harness, self.tmp / harness, "x", remove=True)
                self.assertEqual(proc.stdout.strip(), f"removed {harness} SessionEnd hook in {path}")
                self.assertEqual(self.read(harness), real_shaped_config())

    def test_remove_drops_empty_containers(self) -> None:
        for harness in CONFIG_FILENAMES:
            with self.subTest(harness=harness):
                path = self.config_path(harness)
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
                install(harness, self.tmp / harness)
                install(harness, self.tmp / harness, "x", remove=True)
                self.assertEqual(self.read(harness), {"model": "opus"})

                proc = install(harness, self.tmp / harness, "x", remove=True)
                self.assertIn("not registered", proc.stdout)

    def test_invalid_json_is_left_untouched(self) -> None:
        for harness in CONFIG_FILENAMES:
            with self.subTest(harness=harness):
                path = self.config_path(harness)
                path.parent.mkdir(parents=True)
                path.write_text('{"model": "opus",', encoding="utf-8")
                proc = install(harness, self.tmp / harness)
                self.assertEqual(proc.returncode, 1)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(len(proc.stderr.strip().splitlines()), 1)
                self.assertEqual(path.read_text(encoding="utf-8"), '{"model": "opus",')

    def test_claude_code_writes_no_config_toml(self) -> None:
        install("claude-code", self.tmp / "claude-code")
        self.assertFalse((self.tmp / "claude-code/config.toml").exists())


class CodexTrustRecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.toml_path = self.tmp / "config.toml"
        self.key = f"{os.path.abspath(self.tmp / 'hooks.json')}:session_end:0:0"

    def trust(self) -> dict:
        return tomllib.loads(self.toml_path.read_text(encoding="utf-8"))["hooks"]["state"]

    def test_register_writes_pinned_hash(self) -> None:
        install("codex", self.tmp)
        self.assertEqual(self.trust(), {self.key: {"trusted_hash": COMMAND_TRUSTED_HASH, "enabled": True}})

    def test_register_twice_keeps_one_table(self) -> None:
        self.toml_path.write_text(FOREIGN_TOML, encoding="utf-8")
        install("codex", self.tmp)
        proc = install("codex", self.tmp)
        self.assertIn("already registered", proc.stdout)
        self.assertEqual(self.toml_path.read_text(encoding="utf-8").count("[hooks.state."), 1)

    def test_changed_command_replaces_table(self) -> None:
        self.toml_path.write_text(FOREIGN_TOML, encoding="utf-8")
        install("codex", self.tmp)
        install("codex", self.tmp, "/y/bin/reflect hook end")
        text = self.toml_path.read_text(encoding="utf-8")
        self.assertEqual(text.count("[hooks.state."), 1)
        self.assertNotEqual(self.trust()[self.key]["trusted_hash"], COMMAND_TRUSTED_HASH)

    def test_remove_restores_foreign_toml_around_the_table(self) -> None:
        tail = '\n[mcp_servers.demo]\nurl = "https://example.com/mcp"\n'
        self.toml_path.write_text(FOREIGN_TOML, encoding="utf-8")
        install("codex", self.tmp)
        registered = self.toml_path.read_text(encoding="utf-8")
        self.toml_path.write_text(registered + tail, encoding="utf-8")

        install("codex", self.tmp, "/y/bin/reflect hook end")
        replaced = self.toml_path.read_text(encoding="utf-8")
        self.assertTrue(replaced.startswith(FOREIGN_TOML))
        self.assertTrue(replaced.endswith(tail))

        install("codex", self.tmp, "x", remove=True)
        self.assertEqual(self.toml_path.read_text(encoding="utf-8"), FOREIGN_TOML + tail)


class ReflectBinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.script = self.tmp / "bin/reflect"
        self.script.parent.mkdir()
        shutil.copy2(REFLECT_BIN, self.script)

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.script), *args],
            check=False,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin"},
        )

    def test_missing_venv_prints_empty_json(self) -> None:
        proc = self.run_script("hook", "end")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "{}\n")
        self.assertIn(".venv/bin/python", proc.stderr)

    def test_execs_primitive_with_args(self) -> None:
        python = self.tmp / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
        python.chmod(0o755)

        proc = self.run_script("hook", "end")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        argv = proc.stdout.splitlines()
        self.assertEqual(Path(argv[0]).resolve(), self.tmp.resolve() / "packs/powerset/primitives/reflect/reflect.py")
        self.assertEqual(argv[1:], ["hook", "end"])


class AdapterRegistrationTest(unittest.TestCase):
    def test_adapters_register_the_hook(self) -> None:
        for harness, adapter in ADAPTERS.items():
            with self.subTest(harness=harness):
                text = adapter.read_text(encoding="utf-8")
                self.assertIn("packs/powerset/primitives/reflect/hooks_install.py", text)
                self.assertIn(f"--harness {harness} ", text)
                self.assertIn('--command "$REPO_ROOT/bin/reflect hook end"', text)


if __name__ == "__main__":
    unittest.main()

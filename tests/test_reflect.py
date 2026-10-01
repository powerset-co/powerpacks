"""Tests for the post-session reflection primitive (packs/powerset/primitives/reflect)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from packs.powerset.primitives.reflect import reflect as reflect_cli
from packs.powerset.primitives.reflect.report import build_report, gate, scrub
from packs.powerset.primitives.reflect.transcripts import parse

REFLECT_PY = Path(reflect_cli.__file__).resolve()
T0 = datetime(2026, 9, 30, 17, 0, 0, tzinfo=timezone.utc)
LONG_REPLY = "word " * 200
POLL_S = 10
BIG_FIXTURE_BYTES = 20 * 1024 * 1024
HOOK_BUDGET_S = 2.0


def _ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def _ms(seconds: float) -> int:
    return int((T0 + timedelta(seconds=seconds)).timestamp() * 1000)


def _claude_line(kind: str, seconds: float, content) -> dict:
    message = {"role": kind, "content": content}
    if kind == "assistant":
        message["model"] = "claude-test"
    return {"type": kind, "timestamp": _ts(seconds), "cwd": "/work/powerpacks", "message": message}


def _tool_use(tool_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}


def _tool_result(tool_id: str, text: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": [{"type": "text", "text": text}]}


CLAUDE_SKILL_SESSION = [
    _claude_line("user", 0, "<system-reminder>injected</system-reminder>\nRun $deep-context for Jordan Bravo"),
    _claude_line("assistant", 5, [{"type": "text", "text": "Starting."}, _tool_use("t1", "Bash", {"command": "uv run python run_thing.py"})]),
    _claude_line("user", 10, [_tool_result("t1", "Exit code 2\nTraceback ... tail-marker-boom")]),
    _claude_line("assistant", 20, [_tool_use("t2", "Bash", {"command": "python serve.py"})]),
    _claude_line("user", 60, [_tool_result("t2", "Exit code 143\nserving")]),
    _claude_line("assistant", 60.5, [_tool_use("t3", "Bash", {"command": "pkill -f serve.py"})]),
    _claude_line("user", 61, [_tool_result("t3", "")]),
    _claude_line("user", 70, "<task-notification>done</task-notification>"),
    _claude_line("assistant", 300, [{"type": "text", "text": LONG_REPLY}]),
    _claude_line("user", 400, "why did it fail? mail casey@example.com"),
    _claude_line("user", 410, "<command-name>/search</command-name>\n<command-message>search</command-message>\n<command-args>engineers</command-args>"),
]

CLAUDE_SKILL_READ_SESSION = [
    _claude_line("user", 0, "set me up"),
    _claude_line("assistant", 5, [_tool_use("r1", "Read", {"file_path": "/repo/packs/ingestion/skills/setup/SKILL.md"})]),
    _claude_line("user", 6, [_tool_result("r1", "# setup")]),
]

CLAUDE_DEV_SESSION = [
    _claude_line("user", 0, "fix the flaky test"),
    _claude_line("assistant", 5, [_tool_use("d1", "Read", {"file_path": "/repo/packs/search/primitives/x.py"})]),
    _claude_line("user", 6, [_tool_result("d1", "code")]),
    _claude_line("assistant", 7, [_tool_use("d2", "Bash", {"command": "uv run python -m unittest"})]),
    _claude_line("user", 9, [_tool_result("d2", "OK")]),
]

CLAUDE_NO_TOOLS_SESSION = [
    _claude_line("user", 0, "what does $search do?"),
    _claude_line("assistant", 5, [{"type": "text", "text": "It searches."}]),
]


def _codex_line(kind: str, seconds: float, payload: dict) -> dict:
    return {"timestamp": _ts(seconds), "type": kind, "payload": payload}


def _codex_message(seconds: float, role: str, text: str) -> dict:
    content_type = "input_text" if role == "user" else "output_text"
    return _codex_line("response_item", seconds, {"type": "message", "role": role, "content": [{"type": content_type, "text": text}]})


CODEX_SESSION = [
    _codex_line("session_meta", 0, {"id": "s1", "cwd": "/work/powerpacks"}),
    _codex_line("turn_context", 0, {"model": "gpt-test"}),
    _codex_message(1, "user", "# AGENTS.md instructions for /work\n\nlots of rules"),
    _codex_message(2, "user", "<environment_context>x</environment_context>"),
    _codex_message(3, "user", "run $search for engineers"),
    _codex_message(4, "assistant", "Running the search."),
    _codex_line("event_msg", 10, {
        "type": "item_completed", "started_at_ms": _ms(5), "completed_at_ms": _ms(7.5),
        "item": {
            "type": "CommandExecution", "command": ["/bin/zsh", "-lc", "uv run python packs/search/primitives/find.py"],
            "exit_code": "0", "duration": {"secs": 2, "nanos": 500_000_000}, "aggregated_output": "found 3", "cwd": "/work",
        },
    }),
    _codex_line("event_msg", 12, {
        "type": "item_completed", "started_at_ms": _ms(8), "completed_at_ms": _ms(9),
        "item": {
            "type": "McpToolCall", "server": "powerset", "tool": "search_people", "arguments": {"q": "eng"},
            "status": "failed", "result": {"isError": True}, "duration": {"secs": 1, "nanos": 0},
        },
    }),
    _codex_message(3, "user", "run $search for engineers"),
]


def _write_jsonl(path: Path, lines: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class ParseTests(_TempDirCase):
    def test_claude_turns_and_tools(self) -> None:
        session = parse(_write_jsonl(self.tmp / "s.jsonl", CLAUDE_SKILL_SESSION))
        self.assertEqual(session.harness, "claude-code")
        self.assertEqual(session.cwd, "/work/powerpacks")
        self.assertEqual(session.models, ("claude-test",))
        self.assertEqual(
            [t.text for t in session.user_turns],
            ["Run $deep-context for Jordan Bravo", "why did it fail? mail casey@example.com", "/search engineers"],
        )
        self.assertEqual([c.exit_code for c in session.tools], [2, 143, 0])
        self.assertEqual(session.tools[0].ended - session.tools[0].started, 5)
        self.assertTrue(all(c.is_shell for c in session.tools))

    def test_codex_turns_commands_and_mcp(self) -> None:
        session = parse(_write_jsonl(self.tmp / "rollout.jsonl", CODEX_SESSION))
        self.assertEqual(session.harness, "codex")
        self.assertEqual(session.cwd, "/work/powerpacks")
        self.assertEqual(session.models, ("gpt-test",))
        self.assertEqual([t.text for t in session.user_turns], ["run $search for engineers"])
        command, mcp = session.tools
        self.assertEqual(command.command, "uv run python packs/search/primitives/find.py")
        self.assertEqual(command.exit_code, 0)
        self.assertAlmostEqual(command.ended - command.started, 2.5)
        self.assertEqual(command.output, "found 3")
        self.assertEqual(mcp.name, "mcp")
        self.assertTrue(mcp.command.startswith("powerset.search_people"))
        self.assertEqual(mcp.exit_code, 1)
        self.assertFalse(mcp.is_shell)


class GateTests(_TempDirCase):
    def _gate(self, lines: list[dict]):
        return gate(parse(_write_jsonl(self.tmp / "s.jsonl", lines)))

    def test_gate_in_on_named_skill(self) -> None:
        result = self._gate(CLAUDE_SKILL_SESSION)
        self.assertTrue(result.passed)
        self.assertEqual(result.signals["user_named_skill"], ["deep-context", "search"])

    def test_gate_in_on_skill_md_read(self) -> None:
        result = self._gate(CLAUDE_SKILL_READ_SESSION)
        self.assertTrue(result.passed)
        self.assertEqual(result.signals, {"skill_read": ["setup"]})

    def test_gate_in_on_primitive_command(self) -> None:
        result = gate(parse(_write_jsonl(self.tmp / "rollout.jsonl", CODEX_SESSION)))
        self.assertIn("packs/search/primitives/find.py", result.signals["ran_powerpacks_command"])

    def test_gate_out_dev_session(self) -> None:
        result = self._gate(CLAUDE_DEV_SESSION)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "no skill signal")

    def test_gate_out_without_tool_calls(self) -> None:
        result = self._gate(CLAUDE_NO_TOOLS_SESSION)
        self.assertFalse(result.passed)
        self.assertEqual(result.reason, "no tool calls")

    def test_bin_and_primitive_count_only_when_run(self) -> None:
        read_only = CLAUDE_DEV_SESSION + [
            _claude_line("assistant", 20, [_tool_use("b1", "Bash", {"command": "cat bin/reflect; sed -n '1,5p' packs/powerset/primitives/reflect/reflect.py"})]),
            _claude_line("user", 21, [_tool_result("b1", "...")]),
        ]
        self.assertFalse(self._gate(read_only).passed)
        ran = CLAUDE_DEV_SESSION + [
            _claude_line("assistant", 30, [_tool_use("b2", "Bash", {"command": "cd /repo && bin/reflect latest"})]),
            _claude_line("user", 31, [_tool_result("b2", "/x")]),
        ]
        self.assertEqual(self._gate(ran).signals, {"ran_powerpacks_command": ["bin/reflect"]})


class ReportTests(_TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.report = build_report(parse(_write_jsonl(self.tmp / "s.jsonl", CLAUDE_SKILL_SESSION)))

    def test_output_tail_for_calls_after_user_turn(self) -> None:
        tool_section = self.report.split("## Tool calls")[1].split("## Failures")[0]
        self.assertIn("output: Exit code 2", tool_section)
        self.assertIn("tail-marker-boom", tool_section)

    def test_agent_reply_kept_to_800_chars(self) -> None:
        self.assertIn(f"(agent said, 17:05:00): {'word ' * 160}\n", self.report)

    def test_kill_coincidence_flag(self) -> None:
        self.assertIn("KILLED (ended by a later kill at 17:01:00) exit=143", self.report)
        self.assertIn("failed/errored tool calls: 1", self.report)

    def test_report_is_scrubbed(self) -> None:
        self.assertNotIn("casey@example.com", self.report)
        self.assertIn("<email>", self.report)


class ScrubTests(unittest.TestCase):
    def test_scrubs_contact_data_and_keys(self) -> None:
        text = scrub(
            "mail casey@example.com or casey\\@example.com, call +15550100 or (555) 010-0100, "
            "key sk-abcdefghijklmnopqrstuvwx, Authorization: Bearer abc.def, home " + str(Path.home()) + "/x"
        )
        for leaked in ("casey", "+15550100", "010-0100", "sk-abc", "abc.def", str(Path.home())):
            self.assertNotIn(leaked, text)
        self.assertIn("~/x", text)

    def test_http_log_line_is_not_a_phone(self) -> None:
        line = '127.0.0.1 - - [30/Sep/2026 17:00:00] "GET /api/people?page=2 HTTP/1.1" 200 -'
        self.assertEqual(scrub(line), line)


class RepoRootTests(unittest.TestCase):
    def test_repo_root_holds_pyproject(self) -> None:
        self.assertTrue((reflect_cli._REPO_ROOT / "pyproject.toml").is_file())


class HookTests(_TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "reflect"
        self.argv_file = self.tmp / "narrator-argv.json"
        self.narrator = self.tmp / "narrator"
        self.narrator.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, pathlib\n"
            f"with open({str(self.argv_file)!r}, 'a') as fh: fh.write(json.dumps({{'argv': sys.argv, 'reflect': os.environ.get('POWERPACKS_REFLECT')}}) + '\\n')\n"
            "(pathlib.Path(sys.argv[1]) / 'narrative.md').write_text('stub narrative')\n"
        )
        self.narrator.chmod(0o755)
        self.transcript = _write_jsonl(self.tmp / "s.jsonl", CLAUDE_SKILL_SESSION)

    def _hook(self, stdin: str, **env_overrides: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.pop("POWERPACKS_REFLECT", None)
        env.update({"POWERPACKS_REFLECT_ROOT": str(self.root), "POWERPACKS_REFLECT_NARRATOR": str(self.narrator)})
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, str(REFLECT_PY), "hook", "end"], input=stdin, capture_output=True, text=True, env=env, timeout=30,
        )

    def _wait_for(self, path: Path) -> None:
        deadline = time.monotonic() + POLL_S
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)

    def _payload(self) -> str:
        return json.dumps({
            "session_id": "abc123", "transcript_path": str(self.transcript), "cwd": "/work/powerpacks",
            "reason": "prompt_input_exit", "hook_event_name": "SessionEnd",
        })

    def test_writes_meta_and_spawns_detached_child(self) -> None:
        proc = self._hook(self._payload())
        self.assertEqual(proc.stdout, "{}\n")
        self.assertEqual(proc.returncode, 0)
        session_dir = self.root / "claude-code-abc123"
        meta = json.loads((session_dir / "meta.json").read_text())
        self.assertEqual(meta["harness"], "claude-code")
        self.assertEqual(meta["narrative"], "pending")
        self.assertEqual(meta["reason"], "prompt_input_exit")
        self.assertEqual(meta["transcript_path"], str(self.transcript))
        self._wait_for(session_dir / "narrative.md")
        self.assertEqual((session_dir / "narrative.md").read_text(), "stub narrative")
        (recorded,) = [json.loads(line) for line in self.argv_file.read_text().splitlines()]
        self.assertEqual(recorded["argv"][1:], [str(session_dir)])
        self.assertEqual(recorded["reflect"], "off")

    def test_second_end_for_same_session_spawns_nothing(self) -> None:
        self._hook(self._payload())
        session_dir = self.root / "claude-code-abc123"
        self._wait_for(session_dir / "narrative.md")
        meta_before = (session_dir / "meta.json").read_text()
        proc = self._hook(self._payload())
        self.assertEqual(proc.stdout, "{}\n")
        self.assertEqual(proc.returncode, 0)
        time.sleep(0.5)
        self.assertEqual(len(self.argv_file.read_text().splitlines()), 1)
        self.assertEqual((session_dir / "meta.json").read_text(), meta_before)

    def test_codex_harness_from_path_shape(self) -> None:
        codex_dir = self.tmp / ".codex" / "sessions"
        codex_dir.mkdir(parents=True)
        self.transcript = _write_jsonl(codex_dir / "rollout.jsonl", CODEX_SESSION)
        self._hook(self._payload(), POWERPACKS_REFLECT_NARRATOR="/usr/bin/true")
        self.assertTrue((self.root / "codex-abc123" / "meta.json").is_file())

    def test_returns_fast_on_a_large_transcript(self) -> None:
        line = json.dumps(_claude_line("assistant", 5, [{"type": "text", "text": "x" * 1000}])) + "\n"
        self.transcript = self.tmp / "big.jsonl"
        self.transcript.write_text(line * (BIG_FIXTURE_BYTES // len(line)))
        started = time.monotonic()
        proc = self._hook(self._payload(), POWERPACKS_REFLECT_NARRATOR="/usr/bin/true")
        self.assertLess(time.monotonic() - started, HOOK_BUDGET_S)
        self.assertEqual(proc.stdout, "{}\n")

    def test_off_writes_nothing(self) -> None:
        proc = self._hook(self._payload(), POWERPACKS_REFLECT="off")
        self.assertEqual(proc.stdout, "{}\n")
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(self.root.exists())

    def test_malformed_stdin_still_replies(self) -> None:
        proc = self._hook("not json")
        self.assertEqual(proc.stdout, "{}\n")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("reflect hook", proc.stderr)


class ProcessAndNarrateTests(_TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        self.session_dir = self.tmp / "claude-code-abc123"
        self.session_dir.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.env_file = self.tmp / "claude-env.json"

    def _session(self, lines: list[dict]) -> None:
        transcript = _write_jsonl(self.tmp / "s.jsonl", lines)
        meta = {"harness": "claude-code", "transcript_path": str(transcript), "narrative": "pending"}
        (self.session_dir / "meta.json").write_text(json.dumps(meta))

    def _fake_claude(self) -> None:
        result = {"result": "## What the user wanted\nReach casey@example.com", "total_cost_usd": 0.01, "usage": {"output_tokens": 9}}
        fake = self.bin / "claude"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys, pathlib\n"
            "sys.stdin.read()\n"
            f"pathlib.Path({str(self.env_file)!r}).write_text(json.dumps({{'reflect': os.environ.get('POWERPACKS_REFLECT'), 'nested': 'CLAUDECODE' in os.environ, 'argv': sys.argv}}))\n"
            f"print(json.dumps({result!r}))\n"
        )
        fake.chmod(0o755)

    def _run(self, command: str) -> subprocess.CompletedProcess:
        env = {"PATH": str(self.bin), "HOME": os.environ.get("HOME", ""), "CLAUDECODE": "1"}
        return subprocess.run(
            [sys.executable, str(REFLECT_PY), command, str(self.session_dir)], capture_output=True, text=True, env=env, timeout=30,
        )

    def _meta(self) -> dict:
        return json.loads((self.session_dir / "meta.json").read_text())

    def test_process_gated_in_writes_report_and_narrative(self) -> None:
        self._session(CLAUDE_SKILL_SESSION)
        self._fake_claude()
        proc = self._run("process")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("# Session report (claude-code)", (self.session_dir / "report.md").read_text())
        narrative = (self.session_dir / "narrative.md").read_text()
        self.assertIn("## What the user wanted", narrative)
        self.assertNotIn("casey@example.com", narrative)
        meta = self._meta()
        self.assertTrue(meta["gate"]["passed"])
        self.assertIn("user_named_skill", meta["gate"]["signals"])
        self.assertEqual(meta["narrative"], "done")
        self.assertEqual(meta["model"], "opus")
        self.assertEqual(meta["total_cost_usd"], 0.01)
        seen = json.loads(self.env_file.read_text())
        self.assertEqual(seen["reflect"], "off")
        self.assertFalse(seen["nested"])
        self.assertIn("--no-session-persistence", seen["argv"])

    def test_process_gated_out_removes_session_dir(self) -> None:
        self._session(CLAUDE_DEV_SESSION)
        proc = self._run("process")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertFalse(self.session_dir.exists())

    def test_narrate_without_cli_is_skipped(self) -> None:
        self._session(CLAUDE_SKILL_SESSION)
        (self.session_dir / "report.md").write_text("# Session report")
        proc = self._run("narrate")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self._meta()["narrative"], "skipped: no claude on PATH")
        self.assertFalse((self.session_dir / "narrative.md").exists())


if __name__ == "__main__":
    unittest.main()

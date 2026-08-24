import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ADAPTER_ROOT = Path(__file__).resolve().parents[1]
TUI_PATH = ADAPTER_ROOT / "primitives" / "view_search_results" / "search_tui.py"

spec = importlib.util.spec_from_file_location("search_tui", TUI_PATH)
search_tui = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(search_tui)

POWERCLAW_PATH = ADAPTER_ROOT / "bin" / "powerclaw"
powerclaw_loader = importlib.machinery.SourceFileLoader("powerclaw", str(POWERCLAW_PATH))
powerclaw_spec = importlib.util.spec_from_loader("powerclaw", powerclaw_loader)
assert powerclaw_spec is not None
powerclaw = importlib.util.module_from_spec(powerclaw_spec)
powerclaw_loader.exec_module(powerclaw)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_state(path: Path, *, status: str = "completed", query: str = "software engineers in sf") -> None:
    write_json(
        path,
        {
            "task_id": "legacy-search-test",
            "task": "search_network",
            "status": status,
            "query": query,
            "created_at": "2026-04-29T00:00:00Z",
            "updated_at": "2026-04-29T00:01:00Z",
            "steps": [],
        },
    )


def write_canonical_run(
    path: Path,
    *,
    run_query: str = "software engineers in sf",
    profile: str = "gtm",
    backend: str = "local",
    corpus: dict | None = None,
    status: str = "completed",
) -> None:
    write_json(
        path,
        {
            "schema_version": "search.stage_result.v1",
            "stage": profile,
            "status": status,
            "frontier": {
                "schema_version": "candidate.frontier.v1",
                "candidates": [
                    {
                        "person_id": "p1",
                        "hydration_disposition": "hydrated",
                        "hydrated_profile": {"name": "Ada Engineer"},
                    }
                ],
                "input_count": 1,
                "output_count": 1,
                "limit": None,
                "truncated": False,
            },
        },
    )
    write_json(
        path.with_name("search_spec.json"),
        {
            "schema_version": "search.spec.v1",
            "raw_request": run_query,
            "profile": profile,
            "backend": backend,
            "corpus": corpus or {"kind": "local", "db_path": "/tmp/search.duckdb"},
        },
    )


def create_nanoclaw_session(root: Path, thread_id: str = "thread-test") -> Path:
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(data_dir / "v2.db")
    conn.execute("CREATE TABLE messaging_groups (id TEXT PRIMARY KEY, channel_type TEXT)")
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            agent_group_id TEXT,
            messaging_group_id TEXT,
            thread_id TEXT,
            created_at TEXT,
            status TEXT,
            container_status TEXT,
            last_active TEXT
        )
        """
    )
    conn.execute("INSERT INTO messaging_groups VALUES (?, ?)", ("mg1", "cli-threaded"))
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("sess1", "ag1", "mg1", thread_id, "2026-04-29T00:00:00Z", "active", "running", None),
    )
    conn.commit()
    conn.close()

    session_dir = data_dir / "v2-sessions" / "ag1" / "sess1"
    session_dir.mkdir(parents=True, exist_ok=True)
    inbound = sqlite3.connect(session_dir / "inbound.db")
    inbound.execute("CREATE TABLE messages_in (seq INTEGER, thread_id TEXT, kind TEXT, status TEXT, content TEXT)")
    inbound.commit()
    inbound.close()
    outbound = sqlite3.connect(session_dir / "outbound.db")
    outbound.execute("CREATE TABLE messages_out (seq INTEGER, thread_id TEXT, kind TEXT, content TEXT)")
    outbound.commit()
    outbound.close()
    return session_dir


def insert_outbound(session_dir: Path, seq: int, thread_id: str, text: str) -> None:
    conn = sqlite3.connect(session_dir / "outbound.db")
    conn.execute(
        "INSERT INTO messages_out VALUES (?, ?, ?, ?)",
        (seq, thread_id, "chat", json.dumps({"text": text})),
    )
    conn.commit()
    conn.close()


class SearchTuiTests(unittest.TestCase):
    def test_run_browser_title_does_not_retain_previous_run_id(self) -> None:
        class RecordingWindow:
            def __init__(self) -> None:
                self.lines: list[str] = []

            def addstr(self, _y: int, _x: int, text: str, _attr: int = 0) -> None:
                self.lines.append(text)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_path = root / "search-runs" / "canonical-role" / "result.json"
            write_canonical_run(run_path)
            tui = search_tui.SearchTui(run_path, root / "search-runs", None, root, None, None, None, False)
            window = RecordingWindow()

            tui.reload_runs()
            tui.draw_chat(window, 0, 0, 10, 80)

        self.assertTrue(window.lines[0].startswith(" NanoClaw Chat | run browser "))
        self.assertNotIn("canonical-role", window.lines[0])

    def test_defaults_to_canonical_search_runs_without_legacy_discovery(self) -> None:
        self.assertEqual(search_tui.DEFAULT_RUNS_DIR.name, "search-runs")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".powerpacks"
            canonical = root / "search-runs" / "canonical-role" / "result.json"
            legacy = root / "runs" / "legacy.json"
            write_canonical_run(canonical)
            write_state(legacy)
            runs = search_tui.discover_runs(root / "search-runs")
        self.assertEqual([Path(row["path"]).name for row in runs], ["result.json"])

    def test_powerclaw_resume_uses_nested_canonical_discovery_and_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / ".powerpacks" / "search-runs" / "canonical-role" / "result.json"
            invalid = root / ".powerpacks" / "search-runs" / "missing-spec" / "result.json"
            invalid_spec = root / ".powerpacks" / "search-runs" / "invalid-spec" / "result.json"
            unrelated = root / ".powerpacks" / "search-runs" / "unrelated.json"
            legacy = root / ".powerpacks" / "runs" / "legacy.json"
            write_canonical_run(canonical)
            write_json(invalid, {"schema_version": "search.stage_result.v1"})
            write_json(invalid_spec, {"schema_version": "search.stage_result.v1"})
            write_json(invalid_spec.with_name("search_spec.json"), {"schema_version": "search.spec.v1", "raw_request": ""})
            write_json(unrelated, {"schema_version": "search.stage_result.v1"})
            write_state(legacy)
            older = time.time() - 10
            newer = time.time()
            for path in (canonical, canonical.with_name("search_spec.json")):
                os.utime(path, (older, older))
            for path in (invalid, invalid_spec, invalid_spec.with_name("search_spec.json"), unrelated, legacy):
                os.utime(path, (newer, newer))
            captured = []
            with (
                mock.patch.dict("os.environ", {"POWERPACKS_ROOT": str(root)}),
                mock.patch.object(powerclaw.subprocess, "call", side_effect=lambda argv: captured.append(argv) or 0),
                mock.patch.object(sys, "argv", ["powerclaw", "--resume-run", "--dump"]),
            ):
                self.assertEqual(powerclaw.main(), 0)
        argv = captured[0]
        self.assertEqual(Path(argv[argv.index("--runs-dir") + 1]).name, "search-runs")
        self.assertEqual(Path(argv[argv.index("--state") + 1]), canonical)

    def test_approval_prompt_stale_after_terminal_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tui = search_tui.SearchTui(None, root / "runs", None, root, "thread-test", "echo ok", None, False)

            tui.add_message("agent", "**Search plan ready — awaiting approval**")
            self.assertTrue(tui.approval_prompt_active())

            tui.add_message("agent", "**Done — 99 SF software engineers found and hydrated**")
            self.assertFalse(tui.approval_prompt_active())

            tui.add_message("you", "/search product managers in nyc")
            tui.add_message("agent", "**Search plan ready — awaiting approval**")
            self.assertTrue(tui.approval_prompt_active())

    def test_outbound_db_poll_adds_followup_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            thread_id = "thread-test"
            session_dir = create_nanoclaw_session(root, thread_id)
            insert_outbound(session_dir, 1, thread_id, "already loaded")

            tui = search_tui.SearchTui(None, root / "runs", None, root, thread_id, "echo ok", None, False)
            insert_outbound(session_dir, 2, thread_id, "late follow-up")

            tui.poll_outbound_once()
            tui.poll_outbound_once()

            messages = [message for speaker, message in tui.messages if speaker == "agent"]
            self.assertEqual(messages.count("late follow-up"), 1)

    def test_discovers_group_run_and_refreshes_state_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_path = root / "groups" / "cli-with-arthur" / ".powerpacks" / "search-runs" / "run" / "result.json"
            write_canonical_run(run_path, status="running")

            tui = search_tui.SearchTui(None, root / "powerpacks" / ".powerpacks" / "search-runs", None, root, None, None, None, False)
            self.assertEqual(len(tui.runs), 1)
            self.assertEqual(tui.runs[0]["row_count"], 1)
            self.assertEqual(tui.runs[0]["status"], "running")

            tui.open_selected_run()
            self.assertEqual(tui.mode, "results")
            self.assertEqual(len(tui.rows), 1)

            write_canonical_run(run_path, status="completed")
            time.sleep(0.01)
            tui.refresh_active_state()
            self.assertEqual(tui.state["status"], "completed")

    def test_opens_canonical_nested_result_with_discovered_run_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_path = root / "search-runs" / "directory-name-is-not-the-query" / "result.json"
            corpus = {"kind": "powerset", "set_id": "set-123"}
            write_canonical_run(
                run_path,
                run_query="founding product designer",
                profile="recruiting",
                backend="powerset",
                corpus=corpus,
            )

            tui = search_tui.SearchTui(None, root / "search-runs", None, root, None, None, None, False)
            self.assertEqual(tui.runs[0]["task_id"], "directory-name-is-not-the-query")
            self.assertEqual(tui.runs[0]["query"], "founding product designer")
            self.assertEqual(tui.runs[0]["profile"], "recruiting")
            self.assertEqual(tui.runs[0]["backend"], "powerset")
            self.assertEqual(tui.runs[0]["corpus"], corpus)

            tui.open_selected_run()

            self.assertEqual(tui.state["task_id"], "directory-name-is-not-the-query")
            self.assertEqual(tui.state["query"], "founding product designer")
            self.assertEqual(tui.state["profile"], "recruiting")
            self.assertEqual(tui.state["backend"], "powerset")
            self.assertEqual(tui.state["corpus"], corpus)
            self.assertEqual([row["person_id"] for row in tui.rows], ["p1"])
            self.assertIn(
                ("system", "Loaded 1 candidates for: founding product designer"),
                tui.messages,
            )

    def test_missing_and_malformed_search_specs_are_visible_but_not_opened(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs_dir = Path(tmp) / "search-runs"
            missing = runs_dir / "missing-spec" / "result.json"
            malformed = runs_dir / "malformed-spec" / "result.json"
            write_json(missing, {"schema_version": "search.stage_result.v1", "status": "completed"})
            write_json(malformed, {"schema_version": "search.stage_result.v1", "status": "completed"})
            malformed.with_name("search_spec.json").write_text("{not json\n")

            tui = search_tui.SearchTui(None, runs_dir, None, Path(tmp), None, None, None, False)

            self.assertEqual({run["status"] for run in tui.runs}, {"invalid"})
            errors = " ".join(str(run["error"]) for run in tui.runs)
            self.assertIn("missing search_spec.json", errors)
            self.assertIn("malformed search_spec.json", errors)
            self.assertTrue(all(str(run["query"]).startswith("[invalid run:") for run in tui.runs))
            tui.open_selected_run()
            self.assertEqual(tui.mode, "runs")
            self.assertTrue(any("Cannot open" in text for speaker, text in tui.messages if speaker == "system"))

    def test_help_uses_search_only_and_source_has_no_legacy_detector(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tui = search_tui.SearchTui(None, root / "search-runs", None, root, None, None, None, False)
            tui.handle_command("/help")

            help_text = "\n".join(text for speaker, text in tui.messages if speaker == "system")
            retired_command = "/search" + "-network"
            self.assertIn("/search who are software engineers in sf", help_text)
            self.assertNotIn(retired_command, help_text)
            self.assertNotIn(retired_command, TUI_PATH.read_text())

    def test_followup_polling_is_based_on_submitted_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            create_nanoclaw_session(root)
            tui = search_tui.SearchTui(
                None,
                root / "search-runs",
                None,
                root,
                "thread-test",
                "echo ok",
                None,
                False,
            )
            with mock.patch.object(tui, "poll_thread_followups") as poll:
                tui.run_agent_command([sys.executable, "-c", "print('submitted')"], "plain threaded request", search_tui.threading.Event())

            poll.assert_called_once()

    def test_busy_input_queues_and_flushes_next_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = (
                f"{sys.executable} -c "
                "'import sys,time; time.sleep(0.1); print(\"reply:\" + sys.argv[-1].splitlines()[0])'"
            )
            tui = search_tui.SearchTui(None, root / "runs", None, root, "thread-test", command, None, False)

            self.assertTrue(tui.handle_command("first"))
            self.assertTrue(tui.handle_command("second"))

            deadline = time.time() + 5
            while time.time() < deadline:
                thread = tui.agent_thread
                if thread and thread.is_alive():
                    thread.join(timeout=0.05)
                    continue
                with tui.agent_state_lock:
                    if not tui.pending_messages:
                        break
                time.sleep(0.01)

            messages = [message for speaker, message in tui.messages if speaker == "agent"]
            self.assertIn("reply:first", messages)
            self.assertIn("reply:second", messages)

    def test_chat_wrapping_preserves_full_text(self) -> None:
        text = "This is a long response with retrieval payload details and enough text to wrap across several terminal rows."
        lines = search_tui.wrap_chat_line("agent", text, 36)
        rendered = ""
        for line in lines:
            if line.startswith("agent> "):
                rendered += line[len("agent> "):]
            else:
                rendered += line[len("agent> "):]
        self.assertEqual(rendered, text)
        self.assertGreater(len(lines), 1)


if __name__ == "__main__":
    unittest.main()

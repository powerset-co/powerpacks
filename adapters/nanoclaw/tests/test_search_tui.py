import importlib.util
import json
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path


ADAPTER_ROOT = Path(__file__).resolve().parents[1]
TUI_PATH = ADAPTER_ROOT / "primitives" / "view_search_results" / "search_tui.py"

spec = importlib.util.spec_from_file_location("search_tui", TUI_PATH)
search_tui = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(search_tui)


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def write_state(path: Path, *, status: str = "completed", query: str = "software engineers in sf") -> None:
    write_json(
        path,
        {
            "task_id": "search-network-test",
            "task": "search_network",
            "status": status,
            "query": query,
            "created_at": "2026-04-29T00:00:00Z",
            "updated_at": "2026-04-29T00:01:00Z",
            "steps": [
                {
                    "id": "hydrate_people",
                    "status": "completed",
                    "recorded_at": "2026-04-29T00:01:00Z",
                    "output": {
                        "profiles": [
                            {
                                "person_id": "p1",
                                "name": "Ada Engineer",
                                "headline": "Software Engineer",
                                "location": "San Francisco",
                                "linkedin_url": "https://linkedin.com/in/ada",
                                "current_positions": [{"title": "Software Engineer", "company": "ExampleCo"}],
                            }
                        ]
                    },
                }
            ],
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
    def test_approval_prompt_stale_after_terminal_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tui = search_tui.SearchTui(None, root / "runs", None, root, "thread-test", "echo ok", None, False)

            tui.add_message("agent", "**Search plan ready — awaiting approval**")
            self.assertTrue(tui.approval_prompt_active())

            tui.add_message("agent", "**Done — 99 SF software engineers found and hydrated**")
            self.assertFalse(tui.approval_prompt_active())

            tui.add_message("you", "/search-network product managers in nyc")
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
            run_path = root / "groups" / "cli-with-arthur" / ".powerpacks" / "runs" / "run.json"
            write_state(run_path, status="running")

            tui = search_tui.SearchTui(None, root / "powerpacks" / ".powerpacks" / "runs", None, root, None, None, None, False)
            self.assertEqual(len(tui.runs), 1)
            self.assertEqual(tui.runs[0]["row_count"], 1)
            self.assertEqual(tui.runs[0]["status"], "running")

            tui.open_selected_run()
            self.assertEqual(tui.mode, "results")
            self.assertEqual(len(tui.rows), 1)

            write_state(run_path, status="completed", query="software engineers in sf")
            time.sleep(0.01)
            tui.refresh_active_state()
            self.assertEqual(tui.state["status"], "completed")

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

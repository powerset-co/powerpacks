"""Offline command checks with synthetic stores; only process/provider edges are faked."""
import io
import json
import signal
import sqlite3
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from packs.ingestion.primitives.deep_context_v2 import lookup, owner, run
from packs.ingestion.primitives.deep_context_v2.db.owner import read_owner
from packs.ingestion.primitives.deep_context_v2.db.store import open_store, store_path
from packs.ingestion.primitives.deep_context_v2.import_load.load import ImportLoad


def facts_payload(name="Jordan Bravo"):
    return dict(canonical_name=name, aliases=[], employers=[dict(name="Synthetic Labs", role="Engineer", status="current")], title="Engineer", school="Synthetic College", field_of_study="Robotics", location="Synthetic City", relationship_to_owner="colleague", relationship_category="work", topics=["robotics"], notable_events=[dict(date="2026-01", summary="synthetic project")], identifiers=[], owned_identifiers=dict(emails=[], phones=[], urls=[]), shared_context=[], confidence=0.8, is_owner=False)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.conn = open_store(store_path(self.root))
        self.addCleanup(self.conn.close)

    def write_owner(self):
        path = self.root / owner.OWNER_JSON
        path.write_text(json.dumps(owner.owner_payload({}, "", ["casey@example.com"], [])))
        return path

    def candidate(self, candidate_id, name="Jordan Bravo", parent="family:jordan", with_facts=True):
        self.conn.execute("INSERT INTO candidates VALUES (?, ?, 0, '{}', '2026-01-01')", (candidate_id, name))
        if parent:
            self.conn.execute("INSERT INTO candidate_parent (candidate_id, parent_id, reason, created_at) VALUES (?, ?, 'singleton', '2026-01-01')", (candidate_id, parent))
        if with_facts:
            self.conn.execute("INSERT INTO facts VALUES (?, ?, 'key', 'synthetic', 'medium', '2026-01-01')", (candidate_id, json.dumps(facts_payload())))


class LookupTests(StoreCase):
    def setUp(self):
        super().setUp()
        self.candidate("email:jordan")
        self.candidate("phone:jordan", "J Bravo")
        self.candidate("no-facts", "Casey Synthetic", "family:casey", False)
        self.conn.executemany("INSERT INTO candidate_identifiers VALUES (?, ?, ?, ?)", [
            ("email:jordan", "email", "jordan@example.com", "Jordan@example.com"),
            ("phone:jordan", "phone", "+15550100123", "+1 (555) 010-0123"),
            ("no-facts", "email", "casey@example.com", "casey@example.com"),
        ])
        self.conn.commit()

    def profile(self, url, origin="linkedin_network"):
        if origin != "synthetic":
            self.conn.execute("UPDATE candidate_parent SET parent_id = 'li:member:synthetic' WHERE parent_id = 'family:jordan'")
        self.conn.execute("INSERT INTO candidate_linkedins (candidate_id, linkedin_url, member_id, origin, verdict, decided_by, judgment_fingerprint, confidence, reason, created_at) VALUES ('email:jordan', ?, 'member:synthetic', ?, 'confirmed', 'human', 'key', NULL, '', '2026-01-01')", (url, origin))
        self.conn.commit()

    def test_email_phone_and_name_normalization_same_family(self):
        for keys in [dict(email=" JORDAN@EXAMPLE.COM "), dict(phone="+1 (555) 010-0123"), dict(name="Bravo, Jordan"), dict(name="j bravo")]:
            with self.subTest(keys=keys):
                found = lookup.matches(self.conn, **keys)
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0].parent_id, "family:jordan")
                self.assertEqual(found[0].emails, ("Jordan@example.com",))
                self.assertEqual(found[0].phones, ("+1 (555) 010-0123",))

    def test_union_of_keys_no_duplicates_and_unmatched_words(self):
        self.assertEqual(len(lookup.matches(self.conn, email="jordan@example.com", phone="+15550100123", name="Jordan")), 1)
        self.assertEqual(lookup.matches(self.conn, name="Jordan Unrelated"), [])
        self.assertEqual(lookup.matches(self.conn), [])


    def test_identifier_without_facts_is_not_readable(self):
        self.assertEqual(lookup.matches(self.conn, email="casey@example.com"), [])

    def test_canonical_name_matches_before_parent_exists(self):
        self.candidate("orphan", "Written Alias", parent=None)
        self.assertIn("orphan", [m.parent_id for m in lookup.matches(self.conn, name="Jordan Bravo")])

    def test_real_confirmed_profile_exposed_and_synthetic_hidden(self):
        self.profile("https://www.linkedin.com/in/jordan-synthetic")
        self.assertEqual(lookup.matches(self.conn, name="Jordan")[0].linkedin_url, "https://www.linkedin.com/in/jordan-synthetic")
        self.conn.execute("DELETE FROM candidate_linkedins")
        self.profile("synthetic:family:jordan", "synthetic")
        self.assertEqual(lookup.matches(self.conn, name="Jordan")[0].linkedin_url, "")

    def test_render_all_sections_and_empty_optional_fields(self):
        self.profile("https://www.linkedin.com/in/jordan-synthetic")
        rendered = lookup.render(lookup.matches(self.conn, name="Jordan")[0])
        for text in ["# Jordan Bravo", "Relationship: colleague", "LinkedIn:", "Contact:", "Engineer @ Synthetic Labs", "School: Synthetic College", "Location: Synthetic City", "Topics: robotics", "2026-01: synthetic project"]:
            self.assertIn(text, rendered)
        minimal = lookup.Match("id", "Unknown", "", (), (), "", {})
        self.assertEqual(lookup.render(minimal), "# Unknown  [id]")
        fallback = lookup.Match("id", "Unknown", "", (), (), "", {"employers": [{}]})
        self.assertIn("? @ ?", lookup.render(fallback))

    def test_cli_json_text_and_no_match(self):
        for flags, expected, code in [(["--json", "--email", "jordan@example.com"], '"parent_id": "family:jordan"', 0), (["--name", "Jordan"], "# Jordan Bravo", 0), (["--name", "Nobody Synthetic"], "no one matches", 1)]:
            out = io.StringIO()
            with self.subTest(flags=flags), redirect_stdout(out):
                self.assertEqual(lookup.main(["--data-root", str(self.root), *flags]), code)
            self.assertIn(expected, out.getvalue())
            if "--json" in flags:
                self.assertEqual(len(json.loads(out.getvalue())), 1)

    def test_cli_requires_lookup_key(self):
        with patch("sys.stderr", new=io.StringIO()), self.assertRaises(SystemExit) as raised:
            lookup.main(["--data-root", str(self.root)])
        self.assertEqual(raised.exception.code, 2)

    def test_lookup_does_not_mutate_existing_store(self):
        before = list(self.conn.iterdump())
        lookup.matches(self.conn, name="Jordan")
        self.assertEqual(list(self.conn.iterdump()), before)


class OwnerTests(StoreCase):
    def cache(self):
        url = "https://www.linkedin.com/in/casey-synthetic"
        profile = dict(success=True, full_name=" Casey Synthetic ", member_id="member:casey", location_str=" Synthetic City ", experiences=[dict(company_name=" Synthetic Labs ", title=" Engineer ", starts_at=dict(year="2020"), ends_at=None)], education=[dict(schoolName=" Synthetic College ", degree=" MS ", fieldOfStudy=" Robotics ", starts_at=dict(year=2018), ends_at=dict(year=2020))])
        path = self.root / owner.CACHE_RELATIVE_DIR / "casey-synthetic.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(dict(fetched_at="2026-01-01", raw_response={}, normalized_profile=profile)))
        return url, profile

    def chat_store(self):
        path = self.root / "synthetic-chat.sqlite"
        with sqlite3.connect(path) as conn:
            conn.executescript("CREATE TABLE chat (account_login TEXT); CREATE TABLE message (destination_caller_id TEXT, is_from_me INTEGER);")
            conn.executemany("INSERT INTO chat VALUES (?)", [("P:+15550100123",), ("P:+15550100123",), ("E:casey@example.com",)])
            conn.executemany("INSERT INTO message VALUES (?, ?)", [("+15550100456", 0), ("+15550100123", 0), ("+15550100789", 1)])
        return path

    def test_payload_dates_aliases_and_trimmed_fields(self):
        url, profile = self.cache()
        value = owner.owner_payload(profile, url, ["casey@example.com"], [])
        self.assertEqual(value["name"], "Casey Synthetic")
        self.assertEqual(value["work"], [dict(company="Synthetic Labs", title="Engineer", start=2020, end=0)])
        self.assertEqual(value["education"][0]["note"], "MS, Robotics")
        self.assertEqual(value["locations"], ["Synthetic City"])
        alias = owner.owner_payload(dict(experiences=[dict(companyName="Alias Labs")], education=[dict(school="Alias College")]), "", [], [])
        self.assertEqual(alias["work"][0]["company"], "Alias Labs")
        self.assertEqual(alias["education"][0]["school"], "Alias College")
        self.assertEqual(owner.owner_payload({}, "", [], [])["locations"], [])

    def test_year_unknown_current_numeric_and_malformed(self):
        for value in [None, {}, {"year": 0}, "2020"]:
            self.assertEqual(owner._year(value), 0)
        self.assertEqual(owner._year({"year": "2020"}), 2020)
        with self.assertRaises(ValueError):
            owner._year({"year": "not-a-year"})


    def test_invalid_url_never_fetches_or_writes_owner(self):
        with patch("packs.ingestion.primitives.enrich.rapidapi_client.RapidApiClient.get_profile", side_effect=AssertionError("must reject before fetch")), self.assertRaisesRegex(ValueError, "not a LinkedIn"):
            owner.build_owner(self.root, "https://example.com/not-linkedin", [])
        self.assertFalse((self.root / owner.OWNER_JSON).exists())

    def test_fetch_failure_returns_cli_error_without_owner_write(self):
        # Replace only the actual profile-fetch boundary; a miss stays a miss.
        with patch("packs.ingestion.primitives.enrich.rapidapi_client.RapidApiClient.get_profile", return_value=None), patch("packs.ingestion.primitives.enrich.rapidapi_client.RapidApiClient.__init__", return_value=None), redirect_stdout(io.StringIO()) as out:
            code = owner.main(["--data-root", str(self.root), "--linkedin-url", "https://www.linkedin.com/in/missing-synthetic"])
        self.assertEqual(code, 1)
        self.assertIn("could not fetch", out.getvalue())
        self.assertFalse((self.root / owner.OWNER_JSON).exists())

    def test_cli_cached_profile_and_repeatable_email(self):
        url, _ = self.cache()
        with redirect_stdout(io.StringIO()) as out:
            code = owner.main(["--data-root", str(self.root), "--linkedin-url", url, "--email", "casey@example.com", "--email", "other@example.com", "--chat-db", str(self.root / "absent-chat")])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["emails"], 2)


class RunTests(StoreCase):
    def test_stage_missing_input_fail_fast_then_resume_real_node(self):
        node = ImportLoad(self.conn, self.root, msgvault_db=self.root / "absent-mail")
        with redirect_stdout(io.StringIO()) as out, self.assertRaises(SystemExit) as raised:
            run._stage("load", node)
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("not_ready", out.getvalue())
        self.write_owner()
        with redirect_stdout(io.StringIO()) as out:
            result = run._stage("load", node)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.counts["candidates"], 0)
        self.assertEqual(read_owner(self.conn).emails, ("casey@example.com",))

    def test_run_cli_missing_owner_never_starts_index_or_server(self):
        with patch("subprocess.Popen", side_effect=AssertionError("no process")), patch("packs.shared.web.server.start_server", side_effect=AssertionError("no server")), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as raised:
            run.main(["run", "--data-root", str(self.root), "--msgvault-db", str(self.root / "absent-mail"), "--chat-db", str(self.root / "absent-chat"), "--operator-id", "synthetic"])
        self.assertEqual(raised.exception.code, 1)
        manifests = self.root / "deep-context/v2-manifests"
        self.assertEqual([p.name for p in manifests.iterdir()], ["import_load.json"])

    def test_stage_execution_error_records_failure_and_propagates(self):
        path = self.write_owner()
        path.write_text("not json")
        with redirect_stdout(io.StringIO()), self.assertRaises(json.JSONDecodeError):
            run._stage("load", ImportLoad(self.conn, self.root))
        manifest = json.loads((self.root / "deep-context/v2-manifests/import_load.json").read_text())
        self.assertEqual(manifest["status"], "failed")

    def test_archive_preserves_owner_cache_v2_and_unrelated_files(self):
        self.candidate("synthetic-retained")
        self.conn.commit()
        v2_before = list(self.conn.iterdump())
        folder = self.root / "deep-context"
        with sqlite3.connect(folder / "deep-context.sqlite") as conn:
            conn.execute("CREATE TABLE synthetic (value TEXT)")
            conn.execute("INSERT INTO synthetic VALUES ('archived')")
        for name in ["deep-context.sqlite.bkup", "review-synthetic.log", "jordan.message-linkedin.bkup", "index.md"]:
            (folder / name).write_text("synthetic legacy")
        raw = folder / "raw"
        raw.mkdir()
        (raw / "bundle.json").write_text("{}")
        keep = [self.write_owner(), folder / "identity/jev.json", folder / "v2-manifests/collect.json", folder / "unrelated.txt", self.root / owner.CACHE_RELATIVE_DIR / "profile.json"]
        for path in keep[1:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic retained")
        retained = {path: path.read_bytes() for path in keep}
        # rm is a local deletion boundary; these are only temporary synthetic files.
        with redirect_stdout(io.StringIO()):
            archive = run.archive_v1(self.root)
        with tarfile.open(archive) as tar:
            self.assertIn("deep-context/raw/bundle.json", tar.getnames())
            self.assertIn("deep-context/deep-context.sqlite.bkup", tar.getnames())
            self.assertEqual(tar.extractfile("deep-context/index.md").read(), b"synthetic legacy")
        self.assertFalse(raw.exists())
        self.assertFalse((folder / "deep-context.sqlite").exists())
        self.assertEqual({p: p.read_bytes() for p in keep}, retained)
        self.assertTrue(store_path(self.root).exists())
        self.assertEqual(list(self.conn.iterdump()), v2_before)
        self.assertIsNone(run.archive_v1(self.root))

    def test_archive_without_v1_store_leaves_legacy_named_files_alone(self):
        path = self.root / "deep-context/index.md"
        path.write_text("synthetic")
        self.assertIsNone(run.archive_v1(self.root))
        self.assertEqual(path.read_text(), "synthetic")

    def test_index_command_passes_modal_paths_without_shell(self):
        people = self.root / "path with spaces/people.csv"
        command = run.index_command(self.root, people)
        self.assertEqual(command, [sys.executable, str(run.MODAL_PIPELINE), "index-people", "--people-csv", str(people), "--dest", str(self.root / "search-index"), "--max-usd", run.INDEX_MAX_USD])

    def test_background_index_records_pid_and_appends_log(self):
        log = self.root / run.INDEX_LOG_FILE
        log.write_bytes(b"existing\n")
        with patch("subprocess.Popen", return_value=SimpleNamespace(pid=43210)) as spawn:
            self.assertEqual(run.index_in_background(self.root, self.root / "people.csv", "synthetic"), log)
        self.assertEqual((self.root / run.INDEX_PID_FILE).read_text(), "43210")
        self.assertEqual(log.read_bytes(), b"existing\n")
        self.assertTrue(spawn.call_args.kwargs["start_new_session"])
        self.assertEqual(spawn.call_args.kwargs["cwd"], run.ROOT)
        self.assertEqual(spawn.call_args.kwargs["env"]["POWERPACKS_OPERATOR_ID"], "synthetic")
        self.assertIs(spawn.call_args.kwargs["stdout"], spawn.call_args.kwargs["stderr"])

    def test_wait_for_index_missing_finished_and_empty_pid_no_sleep(self):
        with patch("os.kill", side_effect=ProcessLookupError) as kill, patch("time.sleep", side_effect=AssertionError("no sleep")):
            run.wait_for_index(self.root)
            kill.assert_not_called()
            path = self.root / run.INDEX_PID_FILE
            path.write_text("43210")
            run.wait_for_index(self.root)
            kill.assert_called_once_with(43210, 0)
            self.assertFalse(path.exists())
            path.write_text("")
            run.wait_for_index(self.root)
            self.assertFalse(path.exists())

    def test_stop_missing_running_and_already_gone(self):
        self.assertFalse(run.stop_review(self.root))
        path = self.root / run.REVIEW_PID_FILE
        path.write_text("43210")
        with patch("os.kill") as kill:
            self.assertTrue(run.stop_review(self.root))
            kill.assert_called_once_with(43210, signal.SIGTERM)
        path.write_text("43210")
        with patch("os.kill", side_effect=ProcessLookupError):
            self.assertFalse(run.stop_review(self.root))
        self.assertFalse(path.exists())


    def test_review_cli_server_and_http_boundaries(self):
        response = io.BytesIO(b'{"progress":{"linkedin_pending":3}}')
        with patch.object(run, "start_page", return_value=dict(pid=43210, url="http://127.0.0.1:8123/")) as start, patch("urllib.request.urlopen", return_value=response) as request, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["review", "--data-root", str(self.root), "--port", "8123"]), 0)
        start.assert_called_once_with(self.root.parent, port=8123)
        request.assert_called_once_with("http://127.0.0.1:8123/api/review/page", timeout=5)
        self.assertIn("3 people to check", out.getvalue())
        self.assertEqual((self.root / run.REVIEW_PID_FILE).read_text(), "43210")

    def test_stop_cli_no_server(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["stop", "--data-root", str(self.root)]), 0)
        self.assertIn("no review server running", out.getvalue())

    def test_finish_cli_keeps_review_until_index_succeeds(self):
        self.write_owner()
        pid = self.root / run.REVIEW_PID_FILE
        pid.write_text("43210")
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=7)) as index, patch("os.kill") as kill, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["finish", "--data-root", str(self.root), "--operator-id", "synthetic"]), 7)
        people = self.root / "network-import/merged/people.csv"
        self.assertTrue(people.exists())
        self.assertIn("review server stays up", out.getvalue())
        self.assertEqual(index.call_args.args[0], run.index_command(self.root, people))
        self.assertEqual(index.call_args.kwargs["env"]["POWERPACKS_OPERATOR_ID"], "synthetic")
        kill.assert_not_called()
        self.assertEqual(pid.read_text(), "43210")
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0)), patch("os.kill") as kill, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["finish", "--data-root", str(self.root), "--operator-id", "synthetic"]), 0)
        self.assertIn("review server stopped", out.getvalue())
        kill.assert_called_once_with(43210, signal.SIGTERM)
        self.assertFalse(pid.exists())


if __name__ == "__main__":
    unittest.main()

"""The native daily refresh: its clock, its run record, and what it asks the person to do."""

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode, urlparse

from packs.ingestion.primitives.refresh import api, daily


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone()


class ScheduleTests(unittest.TestCase):
    on = daily.Settings(True, "06:00", "")

    def test_off_is_never_due(self):
        self.assertFalse(daily.due(at("2026-11-03T09:00:00"), daily.Settings(False), None))

    def test_due_once_the_time_passes_and_nothing_ran_since(self):
        self.assertFalse(daily.due(at("2026-11-03T05:59:00"), self.on, at("2026-11-02T06:00:00")))
        self.assertTrue(daily.due(at("2026-11-03T06:00:30"), self.on, at("2026-11-02T06:00:00")))
        self.assertFalse(daily.due(at("2026-11-03T09:00:00"), self.on, at("2026-11-03T06:01:00")))

    def test_missed_days_fold_into_one_run(self):
        last = at("2026-10-30T06:00:00")
        opened = at("2026-11-03T08:15:00")
        self.assertTrue(daily.due(opened, self.on, last))
        self.assertEqual(daily.missed_days(opened, last), 3)
        # The app opened just before today's time: it waits for today's run instead of catching up twice.
        self.assertFalse(daily.due(at("2026-11-03T05:00:00"), self.on, last))

    def test_a_missed_day_catches_up_when_the_next_run_is_far(self):
        # 6:00 AM was missed (the Mac was off); opened at 1 AM the next night, 5 h before the next.
        last = at("2026-11-02T06:00:00")
        self.assertTrue(daily.due(at("2026-11-03T23:00:00"), self.on, last))
        self.assertFalse(daily.due(at("2026-11-04T01:00:00"), self.on, last))

    def test_turning_it_on_does_not_count_the_past_slot_as_missed(self):
        settings = daily.Settings(True, "06:00", "2026-11-03T10:00:00")
        self.assertFalse(daily.due(at("2026-11-03T11:00:00"), settings, None))
        self.assertTrue(daily.due(at("2026-11-04T06:00:00"), settings, None))
        self.assertEqual(daily.next_run_at(at("2026-11-03T11:00:00"), settings, None), at("2026-11-04T06:00:00"))

    def test_weekly_runs_on_its_day_and_catches_up_a_closed_one(self):
        # 2026-11-09 is a Monday.
        monday = daily.Settings(True, "06:00", "2026-11-01T00:00:00", "weekly", 0)
        self.assertFalse(daily.due(at("2026-11-09T05:00:00"), monday, at("2026-11-02T06:00:00")))
        self.assertTrue(daily.due(at("2026-11-09T06:00:00"), monday, at("2026-11-02T06:00:00")))
        # The app was closed all Monday: Wednesday's first tick runs it.
        self.assertTrue(daily.due(at("2026-11-11T09:00:00"), monday, at("2026-11-02T06:00:00")))
        self.assertFalse(daily.due(at("2026-11-11T09:00:00"), monday, at("2026-11-09T06:01:00")))
        self.assertEqual(daily.next_run_at(at("2026-11-09T07:00:00"), monday, at("2026-11-09T06:01:00")),
                         at("2026-11-16T06:00:00"))
        self.assertEqual(monday.summary(), "weekly on Monday at 6:00 AM")

    def test_monthly_runs_on_its_day_and_a_short_month_uses_its_last_day(self):
        first = daily.Settings(True, "06:00", "2026-10-01T00:00:00", "monthly", day=1)
        self.assertTrue(daily.due(at("2026-11-04T09:00:00"), first, at("2026-10-01T06:00:00")))
        self.assertFalse(daily.due(at("2026-11-04T09:00:00"), first, at("2026-11-01T06:00:00")))
        self.assertEqual(daily.next_run_at(at("2026-11-04T09:00:00"), first, at("2026-11-01T06:00:00")),
                         at("2026-12-01T06:00:00"))
        last = daily.Settings(True, "06:00", "2026-10-01T00:00:00", "monthly", day=31)
        self.assertEqual(daily.next_run_at(at("2027-02-10T09:00:00"), last, at("2027-01-31T06:00:00")),
                         at("2027-02-28T06:00:00"))
        self.assertEqual(daily.Settings(True, "18:30", frequency="monthly", day=22).summary(),
                         "monthly on the 22nd at 6:30 PM")

    def test_time_is_validated(self):
        with self.assertRaises(ValueError):
            daily.Settings(True, "25:00")
        self.assertEqual(daily.Settings(True, "18:30").label(), "6:30 PM")


class RecordTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_settings_round_trip(self):
        self.assertFalse(daily.read_settings(self.root).enabled)
        saved = daily.write_settings(self.root, True, "07:00")
        self.assertTrue(saved.enabled_at)
        self.assertEqual(daily.read_settings(self.root), saved)
        # Changing the time while on keeps when it was turned on.
        self.assertEqual(daily.write_settings(self.root, True, "08:00").enabled_at, saved.enabled_at)

    def test_moving_to_another_day_does_not_count_its_past_slot_as_missed(self):
        daily.write_settings(self.root, True, "06:00")
        with patch.object(daily, "now", return_value=at("2026-11-04T09:00:00")):
            moved = daily.write_settings(self.root, True, "06:00", "monthly", day=1)
        self.assertEqual((moved.frequency, moved.day), ("monthly", 1))
        self.assertEqual(daily.read_settings(self.root), moved)
        self.assertFalse(daily.due(at("2026-11-04T09:01:00"), moved, None))
        self.assertTrue(daily.due(at("2026-12-01T06:00:00"), moved, None))
        # A field left out keeps its saved value.
        self.assertEqual(daily.write_settings(self.root, True, "07:00").frequency, "monthly")

    def test_a_run_whose_process_is_gone_is_closed(self):
        run_id = daily._insert(self.root, "daily", 0)
        daily._update(self.root, run_id, pid=2 ** 22 + 12345)
        self.assertIsNone(daily.running(self.root))
        run = daily.get_run(self.root, run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.problems[0].action, "Refresh now")

    def test_a_live_run_is_reused(self):
        run_id = daily._insert(self.root, "manual", 0)
        daily._update(self.root, run_id, pid=os.getpid())
        self.assertEqual(daily.start(self.root, "manual").id, run_id)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / ".powerpacks/search-index").mkdir(parents=True)
        (self.root / daily.PEOPLE_CSV).write_text("name\nA\nB\n")
        (self.root / daily.INDEX_FINGERPRINT).write_text("old")

    def run_with(self, manifest, signins, signed):
        def onboard(*_args, **_kwargs):
            (self.root / daily.PEOPLE_CSV).write_text("name\nA\nB\nC\n")
            (self.root / daily.INDEX_FINGERPRINT).write_text("new")
            return SimpleNamespace(stdout="DONE", stderr="", returncode=0)
        status = MagicMock()
        status.return_value.read.return_value = manifest
        run_id = daily._insert(self.root, "daily", 1)
        with patch.object(daily, "connected_sources", return_value=["gmail", "whatsapp"]), \
                patch.object(daily, "powerset_signed_in", return_value=True), \
                patch.object(daily, "renew_gmail") as renew, \
                patch.object(daily.subprocess, "run", side_effect=onboard) as ran, \
                patch("packs.ingestion.primitives.accounts.accounts.gmail_signins", return_value=signins), \
                patch("packs.ingestion.primitives.accounts.accounts.signed_in", return_value=signed), \
                patch("packs.powerset.primitives.install.status.InstallStatus", status):
            run = daily.run_once(self.root, run_id)
        renew.assert_called_once()
        command = ran.call_args.args[0]
        self.assertIn("--refresh", command)
        self.assertEqual(ran.call_args.kwargs["env"]["POWERPACKS_DESKTOP"], "1")
        return run

    def test_records_people_index_and_sources(self):
        manifest = {"status": "completed", "message": "Search is ready.",
                    "steps": {"gmail_import": {"status": "completed"}, "whatsapp_import": {"status": "completed"}}}
        signed = SimpleNamespace(linkedin=False, imessage=False, whatsapp=True, gmail=True, mailboxes=("a@x.com",),
                                 healthy_mailboxes=("a@x.com",))
        run = self.run_with(manifest, {"a@x.com": "healthy"}, signed)
        self.assertEqual(run.status, "ok")
        self.assertEqual((run.people_before, run.people_after), (2, 3))
        self.assertTrue(run.index_rebuilt)
        self.assertEqual([(s.source, s.status, s.accounts) for s in run.sources],
                         [("gmail", "synced", ["a@x.com"]), ("whatsapp", "synced", [])])
        self.assertEqual(run.problems, [])

    def test_expired_gmail_and_unlinked_whatsapp_need_the_person(self):
        manifest = {"status": "completed", "message": "Search is ready.",
                    "steps": {"gmail_import": {"status": "skipped"}, "whatsapp_import": {"status": "skipped"}}}
        signed = SimpleNamespace(linkedin=False, imessage=False, whatsapp=False, gmail=False, mailboxes=(), healthy_mailboxes=())
        run = self.run_with(manifest, {"a@x.com": "reauthorization_required"}, signed)
        self.assertEqual(run.status, "needs_you")
        self.assertEqual([p.source for p in run.problems], ["gmail", "whatsapp"])
        self.assertIn("a@x.com", run.problems[0].title)
        self.assertEqual(run.problems[0].href, "/tasks")

    def test_an_expired_mailbox_is_reported_while_the_healthy_one_syncs(self):
        manifest = {"status": "completed", "message": "Search is ready.",
                    "steps": {"gmail_import": {"status": "completed"}, "whatsapp_import": {"status": "completed"}}}
        signed = SimpleNamespace(linkedin=False, imessage=False, whatsapp=True, gmail=False,
                                 mailboxes=("old@x.com", "work@x.com"), healthy_mailboxes=("work@x.com",))
        run = self.run_with(manifest, {"old@x.com": "reauthorization_required", "work@x.com": "healthy"}, signed)
        self.assertEqual(run.status, "needs_you")
        self.assertEqual([(s.source, s.status, s.accounts) for s in run.sources],
                         [("gmail", "synced", ["work@x.com"]), ("whatsapp", "synced", [])])
        self.assertEqual([p.source for p in run.problems], ["gmail"])
        self.assertIn("old@x.com", run.problems[0].title)
        self.assertNotIn("work@x.com", run.problems[0].title)

    def test_a_stopped_run_fails_with_its_message(self):
        manifest = {"status": "failed", "message": "Deep Context stopped: no operator id.", "steps": {}}
        signed = SimpleNamespace(linkedin=False, imessage=False, whatsapp=True, gmail=True, mailboxes=("a@x.com",),
                                 healthy_mailboxes=("a@x.com",))
        run = self.run_with(manifest, {"a@x.com": "healthy"}, signed)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.problems[-1].detail, "Deep Context stopped: no operator id.")
        self.assertEqual(run.problems[-1].href, "/accounts?view=sync")

    def test_signed_out_of_powerset_stops_before_setup_waits_for_a_sign_in(self):
        run_id = daily._insert(self.root, "daily", 0)
        with patch.object(daily, "connected_sources", return_value=["linkedin"]), \
                patch.object(daily, "powerset_signed_in", return_value=False), \
                patch.object(daily.subprocess, "run") as ran:
            run = daily.run_once(self.root, run_id)
        ran.assert_not_called()
        self.assertEqual(run.status, "needs_you")
        self.assertEqual(run.problems[0].title, "Signed out of Powerset")

    def test_nothing_connected(self):
        run_id = daily._insert(self.root, "manual", 0)
        with patch.object(daily, "connected_sources", return_value=[]):
            run = daily.run_once(self.root, run_id)
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.problems[0].href, "/accounts")


class RenewTests(unittest.TestCase):
    def test_every_mailbox_that_needs_a_sign_in_is_signed_back_in_once(self):
        from packs.ingestion.primitives.setup.automations import accounts
        check = {"accounts_to_authorize": ["casey@example.com", "jordan@example.com"],
                 "expired_accounts": ["casey@example.com"]}
        with patch("packs.ingestion.primitives.setup.automations.accounts.stored_mailboxes",
                   return_value=("casey@example.com", "jordan@example.com")), \
                patch.object(accounts, "check_accounts_payload", return_value=check), \
                patch.object(accounts, "add_account") as signed:
            daily.renew_gmail()
        self.assertEqual([(call.args[1], call.kwargs["force"]) for call in signed.call_args_list],
                         [("casey@example.com", True), ("jordan@example.com", False)])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.route = api.TasksApi(self.root)

    def call(self, method, path, fields=None):
        body = urlencode(fields or {}).encode()
        handler = MagicMock()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        self.assertTrue(getattr(self.route, method)(handler, urlparse(path)))
        return handler.send_response.call_args.args[0], json.loads(handler.wfile.getvalue())

    def test_settings_turn_it_on_and_show_the_next_run(self):
        code, payload = self.call("post", api.SETTINGS_PATH, {"enabled": "true", "time": "07:30"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["settings"], {"enabled": True, "time": "07:30", "label": "7:30 AM",
                                               "frequency": "daily", "weekday": 0, "day": 1,
                                               "summary": "daily at 7:30 AM"})
        code, payload = self.call("post", api.SETTINGS_PATH, {"frequency": "weekly", "weekday": "4"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["settings"]["summary"], "weekly on Friday at 7:30 AM")
        self.assertIsNotNone(payload["next_run_at"])
        self.assertEqual(self.call("get", api.REFRESH_PATH)[1]["runs"], [])

    def test_bad_time_is_refused(self):
        code, payload = self.call("post", api.SETTINGS_PATH, {"enabled": "true", "time": "7pm"})
        self.assertEqual(code, 400)
        self.assertIn("HH:MM", payload["error"])

    def test_run_now_starts_a_manual_run(self):
        with patch.object(daily, "start") as started:
            code, _ = self.call("post", api.RUN_PATH)
        self.assertEqual(code, 200)
        started.assert_called_once_with(self.root, "manual")

    def test_run_now_while_an_import_runs_says_so(self):
        with patch.object(daily, "setup_busy", return_value=True):
            code, payload = self.call("post", api.RUN_PATH)
        self.assertEqual(code, 400)
        self.assertIn("already running", payload["error"])


if __name__ == "__main__":
    unittest.main()

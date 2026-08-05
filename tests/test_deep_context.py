"""Unit + light-integration tests for the deep-context dossier pipeline.

Covers identity normalization, the privacy gate, adaptive sampling, fact merge,
attributedBody decoding, Jaro-Winkler blocking/merge detection, and an end-to-end
compose -> cluster -> lookup flow over synthetic fixtures (no network, no DB).
"""
from __future__ import annotations

import contextlib
import csv
import http.client
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from packs.ingestion.schemas.people_schema import (
    generate_person_id,
    legacy_message_linkedin_id,
)
from packs.ingestion.primitives.common.legacy import (
    migrate_parent_slug_artifacts,
    parent_slug_migrations,
)
from packs.ingestion.primitives.common.jsonio import write_json
from packs.ingestion.primitives.common import legacy
from packs.ingestion.primitives.deep_context import (
    build_parents as parents,
    check_readiness,
    cluster_merge_candidates as cluster,
    collect_person_context as collect,
    common,
    compose_dossier as compose,
    heal_review as heal,
    lookup_person as lookup,
    apply_retargets as retargets,
    reconcile_deep_research as dresearch,
    reconcile_linkedin as reconcile,
    restart_review,
    sources,
    synthesize_person_context as synth,
    worth_view,
)
from packs.ingestion.primitives.enrich import rapidapi_client
from packs.ingestion.primitives.deep_context.review_web import (
    REVIEW_CSS,
    decisions as web_decisions,
    model as web_model,
    rendering as web_rendering,
    retarget_queue as web_retargets,
    server as web_server,
    workflow as web_workflow,
)
from packs.ingestion.primitives.deep_context.review_store import (
    load_override_rows as load_rows,
    parent_worth_key,
    write_override_rows as write_rows,
)


class TestCommon(unittest.TestCase):
    def test_normalize_phone(self):
        self.assertEqual(common.normalize_phone("(415) 555-1234"), "+14155551234")
        self.assertEqual(common.normalize_phone("+1 415 555 1234"), "+14155551234")
        self.assertEqual(common.normalize_phone("123"), "")

    def test_phone_digits_drops_us_country_code(self):
        self.assertEqual(common.phone_digits("+14155551234"), "4155551234")
        self.assertEqual(common.phone_digits("4155551234"), "4155551234")

    def test_normalize_email_name(self):
        self.assertEqual(common.normalize_email("  Jane@ACME.com "), "jane@acme.com")
        self.assertEqual(common.normalize_name("  Jane   Doe "), "jane doe")

    def test_slugify_stable_and_collision_proof(self):
        self.assertEqual(common.slugify("Jane Doe", "abcd1234-xyz"), "jane-doe-abcd1234")
        self.assertNotEqual(common.slugify("Jane Doe", "id-one"), common.slugify("Jane Doe", "id-two"))
        self.assertEqual(
            common.slugify("Jane Doe", "parent-1234567890ab"),
            "jane-doe-12345678",
        )

    def test_parse_list_handles_json_and_bare(self):
        self.assertEqual(common.parse_list('["a@x.com", "b@x.com"]'), ["a@x.com", "b@x.com"])
        self.assertEqual(common.parse_list("solo@x.com"), ["solo@x.com"])
        self.assertEqual(common.parse_list(""), [])

    def test_load_people_filters_and_parses(self):
        with tempfile.TemporaryDirectory() as d:
            csv_path = Path(d) / "people.csv"
            csv_path.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
                'p1,Jane Doe,jane@acme.com,"[""jane@acme.com""]",+14155551234,"[""+14155551234""]",gmail_msgvault,imessage\n'
                "p2,No Channels,,,,,linkedin_csv\n",
                encoding="utf-8",
            )
            people = list(common.load_people(csv_path))
            self.assertEqual(len(people), 1)
            self.assertEqual(people[0].person_id, "p1")
            self.assertIn("jane@acme.com", people[0].emails)
            self.assertIn("+14155551234", people[0].phones)


class TestContactIdentifierPolicy(unittest.TestCase):
    """'Contact info to reach this person': emails/phones only, owner dropped,
    and an email must be provably theirs (ground truth, or a name token in its
    local part / domain). Everything else — URLs, maps/campaign/meeting links,
    handles, dates — never survives. Phones additionally pass the footer-junk
    scrub: toll-free drops unless it is the only phone, and at most two phones
    survive (known first; a non-known second slot needs a different country)."""

    def test_policy_first_rule_wins(self):
        kept = common.contact_identifiers(
            ["jordan.bravo@acme.com",              # name token in local part
             "casey@jordanbravo.com",              # name token in domain
             "owner@example.com",                  # mailbox owner's own
             "randomperson@acme.com",              # someone else on the thread
             "known@example.net",                  # ground-truth channel email
             "+1 (555) 010-0000",                  # phone
             "555/010-0000",                       # slash-separated phone, normalized
             "11/1/2023",                          # date is not a phone
             "2023/07/30",                         # neither is y/m/d
             "https://maps.app.goo.gl/Zus2dp",     # maps link
             "https://sprh.mn/?vip=jordan@a.com",  # URL with embedded address
             "https://www.amazon.com/dp/1328663795",
             "meet.google.com/abc-defg-hij",
             "@jordanbravo"],                      # bare handle
            name="Jordan Bravo",
            known=["known@example.net"],
            owner_emails=["owner@example.com"],
            owner_phones=["+15550199"])
        # The slash-format duplicate of the same number dedupes by digits.
        self.assertEqual(kept, ["jordan.bravo@acme.com", "casey@jordanbravo.com",
                                "known@example.net", "+1 (555) 010-0000"])

    def test_slash_phone_normalizes_but_dates_do_not(self):
        self.assertEqual(common.contact_identifiers(["650/856-7893", "11/1/2023",
                                                     "2023/07/30", "650/856"]),
                         ["+16508567893"])

    def test_without_context_extracted_emails_drop(self):
        kept = common.contact_identifiers(["someone@example.com", "+15550100000"])
        self.assertEqual(kept, ["+15550100000"])

    def test_owner_phone_drops(self):
        self.assertEqual(
            common.contact_identifiers(["+1 555 019 9000"],
                                       owner_phones=["5550199000"]), [])

    def test_zoom_invite_bridge_numbers_capped(self):
        # A meeting-invite footer: the contact's mobile plus a pile of dial-in
        # bridge numbers. All bridges share the mobile's country, so none may
        # take the second slot — only the real mobile survives the cap.
        kept = common.contact_identifiers(
            ["+1 555 010 0001",   # their mobile (listed first)
             "+1 555 021 6800",   # bridge numbers below
             "+1 555 646 8000",
             "+1 555 669 9000",
             "+1 555 253 2000",
             "+1 555 301 7000"],
            name="Jordan Bravo")
        self.assertEqual(kept, ["+1 555 010 0001"])

    def test_toll_free_drops_unless_only_phone(self):
        # A company 800-line next to a real number is footer junk...
        self.assertEqual(
            common.contact_identifiers(["(800) 555-0199", "+1 555 010 0123"]),
            ["+1 555 010 0123"])
        # ...but as the person's ONLY phone it stays.
        self.assertEqual(common.contact_identifiers(["(800) 555-0199"]),
                         ["(800) 555-0199"])

    def test_signature_known_first_then_different_country(self):
        # A business-signature block: office + mobile (same country) + a line
        # in another country. The ground-truth `known` number is kept first;
        # the second slot goes to the different-country line, never the
        # same-country office number. Emails are untouched by the cap.
        kept = common.contact_identifiers(
            ["jordan.bravo@example.com",
             "+1 555 010 0111",     # office — same country as the known mobile
             "+1 555 010 0222",     # mobile — in `known`
             "+886 9 5550 0100"],   # their line in another country
            name="Jordan Bravo",
            known=["+15550100222"])
        self.assertEqual(kept, ["jordan.bravo@example.com",
                                "+1 555 010 0222", "+886 9 5550 0100"])

    def test_scrub_rewrites_identifier_sections_display_side(self):
        md = ("# Jordan Bravo\n\n## Identifiers\n\n- jordan@acme.com\n"
              "- https://maps.app.goo.gl/Zus2dp\n\n## Timeline\n\n- **2026** — met up")
        out = web_rendering.scrub_identifier_sections(md, name="Jordan Bravo")
        self.assertIn("jordan@acme.com", out)
        self.assertNotIn("maps.app.goo.gl", out)
        self.assertIn("## Timeline", out)
        only_junk = ("## Identifiers\n\n- https://maps.app.goo.gl/Zus2dp\n\n"
                     "## Timeline\n\n- **2026** — met up")
        out2 = web_rendering.scrub_identifier_sections(only_junk, name="Jordan Bravo")
        self.assertNotIn("## Identifiers", out2)   # emptied section disappears
        self.assertIn("## Timeline", out2)


class TestDeepContextRunnerSafety(unittest.TestCase):
    def test_chained_paid_run_is_disabled(self):
        runner = Path(__file__).resolve().parents[1] / "bin" / "deep-context"
        blocked = subprocess.run(
            [str(runner), "run"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("intentionally disabled", blocked.stderr)

        help_result = subprocess.run(
            [str(runner), "run", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("paid stages require", help_result.stderr)

    def test_stop_is_an_idempotent_noop_without_a_review_server(self):
        # `stop` is the post-review cleanup step (skill step 8): it must exit 0
        # and say so when no server holds the session, so agents can run it
        # unconditionally before apply-retargets/realize.
        runner = Path(__file__).resolve().parents[1] / "bin" / "deep-context"
        result = subprocess.run(
            [str(runner), "stop", "--port", "45997"],
            capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"status": "stopped"', result.stdout)
        self.assertIn("no review server was running", result.stdout)

    def test_profile_prefetch_is_an_explicit_runner_task(self):
        runner = Path(__file__).resolve().parents[1] / "bin" / "deep-context"
        result = subprocess.run(
            [str(runner), "profile-prefetch", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--fetch", result.stdout)
        self.assertIn("RapidAPI", result.stdout)

    def test_restart_is_wired_and_defaults_to_dry_run(self):
        runner = Path(__file__).resolve().parents[1] / "bin" / "deep-context"
        result = subprocess.run(
            [str(runner), "restart", "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--apply", result.stdout)
        self.assertIn("machine", result.stdout)

    def test_review_status_does_not_need_uv_cache_access(self):
        # The agent's wait target must run from the provisioned repo
        # interpreter without touching uv's user-level cache.
        root = Path(__file__).resolve().parents[1]
        env = os.environ.copy()
        env["UV_CACHE_DIR"] = "/dev/null/powerpacks-uv-cache"
        result = subprocess.run(
            [str(root / "bin" / "deep-context"), "review-status"],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["primitive"],
                         "deep_context_review_status")

    def test_review_status_wait_returns_waiting_on_timeout(self):
        # --wait on a human-pending state must return (never hang) with
        # status=waiting so the caller simply runs it again.
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            result = subprocess.run(
                [str(root / "bin" / "deep-context"), "review-status", "--wait",
                 "--timeout", "1",
                 "--review", str(base / "review.csv"),
                 "--verdicts", str(base / "verdicts.jsonl"),
                 "--facts-dir", str(base / "facts"),
                 "--people-csv", str(base / "people.csv"),
                 "--synthetic-people", str(base / "synthetic.csv"),
                 "--manifest", str(base / "review" / "manifest.json"),
                 "--enrichment-manifest", str(base / "research" / "manifest.json")],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "waiting")
        self.assertEqual(payload["next_action"], "review_people")
        self.assertGreaterEqual(payload["waited_seconds"], 1)


class TestRestartReview(unittest.TestCase):
    def test_restart_clears_human_identity_decisions_but_keeps_auto(self):
        rows = {
            "human-keep": {"public_identifier": "human-keep",
                           "action": "verify", "approved": "yes",
                           "new_linkedin_url": ""},
            "human-fix": {"public_identifier": "human-fix",
                          "action": "retarget", "approved": "yes",
                          "new_linkedin_url": "https://linkedin.com/in/ada-ex"},
            "human-pasted-unapproved": {"public_identifier": "human-pasted-unapproved",
                                        "action": "", "approved": "",
                                        "new_linkedin_url": "https://linkedin.com/in/bo-ex"},
            "machine-auto": {"public_identifier": "machine-auto",
                             "action": "detach", "approved": "auto",
                             "new_linkedin_url": ""},
            # A deep-research proposal the identity judge stamped: machine
            # work — restart must NOT clear it, or every restart forces a
            # full re-judge (the fingerprint cache keys off the intact row).
            "machine-proposal": {"public_identifier": "machine-proposal",
                                 "action": "retarget", "approved": "",
                                 "new_linkedin_url": "https://linkedin.com/in/cy-ex",
                                 "llm_judge_fingerprint": "sha-cy"},
            # The same proposal after a human approved it: only the human's
            # mark clears; the judged machine proposal survives.
            "human-approved-proposal": {"public_identifier": "human-approved-proposal",
                                        "action": "retarget", "approved": "yes",
                                        "new_linkedin_url": "https://linkedin.com/in/di-ex",
                                        "llm_judge_fingerprint": "sha-di"},
            "untouched": {"public_identifier": "untouched",
                          "action": "", "approved": "", "new_linkedin_url": ""},
        }
        cleared = restart_review.clear_human_identity_decisions(rows)
        self.assertEqual(cleared, 4)
        for key in ("human-keep", "human-fix", "human-pasted-unapproved"):
            self.assertEqual(
                (rows[key]["action"], rows[key]["approved"], rows[key]["new_linkedin_url"]),
                ("", "", ""), key)
        # LLM auto-verify/auto-detach work survives, exactly as a new user sees it
        self.assertEqual(rows["machine-auto"]["action"], "detach")
        self.assertEqual(rows["machine-auto"]["approved"], "auto")
        # The judged machine proposal is untouched...
        self.assertEqual(rows["machine-proposal"]["action"], "retarget")
        self.assertEqual(rows["machine-proposal"]["new_linkedin_url"],
                         "https://linkedin.com/in/cy-ex")
        # ...and a human-approved one loses only the human's mark.
        self.assertEqual(
            (rows["human-approved-proposal"]["action"],
             rows["human-approved-proposal"]["approved"],
             rows["human-approved-proposal"]["new_linkedin_url"]),
            ("retarget", "", "https://linkedin.com/in/di-ex"))

    def test_restart_clears_human_worth_and_keeps_machine_verdicts(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review = base / "review.csv"
            rows = {
                "candidate:email:ana@example.com": {
                    "public_identifier": "candidate:email:ana@example.com",
                    "network_worth": "yes",         # human — must clear
                    "llm_worth": "no",              # machine — must survive
                    "llm_worth_reason": "thin thread",
                },
                "candidate:email:bo@example.com": {
                    "public_identifier": "candidate:email:bo@example.com",
                    "network_worth": "",            # no human mark
                    "llm_worth": "yes",
                    "llm_worth_reason": "dense two-way thread",
                },
            }
            write_rows(review, rows)
            synthetic = base / "synthetic-people.csv"
            with synthetic.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["person_id", "approved"])
                writer.writeheader()
                writer.writerow({"person_id": "candidate:email:ana@example.com",
                                 "approved": "yes"})

            loaded = load_rows(review)
            self.assertEqual(restart_review.clear_human_worth(loaded), 1)
            write_rows(review, loaded)
            synth_result = restart_review.clear_synthetic_approvals(
                synthetic, apply=True)

            after = load_rows(review)
            ana = after["candidate:email:ana@example.com"]
            self.assertEqual(ana["network_worth"], "")          # human cleared
            self.assertEqual(ana["llm_worth"], "no")            # machine kept
            self.assertEqual(ana["llm_worth_reason"], "thin thread")
            self.assertEqual(synth_result["cleared"], 1)
            with synthetic.open(newline="", encoding="utf-8") as fh:
                synth_rows = list(csv.DictReader(fh))
            self.assertEqual(synth_rows[0]["approved"], "")
            # nothing deleted: the synthetic file was backed up first
            self.assertTrue(list(base.glob("synthetic-people.csv.bkup-*")))


import sqlite3  # noqa: E402  (local to the msgvault-con helper below)

_MSGVAULT_SCHEMA = """
CREATE TABLE sources (id INTEGER PRIMARY KEY, source_type TEXT, identifier TEXT, display_name TEXT);
CREATE TABLE participants (id INTEGER PRIMARY KEY, email_address TEXT, display_name TEXT, domain TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, source_id INTEGER, conversation_id INTEGER, message_type TEXT,
    sent_at TEXT, received_at TEXT, internal_date TEXT, deleted_at TEXT, deleted_from_source_at TEXT,
    sender_id INTEGER, subject TEXT, snippet TEXT);
CREATE TABLE message_recipients (id INTEGER PRIMARY KEY, message_id INTEGER, participant_id INTEGER,
    recipient_type TEXT, display_name TEXT);
CREATE TABLE message_bodies (id INTEGER PRIMARY KEY, message_id INTEGER, body_text TEXT, body_html TEXT);
"""


class TestAdaptiveGmailCollection(unittest.TestCase):
    """Gmail is its own 1600-vertical now: keep a thread's back-and-forth, honest counts,
    and don't crowd out chat."""

    def _con(self) -> sqlite3.Connection:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(_MSGVAULT_SCHEMA)
        con.executescript("""
            INSERT INTO sources (id, source_type, identifier, display_name) VALUES (1, 'gmail', 'me@gmail.com', 'Me');
            INSERT INTO participants (id, email_address, display_name) VALUES
                (1, 'jordan@acme.dev', 'Jordan Acme'), (2, 'me@gmail.com', 'Me');
            -- One thread (100), a real 4-message back-and-forth (2 Jordan, 2 me).
            INSERT INTO messages (id, source_id, conversation_id, message_type, sent_at, sender_id, subject, snippet) VALUES
                (10, 1, 100, 'email', '2026-01-01T00:00:00Z', 1, 'coffee', 'lets grab coffee next week sometime'),
                (11, 1, 100, 'email', '2026-01-02T00:00:00Z', 2, 'Re: coffee', 'sure how about tuesday afternoon'),
                (12, 1, 100, 'email', '2026-01-03T00:00:00Z', 1, 'Re: coffee', 'tuesday works great see you then'),
                (13, 1, 100, 'email', '2026-01-04T00:00:00Z', 2, 'Re: coffee', 'perfect talk soon and take care');
            INSERT INTO message_recipients (message_id, participant_id, recipient_type) VALUES
                (10, 2, 'to'), (11, 1, 'to'), (12, 2, 'to'), (13, 1, 'to');
        """)
        con.commit()
        return con

    def _person(self, phones=None):
        return common.Person(person_id="p1", full_name="Jordan Acme",
                             emails=["jordan@acme.dev"], phones=phones or [], source_channels=[])

    def test_read_gmail_keeps_thread_back_and_forth(self):
        con = self._con()
        self.addCleanup(con.close)
        store = sources.gni.MsgvaultStore(connection=con)
        accounts = store.account_emails()
        msgs = sources.read_gmail(self._person(), store, accounts)
        self.assertGreater(len(msgs), 1)            # was 1 (thread collapsed); now the back-and-forth
        self.assertEqual(len(msgs), 4)

    def test_collect_one_honest_available_and_capped(self):
        con = self._con()
        self.addCleanup(con.close)
        store = sources.gni.MsgvaultStore(connection=con)
        accounts = store.account_emails()
        nope = Path("/nonexistent-deepctx")
        # deep_cap below the true total => pool trimmed, but `available` reports the true 4.
        pool, available = collect.collect_one(
            self._person(), store=store, accounts=accounts,
            chat_db=nope, wacli_db=nope, deep_cap=2)
        self.assertEqual(available, 4)
        self.assertEqual(len(pool), 2)
        self.assertGreater(available, len(pool))    # capped == True downstream
        # deep_cap above the total => honest, not capped (the Bretton case).
        pool2, available2 = collect.collect_one(
            self._person(), store=store, accounts=accounts,
            chat_db=nope, wacli_db=nope, deep_cap=50)
        self.assertEqual(available2, 4)
        self.assertEqual(len(pool2), 4)

    def test_gmail_does_not_starve_chat(self):
        con = self._con()
        self.addCleanup(con.close)
        store = sources.gni.MsgvaultStore(connection=con)
        accounts = store.account_emails()
        fake_dms = [{"channel": "imessage", "at": "2026-03-01T00:00:00Z",
                     "direction": "from_them", "text": "hey are we still on for friday"}]
        orig = (sources.read_imessage, sources.count_imessage_dms, sources.read_whatsapp)
        sources.read_imessage = lambda p, db, cap=0: list(fake_dms)
        sources.count_imessage_dms = lambda p, db: len(fake_dms)
        sources.read_whatsapp = lambda p, db, cap=0: []
        try:
            pool, _ = collect.collect_one(
                self._person(phones=["+14155550000"]), store=store, accounts=accounts,
                chat_db=Path("/nope"), wacli_db=Path("/nope"), deep_cap=2)
        finally:
            sources.read_imessage, sources.count_imessage_dms, sources.read_whatsapp = orig
        channels = {m["channel"] for m in pool}
        self.assertIn("gmail", channels)            # gmail's capped vertical...
        self.assertIn("imessage", channels)         # ...still leaves room for chat

    def test_manifest_reports_opted_in_group_body_access(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            people = base / "people.csv"
            people.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n",
                encoding="utf-8",
            )
            manifest = _run_collect(_ns(
                out_dir=base / "raw",
                chat_db=base / "missing-chat.db",
                wacli_db=base / "missing-wacli.db",
                people_csv=people,
                msgvault_db=base / "missing-msgvault.db",
                dry_run=True,
                limit=0,
                person="",
                force=False,
                deep_cap=10,
                include_groups=True,
                max_group_size=12,
            ))
            self.assertTrue(manifest["privacy"]["groups_read"])
            self.assertFalse(manifest["privacy"]["dms_only"])
            self.assertEqual(manifest["privacy"]["group_source"], "imessage")
            self.assertEqual(manifest["privacy"]["max_group_size"], 12)

    def test_full_collection_removes_bundles_outside_current_people(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = base / "raw"
            raw.mkdir()
            (raw / "retired-person.json").write_text(
                '{"person_id":"retired-person","messages":[{"text":"old"}],'
                '"collection_policy":{"deep_cap":10,"include_groups":false,"max_group_size":0}}',
                encoding="utf-8",
            )
            (raw / "manifest.json").write_text(json.dumps({
                "privacy_schema_version": 2,
                "privacy": {"group_bodies_present": False},
            }), encoding="utf-8")
            people = base / "people.csv"
            people.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
                "current-person,Jordan Bravo,,,+15550100,,imessage\n",
                encoding="utf-8",
            )
            message = {
                "channel": "imessage", "at": "2026-07-13T00:00:00Z",
                "direction": "from_them", "text": "hello",
            }
            with mock.patch.object(collect, "collect_one", return_value=([message], 1)):
                manifest = _run_collect(_ns(
                    out_dir=raw,
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    people_csv=people,
                    msgvault_db=base / "missing-msgvault.db",
                    dry_run=False,
                    limit=0,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=False,
                    max_group_size=25,
                ))
            self.assertFalse((raw / "retired-person.json").exists())
            self.assertTrue((raw / "current-person.json").exists())
            self.assertEqual(manifest["orphan_bundles_removed"], 1)

    def test_default_collection_rebuilds_retained_group_bundles(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = base / "raw"
            raw.mkdir()
            people = base / "people.csv"
            people.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
                "p1,Person,,,+14155550000,,imessage\n",
                encoding="utf-8",
            )
            bundle = raw / "p1.json"
            bundle.write_text(json.dumps({
                "messages": [
                    {"channel": "imessage", "text": "dm"},
                    {"channel": "imessage_group", "text": "group body"},
                ],
                "messages_available": 2,
                "capped": False,
                "collection_policy": {
                    "deep_cap": 10,
                    "include_groups": True,
                    "max_group_size": 12,
                },
            }), encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({
                "privacy_schema_version": 2,
                "privacy": {"group_bodies_present": True},
            }), encoding="utf-8")

            dm_message = {
                "channel": "imessage",
                "at": "2026-07-13T00:00:00Z",
                "direction": "from_them",
                "text": "dm",
            }
            with mock.patch.object(collect, "collect_one", return_value=([dm_message], 1)):
                manifest = _run_collect(_ns(
                    out_dir=raw,
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    people_csv=people,
                    msgvault_db=base / "missing-msgvault.db",
                    dry_run=False,
                    limit=0,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=False,
                    max_group_size=25,
                ))

            saved = json.loads(bundle.read_text(encoding="utf-8"))
            self.assertEqual([message["channel"] for message in saved["messages"]], ["imessage"])
            self.assertFalse(saved["collection_policy"]["include_groups"])
            self.assertEqual(manifest["bundles_purged_for_scope"], 1)
            self.assertFalse(manifest["privacy"]["groups_read"])
            self.assertTrue(manifest["privacy"]["dms_only"])

            opted_in_message = {
                "channel": "imessage_group",
                "at": "2026-07-13T00:00:00Z",
                "direction": "from_them",
                "text": "approved group body",
            }
            with mock.patch.object(
                collect,
                "collect_one",
                return_value=([opted_in_message], 1),
            ) as collect_mock:
                opted_in_manifest = _run_collect(_ns(
                    out_dir=raw,
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    people_csv=people,
                    msgvault_db=base / "missing-msgvault.db",
                    dry_run=False,
                    limit=0,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=True,
                    max_group_size=12,
                ))

            collect_mock.assert_called_once()
            restored = json.loads(bundle.read_text(encoding="utf-8"))
            self.assertEqual(restored["messages"][0]["channel"], "imessage_group")
            self.assertTrue(opted_in_manifest["privacy"]["groups_read"])
            self.assertTrue(opted_in_manifest["privacy"]["group_bodies_present"])

    def test_invalid_input_does_not_purge_retained_bundles(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = base / "raw"
            raw.mkdir()
            bundle = raw / "p1.json"
            bundle.write_text('{"messages":[{"channel":"imessage_group","text":"private"}]}',
                              encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({
                "privacy_schema_version": 2,
                "privacy": {"group_bodies_present": True},
            }), encoding="utf-8")

            with self.assertRaises(FileNotFoundError):
                _run_collect(_ns(
                    out_dir=raw,
                    people_csv=base / "missing.csv",
                    msgvault_db=base / "missing-msgvault.db",
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    dry_run=False,
                    limit=0,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=False,
                    max_group_size=25,
                ))
            self.assertTrue(bundle.exists())

    def test_partial_default_collection_refuses_group_scope_transition(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw = base / "raw"
            raw.mkdir()
            people = base / "people.csv"
            people.write_text(
                "id,full_name,primary_email,all_emails,primary_phone,all_phones,source_channels\n"
                "p1,Person,,,+14155550000,,imessage\n",
                encoding="utf-8",
            )
            bundle = raw / "p1.json"
            bundle.write_text('{"messages":[{"channel":"imessage_group","text":"private"}]}',
                              encoding="utf-8")
            (raw / "manifest.json").write_text(json.dumps({
                "privacy_schema_version": 2,
                "privacy": {"group_bodies_present": True},
            }), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "full default collection"):
                _run_collect(_ns(
                    out_dir=raw,
                    people_csv=people,
                    msgvault_db=base / "missing-msgvault.db",
                    chat_db=base / "missing-chat.db",
                    wacli_db=base / "missing-wacli.db",
                    dry_run=False,
                    limit=1,
                    person="",
                    force=False,
                    deep_cap=10,
                    include_groups=False,
                    max_group_size=25,
                ))
            self.assertTrue(bundle.exists())


class TestSampling(unittest.TestCase):
    def test_signal_rank_prefers_signature(self):
        rich = {"text": "I'm CTO at Acme, call +1 415 555 1234 https://acme.com", "at": "2020"}
        thin = {"text": "thanks!", "at": "2021"}
        self.assertGreater(sources.signal_rank(rich), sources.signal_rank(thin))


class TestAttributedBody(unittest.TestCase):
    def test_decode_single_byte_length(self):
        text = "hey are you free tomorrow?"
        blob = b"\x04\x0bstreamtyped" + b"NSString" + b"\x01\x94\x84\x01+" + bytes([len(text)]) + text.encode()
        self.assertEqual(sources.decode_attributed_body(blob), text)

    def test_decode_empty_returns_blank(self):
        self.assertEqual(sources.decode_attributed_body(None), "")
        self.assertEqual(sources.decode_attributed_body(b"no marker here"), "")


class TestSynthesize(unittest.TestCase):
    def test_chunk_messages_budget(self):
        msgs = [{"text": "a" * 50} for _ in range(5)]
        chunks = synth.chunk_messages(msgs, chunk_chars=120)
        self.assertTrue(all(sum(len(m["text"]) for m in c) <= 120 or len(c) == 1 for c in chunks))
        self.assertEqual(sum(len(c) for c in chunks), 5)

    def test_fact_keys_detect_new_info(self):
        a = {"employers": [{"name": "Acme"}], "topics": ["x"], "title": "", "school": "", "location": "", "field_of_study": "", "identifiers": []}
        b = {"employers": [{"name": "Acme"}], "topics": ["x"], "title": "", "school": "", "location": "", "field_of_study": "", "identifiers": []}
        self.assertEqual(synth.fact_keys(a), synth.fact_keys(b))
        c = dict(b, topics=["x", "y"])
        self.assertTrue(synth.fact_keys(c) - synth.fact_keys(a))

    def test_owned_identifier_is_part_of_fact_progress(self):
        before = _facts()
        after = _facts(owned_identifiers={"emails": [], "phones": ["+14155550100"], "urls": []})
        self.assertTrue(synth.fact_keys(after) - synth.fact_keys(before))

    def test_schema_requires_owned_identifiers(self):
        self.assertIn("owned_identifiers", synth.FACT_SCHEMA["required"])

    def test_prompt_phone_discipline(self):
        prompt = synth.SYSTEM_PROMPT
        self.assertIn("at most one or two", prompt)
        self.assertIn("their mobile first", prompt)
        self.assertIn("Zoom/Teams/Webex bridges are no one's personal number", prompt)
        self.assertIn("their own signature or their own words", prompt)

    def test_contract_version_requeues_stale_terminal_facts(self):
        with tempfile.TemporaryDirectory() as d:
            raw, facts = Path(d) / "raw", Path(d) / "facts"
            raw.mkdir(); facts.mkdir()
            bundle = raw / "p1.json"
            bundle.write_text('{"messages": [{"text": "hello"}]}', encoding="utf-8")
            (facts / "p1.jsonl").write_text(json.dumps({
                "synthesis_version": "old-contract",
                "facts": _facts(network_worth={"decision": "yes", "reason": "real person"}),
            }) + "\n", encoding="utf-8")
            self.assertEqual(synth.pending_target_paths(raw, facts, force=False, person_id="", review_rows={}), [bundle])
            (facts / "p1.jsonl").write_text(json.dumps({
                "synthesis_version": synth.SYNTHESIS_VERSION,
                "facts": _facts(network_worth={"decision": "yes", "reason": "real person"}),
            }) + "\n", encoding="utf-8")
            self.assertEqual(synth.pending_target_paths(raw, facts, force=False, person_id="", review_rows={}), [])

    def test_completed_collection_prunes_orphan_facts_but_scoped_run_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            raw, facts = Path(d) / "raw", Path(d) / "facts"
            raw.mkdir()
            facts.mkdir()
            (raw / "current.json").write_text('{"messages":[{"text":"hello"}]}', encoding="utf-8")
            (raw / "manifest.json").write_text('{"status":"completed"}', encoding="utf-8")
            (facts / "current.jsonl").write_text("{}\n", encoding="utf-8")
            orphan = facts / "retired.jsonl"
            orphan.write_text("{}\n", encoding="utf-8")
            self.assertEqual(
                synth.prune_orphan_facts(raw, facts, scoped=True, dry_run=False), 0)
            self.assertTrue(orphan.exists())
            self.assertEqual(
                synth.prune_orphan_facts(raw, facts, scoped=False, dry_run=False), 1)
            self.assertFalse(orphan.exists())


class TestMergeFacts(unittest.TestCase):
    def test_merges_employers_and_picks_confident_scalars(self):
        chunks = [
            {"facts": {"canonical_name": "Jane Doe", "aliases": [], "employers": [{"name": "Acme", "role": "Eng", "status": "past"}],
                       "title": "Engineer", "school": "", "field_of_study": "", "location": "SF",
                       "relationship_to_owner": "colleague", "topics": ["ml"], "notable_events": [], "identifiers": [], "confidence": 0.6}},
            {"facts": {"canonical_name": "Jane Doe", "aliases": ["JD"], "employers": [{"name": "Acme", "role": "", "status": "current"}],
                       "title": "Staff Engineer", "school": "MIT", "field_of_study": "CS", "location": "",
                       "relationship_to_owner": "longtime colleague and friend", "topics": ["ml", "hiring"], "notable_events": [{"date": "2021", "summary": "joined"}], "identifiers": ["@jane"], "confidence": 0.9}},
        ]
        merged = compose.merge_facts(chunks)
        self.assertEqual(merged["canonical_name"], "Jane Doe")
        self.assertEqual(len(merged["employers"]), 1)
        self.assertEqual(merged["employers"][0]["status"], "current")  # current beats past
        self.assertEqual(merged["employers"][0]["role"], "Eng")  # role backfilled
        self.assertEqual(merged["title"], "Staff Engineer")  # higher confidence wins
        self.assertEqual(set(merged["topics"]), {"ml", "hiring"})
        self.assertEqual(merged["school"], "MIT")
        self.assertIn("longtime", merged["relationship_to_owner"])
        self.assertEqual(merged["owned_identifiers"], {"emails": [], "phones": [], "urls": []})

    def test_headline(self):
        self.assertEqual(
            compose.headline({"title": "CTO", "employers": [{"name": "Acme", "status": "current"}]}),
            "CTO at Acme",
        )


class TestIncrementalSynthesis(unittest.TestCase):
    """Stop-logic for the confidence-gated deepening loop (fakes the OpenAI call)."""

    def _run(self, confidences, *, static_facts, nbatches, target=0.85, saturation=2, max_batches=20):
        import asyncio

        seq = list(confidences)
        calls = {"n": 0}

        async def fake_call_one(client, prompt, **kw):
            i = calls["n"]; calls["n"] += 1
            conf = seq[i] if i < len(seq) else seq[-1]
            topic = "same" if static_facts else f"t{i}"  # static => saturates
            return _facts(confidence=conf, topics=[topic]), {"input_tokens": 1, "output_tokens": 1, "reasoning_tokens": 0}, ""

        orig = synth._call_one
        synth._call_one = fake_call_one
        try:
            batches = [[{"text": "hi", "at": "2020", "channel": "imessage", "direction": "from_them"}] for _ in range(nbatches)]
            return asyncio.run(synth.synthesize_person(
                None, {"person_id": "p", "full_name": "X", "messages_available": 99}, batches,
                model="m", effort="low", semaphore=asyncio.Semaphore(1), max_retries=0,
                system_prompt="s", target_confidence=target, saturation_rounds=saturation, max_batches=max_batches,
            ))
        finally:
            synth._call_one = orig

    def test_stops_when_confident(self):
        res = self._run([0.5, 0.9], static_facts=False, nbatches=5)
        self.assertEqual(res["stop_reason"], "confident")
        self.assertEqual(res["batches_used"], 2)

    def test_stops_when_saturated(self):
        res = self._run([0.5, 0.5, 0.5, 0.5], static_facts=True, nbatches=5)
        self.assertEqual(res["stop_reason"], "saturated")
        self.assertEqual(res["batches_used"], 3)  # batch1 new, then 2 stale

    def test_stops_when_exhausted(self):
        res = self._run([0.5, 0.5, 0.5], static_facts=False, nbatches=3)
        self.assertEqual(res["stop_reason"], "exhausted")
        self.assertEqual(res["batches_used"], 3)

    def test_respects_max_batches(self):
        res = self._run([0.5] * 10, static_facts=False, nbatches=10, max_batches=3)
        self.assertEqual(res["stop_reason"], "max_batches")
        self.assertEqual(res["batches_used"], 3)

    def test_chunked_bounds_resident_set(self):
        chunks = list(synth._chunked(list(range(10)), 3))
        self.assertEqual([len(c) for c in chunks], [3, 3, 3, 1])  # never more than 3 at once
        self.assertEqual([x for c in chunks for x in c], list(range(10)))  # lossless

    def test_render_batch_includes_prior_profile(self):
        person = {"full_name": "Jane", "emails": [], "phones": [], "source_channels": []}
        batch = [{"text": "hello", "at": "2020", "channel": "imessage", "direction": "from_them"}]
        self.assertNotIn("PROFILE SO FAR", synth.render_batch(person, batch, None))
        self.assertIn("PROFILE SO FAR", synth.render_batch(person, batch, {"title": "CTO"}))


class _StubAsyncClient:
    """Stands in for AsyncOpenAI. `execute()` only ever closes it — every request
    goes through the patched `_call_one` — so `close()` is the whole surface."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class TestSynthesizeExecute(unittest.TestCase):
    """`execute()` end to end with a stubbed OpenAI client: both paths through its
    single exit. Nothing reaches the network — `_call_one` and the client factory
    are replaced, so no key is read and no request is made."""

    def _node(self, root: Path, **kw) -> synth.SynthesizePersonContext:
        return synth.SynthesizePersonContext(
            raw_dir=root / "raw",
            out_dir=root / "facts",
            review_csv=root / "review.csv",
            concurrency=1,
            no_owner=True,
            **kw,
        )

    def _bundle(self, root: Path, pid: str = "p1") -> None:
        (root / "raw").mkdir(exist_ok=True)
        (root / "raw" / f"{pid}.json").write_text(json.dumps({
            "person_id": pid,
            "full_name": "Jordan Bravo",
            "messages": [{"text": "lunch friday?", "at": "2026-01-02",
                          "channel": "gmail", "direction": "from_them"}],
        }), encoding="utf-8")

    def _execute(self, root: Path, fake_call_one, **kw):
        client = _StubAsyncClient()
        with mock.patch.object(synth, "_call_one", fake_call_one), \
                mock.patch.object(synth, "make_async_client", lambda **_: client):
            return self._node(root, **kw).execute(), client

    def test_nothing_pending_reports_a_zero_run_without_building_a_client(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "raw").mkdir()

            def no_client(**_):
                raise AssertionError("a run with nothing pending must not build a client")

            with mock.patch.object(synth, "make_async_client", no_client):
                payload = self._node(root).execute()
            self.assertEqual(payload.status, "completed")
            self.assertEqual(payload.people, 0)
            self.assertEqual(payload.people_done, 0)
            self.assertEqual(payload.batches_run, 0)
            self.assertEqual(payload.avg_batches_per_person, 0.0)
            self.assertEqual(payload.stop_reasons, {})
            self.assertEqual(payload.errors, 0)
            self.assertEqual(payload.concurrency, 0)  # no pool was ever sized
            self.assertEqual(payload.tokens,
                             {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0})
            self.assertEqual(payload.estimated_cost_usd, 0.0)
            self.assertEqual(payload.out_dir, str(root / "facts"))
            self.assertIsInstance(payload.worth_sync, dict)  # the mirror still runs

    def test_pending_bundle_is_synthesized_checkpointed_and_tallied(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)

            async def fake_call_one(client, prompt, **kw):
                return (_facts(confidence=0.95, topics=["lunch"],
                               network_worth={"decision": "yes", "reason": "real person"}),
                        {"input_tokens": 120, "output_tokens": 40, "reasoning_tokens": 8}, "")

            payload, client = self._execute(root, fake_call_one)
            self.assertEqual(payload.people, 1)
            self.assertEqual(payload.people_done, 1)
            self.assertEqual(payload.batches_run, 1)
            self.assertEqual(payload.avg_batches_per_person, 1.0)
            self.assertEqual(payload.stop_reasons, {"confident": 1})
            self.assertEqual(payload.errors, 0)
            self.assertEqual(payload.concurrency, 1)
            self.assertEqual(payload.tokens,
                             {"input_tokens": 120, "output_tokens": 40, "reasoning_tokens": 8})
            self.assertTrue(client.closed)
            record = json.loads((root / "facts" / "p1.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(record["synthesis_version"], synth.SYNTHESIS_VERSION)
            self.assertEqual(record["stop_reason"], "confident")
            self.assertEqual(record["facts"]["confidence"], 0.95)

    def test_provider_error_is_counted_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)

            async def failing_call_one(client, prompt, **kw):
                return ({}, {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0},
                        "APIError: upstream refused")

            payload, _ = self._execute(root, failing_call_one)
            self.assertEqual(payload.status, "completed")
            self.assertEqual(payload.people_done, 1)
            self.assertEqual(payload.errors, 1)
            self.assertEqual(payload.stop_reasons, {"exhausted": 1})

    def test_prompt_carries_the_content_policy(self):
        # v5: dossiers are professional-context documents. The policy block,
        # the milestone allowance, and the professional-quoting rule must stay
        # pinned — phrasing stays allowlist-shaped (name the class of what
        # belongs, not enumerated examples nor the categories it displaces).
        self.assertIn("CONTENT POLICY", synth.SYSTEM_PROMPT)
        self.assertIn("the kind you would congratulate them on", synth.SYSTEM_PROMPT)
        self.assertIn("Everything else about their personal life stays out",
                      synth.SYSTEM_PROMPT)
        self.assertIn("Quote a message only for its professional content",
                      synth.SYSTEM_PROMPT)
        self.assertIn("still count as relationship evidence", synth.SYSTEM_PROMPT)
        self.assertNotIn("sexual", synth.SYSTEM_PROMPT)
        self.assertNotIn("drug", synth.SYSTEM_PROMPT)
        self.assertEqual(synth.SYNTHESIS_CONTRACT_VERSION, "relationship-category-v6")

    def test_relationship_category_schema_prompt_and_coercion(self):
        # v6: the slice tag is forced by the schema enum, explained in the
        # prompt, and coerced to a legal value at the parse boundary.
        props = synth.FACT_SCHEMA["properties"]["relationship_category"]
        self.assertEqual(props["enum"],
                         ["work", "personal", "family", "service", "mixed", "unknown"])
        self.assertIn("relationship_category", synth.FACT_SCHEMA["required"])
        self.assertIn("`relationship_category` is ONE slice tag", synth.SYSTEM_PROMPT)
        self.assertIn("never infer the category", synth.SYSTEM_PROMPT)
        for raw, want in [("work", "work"), ("Personal", "personal"),
                          ("colleague", "work"), ("friend", "personal"),
                          ("vendor", "service"), ("both", "mixed"),
                          ("", "unknown"), (None, "unknown"),
                          ("bestie", "unknown"), (42, "unknown")]:
            self.assertEqual(synth.coerce_relationship_category(raw), want, raw)

    def test_plan_is_one_typed_value(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            plan = self._node(root)._plan()
            self.assertEqual(plan.paths, [root / "raw" / "p1.json"])
            self.assertIsNone(plan.owner)  # --no-owner
            self.assertEqual(plan.system_prompt, synth.SYSTEM_PROMPT)

    def test_tally_accumulates_tokens_stop_reasons_and_errors(self):
        tally = synth.SynthesisTally()
        tally.record({"usage": {"input_tokens": 3, "output_tokens": 2, "reasoning_tokens": 1},
                      "errors": 0, "batches_used": 2, "stop_reason": "confident"})
        tally.record({"usage": {"input_tokens": 5, "reasoning_tokens": 4},
                      "errors": 1, "batches_used": 3, "stop_reason": "confident"})
        self.assertEqual(tally.people_done, 2)
        self.assertEqual(tally.errors, 1)
        self.assertEqual(tally.batches, 5)
        self.assertEqual(tally.stop_reasons, {"confident": 2})
        self.assertEqual(tally.tokens,
                         {"input_tokens": 8, "output_tokens": 2, "reasoning_tokens": 5})


class TestCheckReadinessDefaults(unittest.TestCase):
    def test_default_chat_db_follows_the_current_home(self):
        with tempfile.TemporaryDirectory() as d:
            expected = Path(d) / "Library" / "Messages" / "chat.db"
            with mock.patch.dict(os.environ, {"HOME": d}):
                self.assertEqual(check_readiness.default_chat_db(), expected)
                self.assertEqual(check_readiness.CheckReadiness().chat_db, expected)


class TestBuildOwner(unittest.TestCase):
    def test_owner_from_profile_maps_schools_and_jobs(self):
        from packs.ingestion.primitives.deep_context import build_owner
        normalized = {
            "full_name": "Jane Doe", "headline": "Eng",
            "location_str": "NYC",
            "education": [{"school": "MIT", "degree": "BS", "field": "CS",
                           "starts_at": {"year": 2006}, "ends_at": {"year": 2010}}],
            "experiences": [{"company_name": "Acme", "title": "Engineer",
                             "starts_at": {"year": 2012}, "ends_at": {"year": 2016}}],
        }
        owner = build_owner.owner_from_profile(normalized, email="jane@x.com")
        self.assertEqual(owner["name"], "Jane Doe")
        self.assertEqual(owner["emails"], ["jane@x.com"])
        self.assertEqual(owner["education"][0], {"school": "MIT", "start": 2006, "end": 2010, "note": "BS CS"})
        self.assertEqual(owner["work"][0], {"company": "Acme", "title": "Engineer", "start": 2012, "end": 2016})
        self.assertEqual(owner["locations"], ["NYC"])


class TestOwnerContext(unittest.TestCase):
    def test_owner_background_block(self):
        block = common.owner_background_block({
            "name": "Jane Doe",
            "education": [{"school": "MIT", "end": 2010, "note": "undergrad"}],
            "work": [{"company": "Acme", "title": "Engineer", "start": 2012, "end": 2016}],
            "locations": ["NYC"],
        })
        self.assertIn("Jane Doe", block)
        self.assertIn("MIT [until 2010]", block)
        self.assertIn("Acme as Engineer [2012-2016]", block)

    def test_shared_context_merges_and_dedupes(self):
        chunks = [
            {"facts": _facts(shared_context=[{"overlap": "school", "detail": "Stanford overlap", "evidence": "e1"}])},
            {"facts": _facts(shared_context=[{"overlap": "school", "detail": "Stanford overlap", "evidence": "e1"},
                                             {"overlap": "employer", "detail": "Globex", "evidence": "e2"}])},
        ]
        merged = compose.merge_facts(chunks)
        details = {s["detail"] for s in merged["shared_context"]}
        self.assertEqual(details, {"Stanford overlap", "Globex"})


def _facts(**over):
    base = {"canonical_name": "X", "aliases": [], "employers": [], "title": "", "school": "",
            "field_of_study": "", "location": "", "relationship_to_owner": "", "topics": [],
            "notable_events": [], "identifiers": [], "shared_context": [], "confidence": 0.5}
    base.update(over)
    return base


class TestJaroWinkler(unittest.TestCase):
    def test_identical_and_similar(self):
        self.assertEqual(cluster.jaro_winkler("jane doe", "jane doe"), 1.0)
        self.assertGreater(cluster.jaro_winkler("jon smith", "john smith"), 0.9)
        self.assertLess(cluster.jaro_winkler("jane doe", "bob jones"), 0.7)

    def test_connected_components(self):
        comps = cluster.connected_components(4, [(0, 1), (1, 2)])
        self.assertEqual(sorted(comps[0]), [0, 1, 2])


class TestParents(unittest.TestCase):
    def test_clusters_from_pairs(self):
        pairs = [
            {"slug_a": "a", "slug_b": "b", "score": "1.0", "reason": "x"},
            {"slug_a": "b", "slug_b": "c", "score": "0.9", "reason": "y"},
            {"slug_a": "d", "slug_b": "e", "score": "0.95", "reason": "z"},
        ]
        cl = sorted(parents.clusters_from_pairs(pairs), key=len, reverse=True)
        self.assertEqual(sorted(cl[0]), ["a", "b", "c"])
        self.assertEqual(sorted(cl[1]), ["d", "e"])

    def test_parent_id_is_stable_and_order_independent(self):
        self.assertEqual(parents.parent_id_for(["p1", "p2"]), parents.parent_id_for(["p2", "p1"]))
        self.assertNotEqual(parents.parent_id_for(["p1", "p2"]), parents.parent_id_for(["p1", "p3"]))

    def test_parent_slug_migration_rewrites_artifacts_once(self):
        old_slug = "jordan-bravo-parent12"
        new_slug = "jordan-bravo-12345678"
        mapping = parent_slug_migrations(
            {old_slug: {"parent_id": "parent-1234567890ab"}},
            {new_slug: {"parent_id": "parent-1234567890ab"}},
        )
        self.assertEqual(mapping, {old_slug: new_slug})

        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            research = base / "deep-research"
            old_dir = research / old_slug
            old_dir.mkdir(parents=True)
            (old_dir / "01_research_parallel.json").write_text(
                '{"status":"completed"}\n', encoding="utf-8"
            )
            queue = research / "research_queue.csv"
            queue.write_text(
                "handle,source_parent_slug,display_name\n"
                f"{old_slug},{old_slug},Jordan Bravo\n",
                encoding="utf-8",
            )
            verdicts_jsonl = base / "verdicts.jsonl"
            verdicts_jsonl.write_text(
                json.dumps({"parent_slug": old_slug, "name": "Jordan Bravo"}) + "\n",
                encoding="utf-8",
            )
            verdicts_csv = base / "verdicts.csv"
            verdicts_csv.write_text(
                f"parent_slug,name\n{old_slug},Jordan Bravo\n",
                encoding="utf-8",
            )
            applied_csv = base / "applied.csv"
            applied_csv.write_text(
                f"parent_slug,name\n{old_slug},Jordan Bravo\n",
                encoding="utf-8",
            )
            synthetic_csv = base / "synthetic.csv"
            synthetic_csv.write_text(
                f"source_parent_slug,full_name\n{old_slug},Jordan Bravo\n",
                encoding="utf-8",
            )

            stats = migrate_parent_slug_artifacts(
                mapping,
                deep_research_dir=research,
                verdicts_jsonl=verdicts_jsonl,
                verdicts_csv=verdicts_csv,
                applied_csv=applied_csv,
                synthetic_people_csv=synthetic_csv,
            )
            self.assertEqual(stats["directories_renamed"], 1)
            self.assertEqual(stats["csv_rows_rewritten"], 4)
            self.assertEqual(stats["jsonl_rows_rewritten"], 1)
            self.assertFalse(old_dir.exists())
            self.assertTrue((research / new_slug / "01_research_parallel.json").exists())
            self.assertNotIn(old_slug, queue.read_text(encoding="utf-8"))
            self.assertNotIn(old_slug, verdicts_jsonl.read_text(encoding="utf-8"))
            self.assertNotIn(old_slug, verdicts_csv.read_text(encoding="utf-8"))
            self.assertNotIn(old_slug, applied_csv.read_text(encoding="utf-8"))
            self.assertNotIn(old_slug, synthetic_csv.read_text(encoding="utf-8"))

            rerun = migrate_parent_slug_artifacts(
                mapping,
                deep_research_dir=research,
                verdicts_jsonl=verdicts_jsonl,
                verdicts_csv=verdicts_csv,
                applied_csv=applied_csv,
                synthetic_people_csv=synthetic_csv,
            )
            self.assertEqual(rerun["directories_renamed"], 0)
            self.assertEqual(rerun["csv_rows_rewritten"], 0)
            self.assertEqual(rerun["jsonl_rows_rewritten"], 0)


def _verdict_rows(path: Path) -> list[dict[str, str]]:
    """merge-verdicts.csv rows (closing the file)."""
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class TestEndToEnd(unittest.TestCase):
    """compose -> cluster -> lookup over synthetic fixtures, detecting a duplicate."""

    def _write_person(self, raw_dir: Path, facts_dir: Path, pid: str, name: str, phone: str, email: str):
        write_json(raw_dir / f"{pid}.json", {
            "person_id": pid, "full_name": name, "emails": [email] if email else [],
            "phones": [phone] if phone else [], "source_channels": ["imessage"],
            "messages": [{"at": "2023-01-01", "channel": "imessage", "direction": "from_them", "subject": "", "text": "hi"}],
        })
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps({
            "chunk_index": 0,
            "facts": {"canonical_name": name, "aliases": [], "employers": [{"name": "Acme", "role": "Eng", "status": "current"}],
                      "title": "Engineer", "school": "", "field_of_study": "", "location": "SF",
                      "relationship_to_owner": "friend", "topics": ["climbing"], "notable_events": [], "identifiers": [], "confidence": 0.8},
            "usage": {}, "error": "",
        }) + "\n", encoding="utf-8")

    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
            raw.mkdir(); facts.mkdir()
            index_json, index_md = base / "index.json", base / "index.md"
            merge_csv, merge_md = base / "merge.csv", base / "merge.md"

            # Two rows for the SAME person (shared phone, name variant) + one distinct.
            self._write_person(raw, facts, "p1", "Jonathan Smith", "+14155551234", "jon@acme.com")
            self._write_person(raw, facts, "p2", "Jon Smith", "+14155551234", "jon.smith@gmail.com")
            self._write_person(raw, facts, "p3", "Maria Garcia", "+13105550000", "maria@x.com")

            _run_compose(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                            index_json=index_json, index_md=index_md, person=""))
            self.assertEqual(len(list(dossiers.glob("*.md"))), 3)

            # Lookup by phone returns BOTH duplicates; by name fuzzy works.
            idx = json.loads(index_json.read_text())
            slugs = lookup.find_slugs(idx, name="", phone="+1 415 555 1234", email="")
            self.assertEqual(len(slugs), 2)
            self.assertEqual(lookup.find_slugs(idx, name="Maria Garcia", phone="", email=""),
                             idx["by_name"]["maria garcia"])

            # Cluster detects the duplicate pair. --no-llm = deterministic (offline test);
            # the live pipeline uses the mandatory LLM tone-aware judge.
            manifest = _run_cluster(_ns(dossier_dir=dossiers, index_json=index_json, raw_dir=raw, facts_dir=facts,
                                       out_csv=merge_csv, out_md=merge_md, confidence=0.7, no_llm=True,
                                       model="m", reasoning_effort="medium", concurrency=1, timeout=10, max_retries=0))
            self.assertEqual(manifest["judge"], "deterministic")
            self.assertGreaterEqual(manifest["candidate_pairs"], 1)
            self.assertEqual(manifest["clusters"], 1)

            # The injected section names the other person.
            p1_slug = idx["by_phone"]["4155551234"][0]
            text = (dossiers / f"{p1_slug}.md").read_text()
            self.assertIn("Possible same person", text)
            self.assertIn("confidence", text.split("Possible same person")[1])

            # Parent layer: the duplicate pair becomes one canonical parent that
            # links both children, and each child backrefs the parent.
            par_dir = base / "parents"
            # Always a complete canonical layer: 1 merged parent (p1/p2 dup) + 1 pointer
            # parent for the unique p3 (Maria). Every person resolves through parents/.
            pman = _run_parents(_ns(merge_csv=merge_csv, index_json=index_json, dossier_dir=dossiers,
                                   facts_dir=facts, raw_dir=raw, parents_dir=par_dir, confirm_threshold=0.85))
            self.assertEqual(pman["merged_parents"], 1)
            self.assertEqual(pman["singleton_parents"], 1)  # Maria, unmerged -> pointer parent
            merged_md = [p.read_text() for p in par_dir.glob("*.md")
                         if "kind: parent\nsingleton" not in p.read_text() and "## Confirmed children" in p.read_text()]
            self.assertTrue(any("[[" + p1_slug + "]]" in t for t in merged_md))
            idx3 = json.loads(index_json.read_text())
            self.assertEqual(len(idx3["parents"]), 2)
            self.assertTrue(any(p.get("singleton") for p in idx3["parents"].values()))
            self.assertIn("Part of [[", (dossiers / f"{p1_slug}.md").read_text())
            # Parent is now resolvable by the shared phone.
            idx2 = json.loads(index_json.read_text())
            self.assertTrue(any(s.endswith(pman_slug := list(idx2["parents"])[0]) or s == pman_slug
                                for s in idx2["by_phone"]["4155551234"]))

    def test_recompose_preserves_the_parents_build_parents_owns(self):
        # THE regression: compose used to write a FRESH index document, silently deleting
        # `parents`. Everything keyed on the parent grouping (one row per person, merged-away
        # candidate suppression, the review collapse) then read an empty map, so one human
        # split back into one row per identity and already-merged people became paid-research
        # eligible again. Compose after parents must be a no-op for `parents`.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
            raw.mkdir(); facts.mkdir()
            index_json, index_md = base / "index.json", base / "index.md"
            par_dir = base / "parents"
            self._write_person(raw, facts, "p1", "Jordan Bravo", "+15550100", "jordan@example.com")
            self._write_person(raw, facts, "p2", "Jordan Bravo", "+15550100", "jb@example.net")
            self._write_person(raw, facts, "p3", "Casey Delta", "+15550111", "casey@example.com")

            def run_compose(person=""):
                return _run_compose(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                                       index_json=index_json, index_md=index_md, person=person))

            run_compose()
            _run_cluster(_ns(dossier_dir=dossiers, index_json=index_json, raw_dir=raw, facts_dir=facts,
                            out_csv=base / "merge.csv", out_md=base / "merge.md", confidence=0.7,
                            no_llm=False, deterministic_only=True, model="m",
                            reasoning_effort="medium", concurrency=1, timeout=10, max_retries=0))
            _run_parents(_ns(merge_csv=base / "merge.csv", index_json=index_json, dossier_dir=dossiers,
                            facts_dir=facts, raw_dir=raw, parents_dir=par_dir, confirm_threshold=0.85))
            after_parents = json.loads(index_json.read_text())
            self.assertEqual(len(after_parents["parents"]), 2)  # 1 merged pair + 1 singleton

            # ...compose again (the documented flow reruns it): parents survive untouched, and
            # because the lookup maps are DERIVED the whole document is reproduced exactly.
            run_compose()
            after_recompose = json.loads(index_json.read_text())
            self.assertEqual(after_recompose["parents"], after_parents["parents"])
            self.assertEqual(after_recompose, after_parents)

            # A parent is still resolvable by the identifiers it inherited from its children.
            merged_slug = next(s for s, p in after_recompose["parents"].items()
                               if not p.get("singleton"))
            self.assertIn(merged_slug, after_recompose["by_phone"]["15550100"])
            self.assertIn(merged_slug, after_recompose["by_email"]["jb@example.net"])

    def test_compose_scoped_to_one_person_keeps_the_rest_of_the_index(self):
        # `compose --person X` skipped everyone else but still wrote the fresh dict, so the
        # index ended up holding ONE person while every other dossier stayed on disk —
        # lookups returned nothing for people whose dossier was right there.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
            raw.mkdir(); facts.mkdir()
            index_json, index_md = base / "index.json", base / "index.md"
            self._write_person(raw, facts, "p1", "Jordan Bravo", "+15550100", "jordan@example.com")
            self._write_person(raw, facts, "p2", "Casey Delta", "+15550111", "casey@example.com")

            def run_compose(person=""):
                return _run_compose(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                                       index_json=index_json, index_md=index_md, person=person))

            run_compose()
            full = json.loads(index_json.read_text())
            scoped = run_compose(person="p1")
            self.assertEqual(scoped["dossiers_written"], 1)
            after = json.loads(index_json.read_text())
            self.assertEqual(set(after["slugs"]), set(full["slugs"]))
            self.assertEqual(len(list(dossiers.glob("*.md"))), 2)
            self.assertEqual(lookup.find_slugs(after, name="Casey Delta", phone="", email=""),
                             full["by_name"]["casey delta"])

    def test_a_renamed_person_leaves_exactly_one_index_record(self):
        # One human, one record: a changed canonical_name yields a new slug, and the stale
        # entry for the same person_id must go — including on a scoped rerun.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
            raw.mkdir(); facts.mkdir()
            index_json, index_md = base / "index.json", base / "index.md"
            self._write_person(raw, facts, "p1", "Jordan Bravo", "+15550100", "jordan@example.com")
            _run_compose(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                            index_json=index_json, index_md=index_md, person=""))
            self._write_person(raw, facts, "p1", "Jordan Bravado", "+15550100", "jordan@example.com")
            _run_compose(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                            index_json=index_json, index_md=index_md, person="p1"))
            after = json.loads(index_json.read_text())
            self.assertEqual([info["person_id"] for info in after["slugs"].values()], ["p1"])
            self.assertEqual(after["by_phone"]["15550100"], list(after["slugs"]))

    def _cluster_fixture(self, base: Path):
        """Three synthetic people (one duplicate pair sharing a phone) composed into an index."""
        raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
        raw.mkdir(); facts.mkdir()
        index_json, index_md = base / "index.json", base / "index.md"
        self._write_person(raw, facts, "p1", "Jonathan Smith", "+15550100", "jon@acme.test")
        self._write_person(raw, facts, "p2", "Jon Smith", "+15550100", "jon.smith@example.com")
        self._write_person(raw, facts, "p3", "Maria Garcia", "+15550111", "maria@example.net")
        _run_compose(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                        index_json=index_json, index_md=index_md, person=""))

        def cluster_run(**over):
            kw = dict(dossier_dir=dossiers, index_json=index_json, raw_dir=raw, facts_dir=facts,
                      out_csv=base / "merge.csv", out_md=base / "merge.md", confidence=0.7,
                      no_llm=True, model="m", reasoning_effort="medium", concurrency=1,
                      timeout=10, max_retries=0)
            kw.update(over)
            return _run_cluster(_ns(**kw))

        return cluster_run

    @staticmethod
    def _fake_pair_judge(same: bool = True):
        """Stand-in for the paid pair judge: every pair gets a real (llm-authored) verdict."""
        async def judge(client, pa, pb, *, model, effort, semaphore, max_retries):
            return {"verdict": {"same_person": same, "confidence": 0.95,
                                "tone_toward_a": "", "tone_toward_b": "",
                                "tone_consistent": True, "reason": "fake judge"},
                    "usage": {"input_tokens": 1, "output_tokens": 1, "reasoning_tokens": 0},
                    "error": ""}
        return judge

    def _judged(self, cluster_run, **over):
        """Run cluster with the judge stubbed out (no network, verdicts marked llm)."""
        class _Client:
            async def close(self):
                return None

        with mock.patch.object(cluster, "judge_pair", self._fake_pair_judge()), \
                mock.patch.object(cluster, "make_async_client", lambda **kw: _Client()), \
                mock.patch.object(cluster, "load_env", lambda: None):
            return cluster_run(no_llm=False, **over)

    def test_merge_cache_reuses_unchanged_pairs(self):
        # A rerun must NOT re-judge pairs whose inputs are unchanged: it reuses the prior
        # merge-verdicts.csv, so the incremental cost is ~0 until the network actually changes.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            cluster_run = self._cluster_fixture(base)
            verdicts_csv = base / "merge-verdicts.csv"

            # 1) First run: nothing cached -> judges everything and writes the cache.
            m1 = self._judged(cluster_run)
            judgeable = m1["pairs_total"] - m1["pairs_deterministic"]
            self.assertGreaterEqual(judgeable, 1)
            self.assertEqual(m1["pairs_reused"], 0)
            self.assertEqual(m1["pairs_judged"], judgeable)
            self.assertTrue(verdicts_csv.exists())

            # 2) Dry-run now sees the cache -> nothing left to judge, zero estimated spend.
            dry = cluster_run(dry_run=True)
            self.assertEqual(dry["candidate_pairs_to_judge"], 0)
            self.assertEqual(dry["cached_reused"], judgeable)
            self.assertEqual(dry["estimated_cost_usd_high"], 0)

            # 3) Second real run reuses every verdict and yields the same clusters.
            m2 = self._judged(cluster_run)
            self.assertEqual(m2["pairs_judged"], 0)
            self.assertEqual(m2["pairs_reused"], judgeable)
            self.assertEqual(m2["clusters"], m1["clusters"])

            # 4) --refresh bypasses the cache -> everything is judged again.
            refreshed = cluster_run(dry_run=True, refresh=True)
            self.assertEqual(refreshed["candidate_pairs_to_judge"], judgeable)
            self.assertEqual(refreshed["cached_reused"], 0)

    def test_no_llm_verdicts_never_satisfy_the_cache(self):
        # THE cache-poisoning guard: a free `--no-llm` run stamps the CURRENT pair sig, so
        # without provenance it would permanently convince every later paid run that those
        # pairs are already decided — the LLM would never see them.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            cluster_run = self._cluster_fixture(base)

            free = cluster_run()                       # --no-llm: guesses the unsettled pairs
            judgeable = free["pairs_total"] - free["pairs_deterministic"]
            self.assertGreaterEqual(judgeable, 1)
            self.assertEqual(free["judge"], "deterministic")
            self.assertEqual(free["pairs_judged"], 0)
            rows = _verdict_rows(base / "merge-verdicts.csv")
            self.assertEqual({r["judge"] for r in rows}, {cluster.JUDGE_NO_LLM})

            # The paid run must still judge all of them, and its own verdicts ARE reusable.
            paid = self._judged(cluster_run)
            self.assertEqual(paid["pairs_reused"], 0)
            self.assertEqual(paid["pairs_judged"], judgeable)
            again = cluster_run(dry_run=True)
            self.assertEqual(again["cached_reused"], judgeable)

    def test_deterministic_only_settles_tier0_and_leaves_the_rest_alone(self):
        # The shipped free tier: merge only what code can prove, never guess, never drop an
        # edge the paid judge already established.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            cluster_run = self._cluster_fixture(base)
            merge_csv = base / "merge.csv"

            tier0 = cluster_run(no_llm=False, deterministic_only=True)
            self.assertEqual(tier0["judge"], "tier0")
            self.assertEqual(tier0["pairs_judged"], 0)
            # p1/p2 share a phone but their NAMES differ, so tier 0 must not merge them.
            self.assertEqual(tier0["pairs_deterministic"], 0)
            self.assertEqual(tier0["pairs_unsettled"], tier0["pairs_total"])
            self.assertEqual(tier0["clusters"], 0)
            # Nothing was invented: no verdict row exists for an unsettled pair.
            self.assertEqual(_verdict_rows(base / "merge-verdicts.csv"), [])

            # After the paid judge merges them, a tier-0 rerun CARRIES that edge forward.
            paid = self._judged(cluster_run)
            self.assertEqual(paid["clusters"], 1)
            carried = cluster_run(no_llm=False, deterministic_only=True)
            self.assertEqual(carried["clusters"], 1)
            self.assertEqual(carried["pairs_unsettled"], 0)
            self.assertTrue(merge_csv.read_text().count("\n") > 1)

    def test_tier0_merges_an_identical_name_sharing_an_identifier(self):
        # What tier 0 IS for: identity equality decided in code, no judge, no spend.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            raw, facts, dossiers = base / "raw", base / "facts", base / "dossiers"
            raw.mkdir(); facts.mkdir()
            index_json = base / "index.json"
            self._write_person(raw, facts, "p1", "Jordan Bravo", "+15550100", "jordan@example.com")
            self._write_person(raw, facts, "p2", "Jordan Bravo", "+15550100", "jb@example.net")
            _run_compose(_ns(raw_dir=raw, facts_dir=facts, dossier_dir=dossiers,
                            index_json=index_json, index_md=base / "index.md", person=""))
            manifest = _run_cluster(_ns(
                dossier_dir=dossiers, index_json=index_json, raw_dir=raw, facts_dir=facts,
                out_csv=base / "merge.csv", out_md=base / "merge.md", confidence=0.7,
                no_llm=False, deterministic_only=True, model="m", reasoning_effort="medium",
                concurrency=1, timeout=10, max_retries=0))
            self.assertEqual(manifest["pairs_deterministic"], 1)
            self.assertEqual(manifest["pairs_unsettled"], 0)
            self.assertEqual(manifest["clusters"], 1)
            rows = _verdict_rows(base / "merge-verdicts.csv")
            self.assertEqual([r["judge"] for r in rows], [cluster.JUDGE_SLAM_DUNK])


def _verdict(verdict, conf, **kw):
    return {"verdict": verdict, "confidence": conf, "supporting_evidence": kw.get("sup", []),
            "contradicting_evidence": kw.get("con", []),
            "linkedin_plausibly_absent": kw.get("absent", False),
            "recommend_deep_research": kw.get("dr", False), "reason": kw.get("reason", "")}


def _rows_by_pub(path: Path) -> dict[str, dict[str, str]]:
    """Read an override/review CSV into a {public_identifier: row} map (closing the file)."""
    with path.open(newline="", encoding="utf-8") as fh:
        return {r["public_identifier"]: r for r in csv.DictReader(fh)}


class TestReconcileLinkedIn(unittest.TestCase):
    """Phase 3: verify each parent's attached LinkedIn (pairing, apply, queue, inject)."""

    def _facts(self, facts_dir, pid, name, employer="Acme", title="Engineer", location="SF"):
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps({
            "chunk_index": 0, "facts": {"canonical_name": name, "aliases": [],
                "employers": [{"name": employer, "role": "Eng", "status": "current"}],
                "title": title, "school": "", "field_of_study": "", "location": location,
                "relationship_to_owner": "friend", "topics": ["climbing"], "notable_events": [],
                "identifiers": [], "shared_context": [], "confidence": 0.8}, "usage": {}}) + "\n", encoding="utf-8")

    def _people_csv(self, path, rows):
        cols = ["id", "public_identifier", "linkedin_url", "full_name", "headline",
                "work_experiences", "education", "current_title", "current_company",
                "city", "state", "country", "primary_email", "all_emails", "primary_phone", "all_phones"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            import csv as _csv
            w = _csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})

    def test_linkedin_view_falls_back_to_people_csv(self):
        row = {"public_identifier": "janedoe", "linkedin_url": "https://www.linkedin.com/in/janedoe",
               "full_name": "Jane Doe", "headline": "Eng at X",
               "work_experiences": json.dumps([{"title": "Eng", "company_name": "Stripe",
                                                 "starts_at": {"year": 2018}, "ends_at": {"year": 2022}}]),
               "education": json.dumps([{"school": "MIT", "degree": "BS", "field": "CS"}]),
               "city": "SF", "state": "CA", "country": "USA"}
        with tempfile.TemporaryDirectory() as d:
            view = reconcile.linkedin_view(row, Path(d))  # empty cache dir -> fallback
        self.assertEqual(view["source"], "people_csv")
        self.assertTrue(view["has_profile"])
        self.assertIn("Eng @ Stripe (2018–2022)", view["experiences"][0])
        self.assertIn("MIT", view["education"][0])

    def test_build_tasks_pairs_conflicts_and_no_link(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            for pid, name in [("pa", "Alice"), ("pb1", "Bob"), ("pb2", "Bob"), ("pc", "Carol")]:
                self._facts(facts, pid, name)
            index = {"slugs": {"alice-c": {"person_id": "pa"}, "bob-c1": {"person_id": "pb1"},
                               "bob-c2": {"person_id": "pb2"}, "carol-c": {"person_id": "pc"}},
                     "parents": {
                         "alice-p": {"name": "Alice", "children": ["alice-c"]},
                         "bob-p": {"name": "Bob", "children": ["bob-c1", "bob-c2"]},   # conflict
                         "carol-p": {"name": "Carol", "children": ["carol-c"]}}}        # no link
            people = {
                "pa": {"id": "pa", "public_identifier": "alice", "linkedin_url": "https://www.linkedin.com/in/alice",
                       "headline": "Eng", "work_experiences": "[]", "education": "[]"},
                "pb1": {"id": "pb1", "public_identifier": "bobx", "linkedin_url": "https://www.linkedin.com/in/bobx",
                        "headline": "PM", "work_experiences": "[]", "education": "[]"},
                "pb2": {"id": "pb2", "public_identifier": "bobceo", "linkedin_url": "https://www.linkedin.com/in/bobceo",
                        "headline": "CEO", "work_experiences": "[]", "education": "[]"},
                "pc": {"id": "pc", "public_identifier": "", "linkedin_url": ""}}
            tasks = reconcile.build_tasks(index, people, facts, raw, cache)
            by_parent = {}
            for t in tasks:
                by_parent.setdefault(t["parent_slug"], []).append(t)
            self.assertEqual(len(by_parent["alice-p"]), 1)
            self.assertEqual(len(by_parent["bob-p"]), 2)             # two distinct linkedins
            self.assertTrue(all(t["conflict"] for t in by_parent["bob-p"]))
            self.assertTrue(by_parent["carol-p"][0]["no_link"])

    def test_candidate_child_uses_existing_link_without_a_second_lookup(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            self._facts(facts, "person-cass", "Cass")
            self._facts(facts, "candidate:email:cass@x.com", "Cass")
            index = {
                "slugs": {
                    "cass-existing": {"person_id": "person-cass"},
                    "cass-candidate": {"person_id": "candidate:email:cass@x.com"},
                },
                "parents": {
                    "cass-parent": {
                        "name": "Cass",
                        "children": ["cass-existing", "cass-candidate"],
                    },
                },
            }
            people = {
                "person-cass": {
                    "id": "person-cass",
                    "public_identifier": "cass",
                    "linkedin_url": "https://www.linkedin.com/in/cass",
                    "headline": "Engineer",
                    "work_experiences": "[]",
                    "education": "[]",
                },
            }
            (task,) = reconcile.build_tasks(index, people, facts, raw, cache)
            self.assertEqual(task["person_ids"], ["person-cass"])
            self.assertEqual(
                task["parent_person_ids"],
                ["person-cass", "candidate:email:cass@x.com"],
            )

    def test_linkedin_connections_are_ground_truth(self):
        """A contact imported from your LinkedIn Connections (linkedin_csv) is auto-confirmed."""
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            self._facts(facts, "pa", "Alice")
            self._facts(facts, "pb", "Bob")
            index = {"slugs": {"alice-c": {"person_id": "pa"}, "bob-c": {"person_id": "pb"}},
                     "parents": {"alice-p": {"name": "Alice", "children": ["alice-c"]},
                                 "bob-p": {"name": "Bob", "children": ["bob-c"]}}}
            people = {
                "pa": {"id": "pa", "public_identifier": "alice", "linkedin_url": "https://www.linkedin.com/in/alice",
                       "headline": "Eng", "work_experiences": "[]", "education": "[]",
                       "source_channels": "gmail_msgvault,linkedin_csv"},   # a connection
                "pb": {"id": "pb", "public_identifier": "bobx", "linkedin_url": "https://www.linkedin.com/in/bobx",
                       "headline": "PM", "work_experiences": "[]", "education": "[]",
                       "source_channels": "imessage"}}                       # not a connection
            tasks = {t["parent_slug"]: t for t in reconcile.build_tasks(index, people, facts, raw, cache)}
            self.assertTrue(tasks["alice-p"]["from_connections"])
            self.assertFalse(tasks["bob-p"]["from_connections"])
            v = reconcile.connection_verdict()
            self.assertEqual((v["verdict"], v["confidence"]), ("confirmed", 1.0))

    def _task(self, parent, pub, action_verdict, conf, **kw):
        return {"parent_slug": parent, "name": parent, "candidate_key": pub,
                "person_ids": [f"pid-{pub}"], "conflict": kw.get("conflict", False), "no_link": False,
                "linkedin": {"linkedin_url": f"https://www.linkedin.com/in/{pub}"},
                "match_emails": kw.get("emails", []), "match_phones": kw.get("phones", []),
                "verdict": _verdict(action_verdict, conf, reason=kw.get("reason", ""))}

    def test_write_overrides_emits_detach_and_verify(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [
                self._task("a", "alice", "confirmed", 0.95, emails=["a@x.com"]),
                self._task("b", "bobceo", "wrong_person", 0.92, emails=["bob@x.com"], reason="CEO != plumber"),
                self._task("c", "carol", "wrong_person", 0.50),  # below threshold -> pending in same file
            ]
            reconcile.decide_actions(tasks, 0.85)
            stats = reconcile.write_overrides(path, tasks)
            self.assertEqual(stats["verified"], 1)
            self.assertEqual(stats["detached"], 1)
            import csv as _csv
            with path.open() as fh:
                rows = {r["public_identifier"]: r for r in _csv.DictReader(fh)}
            self.assertEqual(rows["alice"]["action"], "verify")
            self.assertEqual(rows["alice"]["match_emails"], "a@x.com")
            self.assertEqual(rows["bobceo"]["action"], "detach")
            self.assertEqual(rows["alice"]["approved"], "auto")
            self.assertEqual(rows["carol"]["approved"], "")   # low-confidence -> PENDING in the same file

    def test_write_overrides_upsert_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [self._task("b", "bobceo", "wrong_person", 0.95)]
            reconcile.decide_actions(tasks, 0.85)
            reconcile.write_overrides(path, tasks)
            first = path.read_text()
            reconcile.write_overrides(path, tasks)  # same decision again
            import csv as _csv
            with path.open() as fh:
                rows = list(_csv.DictReader(fh))
            self.assertEqual(len(rows), 1)          # one row per public_identifier, no dupes
            # A pre-existing unrelated override row is preserved across re-runs.
            with path.open("a", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writerow({"public_identifier": "zzz", "action": "detach", "approved": "auto"})
            reconcile.write_overrides(path, tasks)
            with path.open() as fh:
                pubs = {r["public_identifier"] for r in _csv.DictReader(fh)}
            self.assertEqual(pubs, {"bobceo", "zzz"})

    def test_write_overrides_preserves_user_approved_rows(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            # Seed a user decision: bobceo manually approved=no (don't detach).
            with path.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writeheader()
                w.writerow({"public_identifier": "bobceo", "action": "detach", "approved": "no",
                            "reason": "user says keep"})
            tasks = [self._task("b", "bobceo", "wrong_person", 0.99)]  # judge again says detach
            reconcile.decide_actions(tasks, 0.85)
            stats = reconcile.write_overrides(path, tasks)
            self.assertEqual(stats["preserved_user_rows"], 1)
            import csv as _csv
            with path.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["approved"], "no")          # sticky: user decision NOT overwritten
            self.assertEqual(row["reason"], "user says keep")

    def test_upsert_retargets_proposes_pending_and_is_sticky(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            r = reconcile.upsert_retargets(path, [{"old_public_identifier": "bobceo",
                "new_linkedin_url": "https://www.linkedin.com/in/bob-real", "reason": "found"}])
            self.assertEqual(r["proposed"], 1)
            import csv as _csv
            with path.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(row["approved"], "")            # pending by default
            self.assertEqual(row["new_public_identifier"], "bob-real")
            # User approves; a later proposal must NOT clobber it.
            rows = reconcile.load_override_rows(path); rows["bobceo"]["approved"] = "yes"
            reconcile._write_override_rows(path, rows)
            reconcile.upsert_retargets(path, [{"old_public_identifier": "bobceo",
                "new_linkedin_url": "https://www.linkedin.com/in/someone-else"}])
            with path.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["approved"], "yes")
            self.assertEqual(row["new_public_identifier"], "bob-real")  # preserved

    def test_conflict_auto_resolves_one_confirmed_rest_wrong(self):
        # One parent, two different attached links: one confirmed, one wrong -> auto-resolve
        # (keep the confirmed, detach the wrong) instead of deferring to review.
        tasks = [
            {"parent_slug": "sam", "name": "Sam", "person_ids": ["good"], "conflict": True,
             "no_link": False, "verdict": _verdict("confirmed", 0.92)},
            {"parent_slug": "sam", "name": "Sam", "person_ids": ["bad"], "conflict": True,
             "no_link": False, "verdict": _verdict("wrong_person", 0.98)}]
        reconcile.decide_actions(tasks, 0.85)
        by_pid = {t["person_ids"][0]: t for t in tasks}
        self.assertEqual(by_pid["good"]["action"], "confirm")
        self.assertEqual(by_pid["good"]["via"], "conflict_resolved")
        self.assertEqual(by_pid["bad"]["action"], "detach")
        self.assertEqual(by_pid["bad"]["via"], "conflict_resolved")

    def test_ambiguous_conflict_stays_in_review(self):
        # Two confirmed under one parent: not the clean shape -> all review, no mutation.
        tasks = [
            {"parent_slug": "x", "name": "X", "person_ids": ["p1"], "conflict": True,
             "no_link": False, "verdict": _verdict("confirmed", 0.9)},
            {"parent_slug": "x", "name": "X", "person_ids": ["p2"], "conflict": True,
             "no_link": False, "verdict": _verdict("confirmed", 0.9)}]
        reconcile.decide_actions(tasks, 0.85)
        self.assertTrue(all(t["action"] == "review" for t in tasks))

    def test_consolidation_folds_children_onto_kept_link(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            people = base / "people.csv"
            cols = ["id", "public_identifier", "linkedin_url", "primary_email", "all_emails",
                    "primary_phone", "all_phones", "interaction_counts", "source_channels"]
            with people.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerow({"id": "pid-keep", "public_identifier": "patlee",
                            "primary_email": "pat@gmail.com", "all_emails": '["pat@gmail.com"]',
                            "interaction_counts": '{"gmail": 5}', "source_channels": "gmail_msgvault"})
                w.writerow({"id": "pid-sib", "public_identifier": "pat-lee",
                            "primary_email": "pat@work.com", "all_emails": '["pat@work.com"]',
                            "interaction_counts": '{"imessage": 9}', "source_channels": "imessage"})
            tasks = [
                self._task("pat", "patlee", "confirmed", 0.95, conflict=True),
                self._task("pat", "pat-lee", "wrong_person", 0.95, conflict=True)]
            tasks[0]["person_ids"] = ["pid-keep"]
            tasks[1]["person_ids"] = ["pid-sib"]
            reconcile.decide_actions(tasks, 0.85)
            out = base / "consolidate.csv"
            stats = reconcile.write_consolidations(out, tasks, people)
            self.assertEqual(stats["consolidated_parents"], 1)
            import csv as _csv
            with out.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["public_identifier"], "patlee")     # folded onto the KEPT link
            self.assertIn("pat@gmail.com", row["all_emails"])
            self.assertIn("pat@work.com", row["all_emails"])          # sibling email carried
            self.assertEqual(json.loads(row["interaction_counts"]), {"gmail": 5, "imessage": 9})  # per-channel kept
            self.assertEqual(row["rapidapi_response"], "")              # contact-only (no profile pollution)

    def test_conflict_resolution_writes_one_verify_and_rest_detach(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [self._task("sam", "samroe-7a04927", "confirmed", 0.92, conflict=True),
                     self._task("sam", "samroe", "wrong_person", 0.98, conflict=True)]
            reconcile.decide_actions(tasks, 0.85)
            reconcile.write_overrides(path, tasks)
            import csv as _csv
            with path.open() as fh:
                rows = {r["public_identifier"]: r["action"] for r in _csv.DictReader(fh)}
            self.assertEqual(rows["samroe-7a04927"], "verify")
            self.assertEqual(rows["samroe"], "detach")

    def test_override_holds_auto_and_pending_in_one_file(self):
        # Everything judged lands in the ONE decisions table: high-conf -> auto, low-conf -> pending.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "ov.csv"
            tasks = [
                self._task("a", "alice", "confirmed", 0.95),        # auto verify
                self._task("b", "bobceo", "wrong_person", 0.95),    # auto detach
                self._task("c", "carol", "wrong_person", 0.50),     # pending (low conf) -> detach
                self._task("e", "erin", "needs_review", 0.40)]      # pending -> verify (keep)
            reconcile.decide_actions(tasks, 0.85)
            stats = reconcile.write_overrides(path, tasks)
            self.assertEqual(stats["verified"], 1)
            self.assertEqual(stats["detached"], 1)
            self.assertEqual(stats["pending"], 2)
            import csv as _csv
            with path.open() as fh:
                rows = {r["public_identifier"]: r for r in _csv.DictReader(fh)}
            self.assertEqual(rows["alice"]["approved"], "auto")
            self.assertEqual(rows["carol"]["approved"], "")          # pending, in the SAME file
            self.assertEqual(rows["carol"]["action"], "detach")      # suggested action from verdict
            self.assertEqual(rows["erin"]["action"], "verify")       # needs_review -> keep, pending
            self.assertEqual(reconcile.count_pending(path), 2)

    def test_inject_section_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            md = Path(d) / "p.md"
            md.write_text("---\nname: X\n---\n\n# X (canonical)\n\nbody\n", encoding="utf-8")
            sec = reconcile.render_section(_verdict("confirmed", 0.9, reason="lines up"),
                                           {"linkedin_url": "u", "headline": "Eng"})
            reconcile.inject_section(md, sec)
            reconcile.inject_section(md, sec)  # second run must REPLACE, not duplicate
            self.assertEqual(md.read_text().count(reconcile.SECTION_ANCHOR), 1)
            self.assertIn("✅ confirmed", md.read_text())

    def test_run_no_llm_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache, pdir, rdir = (base / "facts", base / "raw", base / "cache",
                                             base / "parents", base / "reconcile")
            for p in (facts, raw, cache, pdir, rdir):
                p.mkdir()
            self._facts(facts, "pa", "Alice")
            self._facts(facts, "pc", "Carol")
            (pdir / "alice-p.md").write_text("---\nname: Alice\n---\n\n# Alice (canonical)\n\nbody\n", encoding="utf-8")
            (pdir / "carol-p.md").write_text("---\nname: Carol\n---\n\n# Carol (canonical)\n\nbody\n", encoding="utf-8")
            index_json = base / "index.json"
            index = {"slugs": {"alice-c": {"person_id": "pa"}, "carol-c": {"person_id": "pc"}},
                     "parents": {"alice-p": {"name": "Alice", "children": ["alice-c"]},
                                 "carol-p": {"name": "Carol", "children": ["carol-c"]}}}
            index_json.write_text(json.dumps(index), encoding="utf-8")
            people_csv = base / "people.csv"
            self._people_csv(people_csv, [
                {"id": "pa", "public_identifier": "alice", "linkedin_url": "https://www.linkedin.com/in/alice",
                 "headline": "Eng", "work_experiences": json.dumps([{"title": "Eng", "company_name": "Acme"}])},
                {"id": "pc", "public_identifier": "", "linkedin_url": ""}])  # Carol has no link
            man = _run_reconcile(_ns(
                index_json=index_json, people_csv=people_csv, profile_cache_dir=cache,
                facts_dir=facts, raw_dir=raw, parents_dir=pdir,
                verdicts_jsonl=rdir / "verdicts.jsonl", verdicts_csv=rdir / "verdicts.csv",
                overrides_csv=rdir / "review.csv",
                consolidate_people_csv=rdir / "consolidate-people.csv",
                confirm_threshold=0.85, model="m", reasoning_effort="high", concurrency=1,
                timeout=10, max_retries=0, dry_run=False, no_overrides=False, no_llm=True))
            self.assertEqual(man["judge"], "deterministic")
            self.assertEqual(man["no_link"], 1)                      # Carol
            self.assertEqual(man["verdicts"]["confirmed"], 1)        # Alice (offline stub)
            self.assertEqual(man["overrides"]["verified"], 1)        # Alice -> verify in the override
            self.assertTrue((rdir / "verdicts.csv").exists())
            self.assertTrue((rdir / "applied.csv").exists())
            # people.csv is NOT mutated by reconcile anymore (the merge applies the override).
            with people_csv.open() as fh:
                self.assertNotIn("linkedin_verified", next(csv.reader(fh)))
            self.assertIn("LinkedIn identity", (pdir / "alice-p.md").read_text())

    def test_contact_only_people_are_reviewable_but_never_research_eligible(self):
        # A real person with no attached LinkedIn used to be stripped out of verdicts.jsonl, so
        # they appeared in NO queue — the review model builds its rows from that file. They must
        # be reviewable, and must NOT become paid-research subjects: their free verdict always
        # carries linkedin_plausibly_absent, which include_plausibly_absent accepts with no
        # worth or recommend gate.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache, pdir, rdir = (base / "facts", base / "raw", base / "cache",
                                             base / "parents", base / "reconcile")
            for p in (facts, raw, cache, pdir, rdir):
                p.mkdir()
            self._facts(facts, "pa", "Jordan Bravo")
            self._facts(facts, "pc", "Casey Delta")
            for slug, name in (("jordan-p", "Jordan Bravo"), ("casey-p", "Casey Delta")):
                (pdir / f"{slug}.md").write_text(f"---\nname: {name}\n---\n\n# {name} (canonical)\n",
                                                 encoding="utf-8")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {"jordan-c": {"person_id": "pa"}, "casey-c": {"person_id": "pc"}},
                "parents": {"jordan-p": {"name": "Jordan Bravo", "children": ["jordan-c"]},
                            "casey-p": {"name": "Casey Delta", "children": ["casey-c"]}}}),
                encoding="utf-8")
            people_csv = base / "people.csv"
            self._people_csv(people_csv, [
                {"id": "pa", "public_identifier": "jordanbravo",
                 "linkedin_url": "https://www.linkedin.com/in/jordanbravo", "headline": "Eng",
                 "work_experiences": json.dumps([{"title": "Eng", "company_name": "Acme"}])},
                # Casey is the PR #330 shape: admitted on contact fields alone, no LinkedIn.
                {"id": "pc", "public_identifier": "", "linkedin_url": "",
                 "full_name": "Casey Delta", "primary_email": "casey@example.com",
                 "primary_phone": "+15550100"}])
            review_csv = rdir / "review.csv"
            _run_reconcile(_ns(
                index_json=index_json, people_csv=people_csv, profile_cache_dir=cache,
                facts_dir=facts, raw_dir=raw, parents_dir=pdir,
                verdicts_jsonl=rdir / "verdicts.jsonl", verdicts_csv=rdir / "verdicts.csv",
                overrides_csv=review_csv,
                consolidate_people_csv=rdir / "consolidate-people.csv",
                confirm_threshold=0.85, model="m", reasoning_effort="high", concurrency=1,
                timeout=10, max_retries=0, dry_run=False, no_overrides=False, no_llm=True))

            verdicts = list(common.read_jsonl(rdir / "verdicts.jsonl"))
            by_parent = {r["parent_slug"]: r for r in verdicts}
            self.assertEqual(set(by_parent), {"jordan-p", "casey-p"})   # Casey is IN the artifact
            casey = by_parent["casey-p"]
            self.assertTrue(casey["no_link"])
            self.assertTrue(casey["verdict"]["linkedin_plausibly_absent"])
            self.assertEqual(casey["match_emails"], ["casey@example.com"])

            # The flat identity CSV stays identity-only (a no-link row has no LinkedIn columns).
            with (rdir / "verdicts.csv").open(newline="", encoding="utf-8") as fh:
                self.assertEqual([r["parent_slug"] for r in csv.DictReader(fh)], ["jordan-p"])

            # Reviewable: the review model renders a card for Casey.
            model_parents, _ = web_model.build_parents(rdir / "verdicts.jsonl", review_csv)
            self.assertIn("casey-p", {p["slug"] for p in model_parents})

            # NOT research-eligible, even on the synthetic (include_plausibly_absent) path.
            for include_absent in (False, True):
                subset = dresearch.eligible_subset(verdicts, 0.85, {},
                                                   include_plausibly_absent=include_absent)
                self.assertNotIn("casey-p", {r.get("parent_slug") for r in subset})

    def test_dry_run_estimates_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, cache = base / "facts", base / "cache"
            facts.mkdir(); cache.mkdir()
            self._facts(facts, "pa", "Alice")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({"slugs": {"alice-c": {"person_id": "pa"}},
                "parents": {"alice-p": {"name": "Alice", "children": ["alice-c"]}}}), encoding="utf-8")
            people_csv = base / "people.csv"
            self._people_csv(people_csv, [{"id": "pa", "public_identifier": "alice",
                "linkedin_url": "https://www.linkedin.com/in/alice", "headline": "Eng",
                "work_experiences": json.dumps([{"title": "Eng", "company_name": "Acme"}])}])
            man = _run_reconcile(_ns(index_json=index_json, people_csv=people_csv, profile_cache_dir=cache,
                facts_dir=facts, raw_dir=base / "raw", parents_dir=base / "parents",
                verdicts_jsonl=base / "r" / "v.jsonl", verdicts_csv=base / "r" / "v.csv",
                overrides_csv=base / "r" / "ov.csv",
                consolidate_people_csv=base / "r" / "consolidate.csv",
                confirm_threshold=0.85, model="m", reasoning_effort="high", concurrency=1,
                timeout=10, max_retries=0, dry_run=True, no_overrides=True, no_llm=True))
            self.assertEqual(man["status"], "dry_run")
            self.assertEqual(man["judgeable"], 1)
            self.assertFalse((base / "r").exists())  # dry-run writes nothing


class TestApplyRetargets(unittest.TestCase):
    """Re-attach a correct LinkedIn: enrich (stubbed) + carry the contact's identity."""

    def test_builds_enriched_row_carrying_contact(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ov = base / "ov.csv"
            with ov.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writeheader()
                w.writerow({"public_identifier": "bobceo", "action": "retarget", "approved": "yes",
                            "new_linkedin_url": "https://www.linkedin.com/in/bob-real",
                            "new_public_identifier": "bob-real", "person_id": "pid-bob"})
                w.writerow({"public_identifier": "carol", "action": "retarget", "approved": "",  # pending -> skip
                            "new_linkedin_url": "https://www.linkedin.com/in/carol-real"})
            people = base / "people.csv"
            cols = ["id", "public_identifier", "linkedin_url", "full_name", "primary_email",
                    "all_emails", "primary_phone", "all_phones", "interaction_counts",
                    "last_interaction", "source_channels"]
            with people.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerow({"id": "pid-bob", "public_identifier": "bobceo", "full_name": "Bob",
                            "primary_email": "bob@x.com", "interaction_counts": '{"gmail": 9}',
                            "source_channels": "gmail_msgvault"})
            fake = {"data": {"raw": 1}, "normalized_profile": {"success": True}, "from_cache": True, "error": ""}
            with mock.patch.object(retargets, "rapidapi_profile", return_value=fake), \
                 mock.patch.object(retargets, "normalize_rapidapi", return_value={}), \
                 mock.patch.object(retargets, "merge_provider_profile",
                                   return_value={"public_identifier": "bob-real", "full_name": "Bob Right",
                                                 "rapidapi_response": '{"raw":1}'}):
                man = _run_retargets(_ns(overrides_csv=ov, people_csv=people,
                    profile_cache_dir=base / "cache", out_csv=base / "retarget-people.csv"))
            self.assertEqual(man["enriched"], 1)        # only the approved one
            self.assertEqual(man["cache_hits"], 1)
            import csv as _csv
            with (base / "retarget-people.csv").open() as fh:
                rows = list(_csv.DictReader(fh))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["public_identifier"], "bob-real")
            self.assertEqual(rows[0]["primary_email"], "bob@x.com")     # contact identity carried
            self.assertEqual(rows[0]["interaction_counts"], '{"gmail": 9}')
            # carol: pending proposal whose old identity resolves nowhere -> stranded, surfaced
            self.assertEqual(man["stranded_count"], 1)
            self.assertEqual(man["stranded"][0]["old"], "carol")

    def test_realized_retarget_is_finalized_and_not_reapplied(self):
        # After the fan-in merge realizes a retarget (the NEW pub lives in
        # people.csv), the source marker must be closed out (approved=yes) and
        # skipped — not re-enriched, not counted as pending forever.
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            ov = base / "ov.csv"
            with ov.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writeheader()
                # proposal whose new pub was ALREADY merged into people.csv
                w.writerow({"public_identifier": "ada-old-42", "action": "retarget",
                            "approved": "",  # never explicitly approved
                            "new_linkedin_url": "https://www.linkedin.com/in/ada-real"})
            people = base / "people.csv"
            cols = ["id", "public_identifier", "linkedin_url", "full_name", "primary_email",
                    "all_emails", "primary_phone", "all_phones", "interaction_counts",
                    "last_interaction", "source_channels"]
            with people.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerow({"id": "pid-ada", "public_identifier": "ada-real",
                            "full_name": "Ada"})
            with mock.patch.object(retargets, "rapidapi_profile",
                                   side_effect=AssertionError("realized rows must not re-enrich")):
                man = _run_retargets(_ns(overrides_csv=ov, people_csv=people,
                    profile_cache_dir=base / "cache", out_csv=base / "retarget-people.csv"))
            self.assertEqual(man["finalized_applied"], 1)
            self.assertEqual(man["enriched"], 0)
            self.assertEqual(man["stranded_count"], 0)
            import csv as _csv
            with ov.open() as fh:
                saved = {r["public_identifier"]: r for r in _csv.DictReader(fh)}
            self.assertEqual(saved["ada-old-42"]["approved"], "yes")  # marker closed out


class TestReconcileDeepResearch(unittest.TestCase):
    def test_queue_sends_dossier_identifiers_and_owner_context(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts = base / "facts"
            facts.mkdir()
            (facts / "pid-ben.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Benjamin Chen",
                "aliases": ["Ben Chen"],
                "relationship_to_owner": "old friend",
                "employers": [],
                "school": "",
                "location": "Louisiana",
                "topics": ["music"],
                "identifiers": ["bencchen89@gmail.com"],
                "shared_context": [{
                    "overlap": "location",
                    "detail": "Bay Area social circle",
                    "evidence": "messages",
                }],
            }}) + "\n", encoding="utf-8")
            subset = [{
                "parent_slug": "benjamin-chen",
                "name": "Benjamin Chen",
                "person_ids": ["pid-ben"],
                "candidate_key": "wrong-ben",
                "linkedin": {"linkedin_url": "https://www.linkedin.com/in/wrong-ben"},
                "verdict": {"reason": "career timeline contradiction"},
            }]
            people = {"pid-ben": {
                "primary_email": "",
                "all_emails": "",
                "primary_phone": "",
                "all_phones": "",
            }}
            owner = {
                "name": "Arthur Chen",
                "education": [{"school": "UCLA", "start": 2007, "end": 2010}],
                "work": [],
                "locations": ["Palo Alto, California, United States"],
            }
            from unittest import mock
            with mock.patch.object(dresearch, "load_owner", return_value=owner):
                (row,) = dresearch.build_queue(subset, people, facts, base / "raw")
            self.assertIn("Also known as: Ben Chen", row["bio"])
            self.assertIn("bencchen89@gmail.com", row["bio"])
            self.assertIn("Bay Area social circle", row["bio"])
            self.assertIn("MAILBOX OWNER BACKGROUND (me): Arthur Chen", row["known_info"])
            self.assertIn("Palo Alto", row["known_info"])

    def test_no_work_overwrites_queue_and_fixed_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            verdicts = base / "verdicts.jsonl"
            verdicts.write_text("", encoding="utf-8")
            manifest_path = base / "research" / "manifest.json"
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                result = _run_dresearch(_ns(
                    verdicts_jsonl=verdicts, people_csv=base / "people.csv",
                    overrides_csv=base / "review.csv", facts_dir=base / "facts",
                    raw_dir=base / "raw", processor="core2x", confirm_threshold=0.85,
                    budget=0.0, approve=False, dry_run=True,
                    include_plausibly_absent=False, include_candidates=False,
                    manifest=manifest_path,
                ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(result["status"], "noop")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual((manifest["stage"], manifest["status"]),
                             ("enrich", "research_complete"))
            self.assertEqual(manifest["counts"],
                             {"total": 0, "completed": 0, "pending": 0, "failed": 0})
            self.assertEqual((base / "research" / "research_queue.csv").read_text().splitlines()[0],
                             ",".join(dresearch.QUEUE_FIELDS))

    def legacy_dry_run_counts_no_link_import_candidate_for_worth_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir()
            raw.mkdir()
            cache.mkdir()
            pid = "candidate:email:professor@example.com"
            sibling_pid = "candidate:email:professor.alias@example.com"
            for person_id in (pid, sibling_pid):
                (facts / f"{person_id}.jsonl").write_text(json.dumps({"facts": {
                    "canonical_name": "Professor Example",
                    "relationship_to_owner": "former professor",
                    "network_worth": {"decision": "maybe", "reason": "profession unknown"},
                }}) + "\n", encoding="utf-8")
                write_json(raw / f"{person_id}.json", {
                    "person_id": person_id,
                    "messages": [{
                        "at": "2020-01-01T00:00:00Z",
                        "direction": "from_them",
                        "text": "Happy to advise you on the course project.",
                    }],
                })
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {
                    "professor-child": {"person_id": pid},
                    "professor-alias-child": {"person_id": sibling_pid},
                },
                "parents": {
                    "professor-parent": {
                        "name": "Professor Example",
                        "children": ["professor-child", "professor-alias-child"],
                    },
                },
            }), encoding="utf-8")
            result = _run_reconcile(_ns(
                index_json=index_json,
                people_csv=base / "people.csv",
                profile_cache_dir=cache,
                facts_dir=facts,
                raw_dir=raw,
                parents_dir=base / "parents",
                verdicts_jsonl=base / "reconcile" / "verdicts.jsonl",
                verdicts_csv=base / "reconcile" / "verdicts.csv",
                overrides_csv=base / "review.csv",
                consolidate_people_csv=base / "consolidate.csv",
                confirm_threshold=0.7,
                detach_threshold=0.85,
                model="m",
                reasoning_effort="high",
                concurrency=1,
                timeout=10,
                max_retries=0,
                dry_run=True,
                no_overrides=False,
                no_llm=False,
            ))
            self.assertEqual(result["identity_judgeable"], 0)
            self.assertEqual(result["worth_only_judgeable"], 1)
            self.assertEqual(result["worth_only_machine_stable"], 0)
            self.assertEqual(result["judgeable"], 1)

            reconcile._write_override_rows(base / "review.csv", {
                pid: {
                    **{key: "" for key in reconcile.OVERRIDE_COLUMNS},
                    "public_identifier": pid,
                    "person_id": pid,
                    "llm_worth": "yes",
                },
                sibling_pid: {
                    **{key: "" for key in reconcile.OVERRIDE_COLUMNS},
                    "public_identifier": sibling_pid,
                    "person_id": sibling_pid,
                    "llm_worth": "no",
                },
            })
            stable = _run_reconcile(_ns(
                index_json=index_json,
                people_csv=base / "people.csv",
                profile_cache_dir=cache,
                facts_dir=facts,
                raw_dir=raw,
                parents_dir=base / "parents",
                verdicts_jsonl=base / "reconcile" / "verdicts.jsonl",
                verdicts_csv=base / "reconcile" / "verdicts.csv",
                overrides_csv=base / "review.csv",
                consolidate_people_csv=base / "consolidate.csv",
                confirm_threshold=0.7,
                detach_threshold=0.85,
                model="m",
                reasoning_effort="high",
                concurrency=1,
                timeout=10,
                max_retries=0,
                dry_run=True,
                no_overrides=False,
                no_llm=False,
            ))
            self.assertEqual(stable["worth_only_judgeable"], 0)
            self.assertEqual(stable["worth_only_machine_stable"], 1)

            reconcile._write_override_rows(base / "review.csv", {pid: {
                **{key: "" for key in reconcile.OVERRIDE_COLUMNS},
                "public_identifier": pid,
                "person_id": pid,
                "network_worth": "yes",
            }})
            mixed = _run_reconcile(_ns(
                index_json=index_json,
                people_csv=base / "people.csv",
                profile_cache_dir=cache,
                facts_dir=facts,
                raw_dir=raw,
                parents_dir=base / "parents",
                verdicts_jsonl=base / "reconcile" / "verdicts.jsonl",
                verdicts_csv=base / "reconcile" / "verdicts.csv",
                overrides_csv=base / "review.csv",
                consolidate_people_csv=base / "consolidate.csv",
                confirm_threshold=0.7,
                detach_threshold=0.85,
                model="m",
                reasoning_effort="high",
                concurrency=1,
                timeout=10,
                max_retries=0,
                dry_run=True,
                no_overrides=False,
                no_llm=False,
            ))
            self.assertEqual(mixed["worth_only_judgeable"], 1)
            self.assertEqual(mixed["worth_only_human_preserved"], 0)
            self.assertEqual(mixed["worth_only_machine_stable"], 0)

            reconcile._write_override_rows(base / "review.csv", {
                pid: {
                    **{key: "" for key in reconcile.OVERRIDE_COLUMNS},
                    "public_identifier": pid,
                    "person_id": pid,
                    "network_worth": "yes",
                },
                sibling_pid: {
                    **{key: "" for key in reconcile.OVERRIDE_COLUMNS},
                    "public_identifier": sibling_pid,
                    "person_id": sibling_pid,
                    "network_worth": "yes",
                },
            })
            preserved = _run_reconcile(_ns(
                index_json=index_json,
                people_csv=base / "people.csv",
                profile_cache_dir=cache,
                facts_dir=facts,
                raw_dir=raw,
                parents_dir=base / "parents",
                verdicts_jsonl=base / "reconcile" / "verdicts.jsonl",
                verdicts_csv=base / "reconcile" / "verdicts.csv",
                overrides_csv=base / "review.csv",
                consolidate_people_csv=base / "consolidate.csv",
                confirm_threshold=0.7,
                detach_threshold=0.85,
                model="m",
                reasoning_effort="high",
                concurrency=1,
                timeout=10,
                max_retries=0,
                dry_run=True,
                no_overrides=False,
                no_llm=False,
            ))
            self.assertEqual(preserved["worth_only_judgeable"], 0)
            self.assertEqual(preserved["worth_only_human_preserved"], 1)
            self.assertEqual(preserved["worth_only_machine_stable"], 0)

    # --- Part 2: evidence-based worth re-judge for the verified-LinkedIn limbo class -----------

    # A limbo person: candidate id, machine worth Maybe, LinkedIn kept/verified at the link level
    # (approved=auto), with a hydrated profile in the cache. Tuple: (pid, cache_pub, headline,
    # title, company, location, user_mark).
    _LIMBO = [
        ("candidate:email:promote@example.com", "promote-li", "Staff Engineer at RealCo",
         "Staff Engineer", "RealCo", "San Francisco", ""),
        ("candidate:email:reject@example.net", "reject-li", "SDR at VendorCo",
         "SDR", "VendorCo", "New York", ""),
        ("candidate:email:stay@example.com", "stay-li", "Student",
         "Student", "State University", "Los Angeles", ""),
        # A user-marked row must never be re-judged, even though its LinkedIn is verified.
        ("candidate:email:usermark@example.net", "user-li", "Anything",
         "Role", "Company", "Austin", "yes"),
    ]
    # The mocked judge's decisive worth per candidate id.
    _JUDGE = {
        "candidate:email:promote@example.com": ("yes", "verified staff engineer — real relationship"),
        "candidate:email:reject@example.net": ("no", "vendor SDR account, purely transactional"),
        "candidate:email:stay@example.com": ("maybe", "still ambiguous even with the profile"),
        "candidate:email:usermark@example.net": ("no", "should never be written — user owns this"),
    }

    def _build_limbo(self, base: Path) -> tuple[Path, dict]:
        facts, raw, cache = base / "facts", base / "raw", base / "cache"
        for directory in (facts, raw, cache):
            directory.mkdir()
        slugs: dict[str, dict] = {}
        parents_index: dict[str, dict] = {}
        overrides: dict[str, dict] = {}
        for pid, pub, headline, title, company, location, user_mark in self._LIMBO:
            handle = pid.split(":")[-1].split("@")[0]
            (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": handle.title(),
                "relationship_to_owner": "former colleague",
                "network_worth": {"decision": "maybe",
                                  "reason": "real person but no professional context"},
            }}) + "\n", encoding="utf-8")
            write_json(raw / f"{pid}.json", {"person_id": pid, "messages": [{
                "at": "2020-01-01T00:00:00Z", "direction": "from_them",
                "text": f"Good to connect, this is {handle}.",
            }]})
            (cache / f"{pub}.json").write_text(json.dumps({
                "public_identifier": pub, "linkedin_url": f"https://www.linkedin.com/in/{pub}",
                "normalized_profile": {"success": True, "full_name": handle.title(),
                                       "headline": headline, "location_str": location,
                                       "experiences": [{"title": title, "company_name": company}],
                                       "education": []},
                "raw_response": {"placeholder": True},
            }), encoding="utf-8")
            child = f"{handle}-child"
            slugs[child] = {"person_id": pid}
            parents_index[f"{handle}-parent"] = {"name": handle.title(), "children": [child]}
            row = {**{col: "" for col in reconcile.OVERRIDE_COLUMNS},
                   "public_identifier": pid, "person_id": pid,
                   "action": "verify", "approved": "auto",
                   "linkedin_url": f"https://www.linkedin.com/in/{pub}",
                   "llm_worth": "maybe",
                   "llm_worth_reason": "real person but no professional context"}
            if user_mark:
                row["network_worth"] = user_mark
            overrides[pid] = row
        index_json = base / "index.json"
        index_json.write_text(json.dumps({"slugs": slugs, "parents": parents_index}),
                              encoding="utf-8")
        review = base / "review.csv"
        reconcile._write_override_rows(review, overrides)
        args = {
            "index_json": index_json, "people_csv": base / "people.csv",
            "profile_cache_dir": cache, "facts_dir": facts, "raw_dir": raw,
            "parents_dir": base / "parents",
            "verdicts_jsonl": base / "reconcile" / "verdicts.jsonl",
            "verdicts_csv": base / "reconcile" / "verdicts.csv",
            "overrides_csv": review, "consolidate_people_csv": base / "consolidate.csv",
            "confirm_threshold": 0.7, "detach_threshold": 0.85, "model": "m",
            "reasoning_effort": "high", "concurrency": 1, "timeout": 10, "max_retries": 0,
            "no_overrides": False,
        }
        return review, args

    def legacy_limbo_dry_run_reports_reprofile_count_and_cost(self):
        with tempfile.TemporaryDirectory() as d:
            _, args = self._build_limbo(Path(d))
            dry = _run_reconcile(_ns(**args, dry_run=True, no_llm=False))
            # 3 limbo maybes carry a verified profile; the user-marked row is human-decided so it
            # is neither re-judged nor counted (spend is gated to the machine-Maybe limbo class).
            self.assertEqual(dry["limbo_worth_reprofile"], 3)
            self.assertEqual(dry["worth_only_judgeable"], 3)
            self.assertGreater(dry["estimated_cost_usd_high"], 0)
            self.assertEqual(dry["identity_judgeable"], 0)

    def _fake_judge_task(self):
        async def judge(client, task, owner_block, *, model, effort, semaphore, max_retries):
            pid = (task.get("worth_person_ids") or task.get("person_ids") or [""])[0]
            # The re-judge MUST receive the verified professional context, not the bare
            # "no LinkedIn attached" prompt — assert the evidence block reached the model.
            prompt = reconcile.judge_prompt(task, owner_block)
            assert "ALREADY VERIFIED" in prompt, f"no verified block for {pid}"
            decision, reason = self._JUDGE.get(pid, ("maybe", "unknown"))
            return {"verdict": {
                "verdict": "needs_review", "confidence": 0.0,
                "supporting_evidence": [], "contradicting_evidence": [],
                "linkedin_plausibly_absent": False, "recommend_deep_research": False,
                "reason": "linkedin already verified",
                "spam_contact": False, "spam_confidence": 0.0, "spam_reason": "",
                "network_worth": {"decision": decision, "reason": reason},
            }, "usage": {"input_tokens": 5, "output_tokens": 5, "reasoning_tokens": 0}, "error": ""}
        return judge

    def legacy_limbo_reprofile_repiles_decisively_and_never_touches_user_marks(self):
        with tempfile.TemporaryDirectory() as d:
            review, args = self._build_limbo(Path(d))

            class _Client:
                async def close(self):
                    return None

            with mock.patch.object(reconcile, "judge_task", self._fake_judge_task()), \
                    mock.patch.object(reconcile, "make_async_client", lambda **kw: _Client()), \
                    mock.patch.object(reconcile, "load_env", lambda: None):
                manifest = _run_reconcile(_ns(**args, dry_run=False, no_llm=False))
            self.assertEqual(manifest["judge"], "llm")
            rows = reconcile.load_override_rows(review)
            # Decisive outcomes re-pile: machine yes -> Yes, machine no -> No, with the new reason.
            self.assertEqual((rows["candidate:email:promote@example.com"]["llm_worth"],
                              rows["candidate:email:promote@example.com"]["llm_worth_reason"]),
                             ("yes", "verified staff engineer — real relationship"))
            self.assertEqual((rows["candidate:email:reject@example.net"]["llm_worth"],
                              rows["candidate:email:reject@example.net"]["llm_worth_reason"]),
                             ("no", "vendor SDR account, purely transactional"))
            # Still-maybe stays pending.
            self.assertEqual(rows["candidate:email:stay@example.com"]["llm_worth"], "maybe")
            # The user-marked row is never re-judged: its worth mark and (stale) llm_worth stand.
            self.assertEqual(rows["candidate:email:usermark@example.net"]["network_worth"], "yes")
            self.assertEqual(rows["candidate:email:usermark@example.net"]["llm_worth"], "maybe")
            # Every link-level decision is preserved throughout (this pass judges worth only).
            for pid, *_ in self._LIMBO:
                self.assertEqual((rows[pid]["action"], rows[pid]["approved"]), ("verify", "auto"))

    def test_limbo_no_llm_leaves_them_maybe(self):
        with tempfile.TemporaryDirectory() as d:
            review, args = self._build_limbo(Path(d))
            # Deterministic path never auto-promotes without a judge.
            _run_reconcile(_ns(**args, dry_run=False, no_llm=True))
            rows = reconcile.load_override_rows(review)
            for pid, *_ in self._LIMBO:
                self.assertEqual(rows[pid]["llm_worth"], "maybe", pid)

    """Phase 3 escalation: subset selection + explicit cost gate (no spend)."""

    def test_eligible_subset_filters(self):
        verdicts = [
            {"parent_slug": "a", "verdict": _verdict("wrong_person", 0.95, dr=True)},                 # eligible
            {"parent_slug": "b", "verdict": _verdict("wrong_person", 0.95, dr=True, absent=True)},    # excluded: no LinkedIn
            {"parent_slug": "c", "verdict": _verdict("wrong_person", 0.5, dr=True)},                  # excluded: low conf
            {"parent_slug": "d", "verdict": _verdict("wrong_person", 0.95, dr=False)},               # excluded: not recommended
            {"parent_slug": "e", "verdict": _verdict("confirmed", 0.99, dr=True)}]                    # excluded: not wrong
        self.assertEqual(len(dresearch.eligible_subset(verdicts, 0.85)), 1)

    def test_eligible_subset_skips_detaches_whose_parent_kept_a_link(self):
        # Conflict-resolved: parent "x" kept a confirmed LinkedIn AND detached a sibling.
        # The detached sibling is the same person -> no need to research it.
        verdicts = [
            {"parent_slug": "x", "verdict": _verdict("confirmed", 0.92)},                 # kept link
            {"parent_slug": "x", "verdict": _verdict("wrong_person", 0.95, dr=True)},     # sibling -> SKIP
            {"parent_slug": "y", "verdict": _verdict("wrong_person", 0.95, dr=True)}]     # parent has no kept link -> research
        elig = dresearch.eligible_subset(verdicts, 0.85)
        self.assertEqual(len(elig), 1)
        self.assertEqual(elig[0]["parent_slug"], "y")

    def test_eligible_subset_skips_user_excluded(self):
        # An X-ed-out person must never be deep-researched / re-attached, even though the
        # model recommends it (unlike a detach, which IS eligible for recovery).
        verdicts = [{"parent_slug": "z", "candidate_key": "zpub",
                     "verdict": _verdict("wrong_person", 0.95, dr=True)}]
        self.assertEqual(len(dresearch.eligible_subset(verdicts, 0.85)), 1)         # baseline: eligible
        ov = {"zpub": {"action": "exclude", "approved": "yes"}}
        self.assertEqual(dresearch.eligible_subset(verdicts, 0.85, ov), [])         # excluded: skipped

    def test_cost_gate_blocks_over_budget(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            recs = [{"parent_slug": f"p{i}", "name": f"N{i}", "person_ids": [f"x{i}"],
                     "linkedin": {"linkedin_url": "u"},
                     "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")} for i in range(600)]
            vj.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                man = _run_dresearch(_ns(
                    verdicts_jsonl=vj, people_csv=base / "nope.csv",
                    overrides_csv=base / "nope_ov.csv",
                    facts_dir=base / "f", raw_dir=base / "r", processor="core2x",
                    confirm_threshold=0.85, budget=25.0, approve=True, dry_run=False,
                ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(man["status"], "needs_approval")   # 600 * $0.05 = $30 > $25
            self.assertGreater(man["estimated_usd"], 25)

    def test_cost_gate_requires_approval_under_budget(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            vj.write_text(json.dumps({
                "parent_slug": "p1", "candidate_key": "wrong", "name": "N1",
                "person_ids": ["x1"], "linkedin": {"linkedin_url": "u"},
                "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong"),
            }) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                manifest = _run_dresearch(_ns(
                    verdicts_jsonl=vj, people_csv=base / "missing.csv",
                    overrides_csv=base / "overrides.csv", facts_dir=base / "facts",
                    raw_dir=base / "raw", processor="core2x", confirm_threshold=0.85,
                    budget=25.0, approve=False, dry_run=False,
                    include_plausibly_absent=False,
                ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(manifest["status"], "needs_approval")
            self.assertLess(manifest["estimated_usd"], 25)

    def test_dry_run_prices_only_net_new_handles(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            recs = [
                {"parent_slug": "pending", "candidate_key": "wrong-a", "name": "Pending A",
                 "person_ids": ["x1"], "linkedin": {"linkedin_url": "u1"},
                 "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")},
                {"parent_slug": "pending", "candidate_key": "wrong-b", "name": "Pending B",
                 "person_ids": ["x2"], "linkedin": {"linkedin_url": "u2"},
                 "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")},
                {"parent_slug": "complete", "candidate_key": "wrong-c", "name": "Complete",
                 "person_ids": ["x3"], "linkedin": {"linkedin_url": "u3"},
                 "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong")},
            ]
            vj.write_text("\n".join(json.dumps(row) for row in recs) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            completed = dresearch.DR_OUT_DIR / "complete" / "01_research_parallel.json"
            completed.parent.mkdir(parents=True)
            completed.write_text("{}\n", encoding="utf-8")
            try:
                manifest = _run_dresearch(_ns(
                    verdicts_jsonl=vj, people_csv=base / "missing.csv",
                    overrides_csv=base / "overrides.csv", facts_dir=base / "facts",
                    raw_dir=base / "raw", processor="core2x", confirm_threshold=0.85,
                    budget=0.0, approve=False, dry_run=True,
                    include_plausibly_absent=False, include_candidates=False,
                ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(manifest["eligible"], 3)
            self.assertEqual(manifest["would_submit"], 1)
            self.assertEqual(manifest["reused_completed"], 1)
            self.assertEqual(manifest["duplicate_handles"], 1)
            self.assertEqual(manifest["estimated_usd"], 0.05)

    def test_cost_gate_runs_only_when_approved_under_budget(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            vj = base / "verdicts.jsonl"
            vj.write_text(json.dumps({
                "parent_slug": "p1", "candidate_key": "wrong", "name": "N1",
                "person_ids": ["x1"], "linkedin": {"linkedin_url": "u"},
                "verdict": _verdict("wrong_person", 0.95, dr=True, reason="wrong"),
            }) + "\n", encoding="utf-8")
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = base / "research"
            dresearch.QUEUE_CSV = dresearch.DR_OUT_DIR / "research_queue.csv"
            try:
                # The in-process research call (no subprocess): the gate having
                # passed means run_research is invoked exactly once and its
                # returned payload drives the receipt.
                with mock.patch.object(
                    dresearch,
                    "run_research",
                    return_value={"status": "completed",
                                  "counts": {"results_fetched": 1, "errors": 0}},
                ) as run_mock:
                    manifest = _run_dresearch(_ns(
                        verdicts_jsonl=vj, people_csv=base / "missing.csv",
                        overrides_csv=base / "overrides.csv", facts_dir=base / "facts",
                        raw_dir=base / "raw", processor="core2x", confirm_threshold=0.85,
                        budget=1.0, approve=True, dry_run=False,
                        include_plausibly_absent=False,
                    ))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(manifest["status"], "ran")
            run_mock.assert_called_once()

    def test_invalid_budget_cannot_bypass_gate(self):
        manifest = _run_dresearch(_ns(budget=float("nan")))
        self.assertEqual(manifest["status"], "invalid_budget")
        with self.assertRaises(SystemExit):
            dresearch.build_parser().parse_args(["--budget", "nan"])

    def test_retarget_reads_canonical_parallel_linkedin_shape(self):
        notes = "Matched employer and location " + ("with supporting evidence " * 20)
        profile = {
            "social": {"linkedin_url": "https://www.linkedin.com/in/right-person"},
            "metadata": {"research_notes": notes},
        }
        self.assertEqual(
            dresearch._find_linkedin(profile),
            "https://www.linkedin.com/in/right-person",
        )
        self.assertEqual(dresearch._find_reason(profile), f"deep research: {notes}")


class TestJudgedResearchProposals(unittest.TestCase):
    """A deep-research retarget carries its OWN confidence and is JUDGED before it lands, so a
    guess the research could not verify doesn't stick silently. Mirrors Eugene Wang: a Gmail-only
    contact whose paid research guessed a namesake LinkedIn at low confidence, admitting it could
    not verify the address. All fictional (example.com) — no live LLM/network."""

    def _research_json(self, out_dir: Path, handle: str, profile: dict) -> None:
        (out_dir / handle).mkdir(parents=True, exist_ok=True)
        (out_dir / handle / "01_research_parallel.json").write_text(
            json.dumps(profile), encoding="utf-8")

    def _facts(self, facts_dir: Path, pid: str, name: str, **over) -> None:
        facts_dir.mkdir(parents=True, exist_ok=True)
        facts = {"canonical_name": name, "aliases": [], "employers": [], "title": "",
                 "school": "", "field_of_study": "", "location": "", "relationship_to_owner": "friend",
                 "topics": [], "notable_events": [], "identifiers": [], "shared_context": [],
                 "confidence": 0.8}
        facts.update(over)
        (facts_dir / f"{pid}.jsonl").write_text(
            json.dumps({"chunk_index": 0, "facts": facts, "usage": {}}) + "\n", encoding="utf-8")

    # --- 1) confidence is carried from the research output, not hardcoded 0.0 -------------
    def test_find_confidence_reads_person_confidence(self):
        self.assertEqual(dresearch._find_confidence({"person": {"confidence": 0.35}}), 0.35)
        self.assertEqual(dresearch._find_confidence({"name_confidence": 0.9}), 0.9)
        self.assertIsNone(dresearch._find_confidence({"person": {}}))        # nothing usable
        self.assertIsNone(dresearch._find_confidence({"summary": {"confidence": 0.7}}))  # not identity

    def test_proposal_carries_research_confidence(self):
        # A confidently-verified proposal keeps its real confidence in the retarget row (not 0.0).
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw = base / "research", base / "facts", base / "raw"
            ov = base / "review.csv"
            self._facts(facts, "candidate:email:vera@example.com", "Vera Stone",
                        employers=[{"name": "Globex", "role": "Eng", "status": "current"}])
            self._research_json(out, "vera-stone-p", {
                "person": {"full_name": "Vera Stone", "confidence": 0.92,
                           "notes": "Confirmed via LinkedIn and matching Globex employer."},
                "social": {"linkedin_url": "https://www.linkedin.com/in/verastone", "linkedin_status": "found"},
                "metadata": {"research_notes": "Employer Globex matches the dossier."}})
            subset = [{"parent_slug": "vera-stone-p", "name": "Vera Stone",
                       "person_ids": ["candidate:email:vera@example.com"], "candidate_key": "candidate:email:vera@example.com",
                       "linkedin": {}, "match_emails": ["vera@example.com"], "match_phones": []}]
            dresearch.propose_retargets_from_output(
                out, subset, ov, facts_dir=facts, raw_dir=raw, use_llm=False)
            rows = _rows_by_pub(ov)
            row = rows["candidate:email:vera@example.com"]
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(float(row["confidence"]), 0.92)          # carried, NOT 0.0
            self.assertEqual(row["new_public_identifier"], "verastone")

    def test_missing_confidence_defaults_zero(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            self._facts(facts, "candidate:email:nc@example.com", "No Conf")
            self._research_json(out, "no-conf-p", {
                "person": {"full_name": "No Conf"},                    # no confidence field
                "social": {"linkedin_url": "https://www.linkedin.com/in/noconf", "linkedin_status": "found"}})
            subset = [{"parent_slug": "no-conf-p", "name": "No Conf",
                       "person_ids": ["candidate:email:nc@example.com"], "candidate_key": "candidate:email:nc@example.com",
                       "linkedin": {}, "match_emails": [], "match_phones": []}]
            dresearch.propose_retargets_from_output(out, subset, ov, facts_dir=facts, raw_dir=raw, use_llm=False)
            rows = _rows_by_pub(ov)
            self.assertEqual(float(rows["candidate:email:nc@example.com"]["confidence"]), 0.0)

    # --- 2) sub-threshold / unverifiable proposal is deterministically rejected (--no-llm) --
    def test_eugene_case_unverifiable_low_confidence_is_rejected_not_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            pid = "candidate:email:eugene6605@example.com"
            self._facts(facts, pid, "Eugene Wang")
            # Mirrors the real Eugene output: confidence 0.35, "could not directly verify" the email.
            self._research_json(out, "eugene-wang-p", {
                "person": {"full_name": "Eugene Wang", "confidence": 0.35,
                           "notes": "Best contextual match found; could not directly verify the Gmail address."},
                "social": {"linkedin_url": "https://www.linkedin.com/in/eugenejwang", "linkedin_status": "found"},
                "metadata": {"research_notes": "Selected best contextual match; identity not confirmed."}})
            subset = [{"parent_slug": "eugene-wang-p", "name": "Eugene Wang",
                       "person_ids": [pid], "candidate_key": pid,
                       "linkedin": {}, "match_emails": ["eugene6605@example.com"], "match_phones": []}]
            res = dresearch.propose_retargets_from_output(
                out, subset, ov, facts_dir=facts, raw_dir=raw, use_llm=False)
            self.assertEqual(res["proposed"], 1)                       # row exists (NOT deleted)
            rows = _rows_by_pub(ov)
            row = rows[pid]
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(float(row["confidence"]), 0.35)          # carried, not hardcoded 0.0
            self.assertEqual(row["llm_reject"], "yes")                # judged/rejected, visible
            self.assertTrue(row["llm_reject_reason"])                 # the human sees WHY
            self.assertEqual(row["approved"], "")                     # never auto-approved

    def test_sub_threshold_confidence_alone_is_rejected_offline(self):
        # Even without an "unverified" phrase, carried confidence < 0.5 is rejected by --no-llm.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            pid = "candidate:email:low@example.com"
            self._facts(facts, pid, "Lo Conf")
            self._research_json(out, "lo-conf-p", {
                "person": {"full_name": "Lo Conf", "confidence": 0.4, "notes": "Plausible profile."},
                "social": {"linkedin_url": "https://www.linkedin.com/in/loconf", "linkedin_status": "found"}})
            subset = [{"parent_slug": "lo-conf-p", "name": "Lo Conf", "person_ids": [pid],
                       "candidate_key": pid, "linkedin": {}, "match_emails": [], "match_phones": []}]
            dresearch.propose_retargets_from_output(out, subset, ov, facts_dir=facts, raw_dir=raw, use_llm=False)
            rows = _rows_by_pub(ov)
            self.assertEqual(rows[pid]["llm_reject"], "yes")

    # --- 3) LLM-judge rejection populates llm_reject columns and does not delete the row ----
    def test_llm_judge_rejection_marks_row_without_deleting(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            pid = "candidate:email:judged@example.com"
            self._facts(facts, pid, "Judged Person")
            self._research_json(out, "judged-p", {
                "person": {"full_name": "Judged Person", "confidence": 0.8, "notes": "Looks plausible."},
                "social": {"linkedin_url": "https://www.linkedin.com/in/judged", "linkedin_status": "found"}})
            subset = [{"parent_slug": "judged-p", "name": "Judged Person", "person_ids": [pid],
                       "candidate_key": pid, "linkedin": {}, "match_emails": [], "match_phones": []}]
            # LLM judge says wrong_person -> row is marked llm_reject=yes, not removed.
            rejecting = _verdict("wrong_person", 0.9, reason="no non-name corroboration")
            with mock.patch.object(dresearch, "judge_research_proposal", return_value=rejecting) as jm:
                dresearch.propose_retargets_from_output(
                    out, subset, ov, facts_dir=facts, raw_dir=raw, use_llm=True)
            jm.assert_called_once()
            rows = _rows_by_pub(ov)
            row = rows[pid]
            self.assertEqual(row["action"], "retarget")               # row still present
            self.assertEqual(row["llm_reject"], "yes")
            self.assertIn("corroboration", row["llm_reject_reason"])
            self.assertEqual(row["approved"], "")

    def test_llm_judge_confirmation_leaves_reject_columns_clear(self):
        from unittest import mock
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            pid = "candidate:email:good@example.com"
            self._facts(facts, pid, "Good Match")
            self._research_json(out, "good-p", {
                "person": {"full_name": "Good Match", "confidence": 0.95, "notes": "Employer matches."},
                "social": {"linkedin_url": "https://www.linkedin.com/in/goodmatch", "linkedin_status": "found"}})
            subset = [{"parent_slug": "good-p", "name": "Good Match", "person_ids": [pid],
                       "candidate_key": pid, "linkedin": {}, "match_emails": [], "match_phones": []}]
            confirming = _verdict("confirmed", 0.9, reason="employer + location match")
            with mock.patch.object(dresearch, "judge_research_proposal", return_value=confirming):
                dresearch.propose_retargets_from_output(
                    out, subset, ov, facts_dir=facts, raw_dir=raw, use_llm=True,
                    confirm_threshold=0.7)
            rows = _rows_by_pub(ov)
            row = rows[pid]
            self.assertEqual(row["llm_reject"], "")                    # confident confirm -> not rejected
            self.assertEqual(row["approved"], "")                     # still pending user approval (never auto)

    # --- research-proposal deterministic verdict semantics -------------------------------
    def test_research_proposal_deterministic_never_auto_confirms(self):
        # A verified-looking, high-confidence guess still is NOT auto-confirmed offline -> needs_review.
        task = reconcile.research_proposal_task(
            {"relationship": "", "title": "", "employers": [], "school": "", "location": "",
             "topics": [], "shared_context": [], "from_me": [], "from_them": []},
            {"linkedin_url": "https://www.linkedin.com/in/x", "has_profile": True},
            name="X", confidence=0.9, unverified=False)
        v = reconcile.deterministic_verdict(task)
        self.assertEqual(v["verdict"], "needs_review")                # not confirmed
        self.assertNotEqual(v["verdict"], "confirmed")

    def test_research_proposal_prompt_requires_non_name_signal(self):
        task = reconcile.research_proposal_task(
            {"relationship": "", "title": "", "employers": [], "school": "", "location": "",
             "topics": [], "shared_context": [], "from_me": [], "from_them": []},
            {"linkedin_url": "https://www.linkedin.com/in/eugenejwang", "full_name": "Eugene Wang",
             "headline": "", "location": "", "experiences": [], "education": []},
            name="Eugene Wang", confidence=0.35, unverified=True)
        prompt = reconcile.judge_prompt(task, "")
        self.assertIn("SPECULATIVE", prompt)
        self.assertIn("NON-NAME", prompt)
        self.assertIn("wrong_person", prompt)

    # --- 5) a judge-rejected retarget does not permanently count as decided ---------------
    def test_rejected_retarget_is_not_decided_and_can_be_reresearched(self):
        verdicts = [{"parent_slug": "z", "candidate_key": "candidate:email:z@example.com",
                     "verdict": _verdict("wrong_person", 0.95, dr=True)}]
        # A pending retarget the judge rejected does NOT block re-research (row is a dead guess).
        rejected = {"candidate:email:z@example.com": {
            "action": "retarget", "approved": "", "llm_reject": "yes",
            "llm_reject_reason": "unverified"}}
        self.assertEqual(len(dresearch.eligible_subset(verdicts, 0.85, rejected)), 1)
        # A NON-rejected pending retarget still counts as decided (skipped).
        pending_ok = {"candidate:email:z@example.com": {"action": "retarget", "approved": ""}}
        self.assertEqual(dresearch.eligible_subset(verdicts, 0.85, pending_ok), [])
        # A user-approved rejected row is terminal — stays decided.
        user_kept = {"candidate:email:z@example.com": {
            "action": "retarget", "approved": "yes", "llm_reject": "yes"}}
        self.assertEqual(dresearch.eligible_subset(verdicts, 0.85, user_kept), [])


class TestRetargetJudgeFingerprintCache(unittest.TestCase):
    """Retarget judgments are cached by an evidence fingerprint stored on the row:
    unchanged evidence (same research output + dossier) reuses the stored verdict —
    including rejections, which previously re-judged on every pass — so a steady-state
    $0 pass makes ZERO judge calls. Changed evidence re-judges; pre-fingerprint rows
    are grandfathered with a stamp instead of one last bootstrap re-judge. All
    fictional (example.com); the judge is always mocked — no live LLM/network."""

    PID = "candidate:email:nia@example.com"

    def _research_json(self, out_dir: Path, handle: str, profile: dict) -> None:
        (out_dir / handle).mkdir(parents=True, exist_ok=True)
        (out_dir / handle / "01_research_parallel.json").write_text(
            json.dumps(profile), encoding="utf-8")

    def _facts(self, facts_dir: Path, pid: str, name: str, **over) -> None:
        facts_dir.mkdir(parents=True, exist_ok=True)
        facts = {"canonical_name": name, "aliases": [], "employers": [], "title": "",
                 "school": "", "field_of_study": "", "location": "",
                 "relationship_to_owner": "friend", "topics": [], "notable_events": [],
                 "identifiers": [], "shared_context": [], "confidence": 0.8}
        facts.update(over)
        (facts_dir / f"{pid}.jsonl").write_text(
            json.dumps({"chunk_index": 0, "facts": facts, "usage": {}}) + "\n", encoding="utf-8")

    def _profile(self, slug: str = "nia-found", notes: str = "Best contextual match.") -> dict:
        return {"person": {"full_name": "Nia Field", "confidence": 0.8, "notes": notes},
                "social": {"linkedin_url": f"https://www.linkedin.com/in/{slug}",
                           "linkedin_status": "found"}}

    def _subset(self) -> list[dict]:
        return [{"parent_slug": "nia-field-p", "name": "Nia Field", "person_ids": [self.PID],
                 "candidate_key": self.PID, "linkedin": {},
                 "match_emails": ["nia@example.com"], "match_phones": []}]

    def test_same_evidence_second_pass_makes_zero_judge_calls(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            self._facts(facts, self.PID, "Nia Field")
            self._research_json(out, "nia-field-p", self._profile())
            rejecting = _verdict("wrong_person", 0.9, reason="no non-name corroboration")
            with mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=rejecting) as jm:
                first = dresearch.propose_retargets_from_output(
                    out, self._subset(), ov, facts_dir=facts, raw_dir=raw, use_llm=True)
            jm.assert_called_once()
            self.assertEqual((first["judge_calls"], first["cached_verdicts"]), (1, 0))
            row = _rows_by_pub(ov)[self.PID]
            self.assertEqual(row["llm_reject"], "yes")
            self.assertTrue(row["llm_judge_fingerprint"])              # verdict cached on the row
            # Second pass, identical evidence: the REJECTED proposal is NOT re-judged.
            with mock.patch.object(dresearch, "judge_research_proposal") as jm2:
                second = dresearch.propose_retargets_from_output(
                    out, self._subset(), ov, facts_dir=facts, raw_dir=raw, use_llm=True)
            jm2.assert_not_called()
            self.assertEqual((second["judge_calls"], second["cached_verdicts"]), (0, 1))
            unchanged = _rows_by_pub(ov)[self.PID]
            self.assertEqual(unchanged["llm_reject"], "yes")           # prior verdict stands
            self.assertEqual(unchanged["llm_judge_fingerprint"], row["llm_judge_fingerprint"])

    def test_changed_evidence_is_rejudged(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            self._facts(facts, self.PID, "Nia Field")
            self._research_json(out, "nia-field-p", self._profile())
            rejecting = _verdict("wrong_person", 0.9, reason="unverified")
            with mock.patch.object(dresearch, "judge_research_proposal", return_value=rejecting):
                dresearch.propose_retargets_from_output(
                    out, self._subset(), ov, facts_dir=facts, raw_dir=raw, use_llm=True)
            first_fp = _rows_by_pub(ov)[self.PID]["llm_judge_fingerprint"]
            # New research output (different proposed URL) -> fingerprint differs -> re-judge.
            self._research_json(out, "nia-field-p", self._profile(slug="nia-real",
                                                                  notes="Employer confirmed."))
            confirming = _verdict("confirmed", 0.9, reason="employer match")
            with mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=confirming) as jm:
                res = dresearch.propose_retargets_from_output(
                    out, self._subset(), ov, facts_dir=facts, raw_dir=raw, use_llm=True,
                    confirm_threshold=0.7)
            jm.assert_called_once()
            self.assertEqual(res["judge_calls"], 1)
            row = _rows_by_pub(ov)[self.PID]
            self.assertEqual(row["llm_reject"], "")                    # fresh verdict landed
            self.assertNotEqual(row["llm_judge_fingerprint"], first_fp)
            # Changed DOSSIER with identical research output also re-judges.
            self._facts(facts, self.PID, "Nia Field",
                        employers=[{"name": "Globex", "role": "Eng", "status": "current"}])
            with mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=confirming) as jm2:
                dresearch.propose_retargets_from_output(
                    out, self._subset(), ov, facts_dir=facts, raw_dir=raw, use_llm=True,
                    confirm_threshold=0.7)
            jm2.assert_called_once()

    def test_pre_fingerprint_rows_are_grandfathered_without_a_judge_call(self):
        # Rows judged BEFORE the fingerprint column existed carry a verdict but no sha.
        # First post-fix pass must be zero-call: stamp the current evidence sha, keep the
        # stored verdict. Later passes then hit the normal cache.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            self._facts(facts, self.PID, "Nia Field")
            self._research_json(out, "nia-field-p", self._profile())
            legacy = {column: "" for column in reconcile.OVERRIDE_COLUMNS}
            legacy.update({
                "public_identifier": self.PID, "action": "retarget", "approved": "",
                "new_linkedin_url": "https://www.linkedin.com/in/nia-found",
                "new_public_identifier": "nia-found", "person_id": self.PID,
                "source": "deep-research", "llm_reject": "yes",
                "llm_reject_reason": "pre-fix rejection",
            })
            reconcile._write_override_rows(ov, {self.PID: legacy})
            with mock.patch.object(dresearch, "judge_research_proposal") as jm:
                res = dresearch.propose_retargets_from_output(
                    out, self._subset(), ov, facts_dir=facts, raw_dir=raw, use_llm=True)
            jm.assert_not_called()
            self.assertEqual((res["judge_calls"], res["grandfathered"]), (0, 1))
            row = _rows_by_pub(ov)[self.PID]
            self.assertTrue(row["llm_judge_fingerprint"])              # stamped
            self.assertEqual(row["llm_reject"], "yes")                 # stored verdict kept
            self.assertEqual(row["llm_reject_reason"], "pre-fix rejection")
            with mock.patch.object(dresearch, "judge_research_proposal") as jm2:
                cached = dresearch.propose_retargets_from_output(
                    out, self._subset(), ov, facts_dir=facts, raw_dir=raw, use_llm=True)
            jm2.assert_not_called()
            self.assertEqual(cached["cached_verdicts"], 1)

    def test_heartbeat_reports_judging_progress_and_run_writes_it_to_the_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            out, facts, raw, ov = base / "r", base / "f", base / "raw", base / "review.csv"
            subset = []
            for i in (1, 2):
                pid = f"candidate:email:p{i}@example.com"
                self._facts(facts, pid, f"Person {i}")
                self._research_json(out, f"person-{i}-p", self._profile(slug=f"person-{i}"))
                subset.append({"parent_slug": f"person-{i}-p", "name": f"Person {i}",
                               "person_ids": [pid], "candidate_key": pid, "linkedin": {},
                               "match_emails": [], "match_phones": []})
            beats: list[tuple[int, int]] = []
            rejecting = _verdict("wrong_person", 0.9, reason="unverified")
            with mock.patch.object(dresearch, "judge_research_proposal", return_value=rejecting):
                res = dresearch.propose_retargets_from_output(
                    out, subset, ov, facts_dir=facts, raw_dir=raw, use_llm=True,
                    heartbeat=lambda done, total: beats.append((done, total)))
            self.assertEqual(res["judge_calls"], 2)
            self.assertEqual(beats, [(0, 2), (1, 2), (2, 2)])          # per-completion progress

            # Through run(): the heartbeat lands in the ONE fixed enrichment manifest.
            verdicts_path = base / "verdicts.jsonl"
            verdicts_path.write_text("".join(
                json.dumps({"parent_slug": row["parent_slug"], "name": row["name"],
                            "person_ids": row["person_ids"],
                            "candidate_key": row["candidate_key"],
                            "linkedin": {"linkedin_url": "https://www.linkedin.com/in/old-guess"},
                            "verdict": _verdict("wrong_person", 0.95, dr=True)}) + "\n"
                for row in subset), encoding="utf-8")
            manifest = base / "deep-research" / "manifest.json"
            payloads: list[dict] = []
            real_write = dresearch.write_enrichment_manifest

            def recording_write(payload, path=manifest):
                payloads.append(dict(payload))
                return real_write(payload, path)

            reconcile._write_override_rows(ov, {})
            old_out, old_queue = dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV
            dresearch.DR_OUT_DIR = out
            dresearch.QUEUE_CSV = out / "research_queue.csv"
            try:
                with mock.patch.object(dresearch, "judge_research_proposal",
                                       return_value=rejecting), \
                     mock.patch.object(dresearch, "write_enrichment_manifest",
                                       side_effect=recording_write):
                    # Research is complete for every eligible person -> the $0 reused
                    # path judges proposals; the queue handles reuse the verdict slugs.
                    with mock.patch.object(dresearch, "build_queue", side_effect=(
                            lambda s, people, f, r: [{"handle": row["parent_slug"]}
                                                     for row in s])):
                        result = _run_dresearch(_ns(
                            verdicts_jsonl=verdicts_path, overrides_csv=ov,
                            people_csv=base / "people.csv", facts_dir=facts, raw_dir=raw,
                            manifest=str(manifest), processor="core2x",
                            confirm_threshold=0.85, budget=0.0, approve=False,
                            dry_run=False, include_plausibly_absent=False,
                            include_candidates=False, no_llm=True))
            finally:
                dresearch.DR_OUT_DIR, dresearch.QUEUE_CSV = old_out, old_queue
            self.assertEqual(result["status"], "reused")
            self.assertEqual(result["judge_calls"], 2)
            judging = [p for p in payloads if p.get("phase") == "judging_retargets"]
            self.assertEqual([(p["done"], p["total"]) for p in judging],
                             [(0, 2), (1, 2), (2, 2)])
            self.assertTrue(all(p["status"] == "running" for p in judging))
            # The pass's terminal persist supersedes the heartbeat in the fixed manifest.
            final = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(final["status"], "research_complete")
            self.assertNotIn("phase", final)

    def test_judge_concurrency_defaults_capped_and_env_overridable(self):
        with mock.patch.dict(os.environ, {"POWERPACKS_OPENAI_CONCURRENCY": "",
                                          "POWERPACKS_OPENAI_USAGE_TIER": "tier_5"}):
            self.assertEqual(dresearch.judge_concurrency(), 128)       # capped below tier 5's 256
        with mock.patch.dict(os.environ, {"POWERPACKS_OPENAI_CONCURRENCY": "",
                                          "POWERPACKS_OPENAI_USAGE_TIER": "tier_4"}):
            self.assertEqual(dresearch.judge_concurrency(), 96)        # smaller tiers cap below the default
        with mock.patch.dict(os.environ, {"POWERPACKS_OPENAI_CONCURRENCY": "",
                                          "POWERPACKS_OPENAI_USAGE_TIER": "tier_1"}):
            self.assertEqual(dresearch.judge_concurrency(), 16)        # low tiers stay lower
        with mock.patch.dict(os.environ, {"POWERPACKS_OPENAI_CONCURRENCY": "64"}):
            self.assertEqual(dresearch.judge_concurrency(), 64)        # explicit override wins


class TestUnsilencedNameMatch(unittest.TestCase):
    """An unconfirmed unique first-degree name match is SURFACED as a visible needs_review row
    naming the connection — not silently reverted to an invisible no_link. All fictional data."""

    def _facts(self, facts_dir: Path, pid: str, name: str) -> None:
        facts_dir.mkdir(parents=True, exist_ok=True)
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps({
            "chunk_index": 0, "facts": {"canonical_name": name, "aliases": [], "employers": [],
                "title": "", "school": "", "field_of_study": "", "location": "",
                "relationship_to_owner": "friend", "topics": [], "notable_events": [],
                "identifiers": [], "shared_context": [], "confidence": 0.8}, "usage": {}}) + "\n",
            encoding="utf-8")

    def test_revert_stashes_review_payload(self):
        # The revert still flips to no-link (worth/lookup unchanged) but records the surfaced match.
        needs_review = {"parent_slug": "b", "name": "Eugene Wang", "candidate_key": "eugenewang",
                        "person_ids": ["msg-eugene"], "no_link": False, "name_matched": True,
                        "linkedin": {"linkedin_url": "https://www.linkedin.com/in/eugenewang"},
                        "match_emails": ["eugene6605@example.com"], "match_phones": [],
                        "verdict": _verdict("needs_review", 0.4)}
        reconcile.revert_unconfirmed_name_matches([needs_review], 0.7)
        self.assertTrue(needs_review["no_link"])                      # still reverts (invariant kept)
        self.assertEqual(needs_review["candidate_key"], "")
        rv = needs_review["name_match_review"]
        self.assertEqual(rv["connection_name"], "Eugene Wang")
        self.assertEqual(rv["connection_pub"], "eugenewang")

    def test_upsert_writes_visible_review_row_naming_the_connection(self):
        with tempfile.TemporaryDirectory() as d:
            ov = Path(d) / "review.csv"
            task = {"parent_slug": "b", "name": "Eugene Wang", "candidate_key": "eugenewang",
                    "person_ids": ["msg-eugene"], "no_link": False, "name_matched": True,
                    "linkedin": {"linkedin_url": "https://www.linkedin.com/in/eugenewang"},
                    "match_emails": [], "match_phones": [], "verdict": _verdict("needs_review", 0.4)}
            reconcile.revert_unconfirmed_name_matches([task], 0.7)
            stats = reconcile.upsert_name_match_reviews(ov, [task])
            self.assertEqual(stats["name_match_reviews"], 1)
            rows = _rows_by_pub(ov)
            row = rows["eugenewang"]
            self.assertEqual(row["action"], "review")
            self.assertEqual(row["approved"], "")                     # pending, visible in the queue
            self.assertIn("Eugene Wang", row["reason"])               # names the connection
            self.assertIn("no non-name corroboration", row["reason"])

    def test_user_decided_name_match_review_is_sticky(self):
        with tempfile.TemporaryDirectory() as d:
            ov = Path(d) / "review.csv"
            # Seed a user decision on the connection's row.
            with ov.open("w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=reconcile.OVERRIDE_COLUMNS)
                w.writeheader()
                w.writerow({"public_identifier": "eugenewang", "action": "review", "approved": "no",
                            "reason": "user says not them"})
            task = {"parent_slug": "b", "name": "Eugene Wang", "candidate_key": "eugenewang",
                    "person_ids": ["msg-eugene"], "no_link": False, "name_matched": True,
                    "linkedin": {"linkedin_url": "https://www.linkedin.com/in/eugenewang"},
                    "match_emails": [], "match_phones": [], "verdict": _verdict("needs_review", 0.4)}
            reconcile.revert_unconfirmed_name_matches([task], 0.7)
            stats = reconcile.upsert_name_match_reviews(ov, [task])
            self.assertEqual(stats["preserved_user_rows"], 1)
            with ov.open(newline="", encoding="utf-8") as _fh:
                row = next(csv.DictReader(_fh))
            self.assertEqual(row["approved"], "no")                   # not overwritten
            self.assertEqual(row["reason"], "user says not them")

    def test_confirmed_name_match_is_not_surfaced_for_review(self):
        confirmed = {"parent_slug": "a", "name": "Confirmed Person", "candidate_key": "confirmedp",
                     "person_ids": ["msg-c"], "no_link": False, "name_matched": True,
                     "linkedin": {"linkedin_url": "https://www.linkedin.com/in/confirmedp"},
                     "match_emails": [], "match_phones": [], "verdict": _verdict("confirmed", 0.9)}
        reconcile.revert_unconfirmed_name_matches([confirmed], 0.7)
        self.assertNotIn("name_match_review", confirmed)              # confirmed stays an identity row
        with tempfile.TemporaryDirectory() as d:
            ov = Path(d) / "review.csv"
            stats = reconcile.upsert_name_match_reviews(ov, [confirmed])
            self.assertEqual(stats["name_match_reviews"], 0)

    # --- 4) both-options competing case ---------------------------------------------------
    def test_competing_research_and_name_match_cross_reference(self):
        with tempfile.TemporaryDirectory() as d:
            ov = Path(d) / "review.csv"
            pid = "candidate:email:eugene6605@example.com"
            # A pending research retarget already exists for the SAME parent person.
            reconcile.upsert_retargets(ov, [{
                "old_public_identifier": pid, "person_id": pid,
                "new_linkedin_url": "https://www.linkedin.com/in/eugenejwang",
                "reason": "deep research best-guess", "source": "deep-research"}])
            # And an unconfirmed unique name match to the owner's first-degree connection.
            task = {"parent_slug": "eugene", "name": "Eugene Wang", "candidate_key": "eugenewang",
                    "person_ids": [pid], "no_link": False, "name_matched": True,
                    "linkedin": {"linkedin_url": "https://www.linkedin.com/in/eugenewang"},
                    "match_emails": [], "match_phones": [], "verdict": _verdict("needs_review", 0.4)}
            reconcile.revert_unconfirmed_name_matches([task], 0.7)
            reconcile.upsert_name_match_reviews(ov, [task])
            rows = _rows_by_pub(ov)
            name_row = rows["eugenewang"]
            research_row = rows[pid]
            # The name-match row mentions the competing research proposal...
            self.assertIn("eugenejwang", name_row["reason"])
            # ...and the research row mentions the competing name match.
            self.assertIn("name match", research_row["reason"].lower())
            self.assertIn("Eugene Wang", research_row["reason"])


class TestReviewWeb(unittest.TestCase):
    """The parent-grouped review UI: join verdicts.jsonl + review.csv, and decision writes."""

    def test_recent_messages_dedupes_same_evidence_across_merged_children(self):
        with tempfile.TemporaryDirectory() as dd:
            raw = Path(dd)
            duplicate = {
                "channel": "whatsapp", "at": "2026-01-02T00:00:00Z",
                "direction": "from_them", "subject": "", "text": "Friendly hello",
            }
            write_json(raw / "child-a.json", {"messages": [duplicate]})
            write_json(raw / "child-b.json", {"messages": [duplicate, {
                **duplicate, "at": "2026-01-01T00:00:00Z",
            }]})
            html = web_rendering._recent_messages_html(
                {"person_ids": ["child-a", "child-b"]}, raw)
        self.assertEqual(html.count("Friendly hello"), 2)
        self.assertEqual(html.count("2026-01-02"), 1)
        self.assertEqual(html.count("2026-01-01"), 1)

    def test_current_parent_membership_beats_legacy_message_alias(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            pub = "jordan-bravo"
            retired = legacy_message_linkedin_id(pub)
            phone_id = "candidate:phone:+15550100"
            for pid, decision in ((retired, "yes"), (phone_id, "maybe")):
                (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                    "canonical_name": "Jordan Bravo",
                    "network_worth": {"decision": decision, "reason": "fixture"},
                }}) + "\n", encoding="utf-8")
            index = base / "index.json"
            index.write_text(json.dumps({
                "slugs": {
                    "jordan-linked": {"person_id": retired},
                    "jordan-phone": {"person_id": phone_id},
                },
                "parents": {
                    "jordan-parent": {
                        "children": ["jordan-linked", "jordan-phone"],
                    },
                },
            }), encoding="utf-8")
            review = base / "review.csv"
            reconcile._write_override_rows(review, {pub: {
                "public_identifier": pub,
                "person_id": retired,
            }})
            web_decisions.apply_worth_decision(review, retired, "yes")
            rows = worth_view.load(facts, review, index)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parent_slug"], "jordan-parent")
        self.assertTrue(rows[0]["key"].startswith("parent-worth:parent-"))
        self.assertEqual(set(rows[0]["person_ids"]), {retired, phone_id})
        self.assertEqual(rows[0]["human"]["decision"], "yes")
        self.assertEqual(worth_view.counts(rows)["pending"], 0)

    def test_legacy_message_alias_still_falls_back_to_durable_parent(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            pub = "casey-delta"
            retired = legacy_message_linkedin_id(pub)
            durable = generate_person_id(pub)
            for pid in (retired, durable):
                (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                    "canonical_name": "Casey Delta",
                    "network_worth": {"decision": "yes", "reason": "fixture"},
                }}) + "\n", encoding="utf-8")
            index = base / "index.json"
            index.write_text(json.dumps({
                "slugs": {"casey-child": {"person_id": durable}},
                "parents": {"casey-parent": {"children": ["casey-child"]}},
            }), encoding="utf-8")
            review = base / "review.csv"
            reconcile._write_override_rows(review, {pub: {
                "public_identifier": pub,
                "person_id": retired,
            }})
            rows = worth_view.load(facts, review, index)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["parent_slug"], "casey-parent")
        self.assertTrue(rows[0]["key"].startswith("parent-worth:parent-"))
        self.assertEqual(set(rows[0]["person_ids"]), {retired, durable})

    def test_parent_machine_worth_uses_yes_then_maybe_then_no_priority(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            people = {
                "candidate:email:jordan@example.com": ("yes", "real correspondence"),
                "candidate:phone:+15550100": ("no", "automated traffic"),
                "candidate:email:alias@example.com": ("maybe", "sparse context"),
            }
            slugs = {}
            for index, (pid, (decision, reason)) in enumerate(people.items()):
                (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                    "canonical_name": "Jordan Bravo",
                    "network_worth": {"decision": decision, "reason": reason},
                }}) + "\n", encoding="utf-8")
                slugs[f"child-{index}"] = {"person_id": pid}
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": slugs,
                "parents": {
                    "jordan-parent": {
                        "parent_id": "parent-fixture",
                        "name": "Jordan Bravo",
                        "children": list(slugs),
                    },
                },
            }), encoding="utf-8")

            row = worth_view.load(facts, base / "review.csv", index_json)[0]
            self.assertEqual(row["key"], parent_worth_key("parent-fixture"))
            self.assertEqual(row["machine"]["decision"], "yes")
            self.assertEqual(row["machine"]["reason"], "real correspondence")

            yes_path = facts / "candidate:email:jordan@example.com.jsonl"
            yes_path.write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo",
                "network_worth": {"decision": "no", "reason": "fixture changed"},
            }}) + "\n", encoding="utf-8")
            row = worth_view.load(facts, base / "review.csv", index_json)[0]
            self.assertEqual(row["machine"]["decision"], "maybe")
            self.assertEqual(row["machine"]["reason"], "sparse context")

    def test_parent_worth_sync_migrates_legacy_human_mark_once(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            first = "candidate:email:jordan@example.com"
            second = "candidate:phone:+15550100"
            for pid, decision in ((first, "maybe"), (second, "no")):
                (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                    "canonical_name": "Jordan Bravo",
                    "network_worth": {"decision": decision, "reason": "fixture"},
                }}) + "\n", encoding="utf-8")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {
                    "jordan-email": {"person_id": first},
                    "jordan-phone": {"person_id": second},
                },
                "parents": {
                    "jordan-parent": {
                        "parent_id": "parent-fixture",
                        "name": "Jordan Bravo",
                        "children": ["jordan-email", "jordan-phone"],
                    },
                },
            }), encoding="utf-8")
            review = base / "review.csv"
            write_rows(review, {
                first: {
                    "public_identifier": first,
                    "person_id": first,
                    "network_worth": "yes",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
                second: {
                    "public_identifier": second,
                    "person_id": second,
                    "action": "exclude",
                    "approved": "yes",
                    "updated_at": "2025-01-01T00:00:00Z",
                },
            })

            stats = worth_view.sync_parent_worth_rows(review, facts, index_json)
            self.assertEqual(stats["parent_rows"], 1)
            self.assertEqual(stats["human_migrated"], 1)
            rows = load_rows(review)
            parent = rows[parent_worth_key("parent-fixture")]
            self.assertEqual(
                set(parent["worth_person_ids"].split("|")),
                {first, second},
            )
            self.assertEqual(parent["network_worth"], "yes")
            self.assertEqual(parent["llm_worth"], "maybe")
            self.assertEqual(rows[first]["network_worth"], "")
            self.assertEqual(rows[second]["action"], "")
            self.assertEqual(rows[second]["approved"], "")
            first_bytes = review.read_bytes()

            rerun = worth_view.sync_parent_worth_rows(review, facts, index_json)
            self.assertEqual(rerun["human_migrated"], 0)
            self.assertEqual(review.read_bytes(), first_bytes)
            effective = worth_view.load(facts, review, index_json)[0]
            self.assertEqual(effective["human"]["decision"], "yes")
            self.assertEqual(effective["effective"], "yes")

    def test_parent_worth_follows_members_across_merge_and_split(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            person_ids = [
                "candidate:email:jordan@example.com",
                "candidate:phone:+15550100",
                "candidate:email:alias@example.com",
            ]
            for pid in person_ids:
                (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                    "canonical_name": "Jordan Bravo",
                    "network_worth": {"decision": "maybe", "reason": "fixture"},
                }}) + "\n", encoding="utf-8")
            index_json = base / "index.json"

            def write_index(parent_rows):
                slugs = {
                    f"child-{offset}": {"person_id": pid}
                    for offset, pid in enumerate(person_ids)
                }
                index_json.write_text(json.dumps({
                    "slugs": slugs,
                    "parents": parent_rows,
                }), encoding="utf-8")

            write_index({
                "jordan-old": {
                    "parent_id": "parent-old",
                    "name": "Jordan Bravo",
                    "children": ["child-0", "child-1"],
                },
                "alias-old": {
                    "parent_id": "parent-alias",
                    "name": "Jordan Alias",
                    "children": ["child-2"],
                },
            })
            review = base / "review.csv"
            write_rows(review, {
                person_ids[0]: {
                    "public_identifier": person_ids[0],
                    "person_id": person_ids[0],
                    "network_worth": "yes",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })
            worth_view.sync_parent_worth_rows(review, facts, index_json)

            write_index({
                "jordan-merged": {
                    "parent_id": "parent-merged",
                    "name": "Jordan Bravo",
                    "children": ["child-0", "child-1", "child-2"],
                },
            })
            worth_view.sync_parent_worth_rows(review, facts, index_json)
            merged = load_rows(review)
            self.assertNotIn(parent_worth_key("parent-old"), merged)
            self.assertEqual(
                merged[parent_worth_key("parent-merged")]["network_worth"],
                "yes",
            )
            self.assertEqual(
                set(merged[parent_worth_key("parent-merged")]["worth_person_ids"].split("|")),
                set(person_ids),
            )

            write_index({
                "jordan-split": {
                    "parent_id": "parent-split-a",
                    "name": "Jordan Bravo",
                    "children": ["child-0"],
                },
                "alias-split": {
                    "parent_id": "parent-split-b",
                    "name": "Jordan Alias",
                    "children": ["child-1", "child-2"],
                },
            })
            worth_view.sync_parent_worth_rows(review, facts, index_json)
            split = load_rows(review)
            self.assertNotIn(parent_worth_key("parent-merged"), split)
            self.assertEqual(
                split[parent_worth_key("parent-split-a")]["network_worth"],
                "yes",
            )
            self.assertEqual(
                split[parent_worth_key("parent-split-b")]["network_worth"],
                "yes",
            )

    def test_parent_worth_migration_retires_legacy_exclude(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            pid = "candidate:email:jordan@example.com"
            (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo",
                "network_worth": {"decision": "maybe", "reason": "fixture"},
            }}) + "\n", encoding="utf-8")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {"jordan-child": {"person_id": pid}},
                "parents": {
                    "jordan-parent": {
                        "parent_id": "parent-fixture",
                        "children": ["jordan-child"],
                    },
                },
            }), encoding="utf-8")
            review = base / "review.csv"
            write_rows(review, {
                pid: {
                    "public_identifier": pid,
                    "person_id": pid,
                    "action": "exclude",
                    "approved": "yes",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })

            worth_view.sync_parent_worth_rows(review, facts, index_json)
            migrated = load_rows(review)
            key = parent_worth_key("parent-fixture")
            self.assertEqual(migrated[key]["network_worth"], "no")
            self.assertEqual(migrated[pid]["action"], "")
            self.assertEqual(migrated[pid]["approved"], "")

            web_decisions.apply_worth_decision(review, key, "")
            restored = worth_view.load(facts, review, index_json)[0]
            self.assertEqual(restored["human"], None)
            self.assertEqual(restored["effective"], "maybe")

    def test_current_parent_worth_wins_timestamp_tie_with_legacy_child(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            pid = "candidate:email:jordan@example.com"
            (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo",
                "network_worth": {"decision": "maybe", "reason": "fixture"},
            }}) + "\n", encoding="utf-8")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {"jordan-child": {"person_id": pid}},
                "parents": {
                    "jordan-parent": {
                        "parent_id": "parent-fixture",
                        "children": ["jordan-child"],
                    },
                },
            }), encoding="utf-8")
            key = parent_worth_key("parent-fixture")
            review = base / "review.csv"
            write_rows(review, {
                key: {
                    "public_identifier": key,
                    "worth_person_ids": pid,
                    "network_worth": "yes",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
                pid: {
                    "public_identifier": pid,
                    "person_id": pid,
                    "network_worth": "no",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })

            worth_view.sync_parent_worth_rows(review, facts, index_json)
            rows = load_rows(review)
            self.assertEqual(rows[key]["network_worth"], "yes")
            self.assertEqual(rows[pid]["network_worth"], "")

    def test_parent_worth_survives_temporarily_missing_facts(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            pid = "candidate:email:jordan@example.com"
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {"jordan-child": {"person_id": pid}},
                "parents": {
                    "jordan-parent": {
                        "parent_id": "parent-fixture",
                        "children": ["jordan-child"],
                    },
                },
            }), encoding="utf-8")
            key = parent_worth_key("parent-fixture")
            review = base / "review.csv"
            write_rows(review, {
                key: {
                    "public_identifier": key,
                    "worth_person_ids": pid,
                    "network_worth": "yes",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })

            worth_view.sync_parent_worth_rows(review, facts, index_json)
            self.assertEqual(load_rows(review)[key]["network_worth"], "yes")

            (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo",
                "network_worth": {"decision": "maybe", "reason": "fixture"},
            }}) + "\n", encoding="utf-8")
            worth_view.sync_parent_worth_rows(review, facts, index_json)
            self.assertEqual(load_rows(review)[key]["network_worth"], "yes")

    def test_parent_worth_preserves_absent_members_during_partial_recluster(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            first = "candidate:email:jordan@example.com"
            second = "candidate:phone:+15550100"
            (facts / f"{first}.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo",
                "network_worth": {"decision": "maybe", "reason": "fixture"},
            }}) + "\n", encoding="utf-8")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {"first-child": {"person_id": first}},
                "parents": {
                    "first-parent": {
                        "parent_id": "parent-first",
                        "children": ["first-child"],
                    },
                },
            }), encoding="utf-8")
            old_key = parent_worth_key("parent-old")
            review = base / "review.csv"
            write_rows(review, {
                old_key: {
                    "public_identifier": old_key,
                    "worth_person_ids": f"{first}|{second}",
                    "network_worth": "yes",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            })

            worth_view.sync_parent_worth_rows(review, facts, index_json)
            partial = load_rows(review)
            self.assertEqual(
                partial[parent_worth_key("parent-first")]["network_worth"],
                "yes",
            )
            self.assertEqual(partial[old_key]["worth_person_ids"], second)

            (facts / f"{second}.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Alias",
                "network_worth": {"decision": "maybe", "reason": "fixture"},
            }}) + "\n", encoding="utf-8")
            index_json.write_text(json.dumps({
                "slugs": {
                    "first-child": {"person_id": first},
                    "second-child": {"person_id": second},
                },
                "parents": {
                    "first-parent": {
                        "parent_id": "parent-first",
                        "children": ["first-child"],
                    },
                    "second-parent": {
                        "parent_id": "parent-second",
                        "children": ["second-child"],
                    },
                },
            }), encoding="utf-8")
            worth_view.sync_parent_worth_rows(review, facts, index_json)
            restored = load_rows(review)
            self.assertNotIn(old_key, restored)
            self.assertEqual(
                restored[parent_worth_key("parent-second")]["network_worth"],
                "yes",
            )

    def test_candidate_research_uses_parent_human_worth(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            facts = base / "facts"
            facts.mkdir()
            pid = "candidate:email:jordan@example.com"
            (facts / f"{pid}.jsonl").write_text(json.dumps({"facts": {
                "canonical_name": "Jordan Bravo",
                "network_worth": {"decision": "maybe", "reason": "sparse context"},
            }}) + "\n", encoding="utf-8")
            index_json = base / "index.json"
            index_json.write_text(json.dumps({
                "slugs": {"jordan-child": {"person_id": pid}},
                "parents": {
                    "jordan-parent": {
                        "parent_id": "parent-fixture",
                        "name": "Jordan Bravo",
                        "children": ["jordan-child"],
                    },
                },
            }), encoding="utf-8")
            parent_key = parent_worth_key("parent-fixture")
            overrides = {
                parent_key: {
                    "public_identifier": parent_key,
                    "worth_person_ids": pid,
                    "network_worth": "yes",
                    "updated_at": "2026-01-01T00:00:00Z",
                },
            }
            candidate = mock.Mock(
                person_id=pid,
                full_name="Jordan Bravo",
                emails=["jordan@example.com"],
                phones=[],
            )
            with mock.patch.object(dresearch, "load_candidates", return_value=[candidate]):
                selected = dresearch.candidate_subset(
                    facts,
                    overrides,
                    resolved_candidates=set(),
                    index_json=index_json,
                )
                self.assertEqual([row["candidate_key"] for row in selected], [pid])
                overrides[parent_key]["network_worth"] = "no"
                self.assertEqual(
                    dresearch.candidate_subset(
                        facts,
                        overrides,
                        resolved_candidates=set(),
                        index_json=index_json,
                    ),
                    [],
                )

    @staticmethod
    def _maybe_parent() -> dict:
        pid = "candidate:email:jordan@example.com"
        key = parent_worth_key("parent-fixture")
        machine = {"decision": "maybe", "reason": "sparse context", "source": "llm"}
        return {
            "slug": "jordan-parent",
            "dossier_slug": "jordan-parent",
            "name": "Jordan Bravo",
            "person_ids": [pid],
            "sources": ["gmail"],
            "candidates": [{
                "pub": pid,
                "full_name": "Jordan Bravo",
                "import_candidate": True,
                "worth_key": key,
                "worth": {"decision": "maybe", "source": "llm", "reason": "sparse context"},
                "machine_worth": machine,
            }],
            "worth": {"decision": "maybe", "source": "llm", "reason": "sparse context"},
            "machine_worth": machine,
            "worth_row": {
                "key": key,
                "parent_id": "parent-fixture",
                "parent_slug": "jordan-parent",
                "person_ids": [pid],
                "machine": machine,
                "human": None,
                "effective": "maybe",
                "source": "llm",
            },
        }

    def test_people_manifest_can_complete_with_unresolved_maybes(self):
        progress = web_workflow.review_progress([self._maybe_parent()])
        with tempfile.TemporaryDirectory() as dd:
            manifest = Path(dd) / "review" / "manifest.json"
            result = web_workflow.write_review_manifest(
                "worth",
                "completed",
                progress,
                path=manifest,
                review_path=Path(dd) / "review.csv",
                synthetic_path=Path(dd) / "synthetic.csv",
            )
            self.assertIn("worth", result["completed_stages"])
            self.assertEqual(result["counts"]["pending"], 1)
            self.assertTrue(web_workflow.phase_is_completed("worth", progress, manifest))

            # LinkedIn is skippable too — Finish means finish. Undecided
            # identities stay undecided (pending stays visible in the
            # stepper) and realization simply skips them.
            result = web_workflow.write_review_manifest(
                "linkedin",
                "completed",
                {**progress, "linkedin_total": 1, "linkedin_pending": 1},
                path=manifest,
                review_path=Path(dd) / "review.csv",
                synthetic_path=Path(dd) / "synthetic.csv",
            )
            self.assertIn("linkedin", result["completed_stages"])
            self.assertEqual(result["counts"]["pending"], 1)

    def test_unresolved_maybe_stays_out_of_lookup_after_people_completion(self):
        maybe = self._maybe_parent()
        yes = json.loads(json.dumps(maybe))
        yes["slug"] = "casey-parent"
        yes["dossier_slug"] = "casey-parent"
        yes["name"] = "Casey Delta"
        yes["person_ids"] = ["candidate:email:casey@example.com"]
        yes["worth_row"]["key"] = parent_worth_key("parent-casey")
        yes["worth_row"]["parent_id"] = "parent-casey"
        yes["worth_row"]["parent_slug"] = "casey-parent"
        yes["worth_row"]["person_ids"] = list(yes["person_ids"])
        yes["worth_row"]["effective"] = "yes"
        yes["worth_row"]["source"] = "user"
        yes["worth_row"]["human"] = {
            "decision": "yes",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        yes["worth"]["decision"] = "yes"
        yes["worth"]["source"] = "user"
        yes["candidates"][0]["pub"] = yes["person_ids"][0]
        yes["candidates"][0]["worth_key"] = yes["worth_row"]["key"]
        yes["candidates"][0]["worth"] = dict(yes["worth"])

        with tempfile.TemporaryDirectory() as dd:
            manifest = Path(dd) / "review" / "manifest.json"
            progress = web_workflow.review_progress([yes, maybe])
            web_workflow.write_review_manifest(
                "worth",
                "completed",
                progress,
                path=manifest,
                review_path=Path(dd) / "review.csv",
                synthetic_path=Path(dd) / "synthetic.csv",
            )
            selection = web_workflow.worth_selection_from_parents(
                [yes, maybe],
                manifest_path=manifest,
            )

        self.assertEqual(progress["lookup_ready"], 1)
        self.assertEqual(selection["yes"], 1)
        self.assertEqual(selection["maybe"], 1)

    def test_worth_page_does_not_add_pending_continue_button(self):
        parent = self._maybe_parent()
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            html = web_rendering.page_html(
                [parent],
                {"stage": ["worth"], "view": ["review"]},
                base / "review.csv",
                parents_dir=base / "parents",
                dossier_dir=base / "dossiers",
                manifest_path=base / "review" / "manifest.json",
                enrichment_manifest_path=base / "research" / "manifest.json",
            ).decode("utf-8")
            self.assertIn("Jordan Bravo", html)
            self.assertNotIn("data-complete='worth'", html)
            self.assertNotIn("Continue with", html)

    def test_serve_initial_snapshot_uses_every_custom_artifact_path(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            paths = {
                name: base / value
                for name, value in {
                    "review": "custom/review.csv",
                    "verdicts": "custom/verdicts.jsonl",
                    "synthetic_people": "custom/synthetic.csv",
                    "facts_dir": "custom/facts",
                    "people_csv": "custom/people.csv",
                    "parents_dir": "custom/parents",
                    "dossier_dir": "custom/dossiers",
                    "profile_cache_dir": "custom/profiles",
                    "manifest": "custom/review/manifest.json",
                    "enrichment_manifest": "custom/research/manifest.json",
                    "avatar_dir": "custom/avatars",
                }.items()
            }
            args = mock.Mock(
                **{name: str(path) for name, path in paths.items()},
                host="127.0.0.1",
                port=43210,
                stage="linkedin",
                fresh=False,
                open=False,
                confirm_threshold=0.7,
                detach_threshold=0.85,
            )
            fake_server = mock.Mock(server_address=("127.0.0.1", 43210))
            with mock.patch.object(
                    web_server.urllib.request, "urlopen",
                    side_effect=web_server.urllib.error.URLError("not running")), \
                 mock.patch.object(web_server, "_all_review_parents", return_value=[]) as build, \
                 mock.patch.object(web_server, "ThreadingHTTPServer", return_value=fake_server):
                web_server.cmd_serve(args)

        build.assert_called_once_with(
            paths["verdicts"],
            paths["review"],
            paths["synthetic_people"],
            paths["facts_dir"],
            paths["people_csv"],
            paths["parents_dir"],
            paths["dossier_dir"],
            paths["profile_cache_dir"],
        )
        fake_server.serve_forever.assert_called_once_with()

    def test_session_lock_enforces_single_writer(self):
        # The advisory flock is what turns "the server is the only writer" from
        # an assumption into an invariant: mutating CLI mains refuse while a
        # server holds it, and a second server cannot start.
        from packs.ingestion.primitives.deep_context import common as dc_common
        with tempfile.TemporaryDirectory() as dd:
            lock_path = Path(dd) / "review" / ".server.lock"
            with mock.patch.object(dc_common, "REVIEW_SESSION_LOCK", lock_path):
                handle = dc_common.acquire_review_session_lock()
                try:
                    with self.assertRaisesRegex(SystemExit, "review server is running"):
                        dc_common.ensure_no_review_session("apply_retargets")
                    with self.assertRaises(RuntimeError):
                        dc_common.acquire_review_session_lock()
                finally:
                    handle.close()
                dc_common.ensure_no_review_session("apply_retargets")  # released -> free

    def test_job_terminal_fires_view_hooks_on_success_and_failure(self):
        # Single-writer refresh points: a job terminal (success OR failure) must
        # re-derive the model and nudge views — it is the only mid-session
        # writer besides clicks.
        import time as _time
        fired: list[str] = []
        web_server._job_events.subscribe(terminal=lambda: fired.append("hook"))
        try:
            web_server._run_pipeline_job("t-ok", lambda: None)
            deadline = _time.time() + 5
            while len(fired) < 1 and _time.time() < deadline:
                _time.sleep(0.01)
            self.assertEqual(len(fired), 1)

            def boom() -> None:
                raise SystemExit("guard path")

            web_server._run_pipeline_job("t-fail", boom)
            deadline = _time.time() + 5
            while len(fired) < 2 and _time.time() < deadline:
                _time.sleep(0.01)
            self.assertEqual(len(fired), 2)
        finally:
            web_server._job_events._terminal_subs.pop()

    def test_browser_observer_subscribes_only_on_wait_screens(self):
        # Single-writer sessions: the browser never polls. Wait screens open ONE
        # EventSource; each server nudge (mutation/job completion) triggers one
        # /api/status re-snapshot. Interactive screens open nothing.
        script = web_rendering.REVIEW_JS.read_text(encoding="utf-8")
        self.assertIn('fetch("/api/status", { cache: "no-store" })', script)
        self.assertIn('new EventSource("/api/events")', script)
        self.assertNotIn("setInterval(pollFileState", script)
        self.assertNotIn("statusPollMs", script)
        self.assertIn(
            'const observesExternalUpdates = document.body.dataset.externalUpdates === "true";',
            script,
        )
        self.assertIn("if (!observesExternalUpdates) return;", script)
        self.assertIn("if (observesExternalUpdates) {", script)
        self.assertNotIn("adoptServerState", script)
        self.assertIn("adoptMutationState(response);", script)
        # One card-advance system: both queues prefetch the NEXT card while the
        # user reads the current one, and the decision POST settles in the
        # background (fired before the prefetched card is awaited/swapped in).
        self.assertIn("function prefetchWorthCard(", script)
        self.assertIn("function prefetchLinkedinCard(", script)
        self.assertIn("/api/linkedin-card?exclude=", script)
        self.assertIn("/api/worth-card?exclude=", script)
        self.assertNotIn("linkedinBufferTarget", script)
        self.assertNotIn("/api/linkedin-cards?", script)
        self.assertLess(
            script.index('const postPromise = post("/decide", values);'),
            script.index("panel.innerHTML = nextHtml; // next parent's card"),
        )
        self.assertIn(
            'document.querySelectorAll("[data-retarget-form] textarea[name=\'guidance\']")',
            script,
        )
        self.assertIn('document.body.dataset.preview === "true"', script)
        self.assertIn(
            "!isStagePreview && state.stage && state.stage !== currentStage",
            script,
        )
        self.assertIn("void syncFileState();", script)
        self.assertIn("renderJobProgress(payload.job)", script)
        self.assertNotIn("visibilitychange", script)
        self.assertNotIn('document.visibilityState !== "visible"', script)
        self.assertIn('leaveAndNavigate("People complete", "/?stage=enrich")', script)
        self.assertNotIn(
            'document.visibilityState !== "visible" || hasIdentityDraft()',
            script,
        )

    def test_pages_mark_only_external_file_update_boundaries_for_polling(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            expected = {
                "worth": "false",
                "enrich": "true",
                "linkedin": "true",  # early preview: enrichment is not current
                "done": "true",
            }
            for stage in ("worth", "enrich", "linkedin", "done"):
                html = web_rendering.page_html(
                    [],
                    {"stage": [stage]},
                    base / "review.csv",
                    parents_dir=base / "parents",
                    dossier_dir=base / "dossiers",
                    manifest_path=base / "review" / "manifest.json",
                    enrichment_manifest_path=base / "research" / "manifest.json",
                    verdicts_path=base / "verdicts.jsonl",
                    facts_dir=base / "facts",
                ).decode("utf-8")
                self.assertIn(f"data-stage='{stage}'", html)
                self.assertIn(f"data-external-updates='{expected[stage]}'", html)
                self.assertIn("data-preview='false'", html)
                self.assertIn(
                    "<script src='/assets/reconcile-review.js' defer></script>",
                    html,
                )

    def test_early_linkedin_preview_polls_without_forcing_current_stage(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            html = web_rendering.page_html(
                [],
                {"stage": ["linkedin"], "preview": ["1"]},
                base / "review.csv",
                parents_dir=base / "parents",
                dossier_dir=base / "dossiers",
                manifest_path=base / "review" / "manifest.json",
                enrichment_manifest_path=base / "research" / "manifest.json",
            ).decode("utf-8")
            self.assertIn("data-stage='linkedin'", html)
            self.assertIn("data-preview='true'", html)
            self.assertIn("data-external-updates='true'", html)
            self.assertIn("href='/?stage=worth&amp;preview=1'", html)
            self.assertIn("href='/?stage=enrich&amp;preview=1'", html)
            self.assertIn("href='/?stage=linkedin&amp;preview=1'", html)

    def test_current_linkedin_queue_does_not_poll(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            review_manifest = base / "review" / "manifest.json"
            review_manifest.parent.mkdir(parents=True)
            review_manifest.write_text(json.dumps({
                "stage": "enrich",
                "status": "completed",
                "completed_stages": ["worth", "enrich"],
                "people_revision": "revision-1",
            }), encoding="utf-8")
            selection = web_workflow.worth_selection_from_parents(
                [], manifest_path=review_manifest)
            enrichment_manifest = base / "research" / "manifest.json"
            enrichment_manifest.parent.mkdir(parents=True)
            enrichment_manifest.write_text(json.dumps({
                "stage": "enrich",
                "status": "completed",
                "selection": selection,
                "counts": {"total": 0, "completed": 0, "pending": 0, "failed": 0},
            }), encoding="utf-8")

            html = web_rendering.page_html(
                [],
                {"stage": ["linkedin"]},
                base / "review.csv",
                parents_dir=base / "parents",
                dossier_dir=base / "dossiers",
                manifest_path=review_manifest,
                enrichment_manifest_path=enrichment_manifest,
            ).decode("utf-8")

        self.assertIn("data-stage='linkedin'", html)
        self.assertIn("data-external-updates='false'", html)

    def test_every_workflow_wait_state_maps_to_a_polled_browser_stage(self):
        expected = {
            "review_people": "worth",
            "preview_enrichment": "enrich",
            "await_enrichment_approval": "enrich",
            "run_approved_enrichment": "enrich",
            "run_enrichment_from_cache": "enrich",
            "wait_for_enrichment": "enrich",
            "retry_enrichment": "enrich",
            "assemble_synthetic": "enrich",
            "continue_enrichment": "enrich",
            "review_linkedin": "linkedin",
            "finish_linkedin": "linkedin",
            "realize": "done",
        }
        self.assertEqual(
            {action: web_workflow.browser_stage_for_next_action(action) for action in expected},
            expected,
        )

    def test_browser_state_token_changes_for_each_observed_file_state_family(self):
        progress = {
            "total": 3,
            "worth_total": 2,
            "worth_pending": 1,
            "worth_yes": 1,
            "worth_no": 0,
            "lookup_ready": 1,
            "linkedin_total": 1,
            "linkedin_pending": 1,
            "linkedin_done": 0,
            "rejected": 0,
        }
        selection = {
            "sha256": "selection-a",
            "total": 2,
            "yes": 1,
            "maybe": 1,
            "no": 0,
            "review_revision": "revision-a",
        }
        enrichment = {
            "status": "running",
            "current": True,
            "approval_current": True,
            "counts": {"total": 1, "completed": 0, "pending": 1, "failed": 0},
            "updated_at": "2026-07-16T00:00:00Z",
        }
        review_manifest = {
            "stage": "enrich",
            "status": "awaiting_user",
            "completed_stages": ["worth"],
            "updated_at": "2026-07-16T00:00:00Z",
        }
        baseline = web_workflow.review_state_token(
            progress, selection, enrichment, review_manifest)

        changed_progress = {**progress, "worth_pending": 0}
        changed_selection = {**selection, "sha256": "selection-b"}
        changed_enrichment = {
            **enrichment,
            "counts": {"total": 1, "completed": 1, "pending": 0, "failed": 0},
        }
        changed_review = {
            **review_manifest,
            "stage": "linkedin",
            "completed_stages": ["worth", "enrich"],
        }
        self.assertNotEqual(
            baseline,
            web_workflow.review_state_token(
                changed_progress, selection, enrichment, review_manifest),
        )
        self.assertNotEqual(
            baseline,
            web_workflow.review_state_token(
                progress, changed_selection, enrichment, review_manifest),
        )
        self.assertNotEqual(
            baseline,
            web_workflow.review_state_token(
                progress, selection, changed_enrichment, review_manifest),
        )
        self.assertNotEqual(
            baseline,
            web_workflow.review_state_token(
                progress, selection, enrichment, changed_review),
        )

    def _fixture(self, d: Path) -> tuple[Path, Path]:
        verdicts = d / "verdicts.jsonl"
        review = d / "review.csv"
        recs = [
            {"parent_slug": "jane-doe-p1", "name": "Jane Doe", "candidate_key": "janedoe",
             "person_ids": ["pid-1"], "conflict": False, "no_link": False,
             "linkedin": {"public_identifier": "janedoe", "linkedin_url": "https://www.linkedin.com/in/janedoe",
                          "full_name": "Jane Doe", "headline": "VP at Acme", "experiences": ["VP @ Acme"],
                          "education": ["MIT"], "location": "SF", "has_profile": True},
             "match_emails": ["jane@acme.com"], "match_phones": [],
             "verdict": {"verdict": "needs_review", "confidence": 0.55, "supporting_evidence": ["same company"],
                         "contradicting_evidence": [], "reason": "plausible but unconfirmed",
                         "linkedin_plausibly_absent": False, "recommend_deep_research": False}, "error": ""},
            {"parent_slug": "pat-lee-p2", "name": "Pat Lee", "candidate_key": "patlee",
             "person_ids": ["pid-2"], "conflict": False, "no_link": False,
             "linkedin": {"public_identifier": "patlee", "linkedin_url": "https://www.linkedin.com/in/patlee",
                          "full_name": "Pat Lee", "headline": "Driver", "experiences": [], "education": [],
                          "location": "", "has_profile": True},
             "match_emails": ["pat@globex.com"], "match_phones": [],
             "verdict": {"verdict": "confirmed", "confidence": 0.95, "supporting_evidence": ["exact match"],
                         "contradicting_evidence": [], "reason": "strong", "linkedin_plausibly_absent": False,
                         "recommend_deep_research": False}, "error": ""},
        ]
        verdicts.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        # review.csv as reconcile would write it: jane pending, pat verified/auto
        reconcile._write_override_rows(review, {
            "janedoe": {"public_identifier": "janedoe", "action": "verify", "approved": "",
                        "linkedin_url": "https://www.linkedin.com/in/janedoe", "confidence": "0.550"},
            "patlee": {"public_identifier": "patlee", "action": "verify", "approved": "auto",
                       "linkedin_url": "https://www.linkedin.com/in/patlee", "confidence": "0.950"},
        })
        return verdicts, review

    def test_superseded_pairs_fold_the_pre_match_identity(self):
        # A matched people.csv row carries the candidate id it superseded; the
        # two identities' dossier slugs must join one cluster at confidence 1.0
        # (import witnessed they are one contact row — no judge needed). Ids
        # without a dossier slug are inert.
        with tempfile.TemporaryDirectory() as d:
            people_csv = Path(d) / "people.csv"
            people_csv.write_text(
                "id,full_name,superseded_person_ids\n"
                'durable-uuid-1,Jordan Bravado,"[""candidate:phone:+15550100""]"\n'
                'durable-uuid-2,Casey Sierra,"[""candidate:phone:+15550199""]"\n',
                encoding="utf-8")
            slugs_info = {
                "jordan-bravado-aaaa1111": {"person_id": "durable-uuid-1"},
                "jordan-bravado-bbbb2222": {"person_id": "candidate:phone:+15550100"},
                # Casey's candidate identity has no dossier slug -> inert
            }
            got = parents.superseded_pairs(people_csv, slugs_info)
            self.assertEqual(len(got), 1)
            self.assertEqual({got[0]["slug_a"], got[0]["slug_b"]},
                             {"jordan-bravado-aaaa1111", "jordan-bravado-bbbb2222"})
            self.assertEqual(got[0]["confidence"], "1.0")
            clusters = parents.clusters_from_pairs(got)
            self.assertEqual(clusters, [sorted(
                ["jordan-bravado-aaaa1111", "jordan-bravado-bbbb2222"])])

    def test_build_parents_joins_and_states(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            ps, _ = web_model.build_parents(verdicts, review)
            by = {p["name"]: p for p in ps}
            self.assertEqual(set(by), {"Jane Doe", "Pat Lee"})
            self.assertEqual(web_model.parent_status(by["Jane Doe"]), "review")
            self.assertEqual(web_model.parent_status(by["Pat Lee"]), "verified")
            self.assertEqual(web_model.picked_link(by["Pat Lee"]), "https://www.linkedin.com/in/patlee")
            # reasoning + profile carried through for display
            cand = by["Jane Doe"]["candidates"][0]
            self.assertEqual(cand["headline"], "VP at Acme")
            self.assertEqual(cand["supporting"], ["same company"])

    def test_decisions_keep_detach_fix_reset(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            TH = reconcile.DEFAULT_CONFIRM

            r = web_decisions.apply_decision(review, verdicts, "janedoe", "keep", "", TH)
            self.assertEqual((r["action"], r["approved"]), ("verify", "yes"))

            r = web_decisions.apply_decision(review, verdicts, "janedoe", "detach", "", TH)
            self.assertEqual((r["action"], r["approved"]), ("detach", "yes"))

            r = web_decisions.apply_decision(review, verdicts, "janedoe", "fix",
                                   "linkedin.com/in/jane-real", TH)
            self.assertEqual(r["action"], "retarget")
            self.assertEqual(r["new_url"], "https://www.linkedin.com/in/jane-real")
            rows = reconcile.load_override_rows(review)
            self.assertEqual(rows["janedoe"]["new_public_identifier"], "jane-real")

            # reset a high-confidence confirmed -> restores auto/verify (re-applies at merge)
            web_decisions.apply_decision(review, verdicts, "patlee", "detach", "", TH)
            r = web_decisions.apply_decision(review, verdicts, "patlee", "reset", "", TH)
            self.assertEqual((r["action"], r["approved"]), ("verify", "auto"))

            # no duplicate rows introduced (still exactly the two pubs)
            self.assertEqual(set(reconcile.load_override_rows(review)), {"janedoe", "patlee"})

    def test_fix_requires_url(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            with self.assertRaises(ValueError):
                web_decisions.apply_decision(review, verdicts, "janedoe", "fix", "", reconcile.DEFAULT_CONFIRM)

    def test_exclude_marks_person_excluded(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._fixture(d)
            r = web_decisions.apply_decision(review, verdicts, "janedoe", "exclude", "", reconcile.DEFAULT_CONFIRM)
            self.assertEqual((r["action"], r["approved"]), ("exclude", "yes"))
            parents, _ = web_model.build_parents(verdicts, review)
            jane = next(p for p in parents if p["name"] == "Jane Doe")
            self.assertEqual(web_model.candidate_state(jane["candidates"][0]), "excluded")
            self.assertEqual(web_model.parent_status(jane), "excluded")

    def _merged_fixture(self, d: Path) -> tuple[Path, Path]:
        """One Merged person: a confirmed keeper, a high-confidence wrong namesake, and a
        still-needs-review third link, all on the same parent."""
        def rec(key, verdict, conf):
            return {"parent_slug": "sam-jones-p1", "name": "Sam Jones", "candidate_key": key,
                    "person_ids": ["pid-1", "pid-2"], "conflict": True, "no_link": False,
                    "linkedin": {"public_identifier": key, "linkedin_url": f"https://www.linkedin.com/in/{key}",
                                 "full_name": "Sam Jones", "headline": "", "experiences": [], "education": [],
                                 "location": "", "has_profile": True},
                    "match_emails": [], "match_phones": [],
                    "verdict": {"verdict": verdict, "confidence": conf, "supporting_evidence": [],
                                "contradicting_evidence": [], "reason": "r", "linkedin_plausibly_absent": False,
                                "recommend_deep_research": False}, "error": ""}
        verdicts = d / "verdicts.jsonl"
        review = d / "review.csv"
        recs = [rec("samwrong", "wrong_person", 0.93), rec("samreal", "confirmed", 0.9),
                rec("sammaybe", "needs_review", 0.4)]
        verdicts.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
        review.write_text("", encoding="utf-8")  # all pending
        return verdicts, review

    def test_staged_identity_queue_floats_best_candidate_and_has_binary_actions(self):
        with tempfile.TemporaryDirectory() as dd:
            d = Path(dd)
            verdicts, review = self._merged_fixture(d)
            parents, _ = web_model.build_parents(verdicts, review)
            sam = next(p for p in parents if p["name"] == "Sam Jones")
            self.assertEqual(len(sam["candidates"]), 3)
            pending = web_workflow.pending_linkedin_candidates(sam)
            self.assertEqual([cand["pub"] for cand in pending], ["samreal", "sammaybe", "samwrong"])
            # A candidate with real profile facts renders the confirm
            # question; factless ones render the invalid-profile ask instead.
            pending[0]["experiences"] = ["Founder @ Jones Robotics"]
            html = web_rendering.render_linkedin_card(sam, pending[0], d, d)
            self.assertIn("Is this the right profile?", html)
            self.assertIn("data-decide='keep'", html)
            # "No" is TERMINAL — it decides detach directly (the same /decide
            # fan-out Skip performs); the guidance box is the separate
            # pre-decision re-research path, opened by its own <summary>.
            self.assertIn("data-decide='detach'", html)
            self.assertNotIn("data-open-guidance", html)
            self.assertNotIn("data-fix-form", html)
            self.assertNotIn("Use a different LinkedIn", html)
            # Skip is folded INTO the question line as an inline secondary link, not a
            # standalone button; it opens the guidance box in skip mode, whose
            # submit performs the detach.
            self.assertIn("Is this the right profile? Or <button", html)
            self.assertIn("class='skip-link' data-open-skip", html)
            self.assertIn(">Skip</button>?", html)
            self.assertNotIn("alternate-skip", html)      # the old standalone Skip is gone
            self.assertNotIn("Exclude", html)
            self.assertNotIn("Maybe", html)


class TestSelfReportedRetarget(unittest.TestCase):
    """Recover the correct LinkedIn when the contact shared it themselves in their messages."""

    def _task(self, name, attached_pub, self_url):
        return {"no_link": False, "name": name, "candidate_key": attached_pub, "person_ids": ["pid-1"],
                "match_emails": ["a@fb.com"], "match_phones": [],
                "linkedin": {"linkedin_url": f"https://www.linkedin.com/in/{attached_pub}"},
                "dossier": {"self_linkedin_url": self_url,
                            "self_linkedin_pub": reconcile.extract_public_identifier(self_url).lower()}}

    def test_retarget_when_self_reported_differs_and_name_matches(self):
        # attached link is the WRONG namesake; the dossier has the URL they shared themselves
        props = reconcile.self_reported_retargets([self._task(
            "Ankita Goyal", "ankita-goyal-9aa66453", "https://www.linkedin.com/in/ankita-goyal")])
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["old_public_identifier"], "ankita-goyal-9aa66453")
        self.assertEqual(props[0]["new_public_identifier"], "ankita-goyal")
        self.assertEqual(props[0]["approved"], "auto")   # name-compatible -> auto-recover

    def test_pending_when_shared_url_is_a_third_party(self):
        # the shared URL's name doesn't match the contact -> likely someone they mentioned -> pending
        props = reconcile.self_reported_retargets([self._task(
            "Ben Taft", "ben-taft-46830679", "https://www.linkedin.com/in/brandonmoak")])
        self.assertEqual(len(props), 1)
        self.assertEqual(props[0]["approved"], "")       # not auto — needs the user's yes

    def test_no_retarget_when_self_reported_matches(self):
        props = reconcile.self_reported_retargets([self._task(
            "Ankita Goyal", "ankita-goyal", "https://www.linkedin.com/in/ankita-goyal")])
        self.assertEqual(props, [])

    def test_no_retarget_without_self_reported(self):
        t = {"no_link": False, "name": "X", "candidate_key": "x", "person_ids": ["p"], "dossier": {}}
        self.assertEqual(reconcile.self_reported_retargets([t]), [])


class TestDeepResearchEligibility(unittest.TestCase):
    """Deep research targets model detaches and never overwrites user decisions."""

    VERDICTS = [
        {"parent_slug": "p1", "candidate_key": "goodlink",
         "verdict": {"verdict": "confirmed", "confidence": 0.9}},
        {"parent_slug": "p2", "candidate_key": "wronglink",
         "verdict": {"verdict": "wrong_person", "confidence": 0.9, "recommend_deep_research": True}},
        {"parent_slug": "p3", "candidate_key": "absentlink",
         "verdict": {"verdict": "wrong_person", "confidence": 0.9, "recommend_deep_research": True,
                     "linkedin_plausibly_absent": True}},
    ]

    def keys(self, overrides):
        return {r["candidate_key"] for r in dresearch.eligible_subset(self.VERDICTS, 0.85, overrides)}

    def test_model_path_unchanged(self):
        # model wrong_person+recommend eligible; the plausibly-absent one excluded
        self.assertEqual(self.keys({}), {"wronglink"})

    def test_user_detach_is_not_researched(self):
        # The one-row override cannot hold a sticky detach and pending retarget together.
        self.assertEqual(self.keys({"goodlink": {"action": "detach", "approved": "yes"}}),
                         {"wronglink"})

    def test_user_decision_blocks_model_research(self):
        self.assertEqual(self.keys({"wronglink": {"action": "detach", "approved": "yes"}}), set())

    def test_pending_user_detach_not_eligible(self):
        # a detach the user hasn't approved (still pending) does NOT trigger research
        self.assertEqual(self.keys({"goodlink": {"action": "detach", "approved": ""}}), {"wronglink"})

    def test_existing_retarget_skipped(self):
        # already has a correct link -> don't research it
        self.assertEqual(self.keys({"wronglink": {"action": "retarget", "approved": "yes"}}), set())

    # --- heal dead-link detaches: re-research invitations, not decisions ----

    HEAL_VERDICT = {
        "parent_slug": "p9", "candidate_key": "deadlink", "person_ids": ["p-dead"],
        "verdict": {"verdict": "needs_review", "confidence": 0.0,
                    "linkedin_plausibly_absent": True, "recommend_deep_research": False,
                    "reason": reconcile.NO_PROFILE_REASON}}

    def heal_keys(self, overrides):
        return {r["candidate_key"] for r in dresearch.eligible_subset(
            [*self.VERDICTS, self.HEAL_VERDICT], 0.85, overrides,
            include_plausibly_absent=True)}

    def test_heal_detach_is_eligible_for_synthetic_research(self):
        # The heal's dead-link detach (approved=auto, confidence 1.0, source
        # deep-context-heal) is an INVITATION: the person stays a visible
        # pending re-research card, so the >=bar-detach money guard must not
        # swallow them. They route through the plausibly-absent branch.
        overrides = {"deadlink": {"action": "detach", "approved": "auto",
                                  "confidence": "1.000",
                                  "source": "deep-context-heal"}}
        self.assertIn("deadlink", self.heal_keys(overrides))

    def test_human_decided_heal_row_stays_excluded(self):
        overrides = {"deadlink": {"action": "detach", "approved": "no",
                                  "confidence": "1.000",
                                  "source": "deep-context-heal"}}
        self.assertNotIn("deadlink", self.heal_keys(overrides))

    def test_ordinary_at_bar_judge_detach_stays_excluded(self):
        # The original money-bug protection stays pinned: a reconcile-judge
        # detach at/above the bar is decided (the review UI hides it) and is
        # never silently re-billed.
        overrides = {"deadlink": {"action": "detach", "approved": "",
                                  "confidence": "0.900",
                                  "source": "deep-context-reconcile"}}
        self.assertNotIn("deadlink", self.heal_keys(overrides))


class TestOwnerExclusion(unittest.TestCase):
    """The mailbox owner on another email (is_owner) is excluded from the parent layer."""

    def test_is_owner_reads_the_flag(self):
        with tempfile.TemporaryDirectory() as d:
            facts = Path(d)
            (facts / "owner-pid.jsonl").write_text(
                json.dumps({"facts": {"canonical_name": "Arthur Chen", "is_owner": True}}) + "\n", encoding="utf-8")
            (facts / "contact-pid.jsonl").write_text(
                json.dumps({"facts": {"canonical_name": "Arthur Lam", "is_owner": False}}) + "\n", encoding="utf-8")
            self.assertTrue(parents._is_owner("owner-pid", facts))
            self.assertFalse(parents._is_owner("contact-pid", facts))
            self.assertFalse(parents._is_owner("missing-pid", facts))


class _ns:
    """Lightweight argparse.Namespace stand-in for run() calls."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def _run_compose(ns):
    """The old `compose.run(args)` surface over the ComposeDossier node."""
    return compose.ComposeDossier(
        raw_dir=ns.raw_dir, facts_dir=ns.facts_dir, dossier_dir=ns.dossier_dir,
        index_json=ns.index_json, index_md=ns.index_md, person=getattr(ns, "person", ""),
    ).run().to_payload()


def _run_cluster(ns):
    """The old `cluster.run(args)` surface over the ClusterMergeCandidates node.
    `dry_run` routes to the estimate bypass exactly as the CLI does — an estimate
    must never write the stage manifest."""
    kw = dict(vars(ns))
    dry_run = kw.pop("dry_run", False)
    node = cluster.ClusterMergeCandidates(**kw)
    if dry_run:
        return node.estimate()
    return node.run().to_payload()


def _run_parents(ns):
    """The old `parents.run(args)` surface over the BuildParents node. The old
    namespaces omit `people_csv` meaning "none" — map that to a nonexistent
    temp-dir path, NOT the constructor default (the real merged people.csv)."""
    return parents.BuildParents(
        merge_csv=ns.merge_csv,
        people_csv=getattr(ns, "people_csv", "") or Path(ns.parents_dir) / "no-people.csv",
        index_json=ns.index_json, dossier_dir=ns.dossier_dir, facts_dir=ns.facts_dir,
        raw_dir=ns.raw_dir, parents_dir=ns.parents_dir,
        confirm_threshold=getattr(ns, "confirm_threshold", 0.85),
    ).run().to_payload()


def _run_collect(ns):
    """The old `collect.build(args)` surface over the CollectPersonContext node
    (`--dry-run` bypasses the template exactly as the CLI does — it must not
    write the raw manifest)."""
    node = collect.CollectPersonContext(**vars(ns))
    payload = node.execute() if getattr(ns, "dry_run", False) else node.run()
    return payload.to_payload()


def _run_reconcile(ns):
    """The old `reconcile.run(args)` surface over the ReconcileLinkedin node.
    A dry run (without --reapply, which wins) routes to the free estimate
    bypass, exactly as the CLI does."""
    kw = dict(vars(ns))
    dry_run = kw.pop("dry_run", False)
    if dry_run and not kw.get("reapply", False):
        return reconcile.dry_run_estimate(
            index_json=Path(kw["index_json"]), people_csv=Path(kw["people_csv"]),
            profile_cache_dir=Path(kw["profile_cache_dir"]), facts_dir=Path(kw["facts_dir"]),
            raw_dir=Path(kw["raw_dir"]), model=kw["model"], effort=kw["reasoning_effort"],
            slug=kw.get("slug"), limit=kw.get("limit", 0),
        )
    return reconcile.ReconcileLinkedin(**kw).run().to_payload()


def _run_retargets(ns):
    """The old `retargets.run(args)` surface over the ApplyRetargets node."""
    return retargets.ApplyRetargets(
        overrides_csv=ns.overrides_csv, people_csv=ns.people_csv,
        profile_cache_dir=ns.profile_cache_dir, out_csv=ns.out_csv,
    ).run().to_payload()


def _run_dresearch(ns):
    """The old `dresearch.run(args)` surface over the ReconcileDeepResearch node.
    The old return value is the emitted RESULT dict (`node.result`), not the
    manifest receipt; a namespace without `manifest` meant "no receipt writes",
    which the constructor spells `manifest=""`."""
    kw = dict(vars(ns))
    kw.setdefault("manifest", "")
    node = dresearch.ReconcileDeepResearch(**kw)
    node.run()
    return node.result


class TestWhatsAppUSJid(unittest.TestCase):
    """read_whatsapp must match US numbers whose stored JID keeps the +1 country
    code, even though phone_digits() strips it for comparison."""

    def _wacli(self, dirpath: Path) -> Path:
        import sqlite3
        db = dirpath / "wacli.db"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE messages (chat_jid TEXT, text TEXT, ts INTEGER, from_me INTEGER)")
        con.executemany(
            "INSERT INTO messages (chat_jid, text, ts, from_me) VALUES (?,?,?,?)",
            [
                ("14155551234@s.whatsapp.net", "us dm", 1700000000, 0),   # US, country code kept
                ("447911123456@s.whatsapp.net", "uk dm", 1700000100, 0),  # non-US, no stripping
                ("123456@g.us", "group", 1700000200, 0),                  # group — must be excluded
            ],
        )
        con.commit()
        con.close()
        return db

    def test_us_number_with_country_code_jid_is_found(self):
        with tempfile.TemporaryDirectory() as d:
            db = self._wacli(Path(d))
            person = common.Person(person_id="p1", full_name="US Person", phones=["+14155551234"])
            rows = sources.read_whatsapp(person, db)
            self.assertEqual([r["text"] for r in rows], ["us dm"])

    def test_non_us_number_still_matches_and_groups_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            db = self._wacli(Path(d))
            person = common.Person(person_id="p2", full_name="UK Person", phones=["+447911123456"])
            rows = sources.read_whatsapp(person, db)
            self.assertEqual([r["text"] for r in rows], ["uk dm"])

    def test_us_number_stored_without_country_code_also_matches(self):
        # The other arm of the both-forms fix: a store that kept the bare 10-digit
        # JID must still match a +1 contact.
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "wacli.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE messages (chat_jid TEXT, text TEXT, ts INTEGER, from_me INTEGER)")
            con.execute("INSERT INTO messages VALUES ('4155551234@s.whatsapp.net', 'bare dm', 1700000000, 0)")
            con.commit()
            con.close()
            person = common.Person(person_id="p3", full_name="US Person", phones=["+14155551234"])
            rows = sources.read_whatsapp(person, db)
            self.assertEqual([r["text"] for r in rows], ["bare dm"])

    def test_direction_is_mapped_from_from_me(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "wacli.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE messages (chat_jid TEXT, text TEXT, ts INTEGER, from_me INTEGER)")
            con.executemany(
                "INSERT INTO messages VALUES (?,?,?,?)",
                [("14155551234@s.whatsapp.net", "mine", 1700000200, 1),
                 ("14155551234@s.whatsapp.net", "theirs", 1700000100, 0)],
            )
            con.commit()
            con.close()
            person = common.Person(person_id="p4", full_name="US Person", phones=["+14155551234"])
            rows = sources.read_whatsapp(person, db)
            by_text = {r["text"]: r["direction"] for r in rows}
            self.assertEqual(by_text, {"mine": "from_me", "theirs": "from_them"})


class TestSpamRejectColumns(unittest.TestCase):
    """LinkedIn identity reconciliation never writes the retired spam verdict."""

    def _task(self, pub: str, spam: bool, conf: float = 0.9) -> dict:
        return {"candidate_key": pub, "action": "confirm", "person_ids": [f"pid-{pub}"],
                "linkedin": {"linkedin_url": f"https://linkedin.com/in/{pub}"},
                "match_emails": [], "match_phones": [],
                "verdict": {"verdict": "confirmed", "confidence": 0.9, "reason": "r",
                            "spam_contact": spam, "spam_confidence": conf, "spam_reason": "cold outreach" if spam else ""}}

    def test_sticky_user_row_gets_llm_columns_without_touching_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.csv"
            # user already verified this pub (sticky)
            reconcile._write_override_rows(path, {"spammy": {
                **{k: "" for k in reconcile.OVERRIDE_COLUMNS},
                "public_identifier": "spammy", "action": "verify", "approved": "yes"}})
            reconcile.write_overrides(path, [self._task("spammy", spam=True)])
            row = reconcile.load_override_rows(path)["spammy"]
            self.assertEqual(row["action"], "verify")
            self.assertEqual(row["approved"], "yes")  # decision untouched
            self.assertEqual(row["llm_reject"], "")
            self.assertEqual(row["llm_reject_reason"], "")
            reconcile.write_overrides(path, [self._task("spammy", spam=False)])
            self.assertEqual(reconcile.load_override_rows(path)["spammy"]["llm_reject"], "")

    def test_backwards_compatible_with_old_csv_without_llm_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "review.csv"
            old_cols = reconcile.OVERRIDE_COLUMNS[:13]  # pre-spam schema
            path.write_text(",".join(old_cols) + "\nold-pub,verify,yes,,,,,,0.9,r,pid-1,src,t\n", encoding="utf-8")
            rows = reconcile.load_override_rows(path)
            self.assertEqual((rows["old-pub"].get("llm_reject") or ""), "")
            reconcile._write_override_rows(path, rows)  # round-trips onto the new schema
            self.assertIn("llm_reject", path.read_text().splitlines()[0])


class TestSubsetReviewMerge(unittest.TestCase):
    def test_subset_run_overlays_instead_of_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            verdicts = Path(tmpdir) / "verdicts.jsonl"
            rows = [
                {"parent_slug": "alice", "candidate_key": "alice-1", "no_link": False,
                 "linkedin": {}, "verdict": {"verdict": "confirmed", "confidence": 0.9}, "error": ""},
                {"parent_slug": "bob", "candidate_key": "bob-1", "no_link": False,
                 "linkedin": {}, "verdict": {"verdict": "confirmed", "confidence": 0.8}, "error": ""},
            ]
            verdicts.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            fresh = [{"parent_slug": "bob", "candidate_key": "bob-1", "no_link": False,
                      "linkedin": {}, "verdict": {"verdict": "wrong_person", "confidence": 0.95,
                                                  "spam_contact": True, "spam_confidence": 0.9,
                                                  "spam_reason": "cold outreach"}, "error": ""}]
            merged = reconcile.merge_subset_tasks(verdicts, fresh)
            by_key = {(t["parent_slug"], t["candidate_key"]): t for t in merged}
            self.assertEqual(len(merged), 2)  # alice preserved, bob overlaid
            self.assertEqual(by_key[("alice", "alice-1")]["verdict"]["verdict"], "confirmed")
            self.assertEqual(by_key[("bob", "bob-1")]["verdict"]["verdict"], "wrong_person")
            self.assertTrue(by_key[("bob", "bob-1")]["verdict"]["spam_contact"])


class TestAssembleSyntheticProfile(unittest.TestCase):
    def _profile(self, completeness=0.7, linkedin=None, positions=True):
        return {
            "person": {"full_name": "Ross Nordeen", "first_name": "Ross", "last_name": "Nordeen", "confidence": 0.9},
            "location": {"city": "San Francisco", "country": "United States", "raw": ""},
            "headline": {"text": "builder"},
            "summary": {"text": "career summary"},
            "positions": ([{"title": "CTO", "company_name": "StealthCo", "is_current": True},
                           {"title": "Eng", "company_name": "PriorCo", "is_current": False}] if positions else []),
            "education": [{"school_name": "MTU", "degree": "BS"}],
            "social": {"linkedin_url": linkedin, "twitter_handle": "rpoo"},
            "metadata": {"estimated_completeness": completeness, "gaps": ["education dates"],
                         "research_date": "2026-07-09", "research_method": "parallel-core2x",
                         "source_channel": "twitter"},
        }

    def test_synth_identifier_prefers_email_then_phone_then_handle(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        a = asp.synth_public_identifier("A@B.com", "+14155551234", "rpoo")
        b = asp.synth_public_identifier("a@b.com", "", "rpoo")
        self.assertEqual(a, b)  # email normalized, wins over phone
        self.assertTrue(asp.synth_public_identifier("", "+14155551234", "rpoo").startswith("synth-phone-"))
        self.assertEqual(asp.synth_public_identifier("", "", "Rpoo"), "synth-x-rpoo")

    def test_build_row_maps_research_to_people_schema(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        contact = asp.ResearchContact(handle="rpoo", primary_email="ross@x.com",
                                      source_channel="twitter")
        original = {"id": "pid-7", "all_emails": "ross@x.com|r@y.com", "interaction_counts": "{'email': 12}"}
        row = asp.build_synthetic_row(self._profile(), contact, original, "pid-7")
        self.assertTrue(row["public_identifier"].startswith("synth-email-"))
        self.assertEqual(row["enrichment_provider"], "synthetic")
        self.assertEqual(row["entity_urn"], "synthetic:pid-7")
        self.assertEqual(row["current_title"], "CTO")
        self.assertEqual(row["current_company"], "StealthCo")
        self.assertEqual(json.loads(row["work_experiences"])[1]["company_name"], "PriorCo")
        self.assertEqual(row["all_emails"], "ross@x.com|r@y.com")  # carry columns
        self.assertEqual(row["approved"], "auto")  # 0.7 >= 0.6
        self.assertIn("education dates", row["synthetic_metadata"])
        self.assertEqual(row["linkedin_url"], "")

    def test_low_completeness_waits_for_review(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        row = asp.build_synthetic_row(self._profile(completeness=0.3),
                                      asp.ResearchContact(handle="rpoo"), None, "")
        self.assertEqual(row["approved"], "")

    def test_research_contact_merges_sources_later_non_empty_wins(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        verdicts = {"display_name": "Jordan Bravo", "primary_email": "jordan@example.com",
                    "source_parent_slug": "jordan-bravo-1111"}
        queue = {"primary_email": "", "phone_e164": "+15550100", "source_channel": "email",
                 "bio": "not part of the identity"}
        contact = asp.ResearchContact.merged("jordan-bravo-1111", verdicts, queue)
        self.assertEqual(contact.handle, "jordan-bravo-1111")
        self.assertEqual(contact.display_name, "Jordan Bravo")
        self.assertEqual(contact.primary_email, "jordan@example.com")  # blank never overrides
        self.assertEqual(contact.phone_e164, "+15550100")
        self.assertEqual(contact.source_channel, "email")
        self.assertEqual(contact.source_candidate_public_identifier, "")  # absent -> default
        self.assertFalse(hasattr(contact, "bio"))  # unknown columns are dropped, not carried

    def test_usability_floor(self) -> None:
        from packs.ingestion.primitives.deep_context import assemble_synthetic_profile as asp
        self.assertTrue(asp.profile_is_usable(self._profile()))
        no_name = self._profile(); no_name["person"]["full_name"] = ""
        self.assertFalse(asp.profile_is_usable(no_name))
        bare = self._profile(positions=False); bare["location"] = {}
        self.assertFalse(asp.profile_is_usable(bare))


class TestSyntheticReviewUI(unittest.TestCase):
    CSV_HEADER = ("id,public_identifier,full_name,headline,summary,location_raw,work_experiences,"
                  "education,primary_email,primary_phone,enrichment_provider,approved,synthetic_metadata\n")

    def _csv_row(self, approved: str) -> str:
        work = json.dumps([{"title": "CTO", "company_name": "StealthCo", "is_current": True}]).replace('"', '""')
        meta = json.dumps({"completeness": 0.75, "gaps": ["education dates"]}).replace('"', '""')
        return (f'pid-9,synth-email-abc,Ross Nordeen,stealth founder,long summary,"San Francisco, US",'
                f'"{work}","[]",ross@x.com,,synthetic,{approved},"{meta}"\n')

    def test_load_synthetic_parents_states_and_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "synthetic-people.csv"
            path.write_text(self.CSV_HEADER + self._csv_row(""), encoding="utf-8")
            parents = web_model.load_synthetic_parents(path)
            self.assertEqual(len(parents), 1)
            cand = parents[0]["candidates"][0]
            self.assertTrue(cand["synthetic"])
            self.assertEqual(web_model.candidate_state(cand), "review")  # pending -> Needs review pile
            self.assertEqual(cand["experiences"], ["CTO @ StealthCo (present)"])
            self.assertIn("research gaps: education dates", cand["reason"])
            html = web_rendering.render_linkedin_card(parents[0], cand, Path(tmpdir), Path(tmpdir))
            # A synthetic card renders the SAME decision UI as a real-LinkedIn card:
            # the "Is this the right profile?" question, a terminal [No]
            # [Use this profile] binary-actions pair, and a collapsed guidance box.
            # No synthetic-only affordances ("No LinkedIn found" eyebrow /
            # "Add their LinkedIn").
            self.assertNotIn("No LinkedIn found", html)
            self.assertNotIn("Add their LinkedIn", html)
            self.assertNotIn("synthetic-correction", html)
            self.assertIn("Is this the right profile?", html)
            self.assertIn("<div class='binary-actions'>", html)
            self.assertNotIn("Use a different LinkedIn", html)
            # Skip is the inline secondary link folded into the question line.
            self.assertIn("class='skip-link' data-open-skip", html)
            self.assertIn(">Skip</button>?", html)
            self.assertNotIn("alternate-skip", html)
            # "No" decides detach directly (terminal, same as a real card); the
            # guidance box stays as the separate re-research path.
            self.assertIn("data-decide='detach'", html)
            self.assertNotIn("data-open-guidance", html)
            self.assertNotIn("data-fix-form", html)
            # synthetic keep still routes through the synthetic approve gate (/decide
            # treats a keep on a synth- pub as the synthetic-people.csv approval).
            self.assertIn("data-decide='keep'", html)
            self.assertIn(">Use this profile</button>", html)
            self.assertIn(">No</button>", html)
            self.assertNotIn(">Yes</button>", html)
            # A synthetic row has no genuine LinkedIn header: no View-LinkedIn link.
            self.assertNotIn("View LinkedIn", html)
            # approved=auto surfaces as verified
            path.write_text(self.CSV_HEADER + self._csv_row("auto"), encoding="utf-8")
            cand = web_model.load_synthetic_parents(path)[0]["candidates"][0]
            self.assertEqual(web_model.candidate_state(cand), "verified")

    def test_linkedin_correction_is_spaced_below_its_divider(self) -> None:
        css = REVIEW_CSS.read_text(encoding="utf-8")
        self.assertRegex(
            css,
            r'body\[data-stage="linkedin"\] \.alternate\s*\{'
            r"[^}]*padding-top:\s*14px;",
        )

    def test_apply_synthetic_decision_flips_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "synthetic-people.csv"
            path.write_text(self.CSV_HEADER + self._csv_row(""), encoding="utf-8")
            self.assertEqual(web_decisions.apply_synthetic_decision(path, "synth-email-abc", "keep")["approved"], "yes")
            self.assertIn(",yes,", path.read_text())
            self.assertEqual(web_decisions.apply_synthetic_decision(path, "synth-email-abc", "detach")["approved"], "no")
            self.assertEqual(web_decisions.apply_synthetic_decision(path, "synth-email-abc", "reset")["approved"], "")
            with self.assertRaises(ValueError):
                web_decisions.apply_synthetic_decision(path, "synth-ghost", "keep")
            with self.assertRaises(ValueError):
                web_decisions.apply_synthetic_decision(path, "synth-email-abc", "fix")


class TestEligibleSubsetPlausiblyAbsent(unittest.TestCase):
    def _verdict(self, slug: str, absent: bool) -> dict:
        return {"parent_slug": slug, "candidate_key": f"{slug}-key", "person_ids": [slug],
                "verdict": {"verdict": "needs_review", "confidence": 0.5,
                            "linkedin_plausibly_absent": absent, "recommend_deep_research": False}}

    def test_absent_people_excluded_by_default_included_with_flag(self) -> None:
        verdicts = [self._verdict("ghost", absent=True), self._verdict("normal", absent=False)]
        self.assertEqual(dresearch.eligible_subset(verdicts, 0.85, {}), [])
        included = dresearch.eligible_subset(verdicts, 0.85, {}, include_plausibly_absent=True)
        self.assertEqual([r["parent_slug"] for r in included], ["ghost"])


class TestNameMatchAttach(unittest.TestCase):
    """Phase 3: a first-degree connection you also message is name-matched to its LinkedIn and
    judged like any other link — instead of a paid web lookup that guesses a stranger."""

    def _facts(self, facts_dir, pid, name):
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps({
            "chunk_index": 0, "facts": {"canonical_name": name, "aliases": [], "employers": [],
                "title": "", "school": "", "field_of_study": "", "location": "",
                "relationship_to_owner": "friend", "topics": [], "notable_events": [],
                "identifiers": [], "shared_context": [], "confidence": 0.8}, "usage": {}}) + "\n",
            encoding="utf-8")

    def _conn(self, pid, pub, full_name):
        # A LinkedIn Connections row: has a link, carries linkedin_csv, no email/phone to join on.
        return {"id": pid, "public_identifier": pub,
                "linkedin_url": f"https://www.linkedin.com/in/{pub}", "full_name": full_name,
                "headline": "Eng", "work_experiences": "[]", "education": "[]",
                "source_channels": "linkedin_csv"}

    def _msg_person(self, pid, name):
        # A message-derived person: has the display name, NO linkedin (nothing to key on).
        # Fictional data only (RFC-2606 example domain) — no real contacts in tests.
        return {"id": pid, "public_identifier": "", "linkedin_url": "", "full_name": name,
                "primary_email": f"{name.split()[0].lower()}@example.com",
                "source_channels": "gmail_msgvault"}

    def test_names_compatible_handles_abbreviation(self):
        tok = reconcile._name_tokens
        cmp = reconcile._names_compatible
        self.assertTrue(cmp(tok("Robin Ellis"), tok("Robin E.")))    # LinkedIn abbreviates last name
        self.assertTrue(cmp(tok("Casey Nguyen"), tok("Casey Nguyen")))
        self.assertTrue(cmp(tok("Taylor Morgan Reed"), tok("Taylor Reed")))  # ignore middle
        self.assertFalse(cmp(tok("Robin Ellis"), tok("Robin Zhao")))  # the bad web-lookup guess
        self.assertFalse(cmp(tok("Robin"), tok("Robin E.")))         # a lone first name never matches
        self.assertFalse(cmp(tok("Sam Rivera"), tok("Alex Rivera")))  # different first name

    def test_unique_connection_match_requires_uniqueness(self):
        conns = reconcile.connection_name_rows({
            "c1": self._conn("c1", "robine1", "Robin E."),
            "c2": self._conn("c2", "robine2", "Robin E."),   # a second same-name connection
            "e1": self._conn("e1", "casey-nguyen", "Casey Nguyen")})
        self.assertIsNone(reconcile.unique_connection_match("Robin Ellis", conns))   # ambiguous -> None
        self.assertEqual(
            reconcile.unique_connection_match("Casey Nguyen", conns)["public_identifier"],
            "casey-nguyen")

    def test_build_tasks_attaches_unique_name_match_optimistically(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            self._facts(facts, "msg-robin", "Robin Ellis")
            self._facts(facts, "msg-nomatch", "Pat Quinn")
            index = {"slugs": {"robin-c": {"person_id": "msg-robin"},
                               "nomatch-c": {"person_id": "msg-nomatch"}},
                     "parents": {"robin-p": {"name": "Robin Ellis", "children": ["robin-c"]},
                                 "nomatch-p": {"name": "Pat Quinn", "children": ["nomatch-c"]}}}
            people = {
                "msg-robin": self._msg_person("msg-robin", "Robin Ellis"),
                "msg-nomatch": self._msg_person("msg-nomatch", "Pat Quinn"),
                # the first-degree connection, a SEPARATE row with the abbreviated export name
                "conn-robin": self._conn("conn-robin", "robinelliszz", "Robin E.")}
            tasks = {t["parent_slug"]: t for t in reconcile.build_tasks(index, people, facts, raw, cache)}
            robin = tasks["robin-p"]
            self.assertTrue(robin["name_matched"])
            self.assertFalse(robin["no_link"])
            self.assertFalse(robin["from_connections"])         # optimistic, NOT ground truth
            self.assertEqual(robin["candidate_key"], "robinelliszz")
            self.assertEqual(robin["person_ids"], ["msg-robin"])
            self.assertTrue(tasks["nomatch-p"]["no_link"])       # no connection matches -> unchanged

    def test_ambiguous_name_match_stays_no_link(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            facts, raw, cache = base / "facts", base / "raw", base / "cache"
            facts.mkdir(); raw.mkdir(); cache.mkdir()
            self._facts(facts, "msg-robin", "Robin Ellis")
            index = {"slugs": {"robin-c": {"person_id": "msg-robin"}},
                     "parents": {"robin-p": {"name": "Robin Ellis", "children": ["robin-c"]}}}
            people = {"msg-robin": self._msg_person("msg-robin", "Robin Ellis"),
                      "conn-a": self._conn("conn-a", "robina", "Robin E."),
                      "conn-b": self._conn("conn-b", "robinb", "Robin E.")}   # two same-name connections
            (task,) = reconcile.build_tasks(index, people, facts, raw, cache)
            self.assertTrue(task["no_link"])
            self.assertNotIn("name_matched", task)

    def test_unconfirmed_name_match_reverts_to_no_link(self):
        confirmed = {"parent_slug": "a", "name": "A", "candidate_key": "aconn",
                     "person_ids": ["candidate:email:a@x.com"], "no_link": False,
                     "name_matched": True, "linkedin": {"linkedin_url": "x"},
                     "verdict": _verdict("confirmed", 0.9)}
        needs_review = {"parent_slug": "b", "name": "B", "candidate_key": "bconn",
                        "person_ids": ["candidate:email:b@x.com"], "no_link": False,
                        "name_matched": True, "linkedin": {"linkedin_url": "y"},
                        "verdict": _verdict("needs_review", 0.4)}
        reverted = reconcile.revert_unconfirmed_name_matches([confirmed, needs_review], 0.7)
        self.assertEqual(reverted, 1)
        self.assertFalse(confirmed["no_link"])          # confirmed match stays an identity row
        self.assertTrue(confirmed["name_matched"])
        self.assertTrue(needs_review["no_link"])         # unconfirmed falls back to worth/lookup
        self.assertFalse(needs_review["name_matched"])
        self.assertEqual(needs_review["candidate_key"], "")

    def test_name_match_never_detaches_the_connection(self):
        # Even a high-confidence wrong_person on a name-matched task must NOT detach: the link
        # belongs to a real connection (a separate row), so a wrong guess is dropped, not applied.
        task = {"parent_slug": "a", "name": "A", "person_ids": ["p"], "conflict": False,
                "no_link": False, "name_matched": True, "verdict": _verdict("wrong_person", 0.99)}
        reconcile.decide_actions([task], 0.85)
        self.assertNotEqual(task["action"], "detach")

    def test_confirmed_name_match_folds_onto_connection(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            people = base / "people.csv"
            cols = ["id", "public_identifier", "linkedin_url", "primary_email", "all_emails",
                    "primary_phone", "all_phones", "interaction_counts", "source_channels"]
            with people.open("w", newline="") as fh:
                w = __import__("csv").DictWriter(fh, fieldnames=cols)
                w.writeheader()
                w.writerow({"id": "msg-robin", "public_identifier": "",
                            "primary_email": "robin@acme.test", "all_emails": '["robin@acme.test"]',
                            "interaction_counts": '{"gmail": 7}', "source_channels": "gmail_msgvault"})
            # A confirmed name-match: ONE identity task, no detached sibling, kept pids == all pids.
            task = {"parent_slug": "robin", "name": "Robin Ellis", "candidate_key": "robinelliszz",
                    "person_ids": ["msg-robin"], "parent_person_ids": ["msg-robin"], "conflict": False,
                    "no_link": False, "name_matched": True,
                    "linkedin": {"linkedin_url": "https://www.linkedin.com/in/robinelliszz"},
                    "match_emails": ["robin@acme.test"], "match_phones": [],
                    "verdict": _verdict("confirmed", 0.9)}
            reconcile.decide_actions([task], 0.85)
            self.assertEqual(task["action"], "confirm")
            out = base / "consolidate.csv"
            stats = reconcile.write_consolidations(out, [task], people)
            self.assertEqual(stats["consolidated_parents"], 1)   # folds despite no sibling to detach
            import csv as _csv
            with out.open() as fh:
                row = next(_csv.DictReader(fh))
            self.assertEqual(row["public_identifier"], "robinelliszz")  # keyed by the connection
            self.assertEqual(row["primary_email"], "robin@acme.test")      # message contacts folded on
            self.assertIn("gmail", row["interaction_counts"])

    def test_no_llm_never_auto_confirms_a_name_match(self):
        # Offline stub trusts a normal attached link, but a SPECULATIVE name-match must not be
        # auto-confirmed (that would bypass the judgment the LLM is meant to make).
        speculative = reconcile.deterministic_verdict(
            {"name_matched": True, "linkedin": {"has_profile": True}})
        self.assertNotEqual(speculative["verdict"], "confirmed")
        normal = reconcile.deterministic_verdict({"linkedin": {"has_profile": True}})
        self.assertEqual(normal["verdict"], "confirmed")

    def test_name_match_prompt_requires_a_non_name_signal(self):
        task = {"name_matched": True, "name": "Robin Ellis", "match_emails": [], "match_phones": [],
                "dossier": {"relationship": "", "title": "", "employers": [], "school": "",
                            "location": "", "topics": [], "shared_context": [],
                            "from_me": [], "from_them": []},
                "linkedin": {"linkedin_url": "https://www.linkedin.com/in/robine", "full_name": "Robin E.",
                             "headline": "", "location": "", "experiences": [], "education": []}}
        prompt = reconcile.judge_prompt(task, "")
        self.assertIn("SPECULATIVE", prompt)
        self.assertIn("NON-NAME", prompt)
        self.assertIn("needs_review", prompt)

    def test_reapply_reverts_a_no_longer_confirmed_name_match(self):
        # A verdict that used to clear a lower bar but no longer meets confirm_threshold must fall
        # back to no-link on reapply, not linger as a stale LinkedIn review row.
        stale = {"parent_slug": "a", "name": "A", "candidate_key": "aconn",
                 "person_ids": ["candidate:email:a@x.com"], "no_link": False, "name_matched": True,
                 "linkedin": {"linkedin_url": "x"}, "verdict": _verdict("confirmed", 0.75)}
        reverted = reconcile.revert_unconfirmed_name_matches([stale], 0.85)
        self.assertEqual(reverted, 1)
        self.assertTrue(stale["no_link"])
        self.assertEqual(stale["candidate_key"], "")

    def test_subset_refresh_replaces_all_tasks_for_a_parent(self):
        # verdicts.jsonl holds a prior name-matched LinkedIn task for parent "robin"; a subset rerun
        # reverts it to no-link (candidate_key "" instead of the connection pub). The merge must
        # drop the stale LinkedIn task, not keep BOTH keyed by their differing candidate_keys.
        with tempfile.TemporaryDirectory() as d:
            jsonl = Path(d) / "verdicts.jsonl"
            reconcile.write_verdicts(jsonl, Path(d) / "verdicts.csv", [
                {"parent_slug": "robin", "name": "Robin Ellis", "candidate_key": "robinelliszz",
                 "person_ids": ["msg-robin"], "conflict": False, "no_link": False, "name_matched": True,
                 "linkedin": {"linkedin_url": "x"}, "match_emails": [], "match_phones": [],
                 "verdict": _verdict("confirmed", 0.9), "error": ""},
                {"parent_slug": "other", "name": "Other", "candidate_key": "otherpub",
                 "person_ids": ["p"], "conflict": False, "no_link": False,
                 "linkedin": {"linkedin_url": "y"}, "match_emails": [], "match_phones": [],
                 "verdict": _verdict("confirmed", 0.9), "error": ""}])
            fresh = [{"parent_slug": "robin", "name": "Robin Ellis", "candidate_key": "",
                      "person_ids": ["msg-robin"], "conflict": False, "no_link": True,
                      "linkedin": {}, "verdict": _verdict("needs_review", 0.0)}]
            merged = reconcile.merge_subset_tasks(jsonl, fresh)
            robin = [t for t in merged if t["parent_slug"] == "robin"]
            self.assertEqual(len(robin), 1)              # exactly one task for the refreshed parent
            self.assertTrue(robin[0]["no_link"])         # the fresh no-link one; stale LinkedIn dropped
            self.assertEqual(len(merged), 2)             # untouched "other" parent still present
            self.assertTrue(any(t["parent_slug"] == "other" for t in merged))

    def test_write_verdicts_persists_name_matched_for_reapply(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            jsonl, csvp = base / "verdicts.jsonl", base / "verdicts.csv"
            reconcile.write_verdicts(jsonl, csvp, [
                {"parent_slug": "a", "name": "A", "candidate_key": "aconn", "person_ids": ["p"],
                 "conflict": False, "no_link": False, "name_matched": True,
                 "linkedin": {"linkedin_url": "x"}, "match_emails": [], "match_phones": [],
                 "verdict": _verdict("confirmed", 0.9), "error": ""}])
            (loaded,) = reconcile.load_tasks_from_verdicts(jsonl)
            self.assertTrue(loaded["name_matched"])   # survives the round-trip -> reapply stays safe


class TestMergeIdentifierEmails(unittest.TestCase):
    """Same-person recall uses only CONTACT-owned message emails, never a third-party mention."""

    def _p(self, name, emails=(), extra=(), phones=()):
        return {"name": name, "name_key": cluster.normalize_name(name), "emails": list(emails),
                "extra_emails": list(extra), "phone_digits": list(phones)}

    def test_identifier_emails_keeps_only_full_emails(self):
        got = cluster.identifier_emails([
            "jordan.chen@example.net", "https://linkedin.com/in/x",
            "https://meet.google.com/abc", "Jordan", "+1 (415) 555-1212", "JORDAN.C@Work.EXAMPLE"])
        self.assertEqual(got, {"jordan.chen@example.net", "jordan.c@work.example"})

    def test_owned_message_email_pairs_with_its_registered_owner(self):
        # Yale's owned message-email IS Chen's registered address -> proposed, despite different
        # surnames. An unrelated third record is not dragged in.
        people = [
            self._p("Morgan Yale", emails=["morgan.yale@example.com"], extra=["jordan.chen@example.net"]),
            self._p("Jordan Chen", emails=["jordan.chen@example.net"]),
            self._p("Unrelated Person", emails=["someone@example.org"]),
        ]
        pairs = cluster.generate_pairs(people)
        self.assertIn((0, 1), pairs)
        self.assertNotIn((0, 2), pairs)
        self.assertNotIn((1, 2), pairs)

    def test_shared_first_name_localpart_never_pairs(self):
        # Two different people whose only overlap is a first-name local-part (robin@…) — via
        # message emails on different domains — must NOT pair: local-parts come from registered
        # emails only, and the full addresses differ.
        people = [
            self._p("Robin Kwan", emails=["robin.kwan@example.com"], extra=["robin@shared.example"]),
            self._p("Robin Feld", emails=["robin.feld@example.org"], extra=["robin@other.example"]),
        ]
        self.assertEqual(cluster.generate_pairs(people), set())

    def test_shared_owned_message_email_is_proposed_for_the_judge(self):
        # Two records that both own team@shared.example are proposed for the LLM judge.
        people = [self._p("Alice Smith", emails=["a@example.com"], extra=["team@shared.example"]),
                  self._p("Bob Jones", emails=["c@example.org"], extra=["team@shared.example"])]
        self.assertIn((0, 1), cluster.generate_pairs(people))

    def test_owner_email_is_excluded_from_owned_message_identifiers(self):
        import tempfile as _tf
        with _tf.TemporaryDirectory() as d:
            base = Path(d)
            (base / "owner.json").write_text(json.dumps({"emails": ["me@owner.example"]}), encoding="utf-8")
            dossiers = base / "dossiers"; raw = base / "raw"; facts = base / "facts"
            for p in (dossiers, raw, facts):
                p.mkdir()
            pid = "candidate:email:kai@work.example"
            (dossiers / "kai-c.md").write_text(
                '---\nname: "Kai"\nemails: ["kai@work.example"]\nphones: []\n---\n<!-- x -->\n', encoding="utf-8")
            (raw / f"{pid}.json").write_text(json.dumps({"messages": []}), encoding="utf-8")
            (facts / f"{pid}.jsonl").write_text(json.dumps({"chunk_index": 0, "facts": {
                "canonical_name": "Kai", "identifiers": ["me@owner.example", "kai.alt@home.example"],
                "owned_identifiers": {"emails": ["me@owner.example", "kai.alt@home.example"], "phones": [], "urls": []}}, "usage": {}}) + "\n",
                encoding="utf-8")
            index = {"slugs": {"kai-c": {"person_id": pid}}, "by_phone": {}}
            (person,) = cluster.load_people(index, dossiers, raw, facts)
            self.assertIn("kai.alt@home.example", person["extra_emails"])   # a genuine second address is kept
            self.assertNotIn("me@owner.example", person["extra_emails"])    # the owner's is dropped as noise


class TestCompositeBlockingKeys(unittest.TestCase):
    """Name blocking uses composite <first>|<last-initial> + <first-initial>|<last> keys, not
    per-token. Splits common-surname buckets by first-initial (no shared-surname O(n^2), no
    200-cap cliff), keeps nickname/truncated-surname dupes, and stops same-first-name /
    different-surname false-merge pairs from ever reaching the judge. Fixtures are synthetic."""

    def _p(self, name, emails=(), extra=(), phones=()):
        return {"name": name, "name_key": cluster.normalize_name(name), "emails": list(emails),
                "extra_emails": list(extra), "phone_digits": list(phones)}

    def keys(self, name):
        return cluster.blocking_name_keys(cluster.normalize_name(name))

    def shares(self, a, b):
        return bool(self.keys(a) & self.keys(b))

    def test_nickname_first_name_co_blocks(self):
        self.assertTrue(self.shares("Chris Foxtrot", "Christopher Foxtrot"))    # first-initial|last

    def test_truncated_surname_co_blocks(self):
        self.assertTrue(self.shares("Jordan Bravado", "Jordan Brav"))           # first|last-initial
        self.assertTrue(self.shares("Jordan Bravo", "Jordan B"))

    def test_initial_surname_keeps_its_last_initial(self):
        # 'Casey S.' must keep 'S' as a last-initial so it meets 'Casey Sierra'.
        self.assertTrue(self.shares("Casey S.", "Casey Sierra"))

    def test_same_first_name_different_surname_does_not_co_block(self):
        # The false-merge class: same first name, different real surnames never meet on name.
        self.assertFalse(self.shares("Jordan Alpha", "Jordan Bravo"))

    def test_common_surname_splits_by_first_initial(self):
        self.assertFalse(self.shares("Alice Kilo", "Bob Kilo"))     # different first-initial -> split
        # ...but same first-initial + same surname still meets (adversarial case, by design).
        self.assertTrue(self.shares("Alice Kilo", "Adam Kilo"))

    def test_hyphen_and_spacing_variants_co_block(self):
        self.assertTrue(self.shares("Robin Del-Tango", "Robin DelTango"))

    def test_surnameless_records_meet_via_first_name_fallback(self):
        self.assertTrue(self.shares("Robin", "Robin F"))            # both sparse -> fn:robin
        # a record with a REAL surname never emits fn:* -> common first names don't re-explode
        self.assertNotIn("fn:alice", self.keys("Alice Kilo"))

    def test_generate_pairs_never_proposes_the_false_merge_class(self):
        people = [self._p("Jordan Alpha", emails=["a@ex1.example"]),
                  self._p("Jordan Bravo", emails=["b@ex2.example"])]
        self.assertEqual(cluster.generate_pairs(people), set())

    def test_generate_pairs_keeps_a_real_nickname_pair(self):
        people = [self._p("Chris Foxtrot", emails=["c1@ex.example"]),
                  self._p("Christopher Foxtrot", emails=["c2@ex.example"])]
        self.assertIn((0, 1), cluster.generate_pairs(people))


class TestMergeCache(unittest.TestCase):
    """The same-person merge reuses prior verdicts (merge-verdicts.csv) so reruns only judge
    NEW/changed pairs. The pair signature is the correctness crux: stable across runs, order-
    independent, and it changes exactly when a pair's judge inputs change."""

    def _p(self, slug, name, emails=(), extra=(), profile=None):
        return {"slug": slug, "name": name, "name_key": cluster.normalize_name(name),
                "emails": list(emails), "extra_emails": list(extra), "phone_digits": [],
                "profile": profile or {"relationship": "", "title": "", "employers": [], "school": "",
                                       "location": "", "topics": []}}

    def test_pair_sig_is_stable_and_order_independent(self):
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        b = self._p("b", "Ann Li", ["ann@example.net"])
        self.assertEqual(cluster.pair_sig(a, b), cluster.pair_sig(a, b))       # stable
        self.assertEqual(cluster.pair_sig(a, b), cluster.pair_sig(b, a))       # order-independent

    def test_pair_sig_changes_when_identity_changes(self):
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        b = self._p("b", "Ann Li", ["ann@example.net"])
        base = cluster.pair_sig(a, b)
        b_new_email = self._p("b", "Ann Li", ["ann@example.net", "ann2@example.org"])
        self.assertNotEqual(cluster.pair_sig(a, b_new_email), base)           # new email -> re-judge
        b_new_job = self._p("b", "Ann Li", ["ann@example.net"],
                            profile={"relationship": "", "title": "", "employers": ["NewCo"],
                                     "school": "", "location": "", "topics": []})
        self.assertNotEqual(cluster.pair_sig(a, b_new_job), base)             # new employer -> re-judge

    def test_split_reuses_matching_sig_and_rejudges_the_rest(self):
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        b = self._p("b", "Ann Li", ["ann@example.com"])   # shared email -> a real pair
        c = self._p("c", "Bob Fox", ["bob@example.org"])
        people = [a, b, c]
        cache = {frozenset({"a", "b"}): (cluster.pair_sig(a, b),
                                         {"same_person": True, "confidence": 0.9,
                                          "tone_consistent": True, "reason": "cached"})}
        reused, to_judge = cluster.split_cached_pairs([(0, 1), (0, 2)], people, cache)
        self.assertEqual({(r[0], r[1]) for r in reused}, {(0, 1)})   # cached pair reused
        self.assertEqual({(t[0], t[1]) for t in to_judge}, {(0, 2)})  # uncached pair judged

    def _write_legacy_csv(self, path: Path, rows: list[dict]) -> None:
        # A pre-sig merge-verdicts.csv: name-only, no slug_a/slug_b/sig columns.
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["name_a", "name_b", "same_person",
                                               "confidence", "tone_consistent", "reason"])
            w.writeheader()
            w.writerows(rows)

    def test_legacy_verdicts_adopted_by_name(self):
        # An old file that predates the sig columns is reused by matching names back to the people.
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        b = self._p("b", "Bob Fox", ["bob@example.org"])
        people = [a, b]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "merge-verdicts.csv"
            self._write_legacy_csv(path, [{"name_a": "Ann Lee", "name_b": "Bob Fox",
                                           "same_person": "True", "confidence": "0.88",
                                           "tone_consistent": "True", "reason": "paid earlier"}])
            legacy = cluster.load_legacy_verdicts(path, people)
        self.assertIn(frozenset({"a", "b"}), legacy)
        self.assertEqual(legacy[frozenset({"a", "b"})]["reason"], "paid earlier")
        self.assertTrue(legacy[frozenset({"a", "b"})]["same_person"])

    def test_legacy_ambiguous_name_is_not_adopted(self):
        # Two current people share a name -> can't safely map the old row; that pair re-judges.
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        dup = self._p("a2", "Ann Lee", ["ann2@example.net"])
        b = self._p("b", "Bob Fox", ["bob@example.org"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "merge-verdicts.csv"
            self._write_legacy_csv(path, [{"name_a": "Ann Lee", "name_b": "Bob Fox",
                                           "same_person": "True", "confidence": "0.9",
                                           "tone_consistent": "True", "reason": "x"}])
            legacy = cluster.load_legacy_verdicts(path, [a, dup, b])
        self.assertEqual(legacy, {})   # ambiguous "Ann Lee" -> row skipped

    def test_sigged_rows_are_ignored_by_legacy_loader(self):
        # A row that already has slug/sig belongs to the precise cache, not the legacy adopter.
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        b = self._p("b", "Bob Fox", ["bob@example.org"])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "merge-verdicts.csv"
            with path.open("w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["slug_a", "slug_b", "name_a", "name_b",
                                                   "same_person", "confidence", "tone_consistent",
                                                   "reason", "sig"])
                w.writeheader()
                w.writerow({"slug_a": "a", "slug_b": "b", "name_a": "Ann Lee", "name_b": "Bob Fox",
                            "same_person": "True", "confidence": "0.9", "tone_consistent": "True",
                            "reason": "x", "sig": cluster.pair_sig(a, b)})
            legacy = cluster.load_legacy_verdicts(path, [a, b])
        self.assertEqual(legacy, {})   # sig-keyed row is not adopted as legacy

    def test_split_adopts_legacy_and_stamps_current_sig(self):
        # A legacy-adopted pair is reused (no judge) and carries the CURRENT sig so it upgrades in place.
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        b = self._p("b", "Ann Li", ["ann@example.com"])
        c = self._p("c", "Bob Fox", ["bob@example.org"])
        people = [a, b, c]
        legacy = {frozenset({"a", "b"}): {"same_person": True, "confidence": 0.88,
                                          "tone_consistent": True, "reason": "adopted"}}
        reused, to_judge = cluster.split_cached_pairs([(0, 1), (0, 2)], people, {}, legacy)
        self.assertEqual({(r[0], r[1]) for r in reused}, {(0, 1)})
        adopted = next(r for r in reused if (r[0], r[1]) == (0, 1))
        self.assertEqual(adopted[2], cluster.pair_sig(a, b))   # stamped with current sig
        self.assertEqual(adopted[3]["reason"], "adopted")
        self.assertEqual({(t[0], t[1]) for t in to_judge}, {(0, 2)})  # unknown pair still judged

    def test_sig_cache_wins_over_legacy(self):
        # When a pair is in BOTH the sig cache (matching) and legacy, the precise verdict wins.
        a = self._p("a", "Ann Lee", ["ann@example.com"])
        b = self._p("b", "Ann Li", ["ann@example.com"])
        people = [a, b]
        cache = {frozenset({"a", "b"}): (cluster.pair_sig(a, b),
                                         {"same_person": True, "confidence": 0.95,
                                          "tone_consistent": True, "reason": "precise"})}
        legacy = {frozenset({"a", "b"}): {"same_person": False, "confidence": 0.1,
                                          "tone_consistent": False, "reason": "stale-legacy"}}
        reused, to_judge = cluster.split_cached_pairs([(0, 1)], people, cache, legacy)
        self.assertEqual(reused[0][3]["reason"], "precise")
        self.assertEqual(to_judge, [])


class TestDirectoryView(unittest.TestCase):
    """The /directory browse surface: A-Z sidebar island + read-only person pane."""

    @staticmethod
    def _parent(slug: str, name: str, **candidate: object) -> dict:
        # Confirmed by default: the directory only renders machine ("auto") or
        # human ("yes") confirmed identities; pass approved="" for a pending one.
        base = {
            "pub": f"{slug}-pub", "full_name": name,
            "approved": "auto", "action": "verify",
            "match_emails": [], "match_phones": [],
        }
        base.update(candidate)
        return {"slug": slug, "dossier_slug": slug, "name": name,
                "person_ids": [f"candidate:email:{slug}@example.com"],
                "candidates": [base]}

    def test_markdown_to_html_renders_dossier_shapes(self):
        markdown = (
            "# Jordan Bravo\n\n"
            "<!-- parent-link --> _Part of [[jordan-parent]] **Jordan Bravo**_\n\n"
            "## Summary\n\n"
            "Knows **everyone** at [[acme-corp|Acme]] — _allegedly_.\n"
            "Second line of the same paragraph.\n\n"
            "## Timeline\n\n"
            "- **2026-01-02** — Said hello\n"
            "- Replied <script>alert(1)</script>\n\n"
            + "─" * 56 + "\n\n"
            "Tail after the merge rule.\n")
        html = web_rendering.markdown_to_html(markdown)
        self.assertIn("<h3>Jordan Bravo</h3>", html)
        self.assertIn("<h4>Summary</h4>", html)
        self.assertIn("Knows <strong>everyone</strong> at Acme — <em>allegedly</em>. "
                      "Second line of the same paragraph.", html)
        self.assertIn("<ul><li><strong>2026-01-02</strong> — Said hello</li>", html)
        self.assertIn("&lt;script&gt;", html)      # dossier text can never inject markup
        self.assertNotIn("<script>", html)
        self.assertIn("<hr>", html)
        self.assertNotIn("parent-link", html)       # HTML comments are stripped
        self.assertNotIn("[[", html)                # wiki links become display text

    def test_directory_entries_sorted_deduped_and_worth_labeled(self):
        yes = self._parent("zed-zulu", "Zed Zulu")
        yes["worth_row"] = {"effective": "yes"}
        no = self._parent("amy-alpha", "Amy Alpha")
        no["worth"] = {"decision": "no"}  # no worth_row -> parent effective
        parents = [
            yes,
            no,
            {"slug": "split-twin", "dossier_slug": "amy-alpha", "name": "Amy Alpha",
             "candidates": []},  # split parent sharing a dossier appears once
            {"slug": "", "name": "No Slug", "candidates": []},
            self._parent("mel-maybe", "Mel Maybe"),  # undecided -> maybe
        ]
        entries = web_rendering.directory_entries(parents)
        self.assertEqual(entries, [
            {"slug": "amy-alpha", "name": "Amy Alpha", "worth": "no"},
            {"slug": "mel-maybe", "name": "Mel Maybe", "worth": "maybe"},
            {"slug": "zed-zulu", "name": "Zed Zulu", "worth": "yes"},
        ])

    def test_person_detail_reuses_profile_renderers_over_full_dossier(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            dossiers = base / "dossiers"
            dossiers.mkdir()
            (dossiers / "jordan-parent.md").write_text(
                "---\nslug: jordan-parent\n---\n\n# Jordan Bravo\n\n"
                "## Relationship & cadence\n\nWarm intro via Casey.\n\n"
                "## Identifiers\n\n- jordan.bravo@acme.com\n- casey@example.com\n",
                encoding="utf-8")
            parent = self._parent(
                "jordan-parent", "Jordan Bravo",
                url="https://www.linkedin.com/in/jordan-bravo-test",
                headline="Builds things", location="Springfield",
                experiences=["Engineer @ Acme (2020 - Present)"],
                education=["BS — State"],
                match_emails=["jordan@example.com"])
            html = web_rendering.render_person_detail(
                parent, base / "parents", dossiers, base / "profiles")
        self.assertIn("Jordan Bravo", html)
        self.assertIn("View LinkedIn", html)
        self.assertIn("Builds things", html)
        self.assertIn("<div><dt>Work</dt>", html)
        self.assertIn("<div><dt>Education</dt>", html)
        # Contact merges match values with the dossier's Identifiers section —
        # but only identifiers the contact policy can prove are THIS person's:
        # casey@example.com is someone else on the thread and never surfaces.
        self.assertIn("jordan@example.com · jordan.bravo@acme.com", html)
        self.assertNotIn("casey@example.com", html)
        self.assertIn("<h4>Relationship &amp; cadence</h4>", html)
        self.assertIn("Warm intro via Casey.", html)
        # Browse-only: no decision affordances anywhere in the pane.
        for marker in ("data-worth", "data-decide", "data-complete", "data-open-guidance"):
            self.assertNotIn(marker, html)

    def test_directory_page_embeds_island_and_selected_person(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            dossiers = base / "dossiers"
            dossiers.mkdir()
            (dossiers / "amy-alpha.md").write_text(
                "# Amy Alpha\n\n## Summary\n\nAlpha tester.\n", encoding="utf-8")
            amy = self._parent("amy-alpha", "Amy Alpha")
            amy["worth_row"] = {"effective": "yes"}
            zed = self._parent("zed-zulu", "Zed Zulu")
            zed["worth_row"] = {"effective": "no"}
            parents = [amy, zed]
            kwargs = {"parents_dir": base / "parents", "dossier_dir": dossiers,
                      "profile_cache_dir": base / "profiles"}
            html = web_rendering.directory_page_html(parents, {}, **kwargs).decode("utf-8")
            picked = web_rendering.directory_page_html(
                parents, {"person": ["amy-alpha"]}, **kwargs).decode("utf-8")
        self.assertIn("data-directory-people", html)
        self.assertIn("data-directory-list", html)
        self.assertIn("data-directory-search", html)
        self.assertIn('"slug": "zed-zulu"', html)
        self.assertIn("data-stage='directory'", html)
        self.assertIn("data-external-updates='false'", html)  # no SSE on this page
        self.assertIn("Pick a person", html)                  # no selection -> empty state
        # Worth tabs sit UNDER the search bar; Maybe hides at zero.
        self.assertIn("decision-tab active' data-directory-tab='yes'>Yes<span>1</span>", html)
        self.assertNotIn("data-directory-tab='maybe'", html)
        self.assertIn("data-directory-tab='no'>No<span>1</span>", html)
        self.assertLess(html.index("data-directory-search"),
                        html.index("data-directory-tab='yes'"))
        self.assertIn("Alpha tester.", picked)                # ?person= pre-renders the pane
        self.assertNotIn("Pick a person", picked)
        # The facts table already carries a "Summary" row; the dossier's own
        # heading is dropped so the pane never says Summary twice (the summary
        # TEXT stays, asserted above).
        self.assertNotIn("<h4>Summary</h4>", picked)

    def test_directory_maybe_tab_is_the_burn_down_pile(self):
        parents = [self._parent("mel-maybe", "Mel Maybe")]  # only undecided people
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            html = web_rendering.directory_page_html(
                parents, {}, parents_dir=base / "parents", dossier_dir=base / "dossiers",
                profile_cache_dir=base / "profiles").decode("utf-8")
        # Undecided people live on the Maybe tab (they re-roll every refresh
        # until decided); Yes stays the default even at zero.
        self.assertIn("data-directory-tab='maybe'>Maybe<span>1</span>", html)
        self.assertIn("decision-tab active' data-directory-tab='yes'>Yes<span>0</span>", html)
        self.assertIn("data-directory-tab='no'>No<span>0</span>", html)

    def test_serve_stage_directory_lands_on_directory_and_writes_nothing(self):
        # The explicit read-only browse landing opens /directory and never
        # begins a people-review revision (no review manifest write).
        from packs.ingestion.primitives.deep_context.review_web import cli as web_cli
        parsed = web_cli.build_parser().parse_args(["serve", "--stage", "directory"])
        self.assertEqual(parsed.stage, "directory")
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            manifest = base / "review" / "manifest.json"
            args = mock.Mock(
                review=str(base / "review.csv"), verdicts=str(base / "verdicts.jsonl"),
                synthetic_people=str(base / "synthetic.csv"), facts_dir=str(base / "facts"),
                people_csv=str(base / "people.csv"), parents_dir=str(base / "parents"),
                dossier_dir=str(base / "dossiers"),
                profile_cache_dir=str(base / "profiles"),
                manifest=str(manifest),
                enrichment_manifest=str(base / "research" / "manifest.json"),
                avatar_dir=str(base / "avatars"),
                host="127.0.0.1", port=43211, stage="directory", fresh=False,
                open=False, confirm_threshold=0.7, detach_threshold=0.85,
            )
            fake_server = mock.Mock(server_address=("127.0.0.1", 43211))
            out = io.StringIO()
            with mock.patch.object(
                    web_server.urllib.request, "urlopen",
                    side_effect=web_server.urllib.error.URLError("not running")), \
                 mock.patch.object(web_server, "_all_review_parents", return_value=[]), \
                 mock.patch.object(web_server, "ThreadingHTTPServer",
                                   return_value=fake_server), \
                 contextlib.redirect_stdout(out):
                web_server.cmd_serve(args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["url"], "http://127.0.0.1:43211/directory")
            self.assertFalse(manifest.exists())

    def test_serve_reuses_live_server_without_touching_the_session_lock(self):
        # The live server HOLDS the session flock; the reuse path must never
        # try to take it (locking first refused the very server being reused —
        # directory browsing and the enrichment-running review deferral hit this).
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            manifest = base / "review" / "manifest.json"
            args = mock.Mock(
                review=str(base / "review.csv"), verdicts=str(base / "verdicts.jsonl"),
                synthetic_people=str(base / "synthetic.csv"), facts_dir=str(base / "facts"),
                people_csv=str(base / "people.csv"), parents_dir=str(base / "parents"),
                dossier_dir=str(base / "dossiers"),
                profile_cache_dir=str(base / "profiles"),
                manifest=str(manifest),
                enrichment_manifest=str(base / "research" / "manifest.json"),
                avatar_dir=str(base / "avatars"),
                host="127.0.0.1", port=43212, stage="directory", fresh=False,
                open=False, confirm_threshold=0.7, detach_threshold=0.85,
            )
            live = mock.Mock()
            live.read.return_value = json.dumps({
                "primitive": "reconcile_review_web", "manifest": str(manifest),
            }).encode("utf-8")
            live.__enter__ = mock.Mock(return_value=live)
            live.__exit__ = mock.Mock(return_value=False)
            out = io.StringIO()
            with mock.patch.object(web_server, "LINKEDIN_OVERRIDES_CSV",
                                   base / "review.csv"), \
                 mock.patch.object(
                     web_server, "acquire_review_session_lock",
                     side_effect=AssertionError("reuse must not take the lock")), \
                 mock.patch.object(web_server.urllib.request, "urlopen",
                                   return_value=live), \
                 contextlib.redirect_stdout(out):
                web_server.cmd_serve(args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["status"], "reused")
            self.assertEqual(payload["url"], "http://127.0.0.1:43212/directory")

    @staticmethod
    def _serve_args(base, manifest, port, stage):
        return mock.Mock(
            review=str(base / "review.csv"), verdicts=str(base / "verdicts.jsonl"),
            synthetic_people=str(base / "synthetic.csv"), facts_dir=str(base / "facts"),
            people_csv=str(base / "people.csv"), parents_dir=str(base / "parents"),
            dossier_dir=str(base / "dossiers"),
            profile_cache_dir=str(base / "profiles"),
            manifest=str(manifest),
            enrichment_manifest=str(base / "research" / "manifest.json"),
            avatar_dir=str(base / "avatars"),
            host="127.0.0.1", port=port, stage=stage, fresh=False,
            open=False, confirm_threshold=0.7, detach_threshold=0.85,
        )

    def test_review_always_lands_on_directory(self):
        # Bare `review` is browse-only regardless of the workflow's current stage.
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            args = self._serve_args(base, base / "review" / "manifest.json",
                                    43213, None)
            fake_server = mock.Mock(server_address=("127.0.0.1", 43213))
            out = io.StringIO()
            with mock.patch.object(
                    web_server.urllib.request, "urlopen",
                    side_effect=web_server.urllib.error.URLError("down")), \
                 mock.patch.object(web_server, "_all_review_parents",
                                   return_value=[]), \
                 mock.patch.object(web_server, "ThreadingHTTPServer",
                                   return_value=fake_server), \
                 contextlib.redirect_stdout(out):
                web_server.cmd_serve(args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["url"], "http://127.0.0.1:43213/directory")

    def test_reused_server_bare_review_lands_on_directory(self):
        # A live staged server does not change bare `review` browse behavior.
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            manifest = base / "review" / "manifest.json"
            args = self._serve_args(base, manifest, 43214, None)
            live = mock.Mock()
            live.read.return_value = json.dumps({
                "primitive": "reconcile_review_web", "manifest": str(manifest),
                "stage": "linkedin",
            }).encode("utf-8")
            live.__enter__ = mock.Mock(return_value=live)
            live.__exit__ = mock.Mock(return_value=False)
            out = io.StringIO()
            with mock.patch.object(web_server.urllib.request, "urlopen",
                                   return_value=live), \
                 contextlib.redirect_stdout(out):
                web_server.cmd_serve(args)
            payload = json.loads(out.getvalue())
            self.assertEqual(payload["url"], "http://127.0.0.1:43214/directory")

    def test_person_detail_worth_buttons_follow_current_tag(self):
        # Top-right decision affordance: the button moves the person to the
        # OTHER pile; undecided people get both directions; no worth key -> none.
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            kwargs = {"profile_cache_dir": base / "profiles"}
            yes = self._parent("amy-alpha", "Amy Alpha")
            yes["worth_row"] = {"effective": "yes", "key": "parent-worth:amy"}
            yes["candidates"][0]["worth_key"] = "parent-worth:amy"
            html = web_rendering.render_person_detail(yes, base / "p", base / "d", **kwargs)
            self.assertIn("Move to No", html)
            self.assertNotIn("Move to Yes", html)
            no = self._parent("zed-zulu", "Zed Zulu")
            no["worth_row"] = {"effective": "no", "key": "parent-worth:zed"}
            no["candidates"][0]["worth_key"] = "parent-worth:zed"
            html = web_rendering.render_person_detail(no, base / "p", base / "d", **kwargs)
            self.assertIn("Move to Yes", html)
            self.assertNotIn("Move to No", html)
            maybe = self._parent("mel-maybe", "Mel Maybe")
            maybe["worth_row"] = {"effective": "maybe", "key": "parent-worth:mel"}
            maybe["candidates"][0]["worth_key"] = "parent-worth:mel"
            html = web_rendering.render_person_detail(maybe, base / "p", base / "d", **kwargs)
            self.assertIn("Move to Yes", html)
            self.assertIn("Move to No", html)
            # _worth_key falls back to person_ids, so a normal parent always
            # has a decision key; only a truly keyless shell hides the buttons.
            keyless = {"slug": "kai-keyless", "dossier_slug": "kai-keyless",
                       "name": "Kai Keyless", "candidates": []}
            html = web_rendering.render_person_detail(keyless, base / "p", base / "d", **kwargs)
            self.assertNotIn("data-dir-worth", html)

    def test_pending_candidate_renders_publess_in_directory(self):
        # The directory shows only confirmed identities. A pending
        # (needs_review) candidate renders exactly like a detached one: the
        # person stays, the link/confidence/photo do not, and the guidance
        # form still keys on the pub.
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            kwargs = {"profile_cache_dir": base / "profiles"}
            pending = self._parent(
                "jordan-bravo", "Jordan Bravo", approved="", confidence=0.62,
                url="https://www.linkedin.com/in/jordan-bravo-wrong")
            html = web_rendering.render_person_detail(pending, base / "p", base / "d", **kwargs)
            self.assertNotIn("linkedin.com/in/jordan-bravo-wrong", html)
            self.assertNotIn("LinkedIn Confidence", html)
            self.assertIn("Jordan Bravo", html)              # the person remains
            self.assertIn("data-retarget-form", html)        # and is retargetable

    def test_confirmed_profile_name_promotes_degraded_display_name(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            kwargs = {"profile_cache_dir": base / "profiles"}
            # Placeholder surname + confirmed profile -> profile name wins.
            degraded = self._parent("jordan-last-name-unknown",
                                    "Jordan (last name unknown)",
                                    full_name="Jordan Bravo")
            html = web_rendering.render_person_detail(degraded, base / "p", base / "d", **kwargs)
            self.assertIn("<h2>Jordan Bravo</h2>", html)
            self.assertNotIn("last name unknown", html)
            # Single-token name + confirmed two-token profile -> promoted.
            single = self._parent("casey", "Casey", full_name="Casey Example")
            html = web_rendering.render_person_detail(single, base / "p", base / "d", **kwargs)
            self.assertIn("<h2>Casey Example</h2>", html)
            # A familiar multi-token message name is kept over the profile's.
            familiar = self._parent("jb", "JB Bravo", full_name="Jordan Bravo")
            html = web_rendering.render_person_detail(familiar, base / "p", base / "d", **kwargs)
            self.assertIn("<h2>JB Bravo</h2>", html)
            # An UNCONFIRMED profile never promotes: candidate facts are
            # stripped, so the degraded name stays.
            pending = self._parent("alex-last-name-unknown",
                                   "Alex (last name unknown)",
                                   full_name="Alex Impostor", approved="")
            html = web_rendering.render_person_detail(pending, base / "p", base / "d", **kwargs)
            self.assertIn("Alex (last name unknown)", html)
            self.assertNotIn("Alex Impostor", html)
        # The sidebar island promotes the same way (inline snapshot name; no
        # cache hydration needed when the snapshot carries full_name).
        entries = web_rendering.directory_entries([
            self._parent("jordan-last-name-unknown", "Jordan (last name unknown)",
                         full_name="Jordan Bravo"),
            self._parent("alex-last-name-unknown", "Alex (last name unknown)",
                         full_name="Alex Impostor", approved=""),
        ])
        names = {e["slug"]: e["name"] for e in entries}
        self.assertEqual(names["jordan-last-name-unknown"], "Jordan Bravo")
        self.assertEqual(names["alex-last-name-unknown"], "Alex (last name unknown)")

    def test_person_detail_linkedin_confidence_badge(self):
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            kwargs = {"profile_cache_dir": base / "profiles"}
            parent = self._parent(
                "amy-alpha", "Amy Alpha",
                url="https://www.linkedin.com/in/amy-alpha-test", confidence=0.87)
            html = web_rendering.render_person_detail(parent, base / "p", base / "d", **kwargs)
            self.assertIn("LinkedIn Confidence: 87%", html)
            bare = self._parent("zed-zulu", "Zed Zulu",
                                url="https://www.linkedin.com/in/zed-zulu-test")
            html = web_rendering.render_person_detail(bare, base / "p", base / "d", **kwargs)
            self.assertNotIn("LinkedIn Confidence", html)

    def test_directory_dossier_prefers_the_canonical_parent(self):
        # A merged person's parent .md IS the consolidated dossier — show it
        # once instead of concatenating N repeating child dossiers. Pointer-only
        # parents (and candidates with no parent file) keep the child fallback.
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            parents = base / "parents"
            dossiers = base / "dossiers"
            parents.mkdir(); dossiers.mkdir()
            (parents / "jordan-parent.md").write_text(
                "---\nslug: jordan-parent\n---\n\n# Jordan Bravo (canonical)\n\n"
                "## Summary\n\nOne consolidated person.\n\n"
                "## Confirmed children (merged)\n\n- [[jordan-a]] **Jordan Bravo**\n",
                encoding="utf-8")
            (dossiers / "jordan-a.md").write_text(
                "# Jordan Bravo\n\n## Summary\n\nChild copy.\n", encoding="utf-8")
            markdown = web_rendering.directory_dossier(parents, dossiers, "jordan-parent")
            self.assertIn("One consolidated person.", markdown)
            self.assertNotIn("Child copy.", markdown)
            self.assertNotIn("merged from", markdown)
            # Pointer-only parent -> child composition fallback.
            (parents / "solo-parent.md").write_text(
                "---\nslug: solo-parent\n---\n\n# Solo (canonical)\n\n"
                "## Confirmed children (merged)\n\n- [[solo-a]]\n"
                "children: [\"solo-a\"]\n", encoding="utf-8")
            (dossiers / "solo-a.md").write_text(
                "# Solo\n\n## Summary\n\nThe child body.\n", encoding="utf-8")
            markdown = web_rendering.directory_dossier(parents, dossiers, "solo-parent")
            self.assertIn("The child body.", markdown)

    def test_children_section_folds_into_debug_dropdown(self):
        # The confirmed-children list is provenance, not person context: the
        # pane body stays clean and the list collapses at the very bottom.
        with tempfile.TemporaryDirectory() as dd:
            base = Path(dd)
            parents = base / "parents"
            parents.mkdir()
            (parents / "jordan-parent.md").write_text(
                "---\nslug: jordan-parent\n---\n\n# Jordan Bravo (canonical)\n\n"
                "## Summary\n\nOne person.\n\n"
                "## Confirmed children (merged)\n\n- [[jordan-a]] judge 0.90\n\n"
                "## Topics\n\n- travel\n", encoding="utf-8")
            parent = self._parent("jordan-parent", "Jordan Bravo")
            html = web_rendering.render_person_detail(
                parent, parents, base / "dossiers", base / "profiles")
        body, _, debug = html.partition("directory-debug")
        self.assertIn("One person.", body)
        self.assertIn("travel", body)             # later sections survive the cut
        self.assertNotIn("judge 0.90", body)      # children not in the main flow
        self.assertIn("Merged children (debug)", debug)
        self.assertIn("judge 0.90", debug)
        self.assertIn("<details", html)

    def test_detached_identity_never_renders_in_the_pane(self):
        # A judged-wrong (detached) LinkedIn shows NOTHING of the wrong person:
        # no link, no confidence badge, no headline — only the guidance form
        # still keys on the detached pub so re-research lands on the right row.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            parent = {"slug": "jordan-bravo-p", "dossier_slug": "jordan-bravo-p",
                      "name": "Jordan Bravo", "person_ids": ["pid-jordan"],
                      "candidates": [{"pub": "jordan-namesake",
                                      "url": "https://www.linkedin.com/in/jordan-namesake",
                                      "full_name": "Jordan Namesake",
                                      "headline": "NIH IT specialist",
                                      "action": "detach", "approved": "auto",
                                      "confidence": 0.93, "verdict": "wrong_person"}]}
            pane = web_rendering.render_person_detail(
                parent, base / "parents", base / "dossiers", base / "profiles")
        self.assertNotIn("View LinkedIn", pane)
        self.assertNotIn("linkedin.com/in/jordan-namesake", pane)
        self.assertNotIn("LinkedIn Confidence", pane)
        self.assertNotIn("NIH IT specialist", pane)
        self.assertIn("data-retarget-form", pane)
        self.assertIn("data-pub='jordan-namesake'", pane)

    def test_review_js_wires_the_directory_view(self):
        script = web_rendering.REVIEW_JS.read_text(encoding="utf-8")
        self.assertIn("setupDirectory", script)
        self.assertIn("/api/person", script)
        self.assertIn("data-directory-list", script)
        self.assertIn("data-directory-tab", script)
        self.assertIn("data-dir-worth", script)


class TestOwnerPhoneLeak(unittest.TestCase):
    """The owner's own iMessage number must never render as a CONTACT's
    reachability: harvested from chat.db account metadata into owner.json,
    then dropped by the shared Contact merge whatever source carried it in."""

    def _chat_db(self, base: Path) -> Path:
        chat_db = base / "chat.db"
        conn = sqlite3.connect(chat_db)
        conn.execute("CREATE TABLE chat (account_login TEXT)")
        conn.executemany("INSERT INTO chat VALUES (?)",
                         [("E:jordanbravo88@example.com",), ("P:+15550100",)])
        conn.execute("CREATE TABLE message (destination_caller_id TEXT, is_from_me INTEGER)")
        conn.executemany("INSERT INTO message VALUES (?, ?)",
                         [("+15550100", 0), ("+15550199", 1)])  # from_me rows never count
        conn.commit()
        conn.close()
        return chat_db

    def test_harvest_reads_own_number_from_chat_db(self):
        from packs.ingestion.primitives.deep_context import build_owner
        with tempfile.TemporaryDirectory() as d:
            phones = build_owner.harvest_owner_phones(chat_db=self._chat_db(Path(d)))
        self.assertEqual(phones, ["+15550100"])
        # Absent db (a snapshot/mirror without raw stores) harvests nothing.
        self.assertEqual(build_owner.harvest_owner_phones(
            chat_db=Path("/nonexistent/chat.db")), [])

    def test_legacy_shim_stamps_missing_phones_key_and_refills_empty(self):
        from packs.ingestion.primitives.common import legacy
        from packs.ingestion.primitives.deep_context import build_owner
        with tempfile.TemporaryDirectory() as d:
            owner_json = Path(d) / "owner.json"
            owner_json.write_text(json.dumps(
                {"name": "Jordan Bravo", "emails": ["jordanbravo88@example.com"]}),
                encoding="utf-8")
            # Missing key stamps (possibly empty) exactly once...
            with mock.patch.object(build_owner, "harvest_owner_phones", return_value=[]):
                self.assertTrue(legacy.ensure_owner_phones(owner_json))
                self.assertFalse(legacy.ensure_owner_phones(owner_json))  # idempotent
            self.assertEqual(json.loads(owner_json.read_text())["phones"], [])
            # ...and an EMPTY key re-harvests once the source yields a number
            # (an install can carry phones: [] from before its store synced).
            with mock.patch.object(build_owner, "harvest_owner_phones",
                                   return_value=["+15550100"]):
                self.assertTrue(legacy.ensure_owner_phones(owner_json))
            self.assertEqual(json.loads(owner_json.read_text())["phones"], ["+15550100"])
            # A populated key is never touched.
            with mock.patch.object(build_owner, "harvest_owner_phones",
                                   return_value=["+15550199"]):
                self.assertFalse(legacy.ensure_owner_phones(owner_json))

    def test_contact_merge_drops_owner_endpoints_from_any_source(self):
        with mock.patch.object(web_rendering, "load_owner",
                               return_value={"emails": ["jordan@example.com"],
                                             "phones": ["+1 (555) 010-0000"]}):
            merged = web_rendering._merge_contacts(
                ["casey@example.com", "555-010-0000", "jordan@example.com"],
                ["+15550199"])
        self.assertEqual(merged, ["casey@example.com", "+15550199"])


class TestGuidedRetargets(unittest.TestCase):
    """The /directory wrong-person queue: guidance rides into the research row,
    the judge decides, and only a confident confirm auto-approves. All offline —
    run_research is patched where defined, fixtures are synthetic."""

    def _facts(self, facts_dir: Path, pid: str, name: str) -> None:
        facts_dir.mkdir(parents=True, exist_ok=True)
        (facts_dir / f"{pid}.jsonl").write_text(json.dumps({"facts": {
            "canonical_name": name, "employers": [{"name": "Acme"}],
            "relationship_to_owner": "friend", "confidence": 0.8}}) + "\n",
            encoding="utf-8")

    def _request(self, slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                 guidance="the Jordan Bravo who ran DevRel at Acme"):
        return web_retargets.GuidedRetarget(
            slug=slug, pub=pub, name="Jordan Bravo", guidance=guidance,
            person_ids=("pid-jordan",),
            linkedin_url="https://www.linkedin.com/in/jordan-bravo-wrong",
            match_emails=("casey@example.com",))

    def _fake_research(self, out_dir_holder: dict, profile: dict):
        """A run_research stand-in that writes the per-handle research JSON the
        staging pass reads, exactly where the real primitive would."""
        def fake(params):
            rows = list(csv.DictReader(
                io.StringIO(params.input_csv.read_text(encoding="utf-8"))))
            out_dir_holder["queue_rows"] = rows
            for row in rows:
                handle_dir = params.output_dir / row["handle"]
                handle_dir.mkdir(parents=True, exist_ok=True)
                (handle_dir / "01_research_parallel.json").write_text(
                    json.dumps(profile), encoding="utf-8")
            return {"status": "completed"}
        return fake

    def test_confirmed_proposal_auto_approves_with_user_guidance_source(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out = (base / "review.csv", base / "facts",
                                       base / "raw", base / "out")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            seen = {}
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.9,
                                  "notes": "DevRel lead at Acme."},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-right",
                                  "linkedin_status": "found"}}
            confirming = _verdict("confirmed", 0.9, reason="employer matches")
            states: list[str] = []
            with mock.patch.object(dresearch, "run_research",
                                   side_effect=self._fake_research(seen, profile)), \
                 mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research(seen, profile)), \
                 mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=confirming):
                result = web_retargets.run_guided_retarget(
                    self._request(), review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=base / "engine", use_llm=True,
                    on_progress=lambda state, detail: states.append(state))
            self.assertEqual(result["state"], "applied")
            self.assertIn("jordan-bravo-right", result["new_url"])
            self.assertEqual(states, ["researching", "judging"])
            # The user's words became the research row's retarget hint.
            self.assertEqual(seen["queue_rows"][0]["retarget_hint"],
                             "the Jordan Bravo who ran DevRel at Acme")
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(row["approved"], "yes")
            self.assertEqual(row["source"], "user-guidance")
            self.assertIn("jordan-bravo-right", row["new_linkedin_url"])

    def test_judge_rejection_of_unreferenced_profile_detaches_and_says_so(self):
        # The judge rejected a profile the guidance never referenced: the wrong
        # link detaches (the user said wrong person) and the outcome is honest —
        # no synthetic is possible when research returned a LinkedIn.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out, engine = (base / "review.csv", base / "facts",
                                               base / "raw", base / "out", base / "engine")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.6,
                                  "notes": "A plausible namesake."},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-namesake",
                                  "linkedin_status": "found"}}
            rejecting = _verdict("wrong_person", 0.9, reason="no non-name corroboration")
            assemble = mock.Mock()
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research({}, profile)), \
                 mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=rejecting), \
                 mock.patch.object(web_retargets.assemble_synthetic_profile,
                                   "AssembleSyntheticProfile", assemble):
                result = web_retargets.run_guided_retarget(
                    self._request(), review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=engine, use_llm=True)
            self.assertEqual(result["state"], "no_match")
            self.assertIn("could not verify", result["detail"])
            assemble.assert_not_called()                    # a found URL owns the person
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertEqual(row["action"], "detach")       # the wrong link is gone
            self.assertEqual(row["approved"], "yes")
            self.assertEqual(row["source"], "user-guidance")
            self.assertEqual(row["new_linkedin_url"], "")
            # The guided result is mirrored into the engine's research home.
            self.assertTrue((engine / "jordan-bravo-p" / "01_research_parallel.json").exists())

    def test_no_linkedin_found_detaches_and_stands_a_synthetic(self):
        # Research found no LinkedIn at all: detach + a synthetic supersedes.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out, engine = (base / "review.csv", base / "facts",
                                               base / "raw", base / "out", base / "engine")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.75,
                                  "notes": "No public LinkedIn presence found."},
                       "social": {"linkedin_status": "not_found"}}
            assemble = mock.Mock()
            assemble.return_value.run.return_value = mock.Mock(built=1, preserved_user_rows=0)
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research({}, profile)), \
                 mock.patch.object(web_retargets.assemble_synthetic_profile,
                                   "AssembleSyntheticProfile", assemble):
                result = web_retargets.run_guided_retarget(
                    self._request(), review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=engine, use_llm=False)
            self.assertEqual(result["state"], "synthetic")
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertEqual(row["action"], "detach")
            self.assertEqual(row["approved"], "yes")
            kwargs = assemble.call_args.kwargs
            self.assertEqual(kwargs["research_dir"], out)
            self.assertFalse(kwargs["prune"])               # scoped run must never prune

    def test_pasted_linkedin_url_applies_directly_no_research_no_judge(self):
        # "i literally gave it the linkedin": an asserted URL IS the decision —
        # zero spend, no judge skepticism, applied immediately. The LLM intent
        # read is consulted online (patched here); offline falls back to URL
        # presence.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review = base / "review.csv"
            with mock.patch.object(web_retargets.deep_research_contacts,
                                   "run_research") as research, \
                 mock.patch.object(dresearch, "judge_research_proposal") as judge, \
                 mock.patch.object(
                     web_retargets, "specified_linkedin_url",
                     return_value=("https://www.linkedin.com/in/jordan-bravo-right",
                                   "jordan-bravo-right")) as intent:
                result = web_retargets.run_guided_retarget(
                    self._request(
                        guidance="this is the right one https://www.linkedin.com/in/jordan-bravo-right"),
                    review_path=review, people_csv=base / "missing-people.csv",
                    facts_dir=base / "facts", raw_dir=base / "raw",
                    out_dir=base / "out", engine_dir=base / "engine", use_llm=True)
            intent.assert_called_once()
            self.assertTrue(intent.call_args.kwargs["use_llm"])
            research.assert_not_called()
            judge.assert_not_called()
            self.assertEqual(result["state"], "applied")
            self.assertIn("no research needed", result["detail"])
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertEqual(row["action"], "retarget")
            self.assertEqual(row["approved"], "yes")
            self.assertEqual(row["new_public_identifier"], "jordan-bravo-right")
            self.assertEqual(row["source"], "user-guidance")
        # Offline fallback: scheme-less URLs count; plain text does not.
        self.assertEqual(
            web_retargets.specified_linkedin_url(
                "try linkedin.com/in/jordan-bravo-right pls", use_llm=False)[1],
            "jordan-bravo-right")
        self.assertEqual(
            web_retargets.specified_linkedin_url("the DevRel at Acme", use_llm=False),
            ("", ""))

    def test_judge_rejection_yields_to_guidance_that_references_the_profile(self):
        # Parallel is told the hint is the strongest clue; when it returns the
        # very profile the guidance references, the user's word outranks the
        # judge's corroboration bar.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out, engine = (base / "review.csv", base / "facts",
                                               base / "raw", base / "out", base / "engine")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.9,
                                  "notes": "Per the user hint."},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-right",
                                  "linkedin_status": "found"}}
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research({}, profile)):
                # use_llm=False: intent read falls back to URL presence (none
                # here), and the deterministic judge always rejects — exactly
                # the belt-and-suspenders case.
                result = web_retargets.run_guided_retarget(
                    self._request(guidance="pretty sure jordan-bravo-right is him"),
                    review_path=review, people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=engine, use_llm=False)
            self.assertEqual(result["state"], "applied")
            self.assertIn("guidance references", result["detail"])
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertEqual(row["approved"], "yes")
            self.assertEqual(row["llm_reject"], "")
            self.assertEqual(row["new_public_identifier"], "jordan-bravo-right")

    def test_identical_guidance_reuses_paid_research_changed_guidance_rebills(self):
        # A retry after a crash must not re-bill: same guidance keeps the
        # existing research (no sidelining); new guidance sidelines + reruns.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out, engine = (base / "review.csv", base / "facts",
                                               base / "raw", base / "out", base / "engine")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            handle_dir = out / "jordan-bravo-p"
            handle_dir.mkdir(parents=True)
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.9,
                                  "notes": "DevRel lead at Acme."},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-right",
                                  "linkedin_status": "found"}}
            (handle_dir / "01_research_parallel.json").write_text(
                json.dumps(profile), encoding="utf-8")
            (handle_dir / "guidance.json").write_text(json.dumps(
                {"guidance": "the Jordan Bravo who ran DevRel at Acme"}), encoding="utf-8")
            confirming = _verdict("confirmed", 0.9, reason="employer matches")
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   return_value={"status": "no_work"}) as research, \
                 mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=confirming):
                result = web_retargets.run_guided_retarget(
                    self._request(), review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=engine, use_llm=True)
            self.assertEqual(result["state"], "applied")
            research.assert_called_once()  # invoked, but nothing to re-bill
            # Same guidance: the paid result stayed in place, no sideline.
            self.assertFalse((handle_dir / "01_research_parallel.json.bkup").exists())
            # Changed guidance: the old result is sidelined for a fresh run.
            assemble = mock.Mock()
            assemble.return_value.run.return_value = mock.Mock(built=1, preserved_user_rows=0)
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   return_value={"status": "completed"}), \
                 mock.patch.object(web_retargets.assemble_synthetic_profile,
                                   "AssembleSyntheticProfile", assemble):
                web_retargets.run_guided_retarget(
                    self._request(guidance="actually the Jordan Bravo at Globex"),
                    review_path=review, people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=engine, use_llm=False)
            self.assertTrue((handle_dir / "01_research_parallel.json.bkup").exists())

    def test_unusable_research_lands_no_match(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out, engine = (base / "review.csv", base / "facts",
                                               base / "raw", base / "out", base / "engine")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            profile = {"person": {"full_name": "", "confidence": 0.0, "notes": ""},
                       "social": {"linkedin_status": "not_found"}}
            assemble = mock.Mock()
            assemble.return_value.run.return_value = mock.Mock(built=0, preserved_user_rows=0)
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research({}, profile)), \
                 mock.patch.object(web_retargets.assemble_synthetic_profile,
                                   "AssembleSyntheticProfile", assemble):
                result = web_retargets.run_guided_retarget(
                    self._request(), review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=engine, use_llm=False)
            self.assertEqual(result["state"], "no_match")
            self.assertIn("not usable", result["detail"])
            # The user's wrong-person verdict still stands as a detach.
            self.assertEqual(_rows_by_pub(review)["jordan-bravo-wrong"]["action"], "detach")

    def test_reguided_person_unsticks_prior_decision_and_stale_output(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out = (base / "review.csv", base / "facts",
                                       base / "raw", base / "out")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            write_rows(review, {"jordan-bravo-wrong": {
                "public_identifier": "jordan-bravo-wrong", "action": "retarget",
                "approved": "yes", "llm_judge_fingerprint": "stale-sha",
                "new_linkedin_url": "https://www.linkedin.com/in/old-guess"}})
            stale = out / "jordan-bravo-p" / "01_research_parallel.json"
            stale.parent.mkdir(parents=True)
            stale.write_text("{}", encoding="utf-8")
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.9,
                                  "notes": "DevRel lead at Acme."},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-right",
                                  "linkedin_status": "found"}}
            confirming = _verdict("confirmed", 0.9, reason="employer matches")
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research({}, profile)), \
                 mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=confirming) as jm:
                result = web_retargets.run_guided_retarget(
                    self._request(), review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=base / "engine", use_llm=True)
            self.assertEqual(result["state"], "applied")
            jm.assert_called_once()  # stale fingerprint blanked -> re-judged
            # Paid artifact sidelined, never deleted.
            self.assertTrue(stale.with_suffix(".json.bkup").exists())
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertIn("jordan-bravo-right", row["new_linkedin_url"])

    def test_failed_research_reports_failed_state(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   return_value={"status": "failed", "error": "PARALLEL_API_KEY missing"}):
                result = web_retargets.run_guided_retarget(
                    self._request(), review_path=base / "review.csv",
                    people_csv=base / "missing-people.csv",
                    facts_dir=base / "facts", raw_dir=base / "raw",
                    out_dir=base / "out", use_llm=False)
            self.assertEqual(result["state"], "failed")
            self.assertIn("PARALLEL_API_KEY", result["detail"])

    def test_failed_research_leaves_review_rows_untouched(self):
        # The silent-loop fix: a job that dies (missing key, network, blocked
        # queue) returns the person to review EXACTLY as they were — the
        # re-judge blanking only runs after research succeeds.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review = base / "review.csv"
            write_rows(review, {"jordan-bravo-wrong": {
                "public_identifier": "jordan-bravo-wrong", "action": "verify",
                "approved": "auto", "llm_judge_fingerprint": "sha-1"}})
            before = review.read_text(encoding="utf-8")
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo", guidance="the Jordan Bravo at Acme",
                candidate_pubs=("jordan-bravo-wrong",))
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=SystemExit("PARALLEL_API_KEY not set")):
                result = web_retargets.run_guided_retarget(
                    request, review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=base / "facts", raw_dir=base / "raw",
                    out_dir=base / "out", use_llm=False)
            self.assertEqual(result["state"], "failed")
            self.assertEqual(review.read_text(encoding="utf-8"), before)

    def test_applied_outcome_settles_sibling_rows_but_mid_job_decision_stands(self):
        # An applied identity answers the WHOLE parent: pending sibling links
        # settle as detached (no bounce back into the linear queue), while a
        # human decision made WHILE the job ran wins its row.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts, raw, out = (base / "review.csv", base / "facts",
                                       base / "raw", base / "out")
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            write_rows(review, {
                "jordan-bravo-other": {"public_identifier": "jordan-bravo-other",
                                       "action": "verify", "approved": ""},
                "jordan-bravo-decided": {"public_identifier": "jordan-bravo-decided",
                                         "action": "verify", "approved": ""}})
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.9,
                                  "notes": "DevRel lead at Acme."},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-right",
                                  "linkedin_status": "found"}}
            fake = self._fake_research({}, profile)

            def research_then_human_click(params):
                outcome = fake(params)
                # Simulate the card staying interactive: the human decides one
                # sibling while Parallel is still running.
                rows_now = web_retargets.load_override_rows(review)
                rows_now["jordan-bravo-decided"].update(
                    {"action": "verify", "approved": "no"})
                web_retargets.write_override_rows(review, rows_now)
                return outcome

            confirming = _verdict("confirmed", 0.9, reason="employer matches")
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo", guidance="the Jordan Bravo who ran DevRel at Acme",
                person_ids=("pid-jordan",),
                linkedin_url="https://www.linkedin.com/in/jordan-bravo-wrong",
                candidate_pubs=("jordan-bravo-wrong", "jordan-bravo-other",
                                "jordan-bravo-decided"))
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=research_then_human_click), \
                 mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=confirming):
                result = web_retargets.run_guided_retarget(
                    request, review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=raw, out_dir=out,
                    engine_dir=base / "engine", use_llm=True)
            self.assertEqual(result["state"], "applied")
            rows = _rows_by_pub(review)
            self.assertEqual(rows["jordan-bravo-other"]["action"], "detach")
            self.assertEqual(rows["jordan-bravo-other"]["approved"], "yes")
            self.assertEqual(rows["jordan-bravo-other"]["source"], "user-guidance")
            self.assertEqual(rows["jordan-bravo-decided"]["action"], "verify")
            self.assertEqual(rows["jordan-bravo-decided"]["approved"], "no")

    def test_direct_url_apply_settles_sibling_rows(self):
        # Pasting the right URL on a multi-option card resolves the whole
        # parent: the other pending links settle as detached immediately.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review = base / "review.csv"
            write_rows(review, {"jordan-bravo-other": {
                "public_identifier": "jordan-bravo-other",
                "action": "verify", "approved": ""}})
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo",
                guidance="this is him https://www.linkedin.com/in/jordan-bravo-right",
                candidate_pubs=("jordan-bravo-wrong", "jordan-bravo-other"))
            result = web_retargets.run_guided_retarget(
                request, review_path=review,
                people_csv=base / "missing-people.csv",
                facts_dir=base / "facts", raw_dir=base / "raw",
                out_dir=base / "out", use_llm=False)
            self.assertEqual(result["state"], "applied")
            rows = _rows_by_pub(review)
            self.assertIn("jordan-bravo-right", rows["jordan-bravo-wrong"]["new_linkedin_url"])
            self.assertEqual(rows["jordan-bravo-other"]["action"], "detach")
            self.assertEqual(rows["jordan-bravo-other"]["approved"], "yes")

    def test_crash_after_research_restores_blanked_rows(self):
        # The blanking write lands before the judge/mirror steps, which can
        # still raise (missing OpenAI key, network). A crash there must
        # restore approved AND the paid-verdict fingerprint, or the person
        # returns half-reset and the next enrichment pass re-bills the judge.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts = base / "review.csv", base / "facts"
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            write_rows(review, {"jordan-bravo-wrong": {
                "public_identifier": "jordan-bravo-wrong", "action": "verify",
                "approved": "auto", "llm_judge_fingerprint": "paid-sha"}})
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.9},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-right",
                                  "linkedin_status": "found"}}
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo", guidance="the Jordan Bravo at Acme",
                person_ids=("pid-jordan",),
                candidate_pubs=("jordan-bravo-wrong",))
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research({}, profile)), \
                 mock.patch.object(web_retargets.reconcile_deep_research,
                                   "propose_retargets_from_output",
                                   side_effect=RuntimeError("judge LLM unreachable")):
                result = web_retargets.run_guided_retarget(
                    request, review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=base / "raw", out_dir=base / "out",
                    engine_dir=base / "engine", use_llm=True)
            self.assertEqual(result["state"], "failed")
            self.assertIn("judge LLM unreachable", result["detail"])
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertEqual(row["approved"], "auto")
            self.assertEqual(row["llm_judge_fingerprint"], "paid-sha")

    def test_applied_settles_folded_synthetic_sibling_but_user_gate_stands(self):
        # A mixed parent (real link + folded synthetic option): an applied
        # identity settles the synthetic through its approve gate, or the
        # parent bounces back as a synthetic-option card. A gate the user
        # already set is never overwritten.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review = base / "review.csv"
            synth = base / "synthetic-people.csv"
            synth.write_text(
                "public_identifier,approved\nsynth-email-abc,\nsynth-phone-def,yes\n",
                encoding="utf-8")
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo",
                guidance="this is him https://www.linkedin.com/in/jordan-bravo-right",
                candidate_pubs=("jordan-bravo-wrong",),
                synthetic_pubs=("synth-email-abc", "synth-phone-def"))
            result = web_retargets.run_guided_retarget(
                request, review_path=review,
                people_csv=base / "missing-people.csv",
                facts_dir=base / "facts", raw_dir=base / "raw",
                out_dir=base / "out", synthetic_path=synth, use_llm=False)
            self.assertEqual(result["state"], "applied")
            gates = {r["public_identifier"]: r["approved"]
                     for r in csv.DictReader(synth.open())}
            self.assertEqual(gates["synth-email-abc"], "no")   # settled
            self.assertEqual(gates["synth-phone-def"], "yes")  # user word stands

    def test_sibling_human_yes_and_auto_rows_survive_apply(self):
        # A sibling row's human yes is NEVER blanked or settled (one pub row
        # can be a DIFFERENT parent's confirmed identity), and a
        # machine-applied auto row is not detached by a direct apply —
        # matching /decide's withdrawal guard.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review = base / "review.csv"
            write_rows(review, {
                "shared-confirmed": {"public_identifier": "shared-confirmed",
                                     "action": "verify", "approved": "yes"},
                "machine-applied": {"public_identifier": "machine-applied",
                                    "action": "verify", "approved": "auto"}})
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo",
                guidance="this is him https://www.linkedin.com/in/jordan-bravo-right",
                candidate_pubs=("jordan-bravo-wrong", "shared-confirmed",
                                "machine-applied"))
            result = web_retargets.run_guided_retarget(
                request, review_path=review,
                people_csv=base / "missing-people.csv",
                facts_dir=base / "facts", raw_dir=base / "raw",
                out_dir=base / "out", use_llm=False)
            self.assertEqual(result["state"], "applied")
            rows = _rows_by_pub(review)
            self.assertEqual(rows["shared-confirmed"]["action"], "verify")
            self.assertEqual(rows["shared-confirmed"]["approved"], "yes")
            self.assertEqual(rows["machine-applied"]["action"], "verify")
            self.assertEqual(rows["machine-applied"]["approved"], "auto")

    def test_failed_notes_reduce_newest_first(self):
        # snapshot() is newest-first: the FIRST item seen per slug is the
        # latest, so an old failure never shadows a later success and a later
        # failure is never shadowed by an old success.
        old_fail_then_ok = [
            {"slug": "jordan-bravo-p", "state": "applied", "detail": "done"},
            {"slug": "jordan-bravo-p", "state": "failed", "detail": "old error"}]
        self.assertEqual(web_retargets.failed_notes_from_items(old_fail_then_ok), {})
        ok_then_new_fail = [
            {"slug": "jordan-bravo-p", "state": "failed", "detail": "new error"},
            {"slug": "jordan-bravo-p", "state": "applied", "detail": "done"}]
        self.assertEqual(
            web_retargets.failed_notes_from_items(ok_then_new_fail),
            {"jordan-bravo-p": "new error"})
        # queue_slug (the review queue's parent slug) wins over the dossier slug.
        synthetic_parent = [
            {"slug": "real-parent", "queue_slug": "synthetic-synth-x",
             "state": "failed", "detail": "boom"}]
        self.assertEqual(
            web_retargets.failed_notes_from_items(synthetic_parent),
            {"synthetic-synth-x": "boom"})

    def test_auto_sibling_survives_research_apply_with_fingerprint(self):
        # The research path must not blank a machine-applied `auto` sibling —
        # one pub row can be ANOTHER parent's machine-confirmed identity, and
        # blanking it hands settle_siblings a detach plus burns the paid
        # judge fingerprint.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts = base / "review.csv", base / "facts"
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            write_rows(review, {"shared-machine-confirmed": {
                "public_identifier": "shared-machine-confirmed",
                "action": "verify", "approved": "auto",
                "llm_judge_fingerprint": "paid-sha"}})
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.9},
                       "social": {"linkedin_url": "https://www.linkedin.com/in/jordan-bravo-right",
                                  "linkedin_status": "found"}}
            confirming = _verdict("confirmed", 0.9, reason="employer matches")
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo", guidance="the Jordan Bravo at Acme",
                person_ids=("pid-jordan",),
                candidate_pubs=("jordan-bravo-wrong", "shared-machine-confirmed"))
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=self._fake_research({}, profile)), \
                 mock.patch.object(dresearch, "judge_research_proposal",
                                   return_value=confirming):
                result = web_retargets.run_guided_retarget(
                    request, review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=base / "raw", out_dir=base / "out",
                    engine_dir=base / "engine", use_llm=True)
            self.assertEqual(result["state"], "applied")
            row = _rows_by_pub(review)["shared-machine-confirmed"]
            self.assertEqual(row["action"], "verify")
            self.assertEqual(row["approved"], "auto")
            self.assertEqual(row["llm_judge_fingerprint"], "paid-sha")

    def test_mid_job_skip_vetoes_automatic_synthetic_gate(self):
        # The realistic mid-job decision is a Skip (detach/approved=yes with a
        # fresh updated_at). It must veto the automatic synthetic-stands gate
        # — otherwise the person the user just skipped gets an approved
        # synthetic identity they never accepted.
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            review, facts = base / "review.csv", base / "facts"
            self._facts(facts, "pid-jordan", "Jordan Bravo")
            profile = {"person": {"full_name": "Jordan Bravo", "confidence": 0.75},
                       "social": {"linkedin_status": "not_found"}}
            fake = self._fake_research({}, profile)

            def research_then_skip(params):
                outcome = fake(params)
                rows_now = web_retargets.load_override_rows(review)
                rows_now["jordan-bravo-wrong"] = {
                    "public_identifier": "jordan-bravo-wrong",
                    "action": "detach", "approved": "yes",
                    "updated_at": "2099-01-01T00:00:00Z"}
                web_retargets.write_override_rows(review, rows_now)
                return outcome

            assemble = mock.Mock()
            assemble.return_value.run.return_value = mock.Mock(built=1, preserved_user_rows=0)
            request = web_retargets.GuidedRetarget(
                slug="jordan-bravo-p", pub="jordan-bravo-wrong",
                name="Jordan Bravo", guidance="the Jordan Bravo at Acme",
                person_ids=("pid-jordan",),
                candidate_pubs=("jordan-bravo-wrong",),
                submitted_at="2026-01-01T00:00:00Z")
            with mock.patch.object(web_retargets.deep_research_contacts, "run_research",
                                   side_effect=research_then_skip), \
                 mock.patch.object(web_retargets.assemble_synthetic_profile,
                                   "AssembleSyntheticProfile", assemble), \
                 mock.patch.object(web_retargets, "sync_synthetic_gate") as gate:
                result = web_retargets.run_guided_retarget(
                    request, review_path=review,
                    people_csv=base / "missing-people.csv",
                    facts_dir=facts, raw_dir=base / "raw", out_dir=base / "out",
                    engine_dir=base / "engine", use_llm=False)
            self.assertEqual(result["state"], "no_match")
            self.assertIn("that stands", result["detail"])
            gate.assert_not_called()
            row = _rows_by_pub(review)["jordan-bravo-wrong"]
            self.assertEqual(row["updated_at"], "2099-01-01T00:00:00Z")  # skip untouched

    def test_queue_drains_serially_and_reports_terminal_states(self):
        order: list[str] = []

        def runner(request, report):
            order.append(request.pub)
            report("judging", "checking")
            if request.pub == "bad-person":
                raise SystemExit("guard tripped")
            return {"state": "applied", "detail": "done",
                    "new_url": "https://www.linkedin.com/in/jordan-bravo-right"}

        queue = web_retargets.RetargetQueue(runner)
        first = queue.submit(self._request(pub="good-person"))
        self.assertEqual(first["state"], "queued")
        queue.submit(self._request(slug="casey-p", pub="bad-person"))
        deadline = threading.Event()
        for _ in range(100):
            if not queue.has_active():
                break
            deadline.wait(0.05)
        by_pub = {item["pub"]: item for item in queue.snapshot()}
        self.assertEqual(order, ["good-person", "bad-person"])
        self.assertEqual(by_pub["good-person"]["state"], "applied")
        self.assertIn("jordan-bravo-right", by_pub["good-person"]["new_url"])
        self.assertEqual(by_pub["bad-person"]["state"], "failed")
        self.assertIn("guard tripped", by_pub["bad-person"]["detail"])

    def test_queue_rejects_duplicate_active_submit(self):
        release = threading.Event()

        def runner(request, report):
            release.wait(5)
            return {"state": "applied", "detail": ""}

        queue = web_retargets.RetargetQueue(runner)
        queue.submit(self._request(pub="jordan-bravo-wrong"))
        with self.assertRaises(ValueError):
            queue.submit(self._request(pub="jordan-bravo-wrong"))
        release.set()

    def test_feedback_request_collects_identity_context_and_guidance(self):
        from packs.ingestion.primitives.deep_context.review_web import feedback as web_feedback
        parent = {"slug": "jordan-bravo-p", "dossier_slug": "jordan-bravo-p",
                  "name": "Jordan Bravo", "person_ids": ["pid-jordan"],
                  "worth_row": {"human": {"decision": "yes"},
                                "machine": {"decision": "maybe",
                                            "reason": "private synthesized prose"}}}
        candidate = {"pub": "jordan-bravo-wrong", "confidence": 0.42,
                     "url": "https://www.linkedin.com/in/jordan-bravo-wrong",
                     "new_url": "https://www.linkedin.com/in/jordan-bravo-right",
                     "action": "retarget", "approved": ""}
        items = [{"guidance": "the DevRel lead at Acme", "state": "applied",
                  "new_url": "https://www.linkedin.com/in/jordan-bravo-right",
                  "submitted_at": "2026-07-30T00:00:00Z"},
                 {"guidance": "", "state": "failed"}]
        request = web_feedback.build_feedback_request(
            parent, candidate, action="worth_no", comment="wrong person",
            retarget_items=items,
            environ={"POWERPACKS_DEFAULT_SET_ID": "0b6f8f3e-8f3e-4e6f-9a2b-1c2d3e4f5a6b"})
        body = request.body()
        meta = body["metadata"]
        self.assertEqual(body["feedback_type"], "data_inconsistency")
        self.assertEqual(body["category"], "linkedin")  # guidance present
        self.assertEqual(body["set_id"], "0b6f8f3e-8f3e-4e6f-9a2b-1c2d3e4f5a6b")
        self.assertEqual(meta["action"], "worth_no")
        self.assertEqual(meta["human_worth"], "yes")
        self.assertEqual(meta["machine_worth"], "maybe")
        self.assertEqual(len(meta["retarget_guidance"]), 1)  # blank guidance dropped
        self.assertEqual(meta["retarget_guidance"][0]["guidance"], "the DevRel lead at Acme")
        self.assertNotIn("person_id", body)  # local ids never ride the UUID column
        # Dossier-synthesized prose stays local: decisions travel, reasons do not.
        self.assertNotIn("private synthesized prose", json.dumps(body))

    def test_retarget_submit_files_its_guidance_as_feedback(self):
        from packs.ingestion.primitives.deep_context.review_web import feedback as web_feedback
        self.assertIn("retarget", web_feedback.FEEDBACK_ACTIONS)
        request = web_feedback.build_feedback_request(
            {"slug": "jordan-bravo-p", "name": "Jordan Bravo", "person_ids": ["pid-jordan"]},
            {"pub": "jordan-bravo-wrong",
             "url": "https://www.linkedin.com/in/jordan-bravo-wrong"},
            action="retarget", comment="the DevRel lead at Acme",
            retarget_items=[{"guidance": "the DevRel lead at Acme", "state": "queued"}],
            environ={})
        body = request.body()
        self.assertEqual(body["comment"], "the DevRel lead at Acme")
        self.assertEqual(body["category"], "linkedin")
        self.assertEqual(body["metadata"]["action"], "retarget")
        self.assertEqual(
            body["metadata"]["retarget_guidance"][0]["guidance"], "the DevRel lead at Acme")

    def test_feedback_request_worth_category_and_junk_set_id(self):
        from packs.ingestion.primitives.deep_context.review_web import feedback as web_feedback
        request = web_feedback.build_feedback_request(
            {"slug": "casey-p", "name": "Casey Delta", "person_ids": []},
            {"pub": "casey-delta", "url": "https://www.linkedin.com/in/casey-delta"},
            action="worth_yes", comment="great fit", retarget_items=[],
            environ={"POWERPACKS_DEFAULT_SET_ID": "not-a-uuid"})
        body = request.body()
        self.assertEqual(body["category"], "worth")
        self.assertNotIn("set_id", body)
        self.assertNotIn("retarget_guidance", body["metadata"])

    def test_directory_pane_and_page_carry_the_retarget_ui(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            parent = {"slug": "jordan-bravo-p", "dossier_slug": "jordan-bravo-p",
                      "name": "Jordan Bravo", "person_ids": ["pid-jordan"],
                      "candidates": [{"pub": "jordan-bravo-wrong",
                                      "url": "https://www.linkedin.com/in/jordan-bravo-wrong",
                                      "full_name": "Jordan Bravo", "approved": "yes",
                                      "action": "verify", "confidence": 0.9}]}
            pane = web_rendering.render_person_detail(
                parent, base / "parents", base / "dossiers", base / "profiles")
            self.assertIn("data-retarget-form", pane)
            self.assertIn("data-pub='jordan-bravo-wrong'", pane)
            self.assertIn(">Re-research</button>", pane)
            page = web_rendering.directory_page_html(
                [parent], {}, parents_dir=base / "parents",
                dossier_dir=base / "dossiers",
                profile_cache_dir=base / "profiles").decode("utf-8")
            self.assertIn("data-retarget-panel", page)
            self.assertNotIn("data-feedback-trigger", pane)  # no standalone button;
            # feedback rides worth clicks + the "…" general-feedback menu.
            self.assertIn("data-person-menu", pane)
            self.assertIn("data-feedback-general", pane)
        script = web_rendering.REVIEW_JS.read_text(encoding="utf-8")
        self.assertIn("/api/retargets", script)
        self.assertIn("data-retarget-form", script)
        self.assertIn("/feedback", script)
        self.assertIn("feedbackPopover", script)
        # Two-step decide: the form opens before the move; the move runs onDone
        # (send or Skip), never before the popover settles.
        self.assertIn("onDone", script)
        self.assertIn("feedback-skip", script)
        self.assertIn("onDone: () => void applyWorth", script)
        # needs_auth recovery: readable error + one-click browser sign-in.
        self.assertIn("needs_auth", script)
        self.assertIn("Sign in to Powerset", script)
        self.assertIn("/auth/login", script)
        self.assertIn("renderFeedbackAlert", script)   # auto-path failures surface too
        server_src = (web_rendering.REVIEW_JS.parent / "server.py").read_text(encoding="utf-8")
        self.assertIn("/auth/login", server_src)
        self.assertIn("def start_auth_login", server_src)
        self.assertIn("feedback_alert", server_src)


class StepperPendingCountTests(unittest.TestCase):
    """A stage the ladder marked complete must still show pending work.

    Real report: manifest had stage=linkedin, status=awaiting_user, total=674,
    pending=573 — but `completed_stages` still contained "linkedin" from an
    earlier pass, so the renderer showed a checkmark and suppressed the count.
    Enrichment adds decisions after a stage completes; the latched gate flag
    must not hide them.
    """

    def test_completed_stage_with_pending_shows_count_not_check(self):
        html = web_rendering._step(3, "Check LinkedIn", False, True, 573, "/?stage=linkedin")
        self.assertIn("573 left", html)
        self.assertNotIn("✓", html)
        self.assertIn(">3<", html)          # keeps its number
        self.assertNotIn("step complete", html)

    def test_completed_stage_with_zero_pending_still_checks(self):
        html = web_rendering._step(3, "Check LinkedIn", False, True, 0, "/?stage=linkedin")
        self.assertIn("✓", html)
        self.assertIn("step complete", html)
        self.assertNotIn("left", html)

    def test_incomplete_stage_shows_count(self):
        html = web_rendering._step(1, "Review Decisions", True, False, 12, "/?stage=worth")
        self.assertIn("12 left", html)
        self.assertNotIn("✓", html)


class LinkedinCardRetargetBoxTests(unittest.TestCase):
    """The guided-retarget box is reachable from the LinkedIn review cards,
    not only the directory pane. The review card is where a reviewer actually
    discovers that every attached profile is the wrong person — or that the
    person has no LinkedIn at all (both attached profiles judged wrong, deep
    research verified identity everywhere BUT LinkedIn) — so the re-research
    escape hatch must live on the card itself."""

    def _parent(self):
        return {"name": "Jordan Bravo", "slug": "jordan-bravo-ab12cd34",
                "dossier_slug": "jordan-bravo-ab12cd34",
                "person_ids": ["pid-1"], "candidates": []}

    def test_single_card_offers_guided_retarget(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo"}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertIn("data-retarget-form", html)
        self.assertIn("data-pub='jordan-bravo'", html)
        self.assertIn("data-parent='jordan-bravo-ab12cd34'", html)
        self.assertIn(">Re-research</button>", html)

    def test_card_has_overflow_menu_and_skip_opens_the_box(self):
        # The "…" menu (general feedback, same markup as the directory pane)
        # sits on every card; Skip opens the guidance box in skip mode instead
        # of deciding directly, so a why-note can ride along.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo"}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertIn("data-person-menu", html)
        self.assertIn("data-feedback-general", html)
        self.assertIn("class='skip-link' data-open-skip", html)
        self.assertNotIn("data-toast='Skipped'", html)  # no direct-decide skip

    def test_failed_reresearch_note_leads_the_returned_card(self):
        # A person whose guided re-research FAILED returns to the queue; the
        # card must say why, or the return reads as an unexplained loop.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo"}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d,
                failure_note="research blocked: PARALLEL_API_KEY not set")
        self.assertIn("class='reresearch-failed'", html)
        self.assertIn("Re-research failed: research blocked", html)
        without = web_rendering.render_linkedin_card(
            self._parent(), cand, Path("."), Path("."), profile_cache_dir=Path("."))
        self.assertNotIn("reresearch-failed", without)

    def test_blank_profile_card_leads_with_open_reresearch(self):
        # A valid URL whose profile is 404/private/empty renders the WHY and
        # the re-research box OPEN — "Is this the right profile?" is
        # unanswerable against a blank card.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo"}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertNotIn("no usable profile content", html)  # no why-paragraph
        self.assertIn("<details class='retarget-guidance' open>", html)
        self.assertIn("No profile data — give re-research guidance", html)
        self.assertNotIn("View LinkedIn", html)          # no dead-link affordance
        self.assertIn("Invalid LinkedIn.", html)
        self.assertNotIn("Is this the right profile?", html)
        self.assertNotIn("Use this profile", html)  # nothing to confirm
        self.assertNotIn("data-open-guidance", html)  # no binary actions on invalid cards
        self.assertNotIn("data-decide='detach'", html)  # no terminal No either
        self.assertIn("data-open-skip", html)  # Skip stays

    def test_machine_reason_and_placeholder_junk_never_render(self):
        # 'no usable LinkedIn profile' is judge state, not a summary; '--' is
        # a letterless placeholder headline. Neither renders as content.
        self.assertEqual(web_rendering._display_reason("no usable LinkedIn profile"), "")
        self.assertEqual(web_rendering._displayable("--"), "")
        self.assertEqual(web_rendering._displayable("—"), "")
        self.assertEqual(web_rendering._displayable("Founder at Bravo"), "Founder at Bravo")
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo",
                    "headline": "--",
                    "reason": "no usable LinkedIn profile"}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertNotIn("--", html.split("<article")[1][:2000])
        self.assertNotIn("no usable LinkedIn profile", html)
        self.assertIn("Invalid LinkedIn.", html)

    def test_headline_only_shell_counts_as_blank(self):
        # The Ace Padua shape: cache holds "Student at ..." and nothing else.
        # A headline alone is a shell — same no-profile bar as the judge.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo",
                    "headline": "Student at Example University"}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertIn("Student at Example University", html)  # still shown
        self.assertIn("<details class='retarget-guidance' open>", html)
        self.assertNotIn("View LinkedIn", html)  # a shell is not a LinkedIn

    def test_rich_profile_card_keeps_collapsed_box(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": ["Founder @ Bravo Robotics"]}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertNotIn("no usable profile content", html)
        self.assertIn("<details class='retarget-guidance'>", html)
        self.assertIn("Wrong person? Provide LinkedIn or re-research", html)
        self.assertIn("View LinkedIn", html)  # real profile keeps its link

    def test_single_card_no_is_terminal_detach_and_guidance_box_survives(self):
        # The old "No" only opened the guidance box and saved NOTHING — the row
        # stayed pending and the person kept returning to the queue. "No" now
        # decides detach directly (the same /decide fan-out as Skip), keyed on
        # the candidate pub + parent slug, while the guidance box remains the
        # independent pre-decision re-research path.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": ["Founder @ Bravo Robotics"]}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        no_button = html.split(">No</button>")[0].rsplit("<button", 1)[1]
        self.assertIn("data-decide='detach'", no_button)
        self.assertIn("data-toast='Not this profile'", no_button)
        self.assertIn("data-pub='jordan-bravo'", no_button)
        self.assertIn("data-parent='jordan-bravo-ab12cd34'", no_button)
        # The box is no longer wired to No — only its own <summary> opens it —
        # and it still exists with the URL-paste/re-research form intact.
        self.assertNotIn("data-open-guidance", html)
        self.assertIn("<details class='retarget-guidance'>", html)
        self.assertIn("data-retarget-form", html)
        self.assertIn(">Re-research</button>", html)

    def test_multi_card_offers_guided_retarget_keyed_on_primary(self):
        parent = self._parent()
        cands = [{"pub": "jordan-bravo"}, {"pub": "jordan-bravo-b2"}]
        parent["candidates"] = cands
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            html = web_rendering.render_linkedin_card(
                parent, cands, d, d, profile_cache_dir=d)
        self.assertEqual(html.count("data-retarget-form"), 1)  # one box, on the parent
        self.assertIn("data-pub='jordan-bravo'", html)

    def test_review_submit_handler_excludes_directory_pane(self):
        # The directory pane has its own handler (it refreshes the sidebar queue
        # panel); the document-level one must skip its forms or submits double.
        script = web_rendering.REVIEW_JS.read_text(encoding="utf-8")
        self.assertIn('form.closest("[data-directory-detail]")', script)


class LinkedinCardConfidenceBadgeTests(unittest.TestCase):
    """Judge-confidence pill on the LinkedIn review cards: a real, judged,
    renderable profile shows "NN% match" beside the View-LinkedIn link — one
    muted style regardless of verdict. Synthetic rows (their confidence is
    research completeness), unjudged rows (confidence 0/missing), and invalid
    cache-miss profiles never show it."""

    def _parent(self):
        return {"name": "Jordan Bravo", "slug": "jordan-bravo-ab12cd34",
                "dossier_slug": "jordan-bravo-ab12cd34",
                "person_ids": ["pid-1"], "candidates": []}

    def test_single_card_shows_percent_badge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo",
                    "headline": "Founder at Bravo Robotics",
                    "experiences": ["Founder @ Bravo Robotics"],
                    "verdict": "confirmed", "confidence": "0.87"}  # float-ish string
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertIn("<span class='linkedin-confidence'>87% match</span>", html)
        self.assertIn("View LinkedIn", html)

    def test_zero_or_missing_confidence_has_no_badge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            for cand in ({"pub": "jordan-bravo",
                          "url": "https://www.linkedin.com/in/jordan-bravo",
                          "experiences": ["Founder @ Bravo Robotics"],
                          "verdict": "confirmed", "confidence": "0.0"},
                         {"pub": "jordan-bravo",
                          "url": "https://www.linkedin.com/in/jordan-bravo",
                          "experiences": ["Founder @ Bravo Robotics"]}):
                html = web_rendering.render_linkedin_card(
                    self._parent(), cand, d, d, profile_cache_dir=d)
                self.assertNotIn("linkedin-confidence", html)
                self.assertIn("View LinkedIn", html)  # the link itself stays

    def test_invalid_cache_miss_profile_has_no_badge(self):
        # No experiences/education and an empty cache dir: the card is the
        # "Invalid LinkedIn" shape — nothing to confirm, so no confidence pill.
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "jordan-bravo",
                    "url": "https://www.linkedin.com/in/jordan-bravo",
                    "verdict": "confirmed", "confidence": 0.9}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertIn("Invalid LinkedIn.", html)
        self.assertNotIn("linkedin-confidence", html)

    def test_synthetic_candidate_has_no_badge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            cand = {"pub": "synth-pid-1", "synthetic": True,
                    "verdict": "synthetic", "confidence": 0.95}
            html = web_rendering.render_linkedin_card(
                self._parent(), cand, d, d, profile_cache_dir=d)
        self.assertNotIn("linkedin-confidence", html)

    def test_multi_card_options_each_carry_their_badge(self):
        parent = self._parent()
        cands = [{"pub": "jordan-bravo",
                  "url": "https://www.linkedin.com/in/jordan-bravo",
                  "experiences": ["Founder @ Bravo Robotics"],
                  "verdict": "confirmed", "confidence": 0.91},
                 {"pub": "jordan-bravo-b2",
                  "url": "https://www.linkedin.com/in/jordan-bravo-b2",
                  "experiences": ["Analyst @ Example Corp"],
                  "verdict": "wrong_person", "confidence": 0.62}]
        parent["candidates"] = cands
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            html = web_rendering.render_linkedin_card(
                parent, cands, d, d, profile_cache_dir=d)
        self.assertIn("<span class='linkedin-confidence'>91% match</span>", html)
        self.assertIn("<span class='linkedin-confidence'>62% match</span>", html)


class WorthWhyNoteTests(unittest.TestCase):
    """The worth review card carries an optional collapsed "why" box; whatever
    is typed rides along with the Yes/No click — saved to review.csv
    (user_worth_note, human-owned) and auto-filed as feedback."""

    def test_worth_card_renders_collapsed_note_box(self):
        parent = {"name": "Jordan Bravo", "slug": "jordan-bravo-ab12cd34",
                  "dossier_slug": "jordan-bravo-ab12cd34",
                  "person_ids": ["pid-1"], "candidates": []}
        with tempfile.TemporaryDirectory() as tmpdir:
            d = Path(tmpdir)
            html = web_rendering.render_worth_card(parent, d, d, profile_cache_dir=d)
        self.assertIn("data-worth-note", html)
        self.assertIn("Give feedback", html)
        self.assertNotIn("<details class='worth-why' open", html)  # collapsed

    def test_note_saves_with_decision_and_survives_noteless_redecision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review = Path(tmpdir) / "review.csv"
            web_decisions.apply_worth_decision(
                review, "parent-worth:pid-1", "no",
                user_worth_note="Cold pitch, we said no twice")
            rows = reconcile.load_override_rows(review)
            self.assertEqual(rows["parent-worth:pid-1"]["user_worth_note"],
                             "Cold pitch, we said no twice")
            # Flipping the decision without typing a new note keeps the old one.
            web_decisions.apply_worth_decision(review, "parent-worth:pid-1", "yes")
            rows = reconcile.load_override_rows(review)
            self.assertEqual(rows["parent-worth:pid-1"]["network_worth"], "yes")
            self.assertEqual(rows["parent-worth:pid-1"]["user_worth_note"],
                             "Cold pitch, we said no twice")

    def test_old_review_csv_without_column_loads_and_rewrites(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            review = Path(tmpdir) / "review.csv"
            old_cols = [c for c in reconcile.OVERRIDE_COLUMNS if c != "user_worth_note"]
            review.write_text(
                ",".join(old_cols) + "\njordan-bravo" + "," * (len(old_cols) - 1) + "\n",
                encoding="utf-8")
            web_decisions.apply_worth_decision(
                review, "parent-worth:pid-1", "no", user_worth_note="spam")
            rows = reconcile.load_override_rows(review)
            self.assertIn("jordan-bravo", rows)                   # legacy row kept
            self.assertEqual(rows["parent-worth:pid-1"]["user_worth_note"], "spam")
            header = review.read_text(encoding="utf-8").splitlines()[0]
            self.assertIn("user_worth_note", header)              # column added

    def test_decide_click_sends_the_note(self):
        script = web_rendering.REVIEW_JS.read_text(encoding="utf-8")
        self.assertIn('[data-worth-note]', script)
        self.assertIn("note,", script)  # rides in the /worth POST payload


class SyntheticFoldTests(unittest.TestCase):
    """A synthetic minted for an import candidate folds onto the REAL person's
    card via the identifier union index.json already encodes (by_email/by_phone),
    instead of minting a duplicate standalone card. One synthetic option per
    card (richest pending wins); none on a settled identity."""

    def _index(self, d):
        idx = {
            "parents": {"jordan-bravo-ab12cd34": {
                "children": ["jordan-bravo-aa11"], "parent_id": "parent-1"}},
            "slugs": {"jordan-bravo-aa11": {"person_id": "pid-1"}},
            "by_email": {"jordan@example.com":
                         ["jordan-bravo-aa11", "jordan-bravo-ab12cd34"]},
            "by_phone": {"5550100999": ["jordan-bravo-aa11"]},
        }
        path = Path(d) / "index.json"
        path.write_text(json.dumps(idx), encoding="utf-8")
        return path

    def _real(self, cands=None):
        return {"slug": "jordan-bravo-ab12cd34",
                "dossier_slug": "jordan-bravo-ab12cd34",
                "name": "Jordan Bravo", "person_ids": ["pid-1"], "sources": [],
                "candidates": cands if cands is not None else [
                    {"pub": "wrong-jordan", "action": "detach", "approved": ""}]}

    def _synth(self, pid, pub, exp=0, edu=0, headline=""):
        return {"slug": f"synthetic-{pub}", "dossier_slug": f"synthetic-{pub}",
                "name": "Jordan Bravo", "person_ids": [pid], "sources": [],
                "candidates": [{"pub": pub, "synthetic": True, "approved": "",
                                "action": "", "experiences": ["x"] * exp,
                                "education": ["e"] * edu, "headline": headline}]}

    def test_email_synthetic_folds_onto_real_parent(self):
        with tempfile.TemporaryDirectory() as d:
            merged = web_model.collapse_by_current_parent(
                [self._real(),
                 self._synth("candidate:email:jordan@example.com", "synth-a", exp=2)],
                self._index(d))
        self.assertEqual(len(merged), 1)
        pubs = [c["pub"] for c in merged[0]["candidates"]]
        self.assertIn("synth-a", pubs)
        self.assertIn("wrong-jordan", pubs)

    def test_phone_digits_normalize_for_the_join(self):
        # +1 country code vs index.json's 10-digit national key.
        with tempfile.TemporaryDirectory() as d:
            merged = web_model.collapse_by_current_parent(
                [self._real(),
                 self._synth("candidate:phone:+15550100999", "synth-p", exp=1)],
                self._index(d))
        self.assertEqual(len(merged), 1)
        self.assertIn("synth-p", [c["pub"] for c in merged[0]["candidates"]])

    def test_two_synthetics_richest_pending_wins(self):
        with tempfile.TemporaryDirectory() as d:
            merged = web_model.collapse_by_current_parent(
                [self._real(),
                 self._synth("candidate:email:jordan@example.com", "synth-rich",
                             exp=2, edu=2),
                 self._synth("candidate:phone:+15550100999", "synth-thin")],
                self._index(d))
        self.assertEqual(len(merged), 1)
        synths = [c["pub"] for c in merged[0]["candidates"] if c.get("synthetic")]
        self.assertEqual(synths, ["synth-rich"])
        # The thinner sibling's gate is settled with the parent, not orphaned.
        self.assertEqual(merged[0]["pruned_synthetic_pubs"], ["synth-thin"])

    def test_settled_identity_still_surfaces_the_synthetic(self):
        # A verified link does NOT suppress a paid researched identity — the
        # human sees both and picks (guided re-research must always surface).
        confirmed = [{"pub": "right-jordan", "action": "verify", "approved": "auto"}]
        with tempfile.TemporaryDirectory() as d:
            merged = web_model.collapse_by_current_parent(
                [self._real(cands=confirmed),
                 self._synth("candidate:email:jordan@example.com", "synth-a", exp=2)],
                self._index(d))
        self.assertEqual(len(merged), 1)
        self.assertEqual([c["pub"] for c in merged[0]["candidates"]],
                         ["right-jordan", "synth-a"])
        self.assertEqual([c["pub"] for c in
                          web_workflow.pending_linkedin_candidates(merged[0])],
                         ["synth-a"])

    def test_shared_identifier_never_folds(self):
        # Two parents own the same email in the index -> the synthetic must not
        # pick a co-owner arbitrarily; it stays a standalone card.
        idx = {
            "parents": {
                "jordan-bravo-ab12cd34": {"children": ["jordan-bravo-aa11"]},
                "casey-example-ff00aa11": {"children": ["casey-example-bb22"]}},
            "slugs": {"jordan-bravo-aa11": {"person_id": "pid-1"},
                      "casey-example-bb22": {"person_id": "pid-2"}},
            "by_email": {"shared@example.com":
                         ["jordan-bravo-aa11", "casey-example-bb22"]},
            "by_phone": {},
        }
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "index.json"
            path.write_text(json.dumps(idx), encoding="utf-8")
            merged = web_model.collapse_by_current_parent(
                [self._real(),
                 self._synth("candidate:email:shared@example.com", "synth-s", exp=1)],
                path)
        self.assertEqual(len(merged), 2)

    def test_unmatched_synthetic_stays_standalone(self):
        with tempfile.TemporaryDirectory() as d:
            merged = web_model.collapse_by_current_parent(
                [self._real(),
                 self._synth("candidate:email:casey@example.com", "synth-x", exp=1)],
                self._index(d))
        self.assertEqual(len(merged), 2)  # no identifier overlap -> untouched


class HighConfidenceDetachTests(unittest.TestCase):
    """A wrong_person verdict at/above the judge's detach bar is authoritative
    for review display even when the conflict group had no confirmed winner to
    trigger write-time auto-apply. The human never re-reviews (or sees an empty
    card for) a hard-contradicted profile; a below-bar detach stays pending."""

    def test_at_bar_unapplied_detach_reads_detached(self):
        cand = {"pub": "wrong-jordan", "action": "detach", "approved": "",
                "confidence": 0.95}
        self.assertEqual(web_model.candidate_state(cand), "detached")

    def test_below_bar_detach_stays_review(self):
        cand = {"pub": "maybe-jordan", "action": "detach", "approved": "",
                "confidence": 0.6}
        self.assertEqual(web_model.candidate_state(cand), "review")

    def test_no_winner_group_shows_only_the_synthetic(self):
        # The reported shape: both attached LinkedIns judged wrong at 0.95+,
        # no confirmed sibling, a synthetic folded in. The card offers the
        # synthetic alone — no blank wrong-person boxes.
        parent = {"slug": "jordan-bravo-p", "dossier_slug": "jordan-bravo-p",
                  "name": "Jordan Bravo", "person_ids": ["pid-1"], "sources": [],
                  "candidates": [
                      {"pub": "wrong-a", "action": "detach", "approved": "",
                       "confidence": 0.95},
                      {"pub": "wrong-b", "action": "detach", "approved": "",
                       "confidence": 0.98},
                      {"pub": "synth-a", "synthetic": True, "approved": "",
                       "action": "", "experiences": ["x"], "education": []},
                  ]}
        pending = web_workflow.pending_linkedin_candidates(parent)
        self.assertEqual([c["pub"] for c in pending], ["synth-a"])

    def test_all_high_conf_detached_no_synthetic_no_card(self):
        parent = {"slug": "jordan-bravo-p", "dossier_slug": "jordan-bravo-p",
                  "name": "Jordan Bravo", "person_ids": ["pid-1"], "sources": [],
                  "candidates": [
                      {"pub": "wrong-a", "action": "detach", "approved": "",
                       "confidence": 0.95}]}
        self.assertEqual(web_workflow.pending_linkedin_candidates(parent), [])
        self.assertFalse(web_workflow.identity_in_scope(parent))

    def test_bar_is_shared_with_reconcile(self):
        self.assertEqual(reconcile.DEFAULT_DETACH, reconcile.JUDGE_DETACH_THRESHOLD)


class LinearRetargetFlowTests(unittest.TestCase):
    """Review is LINEAR: queueing a re-research removes the person from the
    queue (server excludes active guided jobs), results apply automatically in
    the background, and the finish screen reports in-flight counts instead of
    ever re-serving a card. Completion is never blocked."""

    def _pending_parent(self, slug="jordan-bravo-p"):
        return {"slug": slug, "dossier_slug": slug, "name": "Jordan Bravo",
                "person_ids": ["pid-1"], "sources": [],
                "candidates": [{"pub": "jordan-bravo", "action": "", "approved": ""}]}

    def test_queue_excludes_inflight_parents(self):
        parent = self._pending_parent()
        self.assertEqual(len(web_rendering.linkedin_review_queue([parent])), 1)
        self.assertEqual(
            web_rendering.linkedin_review_queue([parent], frozenset({"jordan-bravo-p"})),
            [])

    def test_finished_body_reports_inflight_and_never_blocks(self):
        html = web_rendering.linkedin_finished_body(
            {"linkedin_done": 42}, linkedin_complete=True, retargets_in_flight=3)
        self.assertIn("3 re-researches still running", html)
        self.assertIn("go back to Codex", html)
        html_one = web_rendering.linkedin_finished_body(
            {"linkedin_done": 42}, linkedin_complete=False, retargets_in_flight=1,
            auto_continue=True)
        self.assertIn("1 re-research still running", html_one)
        # An empty queue self-completes — the reviewer only ever sees the
        # go-back state; the button still exists for the non-auto edge.
        self.assertIn("data-complete='linkedin' data-auto-complete", html_one)

    def test_retarget_submit_advances_the_card(self):
        script = web_rendering.REVIEW_JS.read_text(encoding="utf-8")
        self.assertIn("Queued for re-research — moving on", script)
        self.assertIn("/api/linkedin-card?exclude=", script)


class DecisiveConfirmTests(unittest.TestCase):
    """A conflict group's only bar-clearing confirm at >= DECISIVE_CONFIRM wins
    outright: keep it, detach every loser regardless of detach confidence — a
    0.97 confirm never sits hostage to a loser's 0.80. Two bar-clearing
    confirms is genuine ambiguity (family collisions) and stays human."""

    def _task(self, verdict, confidence):
        return {"verdict": {"verdict": verdict, "confidence": confidence}}

    def _bars(self):
        return reconcile.ConfidenceBars(reconcile.DEFAULT_CONFIRM,
                                        reconcile.DEFAULT_DETACH)

    def test_decisive_winner_drops_below_bar_loser(self):
        judged = [self._task("confirmed", 0.97), self._task("wrong_person", 0.80)]
        resolved = reconcile.decide_conflict_group(judged, self._bars())
        self.assertEqual(resolved[0], reconcile.CONFLICT_KEEP)
        self.assertEqual(resolved[1], reconcile.CONFLICT_DROP)

    def test_decisive_winner_drops_unjudged_punt_too(self):
        judged = [self._task("confirmed", 0.96), self._task("needs_review", 0.40)]
        resolved = reconcile.decide_conflict_group(judged, self._bars())
        self.assertEqual(resolved[0], reconcile.CONFLICT_KEEP)
        self.assertEqual(resolved[1], reconcile.CONFLICT_DROP)

    def test_sub_decisive_winner_still_needs_unanimity(self):
        judged = [self._task("confirmed", 0.90), self._task("wrong_person", 0.80)]
        self.assertEqual(reconcile.decide_conflict_group(judged, self._bars()), {})
        judged = [self._task("confirmed", 0.90), self._task("wrong_person", 0.90)]
        resolved = reconcile.decide_conflict_group(judged, self._bars())
        self.assertEqual(resolved[0], reconcile.CONFLICT_KEEP)

    def test_two_bar_clearing_confirms_stay_human(self):
        judged = [self._task("confirmed", 0.97), self._task("confirmed", 0.75)]
        self.assertEqual(reconcile.decide_conflict_group(judged, self._bars()), {})


class StoredIdentityPolicyScrubTests(unittest.TestCase):
    """The 2026-08 judge-apply policy re-derived over STORED review.csv rows at
    review entry (legacy scrub): decisive confirms promote and their siblings
    drop; punts superseded by an applied identity detach — no re-judge."""

    def _write(self, d, rows_spec, index=None):
        review = Path(d) / "review.csv"
        rows = {}
        for pub, action, approved, conf, pid in rows_spec:
            rows[pub] = {**{c: "" for c in reconcile.OVERRIDE_COLUMNS},
                         "public_identifier": pub, "action": action,
                         "approved": approved, "confidence": str(conf),
                         "person_id": pid}
        reconcile.write_override_rows(review, rows)
        idx = Path(d) / "index.json"
        idx.write_text(json.dumps(index or {"parents": {}, "slugs": {}}),
                       encoding="utf-8")
        return review, idx

    def test_decisive_confirm_promotes_and_sibling_drops(self):
        # The Langshur shape: two persons under one parent, winner 0.97
        # pending, loser wrong-person 0.80 pending.
        index = {"parents": {"jordan-p": {"parent_id": "parent-1",
                                          "children": ["c1", "c2"]}},
                 "slugs": {"c1": {"person_id": "pid-a"}, "c2": {"person_id": "pid-b"}}}
        with tempfile.TemporaryDirectory() as d:
            review, idx = self._write(d, [
                ("jordan-bravo", "verify", "", 0.97, "pid-a"),
                ("jordan-bravo-2", "detach", "", 0.80, "pid-b")], index)
            out = legacy.resolve_stored_identity_policy(review, idx)
            rows = reconcile.load_override_rows(review)
        self.assertEqual(out, {"connections": 0, "promoted": 1, "demoted": 1,
                               "siblings_settled": 0})
        self.assertEqual(rows["jordan-bravo"]["approved"], "auto")
        self.assertEqual((rows["jordan-bravo-2"]["action"],
                          rows["jordan-bravo-2"]["approved"]), ("detach", "auto"))

    def test_superseded_punt_detaches(self):
        # The Petkov shape: applied verify + a 0.62 punt on the same person.
        with tempfile.TemporaryDirectory() as d:
            review, idx = self._write(d, [
                ("jordan-bravo", "verify", "auto", 0.90, "pid-a"),
                ("jordan-doppel", "verify", "", 0.62, "pid-a")])
            out = legacy.resolve_stored_identity_policy(review, idx)
            rows = reconcile.load_override_rows(review)
        self.assertEqual(out, {"connections": 0, "promoted": 0, "demoted": 1,
                               "siblings_settled": 0})
        self.assertEqual((rows["jordan-doppel"]["action"],
                          rows["jordan-doppel"]["approved"]), ("detach", "auto"))
        self.assertEqual(rows["jordan-bravo"]["approved"], "auto")  # untouched

    def test_ambiguity_and_user_rows_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            review, idx = self._write(d, [
                ("jordan-bravo", "verify", "", 0.97, "pid-a"),   # decisive but…
                ("jordan-rival", "verify", "", 0.75, "pid-a"),   # …a rival clears the bar
                ("jordan-user", "detach", "no", 0.99, "pid-b"),  # user said no
                ("jordan-rt", "retarget", "", 0.99, "pid-c")])   # retargets never touched
            out = legacy.resolve_stored_identity_policy(review, idx)
            rows = reconcile.load_override_rows(review)
        self.assertEqual(out, {"connections": 0, "promoted": 0, "demoted": 0,
                               "siblings_settled": 0})
        self.assertEqual(rows["jordan-bravo"]["approved"], "")
        self.assertEqual(rows["jordan-user"]["approved"], "no")
        self.assertEqual(rows["jordan-rt"]["action"], "retarget")

    def test_connection_row_auto_verifies(self):
        # The AlSharekh shape: a restart-reset blanked a ground-truth
        # connection row to action='' approved='' — it auto-verifies, and the
        # freshly applied identity supersedes a doppelganger punt same-pass.
        with tempfile.TemporaryDirectory() as d:
            people = Path(d) / "people.csv"
            people.write_text(
                "id,public_identifier,source_channels\n"
                "pid-a,jordan-bravo,\"linkedin_csv,gmail_msgvault\"\n",
                encoding="utf-8")
            review, idx = self._write(d, [
                ("jordan-bravo", "", "", 1.0, "pid-a"),
                ("jordan-doppel", "verify", "", 0.62, "pid-a")])
            rows = reconcile.load_override_rows(review)
            for key in rows:  # connection rows carry their URL
                rows[key]["linkedin_url"] = f"https://www.linkedin.com/in/{key}"
            reconcile.write_override_rows(review, rows)
            out = legacy.resolve_stored_identity_policy(review, idx, people)
            rows = reconcile.load_override_rows(review)
        self.assertEqual(out, {"connections": 1, "promoted": 0, "demoted": 1,
                               "siblings_settled": 0})
        self.assertEqual((rows["jordan-bravo"]["action"],
                          rows["jordan-bravo"]["approved"]), ("verify", "auto"))
        self.assertEqual((rows["jordan-doppel"]["action"],
                          rows["jordan-doppel"]["approved"]), ("detach", "auto"))

    def test_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            review, idx = self._write(d, [
                ("jordan-bravo", "verify", "auto", 0.90, "pid-a"),
                ("jordan-doppel", "verify", "", 0.62, "pid-a")])
            legacy.resolve_stored_identity_policy(review, idx)
            second = legacy.resolve_stored_identity_policy(review, idx)
        self.assertEqual(second, {"connections": 0, "promoted": 0, "demoted": 0,
                                  "siblings_settled": 0})


_SYNTH_COLUMNS = ["id", "public_identifier", "full_name", "headline",
                  "summary", "location_raw", "work_experiences", "education",
                  "primary_email", "primary_phone", "source_parent_slug",
                  "source_person_ids", "approved", "synthetic_metadata"]


def _write_synthetic_csv(path, rows_spec):
    """synthetic-people.csv fixture rows: (pub, source_person_ids, approved, n_exp)."""
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_SYNTH_COLUMNS)
        writer.writeheader()
        for pub, person_ids, approved, exp in rows_spec:
            writer.writerow({
                "id": person_ids[0], "public_identifier": pub,
                "full_name": "Jordan Bravo", "headline": "researcher",
                "work_experiences": json.dumps(
                    [{"title": "CTO", "company_name": "StealthCo",
                      "is_current": True}] * exp),
                "education": "[]",
                "source_person_ids": json.dumps(person_ids),
                "approved": approved,
                "synthetic_metadata": json.dumps({"completeness": 0.5}),
            })


class HalfDecidedParentSettleTests(unittest.TestCase):
    """Scrub rule (4): a parent group holding a HUMAN identity decision settles
    its remaining pending candidate rows exactly like the live /decide fan-out
    (pre-v1.15.3 stores hold parents a human answered on one row while the
    sibling links and synthetic gates stayed pending and kept re-queueing)."""

    def _write_review(self, review, rows_spec):
        rows = {}
        for pub, action, approved, source, pid in rows_spec:
            rows[pub] = {**{c: "" for c in reconcile.OVERRIDE_COLUMNS},
                         "public_identifier": pub, "action": action,
                         "approved": approved, "source": source,
                         "person_id": pid,
                         "linkedin_url": f"https://www.linkedin.com/in/{pub}"}
        reconcile.write_override_rows(review, rows)

    def _index(self, d):
        idx = Path(d) / "index.json"
        idx.write_text(json.dumps({"parents": {}, "slugs": {}}), encoding="utf-8")
        return idx

    def test_human_decided_group_settles_pending_rows_and_gates(self):
        # The half-decided shape: the human skipped this person (detach/yes on
        # one row, a rule the promote/demote pass never touches), while a
        # judged sibling link, a proposed retarget, and a synthetic gate all
        # stayed pending — the card kept re-entering the queue.
        with tempfile.TemporaryDirectory() as d:
            review, idx = Path(d) / "review.csv", self._index(d)
            self._write_review(review, [
                ("jordan-bravo", "detach", "yes", "deep-context-review", "pid-1"),
                ("jordan-bravo-2", "verify", "", "deep-context-reconcile", "pid-1"),
                ("jordan-rt", "retarget", "", "deep-research", "pid-1"),
                ("jordan-auto", "detach", "auto", "deep-context-reconcile", "pid-1"),
                ("casey-example", "verify", "", "deep-context-reconcile", "pid-2")])
            rows = reconcile.load_override_rows(review)
            rows["jordan-rt"]["new_linkedin_url"] = "https://www.linkedin.com/in/other"
            reconcile.write_override_rows(review, rows)
            synth = Path(d) / "synthetic-people.csv"
            _write_synthetic_csv(synth, [
                ("synth-jordan", ["pid-1"], "", 1),
                ("synth-casey", ["pid-2"], "", 1)])
            out = legacy.resolve_stored_identity_policy(review, idx, None, synth)
            rows = reconcile.load_override_rows(review)
            with synth.open(newline="", encoding="utf-8") as fh:
                gates = {r["public_identifier"]: r["approved"]
                         for r in csv.DictReader(fh)}
        self.assertEqual(out, {"connections": 0, "promoted": 0, "demoted": 0,
                               "siblings_settled": 3})
        # pending siblings settle as a link-level No, exactly like /decide
        self.assertEqual((rows["jordan-bravo-2"]["action"],
                          rows["jordan-bravo-2"]["approved"]), ("detach", "yes"))
        self.assertEqual((rows["jordan-rt"]["action"], rows["jordan-rt"]["approved"],
                          rows["jordan-rt"]["new_linkedin_url"]),
                         ("detach", "yes", ""))
        # the human row and the machine-applied auto row are never touched
        self.assertEqual((rows["jordan-bravo"]["action"],
                          rows["jordan-bravo"]["approved"]), ("detach", "yes"))
        self.assertEqual((rows["jordan-auto"]["action"],
                          rows["jordan-auto"]["approved"]), ("detach", "auto"))
        # a parent with NO human decision is never touched
        self.assertEqual((rows["casey-example"]["action"],
                          rows["casey-example"]["approved"]), ("verify", ""))
        self.assertEqual(gates, {"synth-jordan": "no", "synth-casey": ""})

    def test_machine_yes_never_triggers_a_settle(self):
        # Old machine appliers wrote approved=yes with source `deep-research`
        # — machine-grade, not a human answer; the sibling must stay pending.
        with tempfile.TemporaryDirectory() as d:
            review, idx = Path(d) / "review.csv", self._index(d)
            self._write_review(review, [
                ("pat-machine", "retarget", "yes", "deep-research", "pid-3"),
                ("pat-2", "verify", "", "deep-context-reconcile", "pid-3")])
            out = legacy.resolve_stored_identity_policy(review, idx)
            rows = reconcile.load_override_rows(review)
        self.assertEqual(out["siblings_settled"], 0)
        self.assertEqual(rows["pat-2"]["approved"], "")

    def test_scrub_twice_is_idempotent_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as d:
            review, idx = Path(d) / "review.csv", self._index(d)
            self._write_review(review, [
                ("jordan-bravo", "detach", "yes", "deep-context-review", "pid-1"),
                ("jordan-bravo-2", "verify", "", "deep-context-reconcile", "pid-1")])
            synth = Path(d) / "synthetic-people.csv"
            _write_synthetic_csv(synth, [("synth-jordan", ["pid-1"], "", 1)])
            first = legacy.resolve_stored_identity_policy(review, idx, None, synth)
            review_bytes = review.read_bytes()
            synth_bytes = synth.read_bytes()
            second = legacy.resolve_stored_identity_policy(review, idx, None, synth)
            self.assertEqual(first["siblings_settled"], 2)
            self.assertEqual(second, {"connections": 0, "promoted": 0,
                                      "demoted": 0, "siblings_settled": 0})
            self.assertEqual(review.read_bytes(), review_bytes)
            self.assertEqual(synth.read_bytes(), synth_bytes)


class MixedParentSingleDecisionQueueTests(unittest.TestCase):
    """A parent with MANY mixed candidates (two judged LinkedIn rows plus
    synthetic options) leaves the LinkedIn review queue after ONE decision.
    Driven through the real queue (build_parents + extend_and_annotate +
    linkedin_review_queue); the decision is the legacy single-row /decide
    write (pre-v1.15.3 shape) repaired by the boot scrub's sibling settle."""

    def _fixture(self, d):
        base = Path(d)
        for sub in ("facts", "parents", "dossiers", "cache", "research"):
            (base / sub).mkdir()
        verdicts = base / "verdicts.jsonl"
        records = [
            {"parent_slug": "jordan-bravo-p", "name": "Jordan Bravo",
             "person_ids": ["pid-1"], "candidate_key": pub,
             "linkedin": {"linkedin_url": f"https://www.linkedin.com/in/{pub}",
                          "full_name": "Jordan Bravo", "has_profile": True},
             "verdict": {"verdict": "needs_review", "confidence": 0.5}}
            for pub in ("jordan-bravo", "jordan-bravo-2")]
        verdicts.write_text(
            "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        review = base / "review.csv"
        rows = {}
        for pub in ("jordan-bravo", "jordan-bravo-2"):
            rows[pub] = {**{c: "" for c in reconcile.OVERRIDE_COLUMNS},
                         "public_identifier": pub, "action": "verify",
                         "approved": "", "source": "deep-context-reconcile",
                         "person_id": "pid-1",
                         "linkedin_url": f"https://www.linkedin.com/in/{pub}"}
        reconcile.write_override_rows(review, rows)
        synth = base / "synthetic-people.csv"
        _write_synthetic_csv(synth, [("synth-jordan-a", ["pid-1"], "", 2),
                                     ("synth-jordan-b", ["pid-1"], "", 1)])
        index_json = base / "index.json"
        index_json.write_text(json.dumps({
            "parents": {"jordan-bravo-p": {"parent_id": "parent-1",
                                           "children": ["c1"]}},
            "slugs": {"c1": {"person_id": "pid-1"}},
            "by_email": {}, "by_phone": {}}), encoding="utf-8")
        return base, verdicts, review, synth, index_json

    def _queue(self, base, verdicts, review, synth, index_json):
        parents_list, overrides = web_model.build_parents(verdicts, review)
        web_model.extend_and_annotate(
            parents_list, overrides, synth, base / "facts", set(),
            parents_dir=base / "parents", dossier_dir=base / "dossiers",
            profile_cache_dir=base / "cache", research_dir=base / "research",
            index_json=index_json)
        return web_rendering.linkedin_review_queue(parents_list)

    def test_one_decision_settles_the_whole_card(self):
        with tempfile.TemporaryDirectory() as d:
            fixture = self._fixture(d)
            base, verdicts, review, synth, index_json = fixture
            queue = self._queue(*fixture)
            # ONE card for the parent, offering the links + one synthetic
            # option (the thinner synthetic sibling is display-pruned).
            self.assertEqual(len(queue), 1)
            self.assertEqual(len(queue[0][1]), 3)
            # The legacy /decide wrote ONLY the clicked row (Skip = detach):
            # the same single-row write the endpoint still starts from.
            web_decisions.apply_decision(
                review, verdicts, "jordan-bravo", "detach", "",
                reconcile.DEFAULT_CONFIRM)
            # Pre-v1.15.3 that left the card half-decided — still queued.
            self.assertEqual(len(self._queue(*fixture)), 1)
            # The boot scrub settles the group like the live fan-out would.
            out = legacy.resolve_stored_identity_policy(
                review, index_json, None, synth)
            self.assertEqual(out["siblings_settled"], 3)
            self.assertEqual(self._queue(*fixture), [])
            with synth.open(newline="", encoding="utf-8") as fh:
                gates = {r["public_identifier"]: r["approved"]
                         for r in csv.DictReader(fh)}
            self.assertEqual(gates, {"synth-jordan-a": "no",
                                     "synth-jordan-b": "no"})


class HealReviewTests(unittest.TestCase):
    """The pre-serve self-heal pass: selection predicate, fresh-fetch routing
    (CONTENT -> judge, fetched EMPTY -> terminate, ERROR -> untouched), the
    free-identity ladder (case a synthetic gate / b research mint / c pending
    card), idempotency, cap, and keyless skips."""

    PROFILE_STATES = {
        "hydra-jones": "content",
        "casey-dead-a": "empty",
        "bravo-dead-b": "empty",
        "dana-dead-c": "empty",
        "errol-err": "error",
    }

    @staticmethod
    def _rec(parent_slug, pub, pid, *, verdict=None, no_link=False, match_emails=None):
        url = f"https://www.linkedin.com/in/{pub}" if pub else ""
        return {
            "parent_slug": parent_slug, "name": parent_slug.replace("-", " ").title(),
            "candidate_key": pub, "person_ids": [pid], "conflict": False,
            "no_link": no_link, "name_matched": False,
            "linkedin": {"public_identifier": pub, "linkedin_url": url,
                         "has_profile": False, "source": "people_csv"},
            "match_emails": match_emails or [], "match_phones": [],
            "verdict": verdict or {
                "verdict": "needs_review", "confidence": 0.0,
                "supporting_evidence": [], "contradicting_evidence": [],
                "linkedin_plausibly_absent": True, "recommend_deep_research": False,
                "reason": reconcile.NO_PROFILE_REASON},
            "error": "",
        }

    def build_store(self, root: Path) -> "heal.HealReview":
        (root / "cache").mkdir()
        (root / "facts").mkdir()
        (root / "raw").mkdir()
        (root / "parents").mkdir()
        recs = [
            self._rec("hydra-jones-ab12", "hydra-jones", "p-hydra"),
            self._rec("casey-dead-a", "casey-dead-a", "p-deada"),
            self._rec("bravo-dead-b", "bravo-dead-b", "p-deadb",
                      match_emails=["bravo@example.com"]),
            self._rec("dana-dead-c", "dana-dead-c", "p-deadc"),
            self._rec("errol-err", "errol-err", "p-err"),
            self._rec("already-decided", "already-decided", "p-decided"),
            self._rec("retarget-away", "retarget-away", "p-retarget"),
            self._rec("confirmed-fine", "confirmed-fine", "p-fine", verdict={
                "verdict": "confirmed", "confidence": 0.9, "supporting_evidence": [],
                "contradicting_evidence": [], "linkedin_plausibly_absent": False,
                "recommend_deep_research": False, "reason": "fine"}),
            self._rec("nolink-nan", "", "p-nolink", no_link=True),
        ]
        with (root / "verdicts.jsonl").open("w", encoding="utf-8") as fh:
            for rec in recs:
                fh.write(json.dumps(rec) + "\n")
        # Review rows: one human-decided, one pending retarget to a DIFFERENT pub.
        rows = {
            "already-decided": {"public_identifier": "already-decided", "action": "verify",
                                "approved": "yes", "person_id": "p-decided",
                                "source": "deep-context-review"},
            "retarget-away": {"public_identifier": "retarget-away", "action": "retarget",
                              "approved": "", "person_id": "p-retarget",
                              "new_linkedin_url": "https://www.linkedin.com/in/someone-else",
                              "new_public_identifier": "someone-else",
                              "source": "deep-research"},
        }
        write_rows(root / "review.csv", rows)
        with (root / "people.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "id", "full_name", "public_identifier", "linkedin_url", "source_channels",
                "primary_email", "all_emails", "primary_phone", "all_phones",
                "interaction_counts"])
            writer.writeheader()
            writer.writerow({"id": "p-hydra", "full_name": "Hydra Jones",
                             "public_identifier": "hydra-jones",
                             "linkedin_url": "https://www.linkedin.com/in/hydra-jones",
                             "source_channels": "gmail_msgvault"})
        write_json(root / "index.json", {
            "parents": {"hydra-jones-ab12": {"name": "Hydra Jones", "parent_id": "p-hydra",
                                             "children": ["hydra-jones-child"]}},
            "slugs": {"hydra-jones-child": {"person_id": "p-hydra"}},
        })
        # Hydrated cache content for the judged candidate.
        write_json(root / "cache" / "hydra-jones.json", {
            "fetched_at": "2026-08-01T00:00:00Z", "last_checked_at": "2026-08-01T00:00:00Z",
            "public_identifier": "hydra-jones",
            "raw_response": {"full_name": "Hydra Jones"},
            "normalized_profile": {"success": True, "full_name": "Hydra Jones",
                                   "experiences": [{"title": "Founder",
                                                    "company_name": "Hydra Robotics"}],
                                   "education": []}})
        # Case a: an existing synthetic row for casey-dead-a's person.
        with (root / "synthetic-people.csv").open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=[
                "id", "public_identifier", "approved", "source_person_ids"])
            writer.writeheader()
            writer.writerow({"id": "p-deada", "public_identifier": "synth-email-caseydead",
                             "approved": "", "source_person_ids": json.dumps(["p-deada"])})
        # Case b: an engine research output proposing exactly the dead link.
        research_dir = root / "deep-research" / "bravo-dead-b"
        research_dir.mkdir(parents=True)
        write_json(research_dir / "01_research_parallel.json", {
            "person": {"full_name": "Bravo Deadlink", "first_name": "Bravo",
                       "last_name": "Deadlink", "confidence": 0.9},
            "headline": {"text": "Founder"},
            "positions": [{"title": "Founder", "company_name": "Deadlink Labs",
                           "is_current": True}],
            "education": [], "location": {"city": "SF", "country": "US"},
            "social": {"linkedin_url": "https://www.linkedin.com/in/bravo-dead-b"},
            "metadata": {"estimated_completeness": 0.9},
        })
        return heal.HealReview(
            review_csv=root / "review.csv",
            verdicts_jsonl=root / "verdicts.jsonl",
            verdicts_csv=root / "verdicts.csv",
            people_csv=root / "people.csv",
            profile_cache_dir=root / "cache",
            synthetic_csv=root / "synthetic-people.csv",
            index_json=root / "index.json",
            facts_dir=root / "facts",
            raw_dir=root / "raw",
            parents_dir=root / "parents",
            deep_research_dir=root / "deep-research",
            owner_json=root / "owner.json",
            review_manifest=root / "review" / "manifest.json",
        )

    @contextlib.contextmanager
    def heal_mocks(self, states=None, *, openai_key="test-key"):
        states = states if states is not None else self.PROFILE_STATES
        calls: list[str] = []

        def fake_get_profile(client_self, pub, url, *, cache_dir=None, fresh=False, **kw):
            calls.append(pub)
            state = states.get(pub, "error")
            return {"state": state, "normalized_profile": {}, "data": None,
                    "from_cache": False, "fetched": state != "error",
                    "status_code": 200 if state == "content" else 404,
                    "detail": "", "attempts": 1}

        async def fake_judge(client, task, owner_block, **kw):
            return {"verdict": {"verdict": "confirmed", "confidence": 0.95,
                                "supporting_evidence": ["fixture"],
                                "contradicting_evidence": [],
                                "linkedin_plausibly_absent": False,
                                "recommend_deep_research": False,
                                "reason": "fixture confirm"},
                    "usage": {"input_tokens": 1, "output_tokens": 1,
                              "reasoning_tokens": 0}, "error": ""}

        env = {"OPENAI_API_KEY": openai_key} if openai_key else {}
        with mock.patch.object(rapidapi_client.RapidApiClient, "get_profile",
                               fake_get_profile), \
                mock.patch.object(reconcile, "judge_task", fake_judge), \
                mock.patch.object(heal, "load_env", lambda: None), \
                mock.patch.dict(os.environ, env, clear=True):
            yield calls

    def test_selection_predicate_and_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            pass_obj = self.build_store(Path(tmp))
            candidates, skipped_retarget, uncapped = pass_obj.select_candidates()
            self.assertEqual(sorted(c.pub for c in candidates),
                             ["bravo-dead-b", "casey-dead-a", "dana-dead-c",
                              "errol-err", "hydra-jones"])
            self.assertEqual(skipped_retarget, 1)   # retarget-away: live proposal
            self.assertEqual(uncapped, 5)
            pass_obj.cap = 2
            capped, _, uncapped = pass_obj.select_candidates()
            self.assertEqual(len(capped), 2)
            self.assertEqual(uncapped, 5)

    def test_full_heal_routes_content_empty_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pass_obj = self.build_store(root)
            with self.heal_mocks() as fetch_calls, \
                    contextlib.redirect_stderr(io.StringIO()):
                summary = pass_obj.run()

            self.assertEqual(summary["candidates"], 5)
            self.assertEqual(len(fetch_calls), 5)
            self.assertEqual(summary["profiles"],
                             {"content": 1, "empty_fetched": 3, "empty_unfetched": 0,
                              "error": 1, "fetched": 4, "from_cache": 0})
            rows = load_rows(root / "review.csv")
            # CONTENT -> judged through the normal write path: verify/auto.
            self.assertEqual((rows["hydra-jones"]["action"],
                              rows["hydra-jones"]["approved"]), ("verify", "auto"))
            self.assertEqual(summary["rejudge"]["verified"], 1)
            # EMPTY -> machine detach, never a human row.
            for pub in ("casey-dead-a", "bravo-dead-b", "dana-dead-c"):
                self.assertEqual((rows[pub]["action"], rows[pub]["approved"]),
                                 ("detach", "auto"), pub)
                self.assertEqual(rows[pub]["source"], "deep-context-heal")
            # Case a: the existing synthetic row's gate stood to yes.
            with (root / "synthetic-people.csv").open(newline="", encoding="utf-8") as fh:
                synth = {r["public_identifier"]: r for r in csv.DictReader(fh)}
            self.assertEqual(synth["synth-email-caseydead"]["approved"], "yes")
            self.assertEqual(summary["terminated"]["stood_synthetic"], 1)
            # Case b: a synthetic minted from the research output (URL cleared).
            self.assertEqual(summary["terminated"]["minted_synthetic"], 1)
            minted = [pub for pub in synth if pub != "synth-email-caseydead"]
            self.assertEqual(len(minted), 1)
            self.assertEqual(synth[minted[0]].get("linkedin_url", ""), "")
            # Case c: nothing free — a pending re-research card.
            self.assertEqual(summary["terminated"]["pending_reresearch"], 1)
            # ERROR terminates nobody.
            self.assertNotEqual((rows["errol-err"].get("action"),
                                 rows["errol-err"].get("approved")), ("detach", "auto"))
            # Human yes/no untouched; the pending retarget proposal survives.
            self.assertEqual(rows["already-decided"]["approved"], "yes")
            self.assertEqual(rows["retarget-away"]["action"], "retarget")
            self.assertEqual(rows["retarget-away"]["new_public_identifier"], "someone-else")
            # Counts land in the review stage manifest.
            manifest = json.loads((root / "review" / "manifest.json").read_text())
            self.assertEqual(manifest["heal"]["candidates"], 5)

    def test_second_run_is_a_provably_free_no_op(self):
        # Every candidate gets a definitive answer in run 1 (an ERROR stays
        # retryable by design, so it would legitimately reappear in run 2).
        states = {**self.PROFILE_STATES, "errol-err": "empty"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pass_obj = self.build_store(root)
            with self.heal_mocks(states), contextlib.redirect_stderr(io.StringIO()):
                pass_obj.run()
            review_before = (root / "review.csv").read_text()
            synth_before = (root / "synthetic-people.csv").read_text()
            with self.heal_mocks() as fetch_calls, \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                summary = pass_obj.run()
            self.assertEqual(summary["candidates"], 0)
            self.assertEqual(len(fetch_calls), 0)
            self.assertEqual(summary["profiles"]["fetched"], 0)
            self.assertEqual(summary["rejudge"]["candidates"], 0)
            self.assertEqual(summary["terminated"]["detached"], 0)
            self.assertIn("scrubs 0 · fetched 0 · judged 0 · dead-links 0 "
                          "(nothing to do)", err.getvalue())
            self.assertEqual((root / "review.csv").read_text(), review_before)
            self.assertEqual((root / "synthetic-people.csv").read_text(), synth_before)

    def test_error_everywhere_terminates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pass_obj = self.build_store(root)
            states = {pub: "error" for pub in self.PROFILE_STATES}
            with self.heal_mocks(states), contextlib.redirect_stderr(io.StringIO()):
                summary = pass_obj.run()
            self.assertEqual(summary["terminated"]["detached"], 0)
            self.assertEqual(summary["rejudge"]["candidates"], 0)
            rows = load_rows(root / "review.csv")
            for pub in ("casey-dead-a", "bravo-dead-b", "dana-dead-c", "errol-err"):
                self.assertNotEqual((rows.get(pub, {}).get("action"),
                                     rows.get(pub, {}).get("approved")),
                                    ("detach", "auto"), pub)

    def test_keyless_openai_skips_judging_and_unfetched_empty_never_terminates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pass_obj = self.build_store(root)
            # A keyless RapidAPI install serves recorded EMPTY without a fetch:
            # that is NOT a fresh confirmation, so nobody terminates.
            states = dict(self.PROFILE_STATES)

            def keyless_get_profile(client_self, pub, url, *, cache_dir=None,
                                    fresh=False, **kw):
                state = states.get(pub, "error")
                return {"state": state, "normalized_profile": {}, "data": None,
                        "from_cache": state != "error", "fetched": False,
                        "status_code": 0, "detail": "no rapidapi key", "attempts": 0}

            with mock.patch.object(rapidapi_client.RapidApiClient, "get_profile",
                                   keyless_get_profile), \
                    mock.patch.object(heal, "load_env", lambda: None), \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    contextlib.redirect_stderr(io.StringIO()):
                summary = pass_obj.run()
            self.assertEqual(summary["terminated"]["detached"], 0)
            self.assertEqual(summary["profiles"]["empty_unfetched"], 3)
            # The hydrated candidate still routed to judging, which skipped on
            # the missing OpenAI key without writing anything.
            self.assertTrue(summary["rejudge"]["skipped_no_openai_key"])
            self.assertEqual(summary["rejudge"]["candidates"], 1)


if __name__ == "__main__":
    unittest.main()

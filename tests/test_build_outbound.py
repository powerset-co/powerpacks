import contextlib
import csv
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "packs" / "apollo" / "primitives" / "build_outbound" / "build_outbound.py"


def load_module():
    spec = importlib.util.spec_from_file_location("build_outbound", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class Chdir:
    def __init__(self, path):
        self.path = Path(path)
        self.old = Path.cwd()

    def __enter__(self):
        os.chdir(self.path)

    def __exit__(self, *_):
        os.chdir(self.old)


class FakeApolloClient:
    instances = []

    def __init__(self, *, active_campaign=False, existing_contact=False, messages=None):
        self.calls = []
        self.active_campaign = active_campaign
        self.existing_contact = existing_contact
        self.messages = messages or []
        self.add_payloads = []
        FakeApolloClient.instances.append(self)

    def get_email_accounts(self):
        self.calls.append(("get_email_accounts", None))
        return {"email_accounts": [{"id": "sender-1", "user_id": "user-1", "active": True, "default": True}]}

    def get_emailer_schedules(self):
        self.calls.append(("get_emailer_schedules", None))
        return {"emailer_schedules": [{"id": "schedule-1", "default": True}]}

    def bulk_match(self, linkedin_urls):
        self.calls.append(("bulk_match", list(linkedin_urls)))
        return [{"people": [
            {
                "id": "person-1",
                "email": "ada@example.com",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "title": "CTO",
                "organization_name": "Engines",
                "linkedin_url": linkedin_urls[0],
            },
            {
                "id": "person-2",
                "email": "ada@example.com",
                "first_name": "Ada",
                "last_name": "Lovelace",
                "linkedin_url": linkedin_urls[0],
            },
        ]}]

    def create_campaign(self, payload):
        self.calls.append(("create_campaign", payload))
        return {"emailer_campaign": {"id": "camp-1", "active": self.active_campaign, "status": "active" if self.active_campaign else "draft"}}

    def create_step(self, payload):
        self.calls.append(("create_step", payload))
        pos = payload["position"]
        return {"emailer_step": {"id": f"step-{pos}"}, "emailer_template": {"id": f"tpl-{pos}"}, "emailer_touch": {"id": f"touch-{pos}"}}

    def patch_template(self, template_id, payload):
        self.calls.append(("patch_template", template_id, payload))
        return {"id": template_id}

    def search_campaigns(self, q_keywords, per_page=25):
        self.calls.append(("search_campaigns", q_keywords, per_page))
        return {"emailer_campaigns": [{"id": "camp-1", "active": self.active_campaign, "status": "active" if self.active_campaign else "draft"}]}

    def approve_touch(self, touch_id):
        self.calls.append(("approve_touch", touch_id))
        return {"id": touch_id, "approved": True}

    def search_contacts(self, email):
        self.calls.append(("search_contacts", email))
        if self.existing_contact:
            return {"contacts": [{"id": "existing-1"}]}
        return {"contacts": []}

    def create_contact(self, payload):
        self.calls.append(("create_contact", payload))
        return {"contact": {"id": "contact-1"}}

    def add_contact_ids(self, campaign_id, contact_ids, sender_id):
        payload = {
            "url": f"/emailer_campaigns/{campaign_id}/add_contact_ids",
            "body": {
                "emailer_campaign_id": campaign_id,
                "contact_ids": contact_ids,
                "send_email_from_email_account_id": sender_id,
                "sequence_active_in_other_campaigns": False,
            },
        }
        self.add_payloads.append(payload)
        self.calls.append(("add_contact_ids", campaign_id, list(contact_ids), sender_id))
        return payload

    def approve_campaign(self, campaign_id):
        self.calls.append(("approve_campaign", campaign_id, "/approve"))
        return {"id": campaign_id, "approved": True}

    def search_messages(self, campaign_id):
        self.calls.append(("search_messages", campaign_id))
        return {"messages": self.messages}


class BuildOutboundTests(unittest.TestCase):
    def make_sales_nav_run(self, root, name="run-1", query="apollo founders", updated_at="2025-01-01T00:00:00Z", leads=None):
        run = Path(root) / ".powerpacks" / "sales-nav" / "runs" / name
        leads_path = run / "leads.jsonl"
        write_jsonl(leads_path, leads or [{"name": "Ada Lovelace", "linkedin_url": "https://linkedin.com/in/ada"}])
        write_json(run / "state.json", {"query": query, "updated_at": updated_at, "files": {"leads_jsonl": "leads.jsonl"}})
        return run

    def test_manifest_input_resolves_sibling_state_and_leads_paths(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            write_json(run / "manifest.json", {"ok": True})
            result = module.resolve_sales_nav_state(None, run / "manifest.json", None, None)
            leads = module.load_sales_nav_leads(Path(result["selected"]["state_path"]), None)

        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["selected"]["state_path"]).name, "state.json")
        self.assertEqual(leads[0]["linkedin_url"], "https://www.linkedin.com/in/ada")

    def test_sales_nav_csv_export_shape_loads_linkedin_and_name(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = Path(td) / ".powerpacks" / "sales-nav" / "runs" / "sales-nav-brookfield-test"
            exports = run / "exports"
            exports.mkdir(parents=True)
            with (exports / "leads.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["name", "title", "company", "location", "linkedin_url"])
                writer.writeheader()
                writer.writerow({
                    "name": "Arthur Chen",
                    "title": "Founder",
                    "company": "Powerset",
                    "location": "San Francisco Bay Area",
                    "linkedin_url": "[https://www.linkedin.com/in/arthur-chen-78566519/,https://media.licdn.com/photo.jpg",
                })
            write_json(run / "state.json", {
                "query": "Powerpacks Apollo live smoke test target",
                "updated_at": "2026-06-05T00:00:00Z",
                "files": {"final_leads_csv": "exports/leads.csv"},
            })
            write_json(run / "manifest.json", {
                "files": {"state": "state.json", "final_leads_csv": "exports/leads.csv"},
            })
            result = module.resolve_sales_nav_state(None, run / "manifest.json", None, None)
            leads = module.load_sales_nav_leads(Path(result["selected"]["state_path"]), None)

        self.assertTrue(result["ok"])
        self.assertEqual(leads, [{
            "name": "Arthur Chen",
            "first_name": "Arthur",
            "last_name": "Chen",
            "title": "Founder",
            "company": "Powerset",
            "linkedin_url": "https://www.linkedin.com/in/arthur-chen-78566519",
            "location": "San Francisco Bay Area",
            "source": {
                "name": "Arthur Chen",
                "title": "Founder",
                "company": "Powerset",
                "location": "San Francisco Bay Area",
                "linkedin_url": "[https://www.linkedin.com/in/arthur-chen-78566519/,https://media.licdn.com/photo.jpg",
            },
        }])

    def test_newest_matching_run_and_no_match_candidates_errors(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            self.make_sales_nav_run(td, "old", "Apollo founders", "2025-01-01T00:00:00Z")
            newest = self.make_sales_nav_run(td, "new", "Apollo founders", "2025-02-01T00:00:00Z")
            result = module.resolve_sales_nav_state("apollo founders", None, None, None)
            no_match = module.resolve_sales_nav_state("banking compliance", None, None, None)

        self.assertTrue(result["ok"])
        self.assertEqual(Path(result["selected"]["state_path"]), newest / "state.json")
        self.assertFalse(no_match["ok"])
        self.assertIn("no matching", no_match["error"])
        self.assertEqual(no_match["candidates"], [])

    def test_relative_path_resolution_order_and_diagnostics(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td) / "repo"
            cwd = Path(td) / "cwd"
            state_dir = Path(td) / "state"
            for path in (repo, cwd, state_dir):
                path.mkdir(parents=True)
            module.ROOT = repo
            (cwd / "same.jsonl").write_text("cwd")
            (repo / "same.jsonl").write_text("repo")
            (state_dir / "same.jsonl").write_text("state")
            state_path = state_dir / "state.json"
            with Chdir(cwd):
                self.assertEqual(module.resolve_existing_path("same.jsonl", state_path=state_path, repo_root=repo), cwd / "same.jsonl")
                (cwd / "same.jsonl").unlink()
                self.assertEqual(module.resolve_existing_path("same.jsonl", state_path=state_path, repo_root=repo), repo / "same.jsonl")
                (repo / "same.jsonl").unlink()
                self.assertEqual(module.resolve_existing_path("same.jsonl", state_path=state_path, repo_root=repo), state_dir / "same.jsonl")
                with self.assertRaises(FileNotFoundError) as ctx:
                    module.resolve_existing_path("missing.jsonl", state_path=state_path, repo_root=repo)

        self.assertIn("candidates", str(ctx.exception))
        self.assertIn("missing.jsonl", str(ctx.exception))

    def test_lead_loading_filters_dedupes_records_skips_and_respects_limit(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            run = self.make_sales_nav_run(td, leads=[
                {"name": "Ada Lovelace", "title": "CTO", "company": "Engines", "linkedin_url": "linkedin.com/in/ada?trk=x"},
                {"name": "No Profile", "url": "https://example.com/nope"},
                {"name": "Ada Dup", "linkedin_url": "https://www.linkedin.com/in/ada/"},
                {"name": "Grace Hopper", "linkedin_url": "https://linkedin.com/in/grace"},
            ])
            module.ROOT = Path(td)
            leads = module.load_sales_nav_leads(run / "state.json", 1)
            all_leads = module.load_sales_nav_leads(run / "state.json", None)

        self.assertEqual(len(leads), 1)
        self.assertEqual(len(all_leads), 2)
        self.assertEqual(module.LAST_LEAD_LOAD_SUMMARY["skipped"]["no_linkedin_profile_url"], 1)
        self.assertEqual(module.LAST_LEAD_LOAD_SUMMARY["skipped"]["duplicates"], 1)

    def test_default_sequence_waits_variables_and_name(self):
        module = load_module()
        sequence = module.default_sequence("Pitch AI SDRs", "apollo founders")
        self.assertEqual([step["wait_time"] for step in sequence["steps"]], [0, 3, 7])
        self.assertEqual(len(sequence["steps"]), 3)
        self.assertIn("apollo founders", sequence["name"])
        self.assertTrue(any("{{first_name}}" in step["body_text"] or "{{first_name}}" in step["subject"] for step in sequence["steps"]))

    def test_non_dry_run_build_rejects_missing_sequence_unless_default_copy_allowed(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = module.main(["build", "--instructions", "x", "--state", str(run / "state.json")])
            FakeApolloClient.instances = []
            module.ApolloClient = FakeApolloClient
            with contextlib.redirect_stdout(io.StringIO()):
                ok = module.main(["build", "--instructions", "x", "--state", str(run / "state.json"), "--allow-default-copy", "--out-dir", str(Path(td) / "out")])

        self.assertEqual(code, 1)
        self.assertIn("requires --sequence-json", stdout.getvalue())
        self.assertEqual(ok, 0)

    def test_dry_run_writes_preview_manifest_and_skips_enrichment_and_mutations(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            FakeApolloClient.instances = []
            module.ApolloClient = FakeApolloClient
            out = Path(td) / "out"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = module.main(["build", "--instructions", "x", "--state", str(run / "state.json"), "--dry-run", "--out-dir", str(out)])
            payload = json.loads(stdout.getvalue())
            run_dir = Path(payload["run_dir"])
            has_preview = (run_dir / "sequence_preview.md").exists()
            has_manifest = (run_dir / "manifest.json").exists()
            enrichment_called = json.loads((run_dir / "enrichment_summary.json").read_text())["enrichment_called"]

        self.assertEqual(code, 0)
        self.assertEqual(FakeApolloClient.instances, [])
        self.assertTrue(has_preview)
        self.assertTrue(has_manifest)
        self.assertFalse(enrichment_called)

    def test_dry_run_with_allow_enrichment_calls_only_bulk_enrichment(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            FakeApolloClient.instances = []
            module.ApolloClient = FakeApolloClient
            with contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["build", "--instructions", "x", "--state", str(run / "state.json"), "--dry-run", "--allow-enrichment-in-dry-run", "--out-dir", str(Path(td) / "out")])

        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in FakeApolloClient.instances[0].calls], ["bulk_match"])

    def test_non_dry_run_order_and_add_to_sequence_payload(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            sequence = Path(td) / "sequence.json"
            write_json(sequence, {"name": "Seq", "steps": [{"subject": "Hi {{first_name}}", "body_text": "Hi {{first_name}}"}]})
            FakeApolloClient.instances = []
            module.ApolloClient = FakeApolloClient
            with contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["build", "--instructions", "x", "--state", str(run / "state.json"), "--sequence-json", str(sequence), "--out-dir", str(Path(td) / "out")])
            fake = FakeApolloClient.instances[0]

        self.assertEqual(code, 0)
        self.assertEqual([c[0] for c in fake.calls], [
            "get_email_accounts", "get_emailer_schedules", "bulk_match", "create_campaign", "create_step",
            "patch_template", "search_campaigns", "approve_touch", "search_contacts", "create_contact", "add_contact_ids",
        ])
        self.assertNotIn("approve_campaign", [c[0] for c in fake.calls])
        add = fake.add_payloads[0]
        self.assertEqual(add["url"], "/emailer_campaigns/camp-1/add_contact_ids")
        self.assertEqual(add["body"]["emailer_campaign_id"], "camp-1")
        self.assertEqual(add["body"]["send_email_from_email_account_id"], "sender-1")

    def test_non_dry_run_refuses_empty_campaign_when_no_usable_contacts(self):
        module = load_module()

        class NoUsableContactsFake(FakeApolloClient):
            def bulk_match(self, linkedin_urls):
                self.calls.append(("bulk_match", list(linkedin_urls)))
                return [{"people": [{"linkedin_url": linkedin_urls[0], "email": ""}]}]

        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            sequence = Path(td) / "sequence.json"
            write_json(sequence, {"name": "Seq", "steps": [{"subject": "Hi", "body_text": "Body"}]})
            FakeApolloClient.instances = []
            module.ApolloClient = NoUsableContactsFake
            with contextlib.redirect_stdout(io.StringIO()):
                code = module.main([
                    "build", "--instructions", "x", "--state", str(run / "state.json"),
                    "--sequence-json", str(sequence), "--out-dir", str(Path(td) / "out"),
                ])
            fake = FakeApolloClient.instances[0]
            run_dir = next((Path(td) / "out").iterdir())
            contacts = json.loads((run_dir / "contacts.json").read_text())
            manifest = json.loads((run_dir / "manifest.json").read_text())

        self.assertEqual(code, 1)
        self.assertEqual([c[0] for c in fake.calls], ["get_email_accounts", "get_emailer_schedules", "bulk_match"])
        self.assertEqual(contacts["contact_ids"], [])
        self.assertIsNone(manifest["campaign_id"])
        self.assertFalse((run_dir / "apollo_created.json").exists())

    def test_non_dry_run_persists_partial_manifest_after_campaign_creation_failure(self):
        module = load_module()

        class StepFailureFake(FakeApolloClient):
            def create_step(self, payload):
                self.calls.append(("create_step", payload))
                raise RuntimeError("step creation failed")

        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            sequence = Path(td) / "sequence.json"
            write_json(sequence, {"name": "Seq", "steps": [{"subject": "Hi", "body_text": "Body"}]})
            FakeApolloClient.instances = []
            module.ApolloClient = StepFailureFake
            with contextlib.redirect_stdout(io.StringIO()):
                code = module.main([
                    "build", "--instructions", "x", "--state", str(run / "state.json"),
                    "--sequence-json", str(sequence), "--out-dir", str(Path(td) / "out"),
                ])
            fake = FakeApolloClient.instances[0]
            run_dir = next((Path(td) / "out").iterdir())
            manifest = json.loads((run_dir / "manifest.json").read_text())
            created = json.loads((run_dir / "apollo_created.json").read_text())
            activate_cmd = (run_dir / "activate_command.txt").read_text()

        self.assertEqual(code, 1)
        self.assertIn("create_campaign", [c[0] for c in fake.calls])
        self.assertEqual(manifest["campaign_id"], "camp-1")
        self.assertFalse(manifest["build_complete"])
        self.assertEqual(manifest["build_stage"], "campaign_created")
        self.assertEqual(created["campaign"]["emailer_campaign"]["id"], "camp-1")
        self.assertIn("--confirm-activation camp-1", activate_cmd)

    def test_non_dry_run_fails_partial_build_when_no_contact_ids_returned(self):
        module = load_module()

        class NoContactIdFake(FakeApolloClient):
            def create_contact(self, payload):
                self.calls.append(("create_contact", payload))
                return {"contact": {}}

        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            sequence = Path(td) / "sequence.json"
            write_json(sequence, {"name": "Seq", "steps": [{"subject": "Hi", "body_text": "Body"}]})
            FakeApolloClient.instances = []
            module.ApolloClient = NoContactIdFake
            with contextlib.redirect_stdout(io.StringIO()):
                code = module.main([
                    "build", "--instructions", "x", "--state", str(run / "state.json"),
                    "--sequence-json", str(sequence), "--out-dir", str(Path(td) / "out"),
                ])
            fake = FakeApolloClient.instances[0]
            run_dir = next((Path(td) / "out").iterdir())
            manifest = json.loads((run_dir / "manifest.json").read_text())
            contacts = json.loads((run_dir / "contacts.json").read_text())

        self.assertEqual(code, 1)
        self.assertIn("create_campaign", [c[0] for c in fake.calls])
        self.assertNotIn("add_contact_ids", [c[0] for c in fake.calls])
        self.assertEqual(manifest["campaign_id"], "camp-1")
        self.assertFalse(manifest["build_complete"])
        self.assertEqual(manifest["build_stage"], "no_contact_ids")
        self.assertEqual(contacts["contact_ids"], [])

    def test_active_campaign_blocks_touch_approval_and_active_sequences_not_mutated(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            sequence = Path(td) / "sequence.json"
            write_json(sequence, {"name": "Seq", "steps": [{"subject": "Hi", "body_text": "Body"}]})
            FakeApolloClient.instances = []
            module.ApolloClient = lambda: FakeApolloClient(active_campaign=True)
            with contextlib.redirect_stdout(io.StringIO()):
                code = module.main(["build", "--instructions", "x", "--state", str(run / "state.json"), "--sequence-json", str(sequence), "--out-dir", str(Path(td) / "out")])
            fake = FakeApolloClient.instances[0]

        self.assertEqual(code, 1)
        self.assertEqual([c[0] for c in fake.calls], ["get_email_accounts", "get_emailer_schedules", "bulk_match", "create_campaign"])
        self.assertNotIn("approve_touch", [c[0] for c in fake.calls])

        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            run = self.make_sales_nav_run(td)
            sequence = Path(td) / "sequence.json"
            write_json(sequence, {"name": "Seq", "steps": [{"subject": "Hi", "body_text": "Body"}]})
            FakeApolloClient.instances = []

            class BecomesActiveFake(FakeApolloClient):
                def create_campaign(self, payload):
                    self.calls.append(("create_campaign", payload))
                    return {"emailer_campaign": {"id": "camp-1", "active": False, "status": "draft"}}

                def search_campaigns(self, q_keywords, per_page=25):
                    self.calls.append(("search_campaigns", q_keywords, per_page))
                    return {"emailer_campaigns": [{"id": "camp-1", "active": True, "status": "active"}]}

            module.ApolloClient = BecomesActiveFake
            with contextlib.redirect_stdout(io.StringIO()):
                became_active = module.main(["build", "--instructions", "x", "--state", str(run / "state.json"), "--sequence-json", str(sequence), "--out-dir", str(Path(td) / "out2")])
            fake2 = FakeApolloClient.instances[0]

        self.assertEqual(became_active, 1)
        self.assertIn("search_campaigns", [c[0] for c in fake2.calls])
        self.assertNotIn("approve_touch", [c[0] for c in fake2.calls])

    def test_activation_exact_campaign_id_approve_and_status_fields(self):
        module = load_module()
        messages = [
            {"recipient_email": "ada@example.com", "status": "scheduled"},
            {"recipient_email": "grace@example.com", "status": "delayed"},
            {"recipient_email": "katherine@example.com", "status": "delivered"},
            {"recipient_email": "mary@example.com", "status": "sent"},
        ]
        with tempfile.TemporaryDirectory() as td:
            module.ROOT = Path(td)
            manifest = Path(td) / "manifest.json"
            write_json(manifest, {"primitive": "apollo_build_outbound", "source": module.SOURCE, "campaign_id": "camp-1"})
            write_json(Path(td) / "contacts.json", {"contact_ids": ["contact-1", "contact-2"]})
            module.ApolloClient = lambda: FakeApolloClient(messages=messages)
            with contextlib.redirect_stdout(io.StringIO()):
                wrong = module.main(["activate", "--manifest", str(manifest), "--confirm-activation", "other"])
            FakeApolloClient.instances = []
            with mock.patch.object(module.time, "sleep"):
                with contextlib.redirect_stdout(io.StringIO()):
                    ok = module.main(["activate", "--manifest", str(manifest), "--confirm-activation", "camp-1"])
            status = json.loads((Path(td) / "activation_status.json").read_text())
            fake = FakeApolloClient.instances[0]

        self.assertEqual(wrong, 1)
        self.assertEqual(ok, 0)
        self.assertIn(("approve_campaign", "camp-1", "/approve"), fake.calls)
        self.assertEqual(status["messages_scheduled"], 1)
        self.assertEqual(status["messages_delayed"], 1)
        self.assertEqual(status["messages_sent_or_delivered"], 2)
        self.assertIsNone(status["contacts_active_at_step"])
        self.assertIn("not measured", status["contacts_active_at_step_note"])
        self.assertEqual(status["contacts_enrolled_count"], 2)
        self.assertEqual(status["messages_count"], 4)
        self.assertEqual(status["message_recipient_count"], 4)
        self.assertEqual(status["recipients"][0]["email"], "a***@e***.com")

    def test_stdout_stderr_redacts_api_key_and_masks_emails(self):
        module = load_module()
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"APOLLO_API_KEY": "apollo-secret-key"}):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = module.emit({"ok": False, "api_key": "apollo-secret-key", "error": "APOLLO_API_KEY=apollo-secret-key ada@example.com"}, 1)

        combined = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn("apollo-secret-key", combined)
        self.assertIn("<REDACTED>", combined)
        self.assertIn("a***@e***.com", combined)


if __name__ == "__main__":
    unittest.main()

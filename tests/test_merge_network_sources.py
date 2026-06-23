import csv
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from packs.shared.csv_io import CsvIO

MODULE_PATH = Path(__file__).resolve().parents[1] / "packs/ingestion/primitives/merge_network_sources/merge_network_sources.py"
spec = importlib.util.spec_from_file_location("merge_network_sources", MODULE_PATH)
merge_network_sources = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = merge_network_sources
spec.loader.exec_module(merge_network_sources)


class MergeNetworkSourcesTests(unittest.TestCase):
    def invoke(self, argv):
        buf = StringIO()
        with redirect_stdout(buf):
            code = merge_network_sources.main(argv)
        payload = json.loads(buf.getvalue()) if buf.getvalue().strip() else {}
        return code, payload

    def write_people(self, path: Path, name: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = merge_network_sources.PEOPLE_SCHEMA_COLUMNS
        row = {col: "" for col in fields}
        row.update({
            "id": f"id-{name}",
            "public_identifier": "jane-example",
            "linkedin_url": "https://www.linkedin.com/in/jane-example",
            "full_name": name,
            "current_company": "Acme AI",
            "rapidapi_response": json.dumps({"full_name": name, "experiences": [{"title": "CEO", "company": "Acme AI"}]}),
            "source_channels": path.parent.parent.name,
        })
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(row)

    def write_people_row(self, path: Path, row: dict[str, str]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fields = merge_network_sources.PEOPLE_SCHEMA_COLUMNS
        out = {col: "" for col in fields}
        out["rapidapi_response"] = json.dumps({"full_name": row.get("full_name") or "Test Person", "experiences": [{"title": "Operator"}]})
        out.update(row)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerow(out)

    def test_overrides_detach_drops_person_and_verify_annotates(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                bad = Path(".powerpacks/network-import/gmail/run-1/bad.csv")
                good = Path(".powerpacks/network-import/gmail/run-1/good.csv")
                self.write_people_row(bad, {"id": "id-bad", "public_identifier": "bobceo",
                    "linkedin_url": "https://www.linkedin.com/in/bobceo", "full_name": "Bob Plumber",
                    "primary_email": "bob@x.com", "source_channels": "gmail_msgvault"})
                self.write_people_row(good, {"id": "id-good", "public_identifier": "chrissyhu",
                    "linkedin_url": "https://www.linkedin.com/in/chrissyhu", "full_name": "Chrissy Hu",
                    "primary_email": "chrissy@x.com", "source_channels": "gmail_msgvault"})
                overrides = Path(".powerpacks/network-import/overrides/linkedin-reconcile.csv")
                overrides.parent.mkdir(parents=True, exist_ok=True)
                with overrides.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=["public_identifier", "action", "approved",
                        "linkedin_url", "match_emails", "match_phones", "confidence", "reason"])
                    w.writeheader()
                    w.writerow({"public_identifier": "bobceo", "action": "detach", "approved": "auto",
                                "match_emails": "bob@x.com", "confidence": "0.98", "reason": "CEO != plumber"})
                    w.writerow({"public_identifier": "chrissyhu", "action": "verify", "approved": "auto",
                                "match_emails": "", "confidence": "0.92", "reason": "JPM IB match"})

                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--output-dir", str(out_dir),
                    "--input", str(bad), "--input", str(good), "--overrides", str(overrides),
                    "--retarget-people", ""])
                self.assertEqual(code, 0)
                self.assertEqual(payload["overrides_detached"], 1)
                self.assertEqual(payload["overrides_verified"], 1)
                with (out_dir / "people.csv").open() as fh:
                    rows = {r["full_name"]: r for r in csv.DictReader(fh)}
                self.assertNotIn("Bob Plumber", rows)                     # detached -> dropped
                self.assertIn("Chrissy Hu", rows)
                self.assertEqual(rows["Chrissy Hu"]["linkedin_verified"], "confirmed")
                self.assertEqual(rows["Chrissy Hu"]["linkedin_verified_confidence"], "0.92")
            finally:
                os.chdir(old_cwd)

    def test_override_scope_mismatch_is_ignored(self):
        # An override scoped to a different email must NOT detach a same-public_id row.
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                p = Path(".powerpacks/network-import/gmail/run-1/p.csv")
                self.write_people_row(p, {"id": "id-1", "public_identifier": "jane-example",
                    "linkedin_url": "https://www.linkedin.com/in/jane-example", "full_name": "Jane Right",
                    "primary_email": "jane@x.com", "source_channels": "gmail_msgvault"})
                overrides = Path(tmp) / "ov.csv"
                with overrides.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=["public_identifier", "action", "approved", "match_emails", "match_phones"])
                    w.writeheader()
                    w.writerow({"public_identifier": "jane-example", "action": "detach", "approved": "auto",
                                "match_emails": "someone-else@x.com", "match_phones": ""})
                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--output-dir", str(out_dir),
                    "--input", str(p), "--overrides", str(overrides), "--retarget-people", ""])
                self.assertEqual(payload["overrides_detached"], 0)       # scope mismatch -> ignored
                with (out_dir / "people.csv").open() as fh:
                    names = [r["full_name"] for r in csv.DictReader(fh)]
                self.assertIn("Jane Right", names)
            finally:
                os.chdir(old_cwd)

    def test_override_pending_is_not_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                p = Path(".powerpacks/network-import/gmail/run-1/p.csv")
                self.write_people_row(p, {"id": "id-1", "public_identifier": "bobceo",
                    "linkedin_url": "https://www.linkedin.com/in/bobceo", "full_name": "Bob",
                    "source_channels": "gmail_msgvault"})
                overrides = Path(tmp) / "ov.csv"
                with overrides.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=["public_identifier", "action", "approved"])
                    w.writeheader()
                    w.writerow({"public_identifier": "bobceo", "action": "detach", "approved": ""})  # pending
                out_dir = Path(tmp) / "merged"
                _, payload = self.invoke(["run", "--output-dir", str(out_dir),
                    "--input", str(p), "--overrides", str(overrides), "--retarget-people", ""])
                self.assertEqual(payload["overrides_detached"], 0)   # pending -> not applied
                with (out_dir / "people.csv").open() as fh:
                    self.assertIn("Bob", [r["full_name"] for r in csv.DictReader(fh)])
            finally:
                os.chdir(old_cwd)

    def test_retarget_drops_old_and_ingests_enriched_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                # Old wrong-link row for the contact.
                old = Path(".powerpacks/network-import/gmail/run-1/old.csv")
                self.write_people_row(old, {"id": "id-old", "public_identifier": "bobceo",
                    "linkedin_url": "https://www.linkedin.com/in/bobceo", "full_name": "Bob Wrong",
                    "primary_email": "bob@x.com", "source_channels": "gmail_msgvault"})
                # Enriched re-attach row (what apply-retargets would produce) for the correct link.
                retarget_people = Path(".powerpacks/network-import/overrides/retarget-people.csv")
                self.write_people_row(retarget_people, {"id": "id-new", "public_identifier": "bob-real",
                    "linkedin_url": "https://www.linkedin.com/in/bob-real", "full_name": "Bob Right",
                    "primary_email": "bob@x.com", "source_channels": "gmail_msgvault"})
                overrides = Path(".powerpacks/network-import/overrides/linkedin-reconcile.csv")
                with overrides.open("w", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=["public_identifier", "action", "approved",
                        "new_linkedin_url", "match_emails"])
                    w.writeheader()
                    w.writerow({"public_identifier": "bobceo", "action": "retarget", "approved": "yes",
                                "new_linkedin_url": "https://www.linkedin.com/in/bob-real", "match_emails": "bob@x.com"})
                out_dir = Path(".powerpacks/network-import/merged")  # so overrides/ sibling resolves
                _, payload = self.invoke(["run", "--output-dir", str(out_dir), "--input", str(old)])
                self.assertEqual(payload["overrides_retargeted"], 1)
                with (out_dir / "people.csv").open() as fh:
                    rows = {r["public_identifier"]: r for r in csv.DictReader(fh)}
                self.assertNotIn("bobceo", rows)     # old wrong link dropped
                self.assertIn("bob-real", rows)       # correct enriched row kept (auto-ingested)
                self.assertEqual(rows["bob-real"]["full_name"], "Bob Right")
            finally:
                os.chdir(old_cwd)

    def test_consolidation_row_folds_emails_without_polluting_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                # Real kept row (correct LinkedIn + profile) + a sibling being detached.
                src = Path(".powerpacks/network-import/gmail/run-1/src.csv")
                self.write_people_row(src, {"id": "id-keep", "public_identifier": "chrissyhu",
                    "linkedin_url": "https://www.linkedin.com/in/chrissyhu", "full_name": "Chrissy Hu",
                    "headline": "Director of Investments", "primary_email": "c@gmail.com",
                    "source_channels": "gmail_msgvault"})
                self.write_people_row(src.with_name("sib.csv"), {"id": "id-sib", "public_identifier": "chrissy-hu",
                    "linkedin_url": "https://www.linkedin.com/in/chrissy-hu", "full_name": "Chrissy Hu",
                    "headline": "Autonomous Fleet @ Nvidia", "primary_email": "c@jpmorgan.com",
                    "source_channels": "gmail_msgvault"})
                overrides = Path(".powerpacks/network-import/overrides/linkedin-reconcile.csv")
                overrides.parent.mkdir(parents=True, exist_ok=True)
                with overrides.open("w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=["public_identifier", "action", "approved"])
                    w.writeheader()
                    w.writerow({"public_identifier": "chrissy-hu", "action": "detach", "approved": "auto"})
                # Consolidation row: CONTACT-ONLY (no profile/rapidapi), keyed by the kept LinkedIn.
                consol = Path(".powerpacks/network-import/overrides/consolidate-people.csv")
                cols = merge_network_sources.PEOPLE_SCHEMA_COLUMNS
                crow = {c: "" for c in cols}
                crow.update({"public_identifier": "chrissyhu",
                             "linkedin_url": "https://www.linkedin.com/in/chrissyhu",
                             "primary_email": "c@jpmorgan.com",
                             "all_emails": '["c@gmail.com", "c@jpmorgan.com"]'})
                with consol.open("w", newline="") as fh:
                    w = csv.DictWriter(fh, fieldnames=cols)
                    w.writeheader(); w.writerow(crow)

                out_dir = Path(".powerpacks/network-import/merged")  # so overrides/ sibling resolves
                _, payload = self.invoke(["run", "--output-dir", str(out_dir),
                    "--input", str(src), "--input", str(src.with_name("sib.csv"))])
                with (out_dir / "people.csv").open() as fh:
                    merged = {r["public_identifier"]: r for r in csv.DictReader(fh)}
                self.assertNotIn("chrissy-hu", merged)                       # sibling detached -> dropped
                self.assertIn("c@jpmorgan.com", merged["chrissyhu"]["all_emails"])  # sibling email folded in
                self.assertIn("c@gmail.com", merged["chrissyhu"]["all_emails"])
                self.assertEqual(merged["chrissyhu"]["headline"], "Director of Investments")  # profile NOT polluted
            finally:
                os.chdir(old_cwd)

    def test_large_profile_payload_fields_do_not_break_csv_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                people = Path(".powerpacks/network-import/gmail/run-large/people.csv")
                people.parent.mkdir(parents=True, exist_ok=True)
                fields = merge_network_sources.PEOPLE_SCHEMA_COLUMNS
                row = {col: "" for col in fields}
                row.update({
                    "id": "gmail:large-payload",
                    "public_identifier": "large-payload",
                    "linkedin_url": "https://www.linkedin.com/in/large-payload",
                    "full_name": "Large Payload",
                    "source_channels": "gmail_msgvault",
                    "rapidapi_response": json.dumps({
                        "full_name": "Large Payload",
                        "summary": "x" * 200_000,
                        "experiences": [{"title": "Founder", "company": "Big Field Co"}],
                    }),
                })
                with people.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=fields)
                    writer.writeheader()
                    writer.writerow(row)

                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke([
                    "run",
                    "--output-dir", str(out_dir),
                    "--input", str(people),
                ])

                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 1)
                self.assertEqual(payload["merged_rows"], 1)
            finally:
                os.chdir(old_cwd)

    def test_repeated_merge_flattens_and_caps_source_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                people = Path(".powerpacks/network-import/gmail/run-1/people.csv")
                self.write_people_row(people, {
                    "id": "gmail:jane",
                    "public_identifier": "jane-repeat",
                    "linkedin_url": "https://www.linkedin.com/in/jane-repeat",
                    "full_name": "Jane Repeat",
                    "source_channels": "gmail_msgvault",
                    "source_artifacts": json.dumps(["gmail/source-a.csv", "gmail/source-b.csv"]),
                })
                first_out = Path(tmp) / "merged-first"
                code, first = self.invoke([
                    "run",
                    "--output-dir", str(first_out),
                    "--input", str(people),
                ])
                self.assertEqual(code, 0)
                first_people = Path(first["people_csv"])
                second_out = Path(tmp) / "merged-second"
                code, second = self.invoke([
                    "run",
                    "--output-dir", str(second_out),
                    "--input", str(first_people),
                    "--input", str(people),
                ])
                self.assertEqual(code, 0)
                with Path(second["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                artifacts = json.loads(rows[0]["source_artifacts"])
                self.assertIn("gmail/source-a.csv", artifacts)
                self.assertIn("gmail/source-b.csv", artifacts)
                self.assertLess(len(rows[0]["source_artifacts"]), 1000)
            finally:
                os.chdir(old_cwd)

    def test_explicit_input_writes_canonical_merge_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                run_dir = Path(".powerpacks/network-import/linkedin/run-1")
                self.write_people(run_dir / "people.csv", "Jane Canonical")
                self.write_people(run_dir / "people_harmonic_all.csv", "Jane Legacy")
                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--output-dir", str(out_dir), "--input", str(run_dir / "people.csv")])
                self.assertEqual(code, 0)
                self.assertEqual(Path(payload["people_csv"]).name, "people.csv")
                self.assertTrue(Path(payload["people_csv"]).exists())
                self.assertTrue(Path(payload["legacy_output"]).exists())
                self.assertTrue(Path(payload["network_contacts_csv"]).exists())
                self.assertTrue(Path(payload["network_contact_sources_csv"]).exists())
                self.assertTrue(Path(payload["network_companies_csv"]).exists())
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["full_name"], "Jane Canonical")
                with Path(payload["network_contacts_csv"]).open(newline="", encoding="utf-8") as handle:
                    contacts = list(CsvIO.dict_reader(handle))
                self.assertEqual(contacts[0]["source_channels"], "linkedin")
                with Path(payload["network_contact_sources_csv"]).open(newline="", encoding="utf-8") as handle:
                    sources = list(CsvIO.dict_reader(handle))
                self.assertEqual(sources[0]["source_channel"], "linkedin")
                self.assertEqual(sources[0]["source_identifier"], "https://www.linkedin.com/in/jane-example")
                with Path(payload["network_companies_csv"]).open(newline="", encoding="utf-8") as handle:
                    companies = list(CsvIO.dict_reader(handle))
                self.assertEqual(companies[0]["company_name"], "Acme AI")
                self.assertEqual(companies[0]["contact_count"], "1")
            finally:
                os.chdir(old_cwd)

    def test_default_ignores_filesystem_candidates_without_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                self.write_people(Path(".powerpacks/network-import/linkedin/old-run/people.csv"), "Jane Old")
                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--output-dir", str(out_dir)])
                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 0)
                self.assertEqual(payload["merged_rows"], 0)
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    self.assertEqual(list(CsvIO.dict_reader(handle)), [])
            finally:
                os.chdir(old_cwd)

    def test_default_run_does_not_discover_filesystem_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                self.write_people(Path(".powerpacks/network-import/linkedin/old-run/people.csv"), "Jane Old")
                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--output-dir", str(out_dir)])
                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 0)
                self.assertEqual(payload["merged_rows"], 0)
            finally:
                os.chdir(old_cwd)

    def test_default_skips_unreviewed_messages_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                contacts = Path(".powerpacks/messages/contacts.csv")
                contacts.parent.mkdir(parents=True, exist_ok=True)
                contacts.write_text("name,phone,source,message_count,last_message\nJane,+15551234567,imessage,3,2026-01-01\n", encoding="utf-8")
                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke(["run", "--output-dir", str(out_dir)])
                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 0)
                self.assertEqual(payload["merged_rows"], 0)
            finally:
                os.chdir(old_cwd)

    def test_same_linkedin_rows_union_email_aliases(self):
        """Two source rows with the same LinkedIn identity but different emails
        (personal alias vs work address) must union both into all_emails, not
        first-wins. Repro shape: dropped work aliases caused interaction-count
        undercounts because the count join only sees merged all_emails."""
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                gmail_alias = Path(".powerpacks/network-import/gmail/run-a/people.csv")
                work_email = Path(".powerpacks/network-import/gmail/run-b/people.csv")
                base = {
                    "public_identifier": "alex-doe",
                    "linkedin_url": "https://www.linkedin.com/in/alex-doe",
                    "full_name": "Alex Doe",
                }
                self.write_people_row(gmail_alias, {
                    **base,
                    "id": "gmail:alias",
                    "primary_email": "alexdoe@example.com",
                    "all_emails": json.dumps(["alexdoe@example.com"]),
                    "source_channels": "gmail_msgvault",
                })
                self.write_people_row(work_email, {
                    **base,
                    "id": "gmail:work",
                    "primary_email": "alex@work-firm.example",
                    "all_emails": json.dumps(["alex@work-firm.example"]),
                    "source_channels": "gmail_msgvault",
                })

                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke([
                    "run",
                    "--output-dir", str(out_dir),
                    "--input", str(gmail_alias),
                    "--input", str(work_email),
                ])

                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 2)
                self.assertEqual(payload["unfiltered_merged_rows"], 1)
                self.assertEqual(payload["merged_rows"], 1)
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(len(rows), 1)
                emails = set(json.loads(rows[0]["all_emails"]))
                self.assertEqual(emails, {"alexdoe@example.com", "alex@work-firm.example"})
                self.assertIn(rows[0]["primary_email"].lower(), emails)
            finally:
                os.chdir(old_cwd)

    def test_non_linkedin_email_identity_ignores_run_specific_artifact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                old_people = Path(".powerpacks/network-import/gmail/setup-refresh-old/people.csv")
                new_people = Path(".powerpacks/network-import/gmail/setup-refresh-new/people.csv")
                base = {
                    "id": "gmail:stable-jane",
                    "full_name": "Jane Email",
                    "primary_email": "Jane@Example.com",
                    "all_emails": json.dumps(["jane@example.com"]),
                    "source_channels": "gmail_msgvault",
                }
                self.write_people_row(old_people, {**base, "source_artifacts": json.dumps([".powerpacks/network-import/gmail/setup-refresh-old/source.csv"])})
                self.write_people_row(new_people, {**base, "source_artifacts": json.dumps([".powerpacks/network-import/gmail/setup-refresh-new/source.csv"])})

                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke([
                    "run",
                    "--output-dir", str(out_dir),
                    "--input", str(old_people),
                    "--input", str(new_people),
                ])

                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 2)
                self.assertEqual(payload["unfiltered_merged_rows"], 1)
                self.assertEqual(payload["filtered_without_linkedin"], 1)
                self.assertEqual(payload["filtered_people_csv_rows"], 1)
                self.assertEqual(payload["merged_rows"], 0)
                self.assertEqual(payload["filtered_without_rapidapi_payload"], 0)
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(rows, [])
            finally:
                os.chdir(old_cwd)

    def test_non_linkedin_phone_identity_ignores_run_specific_artifact_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                old_people = Path(".powerpacks/network-import/messages/setup-refresh-old/people.csv")
                new_people = Path(".powerpacks/network-import/messages/setup-refresh-new/people.csv")
                base = {
                    "id": "message:stable-jane",
                    "full_name": "Jane Phone",
                    "primary_phone": "+1 (555) 123-4567",
                    "all_phones": json.dumps(["+15551234567"]),
                    "source_channels": "imessage",
                }
                self.write_people_row(old_people, {**base, "source_artifacts": ".powerpacks/network-import/network-runs/setup-refresh-old/source-inputs/messages/contacts.csv"})
                self.write_people_row(new_people, {**base, "source_artifacts": ".powerpacks/network-import/network-runs/setup-refresh-new/source-inputs/messages/contacts.csv"})

                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke([
                    "run",
                    "--output-dir", str(out_dir),
                    "--input", str(old_people),
                    "--input", str(new_people),
                ])

                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 2)
                self.assertEqual(payload["unfiltered_merged_rows"], 1)
                self.assertEqual(payload["filtered_without_linkedin"], 1)
                self.assertEqual(payload["filtered_people_csv_rows"], 1)
                self.assertEqual(payload["merged_rows"], 0)
                self.assertEqual(payload["filtered_without_rapidapi_payload"], 0)
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(rows, [])
            finally:
                os.chdir(old_cwd)

    def test_filters_rows_without_usable_rapidapi_payload_from_people_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = Path.cwd()
            os.chdir(tmp)
            try:
                enriched = Path(".powerpacks/network-import/linkedin/run-1/people.csv")
                contact_only = Path(".powerpacks/network-import/gmail/run-1/people.csv")
                rapidapi_without_linkedin = Path(".powerpacks/network-import/gmail/run-2/people.csv")
                failed = Path(".powerpacks/network-import/linkedin/run-2/people.csv")
                self.write_people_row(enriched, {
                    "id": "linkedin:keep",
                    "public_identifier": "keep-example",
                    "linkedin_url": "https://www.linkedin.com/in/keep-example",
                    "full_name": "Keep Example",
                    "source_channels": "linkedin_csv",
                })
                self.write_people_row(contact_only, {
                    "id": "gmail:drop",
                    "full_name": "Drop Contact",
                    "primary_email": "drop@example.com",
                    "source_channels": "gmail_msgvault",
                    "rapidapi_response": "",
                })
                self.write_people_row(rapidapi_without_linkedin, {
                    "id": "gmail:rapid-no-linkedin",
                    "full_name": "Drop Rapid No LinkedIn",
                    "primary_email": "rapid-no-linkedin@example.com",
                    "source_channels": "gmail_msgvault",
                })
                self.write_people_row(failed, {
                    "id": "linkedin:failed",
                    "public_identifier": "failed-example",
                    "linkedin_url": "https://www.linkedin.com/in/failed-example",
                    "full_name": "Failed Example",
                    "source_channels": "linkedin_csv",
                    "rapidapi_response": json.dumps({"success": False, "message": "not found"}),
                })

                out_dir = Path(tmp) / "merged"
                code, payload = self.invoke([
                    "run",
                    "--output-dir", str(out_dir),
                    "--input", str(enriched),
                    "--input", str(contact_only),
                    "--input", str(rapidapi_without_linkedin),
                    "--input", str(failed),
                ])

                self.assertEqual(code, 0)
                self.assertEqual(payload["input_rows"], 4)
                self.assertEqual(payload["unfiltered_merged_rows"], 4)
                self.assertEqual(payload["filtered_without_rapidapi_payload"], 2)
                self.assertEqual(payload["filtered_without_linkedin"], 2)
                self.assertEqual(payload["filtered_people_csv_rows"], 3)
                self.assertEqual(payload["rapidapi_payload_rows"], 1)
                self.assertEqual(payload["merged_rows"], 1)
                with Path(payload["people_csv"]).open(newline="", encoding="utf-8") as handle:
                    rows = list(CsvIO.dict_reader(handle))
                self.assertEqual([row["full_name"] for row in rows], ["Keep Example"])
                with Path(payload["network_contact_sources_csv"]).open(newline="", encoding="utf-8") as handle:
                    source_rows = list(CsvIO.dict_reader(handle))
                self.assertEqual(len(source_rows), 1)
                self.assertEqual(source_rows[0]["merge_key"], "linkedin:keep-example")
            finally:
                os.chdir(old_cwd)


if __name__ == "__main__":
    unittest.main()

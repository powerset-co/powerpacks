from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from packs.ingestion.primitives.discover.common import read_csv_rows, write_csv_rows
from packs.ingestion.primitives.discover.messages import discover, extract_whatsapp
from packs.ingestion.primitives.discover.messages.merge_contacts import ContactsMerger
from packs.ingestion.primitives.discover.messages.wacli import binary, sync
from packs.ingestion.primitives.imports.messages.importer import MessagesImport
from packs.ingestion.schemas.message_contacts import CSV_HEADERS


PHONE = "+15550100123"
JID = "15550100123@s.whatsapp.net"


def make_store(path: Path, *, push_name: str, full_name: str, chat_name: str = "") -> Path:
    path.mkdir()
    with sqlite3.connect(path / "wacli.db") as conn:
        conn.executescript(
            "CREATE TABLE contacts (jid TEXT PRIMARY KEY, phone TEXT, push_name TEXT, full_name TEXT);"
            "CREATE TABLE chats (jid TEXT PRIMARY KEY, kind TEXT, name TEXT, last_message_ts INTEGER);"
            "CREATE TABLE messages (chat_jid TEXT, ts INTEGER, text TEXT);"
        )
        conn.execute("INSERT INTO contacts VALUES (?,?,?,?)", (JID, PHONE, push_name, full_name))
        conn.execute("INSERT INTO chats VALUES (?,?,?,?)", (JID, "dm", chat_name, 1))
        conn.execute("INSERT INTO messages VALUES (?,?,?)", (JID, 1, "SYNTHETIC BODY"))
    return path


class MessageNameConflictTests(unittest.TestCase):
    def test_whatsapp_conflicting_fields_retain_contact_and_name_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = make_store(Path(td) / "store", push_name="Casey", full_name="Jordan Bravo")
            before = hashlib.sha256((store / "wacli.db").read_bytes()).hexdigest()
            contacts, diagnostics = extract_whatsapp.export_contacts_from_store(store)
            self.assertEqual(contacts[PHONE].name, "")
            self.assertEqual(contacts[PHONE].message_count, 1)
            self.assertEqual(diagnostics["contacts_with_conflicting_names"], 1)
            self.assertEqual(diagnostics["conflicting_names"], [{"phone": PHONE, "names": ["Jordan Bravo", "Casey"]}])
            self.assertEqual(hashlib.sha256((store / "wacli.db").read_bytes()).hexdigest(), before)

    def test_compatible_saved_names_keep_full_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = make_store(Path(td) / "store", push_name="Jordan", full_name="Jordan Bravo")
            contacts, diagnostics = extract_whatsapp.export_contacts_from_store(store)
            self.assertEqual(contacts[PHONE].name, "Jordan Bravo")
            self.assertEqual(diagnostics["contacts_with_conflicting_names"], 0)

    def test_single_invalid_saved_label_is_not_a_source_name_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = make_store(root / "store", push_name=PHONE, full_name="")
            contacts, diagnostics = extract_whatsapp.export_contacts_from_store(store)
            self.assertEqual(contacts[PHONE].name, PHONE)
            self.assertEqual(diagnostics["contacts_with_conflicting_names"], 0)
            source = root / "whatsapp.csv"
            extract_whatsapp.write_csv(source, contacts)
            result = ContactsMerger().merge(inputs=[source], output=root / "contacts.csv")
            self.assertEqual(read_csv_rows(root / "contacts.csv")[1][0]["name"], PHONE)
            self.assertEqual(result["counts"]["contacts_with_conflicting_names"], 0)

    def test_chat_name_cannot_hide_conflicting_contact_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = make_store(Path(td) / "store", push_name="Casey", full_name="Jordan Bravo", chat_name="Jordan Bravo")
            contacts, _ = extract_whatsapp.export_contacts_from_store(store)
            self.assertEqual(contacts[PHONE].name, "")

    def test_group_cache_and_fallback_names_cannot_hide_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = make_store(root / "store", push_name="Jordan", full_name="Jordan Bravo")
            sync.group_participants_cache_path(store).write_text(json.dumps({"groups": {
                "synthetic@g.us": {"name": "Synthetic Group", "participants": [{"phone": PHONE, "name": "Casey Delta"}]},
            }}))
            fallback = root / "fallback.csv"
            write_csv_rows(fallback, ["phone", "name"], [{"phone": PHONE, "name": "Jordan Bravo"}])
            contacts, diagnostics = extract_whatsapp.export_contacts_from_store(store, name_fallback_csv=fallback)
            self.assertEqual(contacts[PHONE].name, "")
            self.assertTrue(contacts[PHONE].is_in_group_chats)
            self.assertIn("Casey Delta", diagnostics["conflicting_names"][0]["names"])

    def test_different_jids_for_one_phone_retain_all_names(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = make_store(Path(td) / "store", push_name="Jordan", full_name="Jordan Bravo")
            with sqlite3.connect(store / "wacli.db") as conn:
                conn.execute("INSERT INTO contacts VALUES (?,?,?,?)", ("synthetic@lid", PHONE, "Casey", "Casey Delta"))
            contacts, diagnostics = extract_whatsapp.export_contacts_from_store(store)
            self.assertEqual(contacts[PHONE].name, "")
            self.assertIn("Casey Delta", diagnostics["conflicting_names"][0]["names"])

    def test_cross_channel_conflict_survives_a_later_compatible_row(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            inputs = []
            for index, name in enumerate(("Jordan Bravo", "Casey Delta", "Jordan Bravo")):
                path = root / f"channel{index}.csv"
                write_csv_rows(path, CSV_HEADERS, [{"phone": PHONE, "name": name, "source": "imessage", "message_count": "1"}])
                inputs.append(path)
            result = ContactsMerger().merge(inputs=inputs, output=root / "contacts.csv")
            self.assertEqual(read_csv_rows(root / "contacts.csv")[1][0]["name"], "")
            self.assertEqual(result["counts"]["contacts_with_conflicting_names"], 1)

    def test_missing_name_does_not_quarantine_a_known_name(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.csv"
            write_csv_rows(source, CSV_HEADERS, [{"phone": PHONE, "name": ""}, {"phone": PHONE, "name": "Jordan Bravo"}])
            result = ContactsMerger().merge(inputs=[source], output=root / "contacts.csv")
            self.assertEqual(read_csv_rows(root / "contacts.csv")[1][0]["name"], "Jordan Bravo")
            self.assertEqual(result["counts"]["contacts_with_conflicting_names"], 0)

    def test_extractor_merger_import_hold_known_conflict_across_channels(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            store = make_store(root / "store", push_name="Casey", full_name="Jordan Bravo")
            whatsapp = root / "whatsapp.contacts.csv"
            manifest = whatsapp.with_suffix(".csv.manifest.json")
            with mock.patch.object(binary, "wacli_version", return_value={}), redirect_stdout(io.StringIO()):
                code = extract_whatsapp.main([
                    "export", "--store", str(store), "--output-csv", str(whatsapp),
                    "--output-jsonl", str(root / "whatsapp.jsonl"), "--manifest", str(manifest),
                ])
            self.assertEqual(code, 0)
            imessage = root / "imessage.contacts.csv"
            write_csv_rows(imessage, CSV_HEADERS, [{"phone": PHONE, "name": "Jordan Bravo", "source": "imessage", "message_count": "2"}])
            output = root / "contacts.csv"
            result = ContactsMerger().merge(inputs=[imessage, whatsapp], output=output, source_manifests=[manifest])
            row = read_csv_rows(output)[1][0]
            self.assertEqual(row["name"], "")
            self.assertEqual(row["message_count"], "3")
            self.assertEqual(result["counts"]["rows_written"], 1)
            self.assertEqual(result["counts"]["contacts_with_conflicting_names"], 1)
            importer = MessagesImport(contacts_csv=output, import_dir=root / "imports")
            importer.run()
            self.assertEqual(importer.written["stats"]["people"], 0)
            self.assertEqual(importer.written["skipped"], {"no_name": 1})
            self.assertEqual(read_csv_rows(whatsapp)[1][0]["phone"], PHONE)

    def test_source_manifest_must_belong_to_an_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.csv"
            write_csv_rows(source, CSV_HEADERS, [{"phone": PHONE, "name": "Jordan Bravo"}])
            manifest = root / "unrelated.manifest.json"
            manifest.write_text(json.dumps({"artifacts": {"csv": str(root / "other.csv")}, "diagnostics": {
                "conflicting_names": [{"phone": PHONE, "names": ["Jordan Bravo", "Casey Delta"]}],
            }}))
            with self.assertRaisesRegex(ValueError, "not a merge input"):
                ContactsMerger().merge(inputs=[source], output=root / "contacts.csv", source_manifests=[manifest])

    def test_discovery_passes_manifests_for_every_input_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            stage = discover.MessagesDiscovery(out_dir=root, include_imessage=True)
            for channel in stage.channels:
                channel.contacts_csv = root / f"{channel.channel}.csv"
                channel.extract_manifest = root / f"{channel.channel}.manifest.json"
                write_csv_rows(channel.contacts_csv, CSV_HEADERS, [])
                channel.extract_manifest.write_text("{}")
            with mock.patch.object(ContactsMerger, "merge", return_value={"status": "ok"}) as merge:
                stage._merge()
            self.assertEqual(merge.call_args.kwargs["source_manifests"], [channel.extract_manifest for channel in stage.channels])


if __name__ == "__main__":
    unittest.main()

"""Lookup resolves contacts to canonical parents without changing SQLite."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from packs.ingestion.primitives.deep_context.db.models import (
    ArtifactRow, LinkRow, ParentRow, PersonIdentifierRow, PersonIdentifiersProjection, PersonRow,
)
from packs.ingestion.primitives.deep_context.db.store import Db
from packs.ingestion.primitives.deep_context.shared.lookup_person import PersonLookup, main
from packs.ingestion.primitives.pipeline.contract import PeopleRow


class PersonLookupSqliteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Db(self.root / "deep-context.sqlite")
        self.add_person("a", "Jordan Bravo", "Jordan A. Bravo")
        self.db.project_rows((
            PersonRow("person-alias", "parent-a", "jordan-alias", "jordan-a", "J. Bravo"),
            PersonIdentifiersProjection("person-alias", (
                PersonIdentifierRow("person-alias", "email", "alias@example.com", "alias@example.com"),
            )),
            ArtifactRow("dossier-person:person-a", "dossier", "parent-a", "/unused/child.md",
                        "child", "projected", person_id="person-a",
                        payload_json=json.dumps({"body": "CHILD BODY"})),
        ))

    def add_person(self, key: str, name: str, child_name: str | None = None,
                   *, dossier: bool = True) -> None:
        parent_id, person_id = f"parent-{key}", f"person-{key}"
        rows = [
            ParentRow(parent_id, f"parent-worth:{parent_id}", name, f"jordan-{key}"),
            PersonRow(person_id, parent_id, f"child-{key}", f"jordan-{key}", child_name or name),
            PersonIdentifiersProjection(person_id, (
                PersonIdentifierRow(person_id, "email", f"{key}@example.com", f"{key.upper()}@Example.com"),
                PersonIdentifierRow(person_id, "phone", "+14155550100" if key == "a" else "+14155550101"),
            )),
        ]
        if dossier:
            rows.append(ArtifactRow(
                f"dossier:{parent_id}", "dossier", parent_id, str(self.root / f"{key}.md"),
                key, "projected", payload_json=json.dumps({"body": f"# {name}\nPARENT {key}\n",
                                                         "headline": f"Engineer {key}"}),
            ))
        self.db.project_rows(tuple(rows))
        imported = tuple(PeopleRow(id=f"person-{item}", full_name=display,
                                  linkedin_url=f"https://www.linkedin.com/in/jordan-{item}/")
                         for item, display in (("a", "Jordan A. Bravo"), (key, child_name or name)))
        self.db.replace_imported_people(tuple({row.id: row for row in imported}.values()))

    def cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(["--db", str(self.db.db_path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_identifiers_and_child_alias_resolve_one_parent(self) -> None:
        for query in ({"email": "A@EXAMPLE.COM"}, {"email": "alias@example.com"},
                      {"name": "J. Bravo"}, {"name": "Jordan A. Bravo"},
                      {"name": "Jordan Bravo"}, {"name": "Jordan"}):
            with self.subTest(query=query):
                result = PersonLookup(db=self.db.db_path, **query).run()
                self.assertEqual(result.status, "found")
                self.assertEqual(len(result.matches), 1)
                match = result.matches[0]
                self.assertEqual(match.parent_id, "parent-a")
                self.assertEqual(match.name, "Jordan Bravo")
                self.assertEqual(match.dossier_body, "# Jordan Bravo\nPARENT a\n\n\nCHILD BODY")
                self.assertEqual(set(match.emails), {"A@Example.com", "alias@example.com"})
                self.assertEqual(match.linkedin_urls, ("https://www.linkedin.com/in/jordan-a",))

    def test_phone_formats_keep_digit_matching(self) -> None:
        for phone in ("415-555-0100", "(415) 555-0100", "4155550100", "+14155550100"):
            with self.subTest(phone=phone):
                result = PersonLookup(db=self.db.db_path, phone=phone).run()
                self.assertEqual(result.status, "found")
                self.assertEqual([m.parent_id for m in result.matches], ["parent-a"])

    def test_lookup_reads_parent_and_all_children_from_sqlite(self) -> None:
        self.db.project_rows((
            ArtifactRow("dossier-parent:parent-a", "dossier", "parent-a", "/missing/parent.md",
                        "stub", "projected", payload_json=json.dumps({
                            "body": "Single identity. Full context in [[child-a]]."})),
            ArtifactRow("dossier-person:person-alias", "dossier", "parent-a", "/missing/alias.md",
                        "alias", "projected", person_id="person-alias",
                        payload_json=json.dumps({"body": "ALIAS RELATIONSHIP CONTEXT"})),
        ))
        result = PersonLookup(db=self.db.db_path, name="J. Bravo").run()
        body = result.matches[0].dossier_body
        for text in ("PARENT a", "Full context in [[child-a]]", "CHILD BODY",
                     "ALIAS RELATIONSHIP CONTEXT"):
            self.assertIn(text, body)
        self.assertLess(body.index("PARENT a"), body.index("CHILD BODY"))

    def test_child_dossier_is_read_without_a_composed_parent_dossier(self) -> None:
        self.add_person("b", "Casey Example", dossier=False)
        self.db.project_rows((ArtifactRow(
            "dossier-person:person-b", "dossier", "parent-b", "/missing/child.md",
            "child-b", "projected", person_id="person-b",
            payload_json=json.dumps({"body": "# Casey Example\nSAVED CHILD CONTEXT"}),
        ),))
        code, out, err = self.cli("--name", "Casey Example", "--json")
        self.assertEqual((code, err), (0, ""))
        match, = json.loads(out)["matches"]
        self.assertEqual(match["parent_id"], "parent-b")
        self.assertIn("SAVED CHILD CONTEXT", match["dossier_body"])

    def test_lookup_deduplicates_bodies_and_excludes_candidates_and_other_parents(self) -> None:
        self.add_person("b", "Casey Example")
        self.db.project_rows((
            ArtifactRow("dossier-copy:person-alias", "dossier", "parent-a", "/missing/copy.md",
                        "copy", "projected", person_id="person-alias",
                        payload_json=json.dumps({"body": "CHILD BODY"})),
            ArtifactRow("dossier-old:person-a", "dossier", "parent-a", "/missing/old.md",
                        "old", "failed", person_id="person-a",
                        payload_json=json.dumps({"body": "FAILED CONTEXT"})),
            LinkRow("candidate-a", "parent-a", "different-jordan", "pub",
                    source="deep-context-reconcile"),
            ArtifactRow("dossier-candidate:candidate-a", "dossier", "parent-a", "/missing/candidate.md",
                        "candidate", "projected", candidate_key="candidate-a",
                        payload_json=json.dumps({"body": "UNRESOLVED CANDIDATE CONTEXT"})),
        ))
        result = PersonLookup(db=self.db.db_path, parent_id="parent-a").run()
        body = result.matches[0].dossier_body
        self.assertEqual(body.count("CHILD BODY"), 1)
        for text in ("PARENT b", "FAILED CONTEXT", "UNRESOLVED CANDIDATE CONTEXT"):
            self.assertNotIn(text, body)

    def test_exact_name_precedes_partial_name(self) -> None:
        self.add_person("b", "Jordan Bravo Senior")
        result = PersonLookup(db=self.db.db_path, name="  JORDAN   BRAVO  ").run()
        self.assertEqual(result.status, "found")
        self.assertEqual([m.parent_id for m in result.matches], ["parent-a"])

    def test_partial_tokens_match_parent_and_child(self) -> None:
        result = PersonLookup(db=self.db.db_path, name="Bravo Jordan").run()
        self.assertEqual([m.parent_id for m in result.matches], ["parent-a"])

    def test_matching_contact_without_parent_dossier_is_found(self) -> None:
        self.add_person("b", "Casey Example", dossier=False)
        for query in ({"name": "Casey Example"}, {"email": "b@example.com"}):
            with self.subTest(query=query):
                result = PersonLookup(db=self.db.db_path, **query).run()
                self.assertEqual(result.status, "found")
                self.assertEqual(result.matches[0].parent_id, "parent-b")
                self.assertEqual(result.matches[0].dossier_body, "")
                self.assertEqual(result.matches[0].dossier_path, "")
        code, out, err = self.cli("--name", "Casey Example")
        self.assertEqual(code, 0)
        self.assertIn("Casey Example", out)
        self.assertIn("No saved dossier", out)
        self.assertEqual(err, "")

    def test_same_name_parents_need_selection_without_mixed_bodies(self) -> None:
        self.add_person("b", "Jordan Bravo")
        result = PersonLookup(db=self.db.db_path, name="Jordan Bravo").run()
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual([m.parent_id for m in result.matches], ["parent-a", "parent-b"])
        self.assertTrue(all(not match.dossier_body for match in result.matches))
        code, out, err = self.cli("--name", "Jordan Bravo")
        self.assertEqual(code, 0)
        self.assertNotIn("PARENT", out + err)
        self.assertIn("--parent-id", out)
        self.assertIn("parent-a", out)
        self.assertIn("parent-b", out)
        self.assertIn("A@Example.com", out)
        self.assertIn("https://www.linkedin.com/in/jordan-b", out)

    def test_ambiguous_choices_include_saved_child_summary_without_full_bodies(self) -> None:
        self.add_person("b", "Jordan Bravo", dossier=False)
        self.db.project_rows((ArtifactRow(
            "dossier-person:person-b", "dossier", "parent-b", "/missing/child.md",
            "child-b", "projected", person_id="person-b", payload_json=json.dumps({
                "body": "PRIVATE FULL CHILD CONTEXT", "headline": "Product designer at North",
            }),
        ),))
        code, out, err = self.cli("--name", "Jordan Bravo", "--json")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ambiguous")
        self.assertEqual([m["headline"] for m in payload["matches"]],
                         ["Engineer a", "Product designer at North"])
        self.assertTrue(all(not m["dossier_body"] for m in payload["matches"]))
        code, out, err = self.cli("--name", "Jordan Bravo")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Product designer at North", out)
        self.assertNotIn("PRIVATE FULL CHILD CONTEXT", out)

    def test_parent_id_selects_only_one_canonical_dossier(self) -> None:
        self.add_person("b", "Jordan Bravo")
        result = PersonLookup(db=self.db.db_path, parent_id="parent-b").run()
        self.assertEqual(result.status, "found")
        self.assertEqual([m.parent_id for m in result.matches], ["parent-b"])
        self.assertEqual(result.matches[0].dossier_body, "# Jordan Bravo\nPARENT b\n")

    def test_json_contains_parent_body_path_and_live_identifiers(self) -> None:
        code, out, err = self.cli("--email", "alias@example.com", "--json")
        self.assertEqual((code, err), (0, ""))
        payload = json.loads(out)
        self.assertEqual(payload["status"], "found")
        match, = payload["matches"]
        self.assertEqual(match["parent_id"], "parent-a")
        self.assertEqual(match["name"], "Jordan Bravo")
        self.assertEqual(match["dossier_path"], str(self.root / "a.md"))
        self.assertEqual(match["dossier_body"], "# Jordan Bravo\nPARENT a\n\n\nCHILD BODY")
        self.assertEqual(set(match["emails"]), {"A@Example.com", "alias@example.com"})
        self.assertEqual(match["phones"], ["+14155550100"])
        self.assertEqual(match["headline"], "Engineer a")

    def test_ambiguous_json_is_metadata_then_selection_returns_body(self) -> None:
        self.add_person("b", "Jordan Bravo")
        code, out, _ = self.cli("--name", "Jordan Bravo", "--json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["status"], "ambiguous")
        self.assertTrue(all(not match["dossier_body"] for match in payload["matches"]))
        code, out, _ = self.cli("--parent-id", "parent-a", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["matches"][0]["dossier_body"], "# Jordan Bravo\nPARENT a\n\n\nCHILD BODY")

    def test_no_match_and_no_query_are_explicit(self) -> None:
        for args, status, expected in ((["--name", "Unknown"], "no_match", 1),
                                       ([], "no_query", 2),
                                       (["--parent-id", "missing"], "no_match", 1)):
            with self.subTest(status=status, args=args):
                code, out, _ = self.cli(*args, "--json")
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(out)["status"], status)
                self.assertEqual(json.loads(out)["matches"], [])

    def test_missing_database_is_actionable_and_does_not_create_store(self) -> None:
        missing = self.root / "missing" / "deep-context.sqlite"
        code, out, err = self.cli("--name", "Jordan", "--db", str(missing), "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["status"], "missing_database")
        self.assertIn("contact/profile lookup", json.loads(out)["message"])
        self.assertNotIn("Traceback", err)
        self.assertFalse(missing.parent.exists())

    def test_legacy_store_is_unreadable_even_when_name_is_absent(self) -> None:
        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute("DROP TABLE imported_people")
            conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
        before = hashlib.sha256(self.db.db_path.read_bytes()).hexdigest()
        for name in ("Jordan Bravo", "Absent Synthetic Zqxv"):
            with self.subTest(name=name):
                code, out, err = self.cli("--name", name, "--json")
                self.assertEqual((code, err), (1, ""))
                self.assertEqual(json.loads(out)["status"], "unreadable_database")
                self.assertEqual(json.loads(out)["matches"], [])
        self.assertEqual(hashlib.sha256(self.db.db_path.read_bytes()).hexdigest(), before)

    def test_non_sqlite_file_is_unreadable_without_traceback(self) -> None:
        invalid = self.root / "invalid.sqlite"
        invalid.write_bytes(b"not a SQLite database")
        code, out, err = self.cli("--name", "Jordan", "--db", str(invalid), "--json")
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(json.loads(out)["status"], "unreadable_database")
        self.assertEqual(invalid.read_bytes(), b"not a SQLite database")

    def test_cli_does_not_upgrade_or_modify_database(self) -> None:
        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute("DROP INDEX research_by_candidate")
        before = hashlib.sha256(self.db.db_path.read_bytes()).hexdigest()
        code, out, err = self.cli("--name", "Jordan Bravo", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["matches"][0]["parent_id"], "parent-a")
        self.assertEqual(hashlib.sha256(self.db.db_path.read_bytes()).hexdigest(), before)
        with sqlite3.connect(self.db.db_path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name='research_by_candidate'").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

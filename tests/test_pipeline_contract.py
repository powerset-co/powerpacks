"""The declared pipeline contract's guardrails must actually FIRE.

Every test here builds a deliberately-broken node (or graph) and asserts the
failure, plus the two behaviors the contract is supposed to buy: an older CSV with
fewer columns still reads, and `PeopleRow` reproduces `normalize_people_row`.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.pipeline.contract import (  # noqa: E402
    Artifact,
    ContractError,
    Node,
    PeopleRow,
    StageManifest,
    row_model_for,
)
from packs.ingestion.primitives.pipeline.graph import check_graph  # noqa: E402
from packs.ingestion.schemas.people_schema import (  # noqa: E402
    PEOPLE_SCHEMA_COLUMNS,
    normalize_people_row,
)
from packs.shared.csv_io import CsvIO  # noqa: E402

OWNER_COLUMNS = ("phone", "name", "message_count")
MATCH_COLUMNS = ("match_status", "matched_person_id")
ContactRow = row_model_for("ContactRow", list(OWNER_COLUMNS) + list(MATCH_COLUMNS))


class _Payload(StageManifest):
    status: str = "completed"


class _Declared(Node):
    """A minimal, fully-declared node; the broken ones below deviate one field."""

    name = "declared"
    inputs = ()
    outputs = ()
    payload = _Payload
    manifest = ""

    def execute(self) -> _Payload:
        return _Payload()


class DeclarationTests(unittest.TestCase):
    def test_missing_payload_raises_at_class_definition(self) -> None:
        with self.assertRaises(TypeError) as caught:
            class NoPayload(Node):
                name = "no_payload"
                inputs = ()
                outputs = ()
                manifest = ""

                def execute(self) -> _Payload:
                    return _Payload()

        self.assertIn("payload must be a StageManifest subclass", str(caught.exception))

    def test_wrong_typed_declarations_raise_together(self) -> None:
        with self.assertRaises(TypeError) as caught:
            class Wrong(Node):
                name = ""
                inputs = [Artifact(path="a.csv")]  # a list, not a tuple
                outputs = ("b.csv",)  # not Artifacts
                payload = dict  # not a StageManifest
                manifest = None  # not a str

                def execute(self) -> _Payload:
                    return _Payload()

        message = str(caught.exception)
        for expected in ("name must be", "inputs must be", "outputs must be", "payload must be", "manifest must be"):
            self.assertIn(expected, message)

    def test_overriding_the_run_template_is_rejected(self) -> None:
        with self.assertRaises(TypeError) as caught:
            class Overrides(Node):
                name = "overrides"
                inputs = ()
                outputs = ()
                payload = _Payload
                manifest = ""

                def execute(self) -> _Payload:
                    return _Payload()

                def run(self) -> dict:
                    return {}

        self.assertIn("run() is the template method", str(caught.exception))


class RunTemplateTests(unittest.TestCase):
    def test_undeclared_write_is_caught_as_a_missing_output(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "people.csv"

            class Forgetful(Node):
                name = "forgetful"
                inputs = ()
                outputs = (Artifact(path=str(out), row_model=PeopleRow, writes="full_rewrite"),)
                payload = _Payload
                manifest = ""

                def execute(self) -> _Payload:
                    return _Payload()  # writes nothing

            with self.assertRaises(ContractError) as caught:
                Forgetful().run()
            self.assertIn("declared output was not written", str(caught.exception))

    def test_drifted_header_is_caught(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "people.csv"

            class Drifter(Node):
                name = "drifter"
                inputs = ()
                outputs = (Artifact(path=str(out), row_model=PeopleRow, writes="full_rewrite"),)
                payload = _Payload
                manifest = ""

                def execute(self) -> _Payload:
                    CsvIO.write_dict_rows(out, ["id", "full_name", "nickname"], [])
                    return _Payload()

            with self.assertRaises(ContractError) as caught:
                Drifter().run()
            message = str(caught.exception)
            self.assertIn("header drifted from PeopleRow", message)
            self.assertIn("unexpected=['nickname']", message)

    def test_missing_required_input_is_not_ready_not_an_exception(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "upstream.csv"

            class Waiting(Node):
                name = "waiting"
                inputs = (Artifact(path=str(missing), row_model=PeopleRow, external=True),)
                outputs = ()
                payload = _Payload
                manifest = ""

                def execute(self) -> _Payload:
                    raise AssertionError("execute() must not run without its input")

            payload = Waiting().run()
            self.assertEqual(payload["status"], "not_ready")
            self.assertEqual(payload["reason"], "missing_inputs")
            self.assertEqual(payload["missing_inputs"], (str(missing),))

    def test_declared_outputs_drive_the_manifest_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "people.csv"
            manifest_json = Path(td) / "manifest.json"

            class Writer(Node):
                name = "writer"
                inputs = ()
                # A key name no allowlist knows about: string-sniffing would have
                # missed this output entirely.
                outputs = (Artifact(path=str(out), row_model=PeopleRow, writes="full_rewrite"),)
                payload = _Payload
                manifest = str(manifest_json)

                def execute(self) -> _Payload:
                    CsvIO.write_dict_rows(out, PEOPLE_SCHEMA_COLUMNS, [{"id": "1"}])
                    return _Payload()

            payload = Writer().run()
            fingerprinted = payload["fingerprints"]["output_artifacts"]
            self.assertEqual(list(fingerprinted), [str(out)])
            self.assertTrue(fingerprinted[str(out)]["sha256"])


class GraphCheckTests(unittest.TestCase):
    @staticmethod
    def _co_writers(first_columns: tuple[str, ...], second_columns: tuple[str, ...]) -> dict:
        shared = ".powerpacks/messages/contacts.csv"

        class Discovery(Node):
            name = "messages_discovery"
            inputs = ()
            outputs = (Artifact(path=shared, row_model=ContactRow, writes="upsert",
                                owns_columns=first_columns, consumers_optional=True),)
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        class Matcher(Node):
            name = "messages_match"
            inputs = ()
            outputs = (Artifact(path=shared, row_model=ContactRow, writes="annotate",
                                owns_columns=second_columns, consumers_optional=True),)
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        return check_graph([Discovery, Matcher])

    def test_two_writers_with_overlapping_owned_columns_fail(self) -> None:
        report = self._co_writers(OWNER_COLUMNS, ("name",) + MATCH_COLUMNS)
        self.assertEqual(len(report["two_writer_conflicts"]), 1)
        conflict = report["two_writer_conflicts"][0]
        self.assertEqual(conflict["overlapping_columns"], ["name"])
        self.assertEqual(sorted(conflict["nodes"]), ["messages_discovery", "messages_match"])

    def test_two_writers_with_disjoint_owned_columns_pass(self) -> None:
        report = self._co_writers(OWNER_COLUMNS, MATCH_COLUMNS)
        self.assertEqual(report["two_writer_conflicts"], [])
        self.assertEqual(report["schema_mismatches"], [])

    def test_a_whole_file_writer_beside_any_other_writer_fails(self) -> None:
        # index.json before #337: two writers, neither scoped to columns.
        report = self._co_writers(OWNER_COLUMNS, ())
        self.assertEqual(len(report["two_writer_conflicts"]), 1)
        self.assertEqual(
            report["two_writer_conflicts"][0]["reason"], "a writer claims the whole file"
        )

    def test_an_output_nobody_reads_is_a_dead_output(self) -> None:
        class Orphan(Node):
            name = "orphan"
            inputs = ()
            outputs = (Artifact(path=".powerpacks/network-import/discover/gmail/contacts.csv"),)
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        report = check_graph([Orphan])
        self.assertEqual(report["dead_outputs"], [{
            "node": "orphan",
            "path": ".powerpacks/network-import/discover/gmail/contacts.csv",
        }])
        # The same declaration, admitted as dead on purpose, is not reported.

        class Documented(Orphan):
            name = "documented"
            outputs = (Artifact(path=".powerpacks/x/contacts.csv", consumers_optional=True),)

        self.assertEqual(check_graph([Documented])["dead_outputs"], [])

    def test_an_input_with_no_producer_is_a_phantom_input(self) -> None:
        class Consumer(Node):
            name = "consumer"
            inputs = (Artifact(path=".powerpacks/network-import/import/gmail/people.csv"),)
            outputs = ()
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        report = check_graph([Consumer])
        self.assertEqual(report["phantom_inputs"], [{
            "node": "consumer",
            "path": ".powerpacks/network-import/import/gmail/people.csv",
        }])

        class External(Consumer):
            name = "external_consumer"
            inputs = (Artifact(path="~/.msgvault/msgvault.db", external=True),)

        self.assertEqual(check_graph([External])["phantom_inputs"], [])

    def test_a_cycle_is_reported(self) -> None:
        class First(Node):
            name = "first"
            inputs = (Artifact(path="b.csv"),)
            outputs = (Artifact(path="a.csv"),)
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        class Second(Node):
            name = "second"
            inputs = (Artifact(path="a.csv"),)
            outputs = (Artifact(path="b.csv"),)
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        report = check_graph([First, Second])
        self.assertEqual(sorted(report["cycles"]), [["first", "second", "first"], ["second", "first", "second"]])

    def test_the_converted_subset_reports_no_conflicts_or_cycles(self) -> None:
        from packs.ingestion.primitives.discover.gmail.discover import (
            GmailAccountChannel,
            GmailDiscovery,
        )
        from packs.ingestion.primitives.imports.merge_people import PeopleMerge

        report = check_graph([GmailAccountChannel, GmailDiscovery, PeopleMerge])
        self.assertEqual(report["two_writer_conflicts"], [])
        self.assertEqual(report["schema_mismatches"], [])
        self.assertEqual(report["cycles"], [])
        # The one real edge in the converted subset.
        self.assertEqual(report["edges"]["gmail_stage_merge"], ["gmail_account_extract"])


class RowModelTests(unittest.TestCase):
    def test_an_older_csv_missing_recent_columns_reads_cleanly(self) -> None:
        # enrichment_status / enrichment_error were appended on 2026-07-24; a
        # people.csv written before that has 37 columns, not 39.
        older_columns = [c for c in PEOPLE_SCHEMA_COLUMNS if c not in {"enrichment_status", "enrichment_error"}]
        self.assertEqual(len(older_columns), len(PEOPLE_SCHEMA_COLUMNS) - 2)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "people.csv"
            CsvIO.write_dict_rows(path, older_columns, [{
                "id": "candidate:casey@example.com",
                "full_name": "Jordan Bravo",
                "primary_email": "casey@example.com",
                "primary_phone": "+15550100",
            }])
            rows = [PeopleRow.model_validate(raw) for raw in CsvIO.read_dict_rows(path)]

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].full_name, "Jordan Bravo")
        self.assertEqual(rows[0].enrichment_status, "")
        self.assertEqual(rows[0].enrichment_error, "")
        self.assertEqual(list(rows[0].to_row()), PEOPLE_SCHEMA_COLUMNS)

    def test_columns_are_the_schema_in_schema_order(self) -> None:
        self.assertEqual(PeopleRow.columns(), PEOPLE_SCHEMA_COLUMNS)

    def test_people_row_matches_normalize_people_row(self) -> None:
        cases: list[dict] = [
            {},
            {"id": "1", "full_name": "Jordan Bravo", "primary_email": "casey@example.com"},
            # linkedin_url normalization: bare host, query, trailing slash.
            {"linkedin_url": "linkedin.com/in/jordan-bravo/?trk=x"},
            {"linkedin_url": "www.linkedin.com/in/jordan-bravo#about"},
            # public_identifier re-derived from the URL, percent-decoded.
            {"linkedin_url": "https://www.linkedin.com/in/jordan%2Dbravo", "public_identifier": "STALE"},
            # slug trusted (lowercased, decoded) only when there is no URL.
            {"public_identifier": "Jordan%2DBravo/"},
            # non-string values: None, ints, and JSON-ish containers.
            {"id": None, "interaction_counts": {"gmail": 12}, "all_emails": ["casey@example.com"],
             "primary_phone": 15550100},
            # an unknown column is dropped by both.
            {"nickname": "JB", "full_name": "Jordan Bravo"},
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(PeopleRow.model_validate(raw).to_row(), normalize_people_row(raw))


if __name__ == "__main__":
    unittest.main()

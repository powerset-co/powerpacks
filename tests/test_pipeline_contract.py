"""The declared pipeline contract's guardrails must actually FIRE.

Every test here builds a deliberately-broken node (or graph) and asserts the
failure, plus the two behaviors the contract is supposed to buy: an older CSV with
fewer columns still reads, and `PeopleRow` reproduces `normalize_people_row`.
"""

from __future__ import annotations

import json
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
            self.assertEqual(payload.status, "not_ready")
            self.assertEqual(payload.reason, "missing_inputs")
            self.assertEqual(payload.missing_inputs, (str(missing),))

    def test_run_returns_the_typed_payload_not_the_written_manifest(self) -> None:
        # The store of a stage consumes its steps' payloads by attribute
        # (enrich_people), so the template hands back the typed object; the dict
        # form is one `to_payload()` away.
        with tempfile.TemporaryDirectory() as td:
            manifest_json = Path(td) / "manifest.json"

            class Quiet(Node):
                name = "quiet"
                inputs = ()
                outputs = ()
                payload = _Payload
                manifest = str(manifest_json)

                def execute(self) -> _Payload:
                    return _Payload()

            payload = Quiet().run()
            self.assertIsInstance(payload, _Payload)
            self.assertEqual(payload.to_payload(), {"status": "completed"})
            # The manifest on disk is the one with the stats block.
            self.assertIn("fingerprints", json.loads(manifest_json.read_text(encoding="utf-8")))

    def test_declared_artifacts_drive_the_manifest_stats_including_row_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "upstream.csv"
            out = Path(td) / "people.csv"
            store = Path(td) / "store.db"
            manifest_json = Path(td) / "manifest.json"
            CsvIO.write_dict_rows(source, PEOPLE_SCHEMA_COLUMNS, [{"id": "0"}])
            store.write_bytes(b"not a csv")

            class Writer(Node):
                name = "writer"
                inputs = (
                    Artifact(path=str(source), row_model=PeopleRow),
                    # No row model: a sqlite store has no rows to count.
                    Artifact(path=str(store), external=True),
                )
                # A key name no allowlist knows about: string-sniffing would have
                # missed this output entirely.
                outputs = (Artifact(path=str(out), row_model=PeopleRow, writes="full_rewrite"),)
                payload = _Payload
                manifest = str(manifest_json)

                def execute(self) -> _Payload:
                    CsvIO.write_dict_rows(out, PEOPLE_SCHEMA_COLUMNS, [{"id": "1"}, {"id": "2"}])
                    return _Payload()

            Writer().run()
            stats = json.loads(manifest_json.read_text(encoding="utf-8"))["fingerprints"]
            self.assertEqual(list(stats["output_artifacts"]), [str(out)])
            self.assertTrue(stats["output_artifacts"][str(out)]["sha256"])
            # THE point of the stats: a consumer reads the row count instead of
            # opening the file, so a file that existed only to be counted can go.
            self.assertEqual(stats["output_artifacts"][str(out)]["rows"], 2)
            self.assertEqual(stats["input_artifacts"][str(source)]["rows"], 1)
            self.assertNotIn("rows", stats["input_artifacts"][str(store)])

    def test_an_unchanged_output_carries_its_row_count_forward(self) -> None:
        # artifact_fingerprint reuses the prior entry when size + mtime match, so
        # the count must ride along with it rather than being recomputed — and a
        # manifest written before row counts existed must still gain one.
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "people.csv"
            manifest_json = Path(td) / "manifest.json"

            class Once(Node):
                name = "once"
                inputs = ()
                outputs = (Artifact(path=str(out), row_model=PeopleRow, writes="full_rewrite"),)
                payload = _Payload
                manifest = str(manifest_json)

                def execute(self) -> _Payload:
                    if not out.exists():
                        CsvIO.write_dict_rows(out, PEOPLE_SCHEMA_COLUMNS, [{"id": "1"}, {"id": "2"}, {"id": "3"}])
                    return _Payload()

            node = Once()
            node.run()
            # A manifest from before this field existed: same fingerprint, no rows.
            legacy = json.loads(manifest_json.read_text(encoding="utf-8"))
            legacy["fingerprints"]["output_artifacts"][str(out)].pop("rows")
            manifest_json.write_text(json.dumps(legacy), encoding="utf-8")

            stats = node.artifact_stats(manifest_json)
            self.assertEqual(stats["output_artifacts"][str(out)]["rows"], 3)


class GraphCheckTests(unittest.TestCase):
    @staticmethod
    def _co_writers(first_columns: tuple[str, ...], second_columns: tuple[str, ...]) -> dict:
        shared = ".powerpacks/messages/contacts.csv"

        class Discovery(Node):
            name = "messages_discovery"
            inputs = ()
            outputs = (Artifact(path=shared, row_model=ContactRow, writes="upsert",
                                owns_columns=first_columns),)
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        class Matcher(Node):
            name = "messages_match"
            inputs = ()
            outputs = (Artifact(path=shared, row_model=ContactRow, writes="annotate",
                                owns_columns=second_columns),)
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
        # There is no opt-out. A dead output is either deleted or it is reported;
        # `consumers_optional` was removed because a flag describing a file that
        # should not exist is code built around the problem, not a fix.
        class Consumer(Node):
            name = "consumer"
            inputs = Orphan.outputs
            outputs = ()
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        self.assertEqual(check_graph([Orphan, Consumer])["dead_outputs"], [])

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


class WholeDeclaredGraphTests(unittest.TestCase):
    """The report for EVERY converted node, not a hand-picked subset.

    Four of the five findings must stay empty. `dead_outputs` is the exception and
    it is listed here on purpose: both entries are the LinkedIn enrichment output
    under its two bindings, whose consumer is the unconverted indexing pack
    (`packs/indexing/modal/linkedin_modal_pipeline.py` downloads
    `discover/linkedin/people.csv` to `import/linkedin/people.csv`). If the
    indexing pack is converted, this list shrinks — it must not GROW quietly."""

    @staticmethod
    def _declared_nodes() -> list[type[Node]]:
        # graph.py's imports ARE the registration; filter to shipped modules so a
        # deliberately-broken fixture from another test module cannot join in.
        import packs.ingestion.primitives.pipeline.graph as graph_module

        return [node for node in graph_module.node_subclasses() if node.__module__.startswith("packs.")]

    def test_no_conflicts_no_phantoms_no_cycles(self) -> None:
        report = check_graph(self._declared_nodes())
        self.assertEqual(report["two_writer_conflicts"], [])
        self.assertEqual(report["schema_mismatches"], [])
        self.assertEqual(report["phantom_inputs"], [])
        self.assertEqual(report["cycles"], [])

    def test_the_only_dead_outputs_are_the_linkedin_enrichment_people_csv(self) -> None:
        report = check_graph(self._declared_nodes())
        self.assertEqual(
            sorted((item["node"], item["path"]) for item in report["dead_outputs"]),
            [
                ("enrich_merge_people", ".powerpacks/network-import/enrichment/people.csv"),
                ("linkedin_import", ".powerpacks/network-import/discover/linkedin/people.csv"),
            ],
        )

    def test_a_manifest_another_node_reads_is_produced_not_phantom(self) -> None:
        # A node's `manifest` is a path it writes; the gmail import takes the
        # account selection from discovery's manifest and the messages import gates
        # on the matcher's, and both are real edges.
        report = check_graph(self._declared_nodes())
        self.assertIn("gmail_stage_merge", report["edges"]["gmail_import"])
        self.assertIn("messages_match_local", report["edges"]["messages_import"])


class MessagesSubsetTests(unittest.TestCase):
    """`.powerpacks/messages/contacts.csv` is the first REAL two-writer file, so
    the split has to hold against a stand-in for the other writer."""

    @staticmethod
    def _messages_nodes() -> list[type[Node]]:
        from packs.ingestion.primitives.discover.messages.channels.i_message_channel import (
            IMessageChannel,
        )
        from packs.ingestion.primitives.discover.messages.channels.whats_app_channel import (
            WhatsAppChannel,
        )
        from packs.ingestion.primitives.discover.messages.discover import MessagesDiscovery

        return [IMessageChannel, WhatsAppChannel, MessagesDiscovery]

    def test_owned_columns_are_the_values_this_stage_computes(self) -> None:
        from packs.ingestion.primitives.discover.messages.models import (
            DISCOVERY_OWNED_COLUMNS,
            MessageContactRow,
        )
        from packs.ingestion.schemas.message_contacts import CSV_HEADERS

        self.assertEqual(MessageContactRow.columns(), CSV_HEADERS)
        self.assertEqual(len(CSV_HEADERS), 19)
        self.assertEqual(len(DISCOVERY_OWNED_COLUMNS), 11)
        # `skip` is claimed by NEITHER writer: every producer writes it empty and
        # only a human ever sets it, so discovery passes it through.
        self.assertNotIn("skip", DISCOVERY_OWNED_COLUMNS)
        unowned = [c for c in CSV_HEADERS if c not in DISCOVERY_OWNED_COLUMNS]
        self.assertEqual(unowned[0], "skip")
        self.assertTrue(all(c.startswith("match") for c in unowned[1:]))

    def test_the_shared_contacts_csv_tolerates_the_import_matcher(self) -> None:
        from packs.ingestion.primitives.discover.messages.discover import MERGED_CONTACTS
        from packs.ingestion.primitives.discover.messages.models import MessageContactRow

        # A stand-in for the OTHER writer, declaring the 8 match columns the
        # matcher annotates. Same row-model object on purpose — an equal-but-
        # distinct model is reported as a schema mismatch.
        class Annotator(Node):
            name = "messages_match_local_candidates"
            inputs = ()
            outputs = (Artifact(
                path=str(MERGED_CONTACTS),
                row_model=MessageContactRow,
                writes="annotate",
                owns_columns=(
                    "match_status", "matched_person_id", "matched_name",
                    "matched_linkedin_url", "match_confidence", "match_method",
                    "match_reason",
                ),
            ),)
            payload = _Payload
            manifest = ""

            def execute(self) -> _Payload:
                return _Payload()

        report = check_graph(self._messages_nodes() + [Annotator])
        self.assertEqual(report["two_writer_conflicts"], [])
        self.assertEqual(report["schema_mismatches"], [])

    def test_the_whatsapp_name_fallback_edge_is_gone(self) -> None:
        # The WhatsApp extractor used to read the MERGED contacts.csv back as its
        # `name_fallback_csv` default, so the two nodes consumed each other's
        # output and the graph reported a cycle. The default is gone (it supplied 0
        # of the 90 named contacts on real data, and the downstream merge already
        # unions names across channels), so the extractor reads the wacli store
        # alone and the messages subset is acyclic.
        report = check_graph(self._messages_nodes())
        self.assertEqual(report["cycles"], [])
        self.assertEqual(
            report["edges"]["messages_stage_merge"],
            ["messages_imessage_extract", "messages_whatsapp_extract"],
        )
        self.assertEqual(report["edges"]["messages_whatsapp_extract"], [])
        from packs.ingestion.primitives.discover.messages.channels.whats_app_channel import (
            WhatsAppChannel,
        )

        self.assertEqual(
            [item.path for item in WhatsAppChannel.inputs],
            [".powerpacks/messages/wacli/wacli.db"],
        )


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


class RunTemplateBypassTests(unittest.TestCase):
    """A base listed BEFORE Node in the MRO must not be able to shadow run().

    The first guard checked `"run" in vars(cls)`, which only inspects the
    subclass's OWN __dict__ — so `class C(Mixin, Node)` where Mixin defines
    run() passed, and every declared input/output check silently stopped
    running. Messages discovery hit exactly this shape (MessageChannel, Node).
    """

    def test_a_mixin_cannot_shadow_the_run_template(self) -> None:
        class Payload(StageManifest):
            pass

        class Mixin:
            def run(self):  # noqa: ANN201 - deliberately shadows the template
                return "bypassed"

        with self.assertRaises(TypeError) as caught:
            class Sneaky(Mixin, Node):
                name = "sneaky"
                inputs = ()
                outputs = ()
                payload = Payload
                manifest = ""

                def execute(self):  # noqa: ANN201
                    return Payload()

        self.assertIn("run() is the template method", str(caught.exception))

"""The import stage's DECLARATIONS must keep saying what the code does.

These lock the declarations that were argued about, so a later edit that quietly
changes one fails here instead of in a user's directory.csv:

  * `directory.csv` has two legitimate writers and they own ROW SLICES, not
    columns — the axis `owns_columns` cannot express.
  * `contacts.csv` has two legitimate writers and they own COLUMNS, disjointly,
    with `skip` owned by neither.
  * the matcher has NO default people catalog. It used to default to
    `merged/people.csv` — the fan-in merge's own output — which made the graph
    cyclic; the catalog is an explicit caller argument now, and nobody may bring
    the default back.
  * `import/linkedin/people.csv` is `external=True` because the Modal indexing
    pipeline writes it, not any node here — and the merge's OTHER two inputs are
    not, so the flag cannot be used to silence a phantom-input report.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packs.ingestion.primitives.imports.directory import (  # noqa: E402
    DIRECTORY_COLUMNS,
    GMAIL_DIRECTORY_ROWS,
    MESSAGES_DIRECTORY_ROWS,
    DirectoryRow,
)
from packs.ingestion.primitives.imports.gmail.importer import GmailImport  # noqa: E402
from packs.ingestion.primitives.imports.linkedin.network_import import LinkedInImport  # noqa: E402
from packs.ingestion.primitives.imports.merge_people import PeopleMerge  # noqa: E402
from packs.ingestion.primitives.imports.messages.importer import MessagesImport  # noqa: E402
from packs.ingestion.primitives.imports.messages.match_local_candidates import (  # noqa: E402
    ContactsMatch,
)
from packs.ingestion.primitives.discover.messages.models import (  # noqa: E402
    MessageContactRow,
)
from packs.ingestion.primitives.imports.messages.util import (  # noqa: E402
    MATCH_ANNOTATION_COLUMNS,
    USER_OWNED_COLUMNS,
)
from packs.ingestion.primitives.pipeline.contract import Artifact, Node, StageManifest  # noqa: E402
from packs.ingestion.primitives.pipeline.graph import check_graph  # noqa: E402
from packs.ingestion.schemas.message_contacts import CSV_HEADERS  # noqa: E402

IMPORT_STAGE = [ContactsMatch, GmailImport, LinkedInImport, MessagesImport, PeopleMerge]


def declared(node: type[Node], path: str, where: str = "outputs") -> Artifact:
    """The one declaration `node` makes for `path`."""
    matches = [item for item in getattr(node, where) if item.path == path]
    assert len(matches) == 1, f"{node.name} declares {len(matches)} {where} for {path}"
    return matches[0]


class DirectoryOwnershipTests(unittest.TestCase):
    directory_csv = ".powerpacks/network-import/directory.csv"

    def test_both_writers_own_row_slices_not_columns(self) -> None:
        gmail = declared(GmailImport, self.directory_csv)
        messages = declared(MessagesImport, self.directory_csv)
        # Columns are the wrong axis here: each writer writes EVERY column of its
        # own source's rows, so neither can name a column subset.
        self.assertEqual(gmail.owns_columns, ())
        self.assertEqual(messages.owns_columns, ())
        self.assertEqual(gmail.owns_rows_where, GMAIL_DIRECTORY_ROWS)
        self.assertEqual(messages.owns_rows_where, MESSAGES_DIRECTORY_ROWS)
        self.assertNotEqual(gmail.owns_rows_where, messages.owns_rows_where)
        # The write modes are the real ones: gmail merges by source_key, messages
        # deletes its whole slice and rewrites it.
        self.assertEqual(gmail.writes, "upsert")
        self.assertEqual(messages.writes, "full_rewrite")

    def test_disjoint_row_slices_are_not_a_conflict_but_the_same_slice_is(self) -> None:
        self.assertEqual(check_graph([GmailImport, MessagesImport])["two_writer_conflicts"], [])

        class Clash(MessagesImport):
            name = "messages_import_clone"
            # Same slice as the real messages writer: that IS a conflict.
            outputs = (Artifact(path=DirectoryOwnershipTests.directory_csv, row_model=DirectoryRow,
                                writes="full_rewrite", owns_rows_where=MESSAGES_DIRECTORY_ROWS),)

        conflicts = check_graph([MessagesImport, Clash])["two_writer_conflicts"]
        self.assertEqual([c["path"] for c in conflicts], [self.directory_csv])
        self.assertEqual(conflicts[0]["reason"], "two writers own the same row slice")

    def test_a_row_slice_writer_without_a_predicate_claims_the_whole_file(self) -> None:
        class Unscoped(MessagesImport):
            name = "messages_import_unscoped"
            outputs = (Artifact(path=DirectoryOwnershipTests.directory_csv, row_model=DirectoryRow,
                                writes="full_rewrite"),)

        conflicts = check_graph([GmailImport, Unscoped])["two_writer_conflicts"]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["reason"], "a writer claims the whole file")

    def test_the_row_model_is_the_on_disk_header(self) -> None:
        self.assertEqual(DirectoryRow.columns(), DIRECTORY_COLUMNS)


class ContactsOwnershipTests(unittest.TestCase):
    def test_the_matcher_owns_exactly_the_seven_annotation_columns(self) -> None:
        contacts = declared(ContactsMatch, ".powerpacks/messages/contacts.csv")
        self.assertEqual(contacts.owns_columns, MATCH_ANNOTATION_COLUMNS)
        self.assertEqual(len(MATCH_ANNOTATION_COLUMNS), 7)
        # It rewrites the file (csv has no in-place cell write) but only these
        # values are its own, so the mode is annotate, never full_rewrite.
        self.assertEqual(contacts.writes, "annotate")

    def test_skip_is_owned_by_neither_writer(self) -> None:
        # `skip` is a USER mark ("yes/true to exclude from research"). The
        # extractors seed it empty and merge_contacts ORs it, but nothing sets it
        # true — so no writer may claim it.
        self.assertEqual(USER_OWNED_COLUMNS, ("skip",))
        self.assertNotIn("skip", MATCH_ANNOTATION_COLUMNS)

    def test_the_nineteen_columns_split_into_eleven_one_and_seven(self) -> None:
        self.assertEqual(len(CSV_HEADERS), 19)
        discovery_owned = [c for c in CSV_HEADERS
                           if c not in MATCH_ANNOTATION_COLUMNS and c not in USER_OWNED_COLUMNS]
        self.assertEqual(len(discovery_owned), 11)
        self.assertEqual(MessageContactRow.columns(), CSV_HEADERS)


class DeclaredGraphTests(unittest.TestCase):
    def test_the_import_stage_declares_no_conflicts_or_schema_mismatches(self) -> None:
        report = check_graph(IMPORT_STAGE)
        self.assertEqual(report["two_writer_conflicts"], [])
        self.assertEqual(report["schema_mismatches"], [])

    def test_the_matcher_declares_no_people_catalog_and_the_stage_is_acyclic(self) -> None:
        # `--local-people` USED to default to the fan-in merge's own output, which
        # closed a loop (merge_people -> messages_match_local -> messages_import ->
        # merge_people). It has no default now: the catalog is a caller argument
        # with no fixed path, like `--candidates`, so it is not declared at all —
        # and nobody may reinstate the default by declaring merged/people.csv here.
        merged_people = ".powerpacks/network-import/merged/people.csv"
        self.assertEqual([item.path for item in ContactsMatch.inputs if item.path == merged_people], [])
        self.assertEqual(check_graph(IMPORT_STAGE)["cycles"], [])

    def test_only_the_linkedin_people_input_is_external(self) -> None:
        # `import/linkedin/people.csv` has no writer in packs/ingestion: the
        # LinkedIn import runs in the Modal sandbox and the indexing pack's
        # linkedin_modal_pipeline.py downloads the enriched file to that path.
        # gmail's and messages' come from their importers, so they are NOT external
        # — the flag states a fact about the producer, it does not silence a report.
        external = [item.path for item in PeopleMerge.inputs if item.external]
        self.assertEqual(external, [".powerpacks/network-import/import/linkedin/people.csv"])

    def test_every_import_node_declares_a_payload_and_its_manifest_home(self) -> None:
        for node in IMPORT_STAGE:
            with self.subTest(node=node.name):
                self.assertTrue(issubclass(node.payload, StageManifest))
                self.assertIsInstance(node.manifest, str)
        # The two importers write through imports/common.py:write_manifest, whose
        # fingerprint chain the no-op gate reads, so the Node template must not
        # write a second manifest.json over it.
        self.assertEqual(GmailImport.manifest, "")
        self.assertEqual(MessagesImport.manifest, "")


if __name__ == "__main__":
    unittest.main()

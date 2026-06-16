import json
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB_DIR = ROOT / "packs/search/primitives/lib"
import sys
sys.path.insert(0, str(LIB_DIR))

import postgres_client  # noqa: E402


SET_ID = "10000000-0000-0000-0000-000000000001"
OPERATOR_ID = "20000000-0000-0000-0000-000000000001"
# Operator who is NOT a member of the active set. Shares PERSON_1 globally;
# their provenance must never appear in a set-scoped search.
OUT_OF_SCOPE_OPERATOR_ID = "20000000-0000-0000-0000-0000000000ff"
PERSON_1 = "00000000-0000-0000-0000-000000000001"
PERSON_2 = "00000000-0000-0000-0000-000000000002"


class PostgresFixtureClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture_path = Path(self.tmp.name) / "postgres-fixture.json"
        fixture = {
            "sets": [
                {
                    "id": SET_ID,
                    "name": "Fixture Set",
                    "created_by": "auth0|owner",
                    "is_active": True,
                    "is_personal": True,
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
            "users": [
                {
                    "id": OPERATOR_ID,
                    "user_id": "auth0|owner",
                    "email": "owner@example.com",
                    "name": "Owner User",
                },
                {
                    "id": OUT_OF_SCOPE_OPERATOR_ID,
                    "user_id": "auth0|stranger",
                    "email": "eric@ef7.co",
                    "name": "",
                },
            ],
            "set_members": [
                {
                    "set_id": SET_ID,
                    "user_id": "auth0|owner",
                    "role": "owner",
                    "joined_at": "2026-01-01T00:00:00Z",
                }
            ],
            "persons": [
                {
                    "id": PERSON_1,
                    "full_name": "Ada Backend",
                    "hydrated_context": json.dumps({"name": "Ada Backend", "positions": []}),
                },
                {
                    "id": PERSON_2,
                    "full_name": "Grace Systems",
                    "hydrated_context": {"name": "Grace Systems", "positions": []},
                },
            ],
            "person_source_summary": [
                {"person_id": PERSON_1, "operator_id": OPERATOR_ID, "total_interactions": 2, "source_channel": "gmail"},
                {"person_id": PERSON_1, "operator_id": OPERATOR_ID, "total_interactions": 5, "source_channel": "linkedin"},
                {"person_id": PERSON_2, "operator_id": OPERATOR_ID, "total_interactions": 3, "source_channel": "gmail"},
                # Out-of-scope operator also has PERSON_1 globally.
                {"person_id": PERSON_1, "operator_id": OUT_OF_SCOPE_OPERATOR_ID, "total_interactions": 9, "source_channel": "imessage"},
            ],
        }
        self.fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
        self.old_env = {key: os.environ.get(key) for key in ["POWERPACKS_POSTGRES_FIXTURE_JSON", "POWERPACKS_DEFAULT_SET_ID", "POWERSET_DEFAULT_SET_ID"]}
        os.environ["POWERPACKS_POSTGRES_FIXTURE_JSON"] = str(self.fixture_path)
        os.environ.pop("POWERPACKS_DEFAULT_SET_ID", None)
        os.environ.pop("POWERSET_DEFAULT_SET_ID", None)

    def tearDown(self) -> None:
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_fetch_set_operator_ids_from_fixture(self) -> None:
        out = postgres_client.fetch_set_operator_ids(set_id=SET_ID)

        self.assertEqual(out["set_id"], SET_ID)
        self.assertEqual(out["set_name"], "Fixture Set")
        self.assertEqual(out["operator_ids"], [OPERATOR_ID])
        self.assertEqual(out["operator_count"], 1)
        self.assertEqual(out["members"][0]["email"], "owner@example.com")

    def test_fetch_person_rows_preserves_requested_order_and_parses_context(self) -> None:
        rows = postgres_client.fetch_person_rows([PERSON_2, PERSON_1])

        self.assertEqual([row["id"] for row in rows], [PERSON_2, PERSON_1])
        self.assertEqual(rows[0]["hydrated_context"]["name"], "Grace Systems")
        self.assertEqual(rows[1]["hydrated_context"]["name"], "Ada Backend")

    def test_fetch_interaction_counts_aggregates_fixture_rows(self) -> None:
        # Unscoped (legacy): aggregates all operators globally, including the
        # out-of-scope operator's 9 interactions with PERSON_1.
        counts = postgres_client.fetch_interaction_counts([PERSON_1, PERSON_2])

        self.assertEqual(counts, {PERSON_1: 16, PERSON_2: 3})

    def test_fetch_interaction_counts_scoped_excludes_out_of_scope_operator(self) -> None:
        counts = postgres_client.fetch_interaction_counts(
            [PERSON_1, PERSON_2], allowed_operator_ids=[OPERATOR_ID]
        )

        # Out-of-scope operator's 9 interactions with PERSON_1 must be excluded.
        self.assertEqual(counts, {PERSON_1: 7, PERSON_2: 3})

    def test_fetch_interaction_counts_empty_scope_fails_closed(self) -> None:
        counts = postgres_client.fetch_interaction_counts(
            [PERSON_1, PERSON_2], allowed_operator_ids=[]
        )

        self.assertEqual(counts, {})

    def test_fetch_source_attribution_scoped_excludes_out_of_scope_operator(self) -> None:
        scoped = postgres_client.fetch_source_attribution(
            [PERSON_1], allowed_operator_ids=[OPERATOR_ID]
        )

        self.assertEqual(scoped[PERSON_1]["operators"], ["Owner User"])
        self.assertNotIn("eric@ef7.co", scoped[PERSON_1]["operators"])
        self.assertEqual(scoped[PERSON_1]["primary_operator"], "Owner User")
        self.assertNotIn("imessage", scoped[PERSON_1]["channels"])

    def test_fetch_source_attribution_unscoped_leaks_all_operators(self) -> None:
        # Documents the legacy (global) behavior the scope param guards against:
        # without scope, the out-of-scope operator's email leaks in.
        unscoped = postgres_client.fetch_source_attribution([PERSON_1])

        self.assertIn("eric@ef7.co", unscoped[PERSON_1]["operators"])


if __name__ == "__main__":
    unittest.main()

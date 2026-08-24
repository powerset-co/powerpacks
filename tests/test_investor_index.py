from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from packs.indexing.primitives.build_investor_index.build_investor_index import rebuild
from packs.search.backends.turbopuffer.resolution import (
    resolve_turbopuffer_investors,
)


class InvestorIndexTests(unittest.TestCase):
    def test_mocked_rebuild_is_operator_scoped_contract_complete_and_alias_aware(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "investors.csv"
            source.write_text(
                "urn,name,type,investment_count\n"
                "urn:a16z,Andreessen Horowitz,firm,42\n"
                "urn:sequoia,Sequoia Capital,firm,50\n"
            )
            namespace = mock.Mock()
            result = rebuild(
                source,
                ("operator-b", "operator-a", "operator-b"),
                namespace_factory=lambda logical_name: namespace,
            )

        self.assertEqual(result["rows"], 4)
        self.assertEqual(result["canonical_rows"], 2)
        self.assertEqual(result["operator_ids"], ["operator-a", "operator-b"])
        namespace.delete_all.assert_not_called()
        namespace.write.assert_called_once()
        written = namespace.write.call_args.kwargs["upsert_rows"]
        alias = next(row for row in written if row["investor_name"] == "a16z")
        self.assertEqual(alias["canonical_name"], "Andreessen Horowitz")
        self.assertEqual(alias["canonical_urn"], "urn:a16z")
        self.assertEqual(alias["allowed_operator_ids"], ["operator-a", "operator-b"])
        self.assertIn("allowed_operator_ids", namespace.write.call_args.kwargs["schema"])
        self.assertEqual(
            namespace.write.call_args.kwargs["delete_by_filter"],
            ("id", "NotEq", ""),
        )
        self.assertFalse(namespace.write.call_args.kwargs["delete_by_filter_allow_partial"])
        self.assertEqual([row["id"] for row in written], sorted(row["id"] for row in written))

    def test_consecutive_rebuilds_replace_removed_investors_aliases_and_operator_scope(self):
        class StatefulNamespace:
            def __init__(self):
                self.rows = {}
                self.calls = []

            def write(
                self,
                *,
                delete_by_filter,
                delete_by_filter_allow_partial,
                upsert_rows,
                schema,
            ):
                snapshot = [dict(row) for row in upsert_rows]
                self.calls.append(("replace", tuple(row["id"] for row in snapshot)))
                self.rows = {row["id"]: row for row in snapshot}

        namespace = StatefulNamespace()
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "investors.csv"
            source.write_text(
                "urn,name,type,investment_count\n"
                "urn:a16z,Andreessen Horowitz,firm,42\n"
                "urn:sequoia,Sequoia Capital,firm,50\n"
            )
            rebuild(
                source,
                ("operator-a", "operator-b"),
                namespace_factory=lambda logical_name: namespace,
            )
            self.assertEqual(
                set(namespace.rows),
                {
                    "urn:a16z",
                    "urn:a16z#alias:a16z",
                    "urn:sequoia",
                    "urn:sequoia#alias:sequoia",
                },
            )

            source.write_text(
                "urn,name,type,investment_count\n"
                "urn:a16z,Andreessen Horowitz Ventures,firm,43\n"
            )
            result = rebuild(
                source,
                ("operator-c",),
                namespace_factory=lambda logical_name: namespace,
            )

        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["operator_ids"], ["operator-c"])
        self.assertEqual(set(namespace.rows), {"urn:a16z"})
        self.assertEqual(
            namespace.rows["urn:a16z"]["investor_name"],
            "Andreessen Horowitz Ventures",
        )
        self.assertEqual(
            namespace.rows["urn:a16z"]["allowed_operator_ids"],
            ["operator-c"],
        )
        self.assertEqual([call[0] for call in namespace.calls], ["replace", "replace"])

    def test_failed_rebuild_leaves_prior_snapshot_available(self):
        class RejectingNamespace:
            def __init__(self):
                self.rows = {"urn:existing": {"investor_name": "Existing Capital"}}
                self.calls = []

            def write(self, **kwargs):
                self.calls.append(kwargs)
                raise RuntimeError("replacement rejected")

        namespace = RejectingNamespace()
        prior_rows = dict(namespace.rows)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "investors.csv"
            source.write_text(
                "urn,name,type,investment_count\n"
                "urn:a16z,Andreessen Horowitz,firm,42\n"
            )
            with self.assertRaisesRegex(RuntimeError, "replacement rejected"):
                rebuild(
                    source,
                    ("operator-a",),
                    namespace_factory=lambda logical_name: namespace,
                )

        self.assertEqual(namespace.rows, prior_rows)
        self.assertEqual(len(namespace.calls), 1)
        self.assertEqual(namespace.calls[0]["delete_by_filter"], ("id", "NotEq", ""))
        self.assertEqual(
            [row["id"] for row in namespace.calls[0]["upsert_rows"]],
            ["urn:a16z", "urn:a16z#alias:a16z"],
        )

    def test_empty_or_fully_invalid_source_preserves_prior_snapshot(self):
        namespace = mock.Mock()
        namespace.rows = {"urn:existing": {"investor_name": "Existing Capital"}}
        namespace_factory = mock.Mock(return_value=namespace)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "investors.csv"
            for contents in (
                "urn,name,type,investment_count\n",
                "urn,name,type,investment_count\n,Missing URN,firm,1\nurn:missing-name,,firm,2\n",
            ):
                with self.subTest(contents=contents):
                    source.write_text(contents)
                    with self.assertRaisesRegex(ValueError, "valid canonical investor row"):
                        rebuild(
                            source,
                            ("operator-a",),
                            namespace_factory=namespace_factory,
                        )

        self.assertEqual(
            namespace.rows,
            {"urn:existing": {"investor_name": "Existing Capital"}},
        )
        namespace_factory.assert_not_called()
        namespace.write.assert_not_called()

    def test_rebuild_requires_explicit_operator_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "investors.csv"
            source.write_text("urn,name,type,investment_count\n")
            with self.assertRaisesRegex(ValueError, "operator_id"):
                rebuild(source, (), namespace_factory=lambda logical_name: mock.Mock())

    def test_resolver_exact_and_alias_queries_always_include_operator_scope(self):
        exact = SimpleNamespace(
            id="urn:sequoia",
            model_extra={
                "investor_name": "Sequoia Capital",
                "canonical_name": "Sequoia Capital",
                "investment_count": 50,
                "canonical_urn": "urn:sequoia",
            },
        )
        alias = SimpleNamespace(
            id="urn:a16z#alias:a16z",
            model_extra={
                "investor_name": "a16z",
                "canonical_name": "Andreessen Horowitz",
                "investment_count": 42,
                "canonical_urn": "urn:a16z",
            },
        )
        namespace = mock.Mock()
        namespace.query.side_effect = [
            SimpleNamespace(rows=[exact]),
            SimpleNamespace(rows=[]),
            SimpleNamespace(rows=[alias]),
        ]
        with mock.patch(
            "packs.search.backends.turbopuffer.resolution.namespace",
            return_value=namespace,
        ):
            rows = asyncio.run(
                resolve_turbopuffer_investors(
                    ["Sequoia Capital", "a16z"],
                    allowed_operator_ids=["operator"],
                    top_k=1,
                )
            )

        self.assertEqual([row["urn"] for row in rows], ["urn:sequoia", "urn:a16z"])
        self.assertEqual([row["match_type"] for row in rows], ["exact", "alias"])
        self.assertEqual(rows[1]["canonical_name"], "Andreessen Horowitz")
        for call in namespace.query.call_args_list:
            self.assertIn(
                ("allowed_operator_ids", "ContainsAny", ["operator"]),
                call.kwargs["filters"][1],
            )


if __name__ == "__main__":
    unittest.main()

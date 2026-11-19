import asyncio
from unittest.mock import patch

import pytest

from packs.search.primitives.local.local_duckdb_store import LocalDuckDBSearchStore


CORE = [{"regex": "Engineer"}]
ADJACENT = [{"regex": "Developer"}]


def search_rows(titles, *, top_k=1000, **payload):
    rows = [
        {"id": f"role-{i:04}", "position_title": title, "person_id": f"person-{i}"}
        for i, title in enumerate(titles)
    ]
    store = LocalDuckDBSearchStore.__new__(LocalDuckDBSearchStore)
    # Fix the fused order to test banding independently of retrieval and RRF.
    fused = [(row["id"], 1 / (i + 1)) for i, row in enumerate(rows)]
    with (
        patch.object(store, "_filtered_rows", return_value=rows),
        patch.object(store, "_vector_rank_sql", return_value=rows),
        patch.object(store, "_rrf", return_value=fused),
    ):
        return asyncio.run(store.hybrid_role_rows(
            {"query_embedding": [1.0], **payload},
            None,
            top_k=top_k,
            include_attributes=["position_title"],
        ))


@pytest.mark.parametrize("count", [0, 1, 30, 99, 100])
def test_role_banding_keeps_lists_up_to_100(count):
    rows = search_rows(["Unrelated"] * count, role_core_patterns=CORE)
    assert [row["id"] for row in rows] == [f"role-{i:04}" for i in range(count)]
    assert all(row["bucket"] == "maybe" for row in rows)


def test_role_banding_cuts_after_dropoff_and_keeps_two_bands_of_matches():
    titles = ["Engineer" if i % 2 == 0 else "Developer" for i in range(130)]
    titles += ["Unrelated"] * 170
    for i in [200, 210, 225, 269, 270, 299]:
        titles[i] = "Developer"
    rows = search_rows(titles, role_core_patterns=CORE, role_adjacent_patterns=ADJACENT)

    # 120:150 has 10/30 matches; 150:180 is the first band below 20%.
    # Keep through 210, then matches only in 210:270.
    expected_ids = list(range(210)) + [210, 225, 269]
    assert [row["id"] for row in rows] == [f"role-{i:04}" for i in expected_ids]
    assert [row["bucket"] for row in rows] == (
        ["good"] * 130 + ["maybe"] * 70 + ["good"] + ["maybe"] * 9
        + ["hail_mary"] * 3
    )
    for row, i in zip(rows, expected_ids):
        assert row["score"] == 1 / (i + 1)
        assert row["position_title"] == titles[i]
        assert row["person_id"] == f"person-{i}"
        assert row["retrieval_mode"] == "hybrid"
        if titles[i] != "Unrelated":
            assert row["has_core_regex"] == (titles[i] == "Engineer")
            assert row["has_adjacent_regex"] == (titles[i] == "Developer")


def test_role_banding_uses_50_percent_for_good_bands():
    titles = ["Engineer"] * 15 + ["Unrelated"] * 15
    titles += ["Developer"] * 14 + ["Unrelated"] * 16
    rows = search_rows(titles, role_core_patterns=CORE, role_adjacent_patterns=ADJACENT)
    assert [row["bucket"] for row in rows] == ["good"] * 44 + ["maybe"] * 16


def test_role_banding_does_not_cut_at_exactly_20_percent():
    titles = ["Unrelated"] * 180
    for start in [90, 120, 150]:
        titles[start:start + 6] = ["Developer"] * 6
    rows = search_rows(titles, role_adjacent_patterns=ADJACENT)
    assert len(rows) == 180


def test_role_banding_checks_the_band_crossing_the_100_row_floor():
    rows = search_rows(["Unrelated"] * 240, role_core_patterns=CORE)
    assert len(rows) == 150
    assert all(row["bucket"] == "maybe" for row in rows)


def test_role_banding_applies_top_k_after_assigning_buckets():
    titles = ["Engineer"] * 130 + ["Unrelated"] * 170
    rows = search_rows(titles, role_core_patterns=CORE, top_k=131)
    assert len(rows) == 131
    assert rows[-1]["bucket"] == "maybe"


def test_role_banding_counts_title_matches_outside_the_truncated_regex_channel():
    rows = search_rows(
        ["Developer", "Engineer", "Engineer"],
        role_core_patterns=CORE,
        role_adjacent_patterns=ADJACENT,
        top_k=1,
    )
    assert rows[0]["bucket"] == "good"
    assert rows[0]["has_core_regex"] is False
    assert rows[0]["has_adjacent_regex"] is True


@pytest.mark.parametrize("payload", [
    {},
    {"role_core_patterns": [{"regex": "["}]},
    {"role_adjacent_patterns": [{"regex": ""}]},
    {"role_function": "founder", "role_core_patterns": CORE},
    {"role_ids": ["founder"], "role_adjacent_patterns": ADJACENT},
])
def test_role_banding_does_not_cut_without_patterns_or_for_founders(payload):
    rows = search_rows(["Unrelated"] * 300, **payload)
    assert [row["id"] for row in rows] == [f"role-{i:04}" for i in range(300)]

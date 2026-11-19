import copy
import json
from unittest import mock

import pytest

from packs.indexing.lib import school_parents
from packs.indexing.lib.artifacts import build_education_corpus, stable_school_uuid


NAME = "The Università-di Bravo (UB)"
SCHOOL_ID = stable_school_uuid({"school_name": NAME})


@pytest.fixture(autouse=True)
def clear_name_cache():
    school_parents._parents_by_name.cache_clear()
    yield
    school_parents._parents_by_name.cache_clear()


def row(school_id, name=NAME, key=None, parent_id=None):
    return dict(id=school_id, school_name=name, key=key, parent_id=parent_id, source="manual")


@pytest.mark.parametrize("items, expected", [
    ([row("a", "Universita di Bravo", "provider:1")], "a"),
    ([row("a", key="provider:1"), row("b", key="provider:2")], SCHOOL_ID),
    ([row("a", key="provider:1", parent_id="b"), row("b", "Middle", parent_id="c"), row("c", "Top")], "c"),
    ([row("a", parent_id="c"), row("b", parent_id="c"), row("c", "Top")], "c"),
    ([row("a"), row("b")], SCHOOL_ID),
    ([row(SCHOOL_ID), row("a", key="provider:1")], SCHOOL_ID),
    ([row(SCHOOL_ID, parent_id="b"), row("b", "Middle", parent_id="c"), row("c", "Top")], "b"),
    ([row("a", "Other University")], SCHOOL_ID),
    ([row("a", key="provider:1"), row("b")], "a"),
], ids=["one-provider", "two-providers", "provider-chain", "shared-top", "different-tops", "mapped-self", "mapped-parent", "no-match", "provider-wins"])
def test_corpus_school_parent(items, expected):
    rows = {item["id"]: item for item in items}
    original = copy.deepcopy(rows)
    people = [{"name": "Jordan Bravo", "linkedin_url": "https://linkedin.com/in/jordan-bravo-example", "education": json.dumps([{"school_name": NAME}])}]
    with mock.patch.object(school_parents, "school_rows", return_value=rows):
        result = build_education_corpus(people)
    edge = result["education"][0]
    assert edge["education_id"] == SCHOOL_ID
    assert edge["canonical_education_id"] == expected
    school = result["schools"][0]
    assert school["id"] == expected
    assert school["school_name"] == (rows[expected]["school_name"] if expected in rows else NAME)
    assert rows == original


def test_unseen_provider_school_stays_itself():
    school = {"school_name": NAME, "school_id": "2"}
    school_id = stable_school_uuid(school)
    rows = {"a": row("a", key="provider:1")}
    people = [{"name": "Jordan Bravo", "education": json.dumps([school])}]
    with mock.patch.object(school_parents, "school_rows", return_value=rows):
        result = build_education_corpus(people)
    assert result["education"][0]["canonical_education_id"] == school_id
    assert result["schools"][0]["id"] == school_id
    assert result["schools"][0]["canonical_key"] == "provider:2"


def test_name_index_is_built_once():
    rows = {"a": row("a", key="provider:1")}
    with mock.patch.object(school_parents, "school_rows", return_value=rows):
        with mock.patch.object(school_parents, "flatten", wraps=school_parents.flatten) as flatten:
            assert school_parents.parent_row("unseen-1", NAME, "name:bravo")["id"] == "a"
            assert school_parents.parent_row("unseen-2", NAME, "name:bravo")["id"] == "a"
            flatten.assert_called_once()

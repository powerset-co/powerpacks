from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace
from unittest import mock

import duckdb
import pytest

from packs.search.primitives.expand_search_request import title_selection
from packs.search.primitives.search_network_pipeline import search_network_pipeline as pipeline
from packs.search.primitives.shared import openai_client


GROUPS = [
    {"stemmed": "appli scientist", "count": 12, "raw_titles": ["Applied Scientist"], "display_title": "Applied Scientist"},
    {"stemmed": "chief staff", "count": 8, "raw_titles": ["Chief of Staff"], "display_title": "Chief of Staff"},
]


def payload(**filters):
    return {"normalized_query": "ai engineers", "role_search_filters": {
        "role_agent_ids": ["ai_engineer"], "bm25_queries": ["Senior AI Engineer"], **filters,
    }}


def test_agent_keywords_come_first_when_model_omits_chief_of_staff():
    keywords = ["Applied Scientist", "Machine Learning", "Machine Learning Engineer"]
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                           return_value=title_selection.TitleSelection(["appli scientist"], keywords)) as pick:
        result = pipeline._apply_title_clusters(payload(bm25_queries=["Chief of Staff", "Special Projects"]),
                                                GROUPS, model="gpt-5.6-luna", reasoning_effort="high")
    filters = result["role_search_filters"]
    assert filters["bm25_queries"] == ["Chief of Staff", "Special Projects", "Applied Scientist",
                                        "Machine Learning", "Machine Learning Engineer"]
    assert filters["local_title_clusters"] == GROUPS[:1]
    assert filters["role_core_patterns"] == [{
        "regex": r"\b(chief\ of\ staff|special\ projects|applied\ scientist|machine\ learning)\b",
        "examples": ["Chief of Staff", "Special Projects", "Applied Scientist", "Machine Learning"],
    }]
    assert re.search(filters["role_core_patterns"][0]["regex"], "Chief of Staff", re.I)
    assert filters["local_title_clustering_status"]["model_used"] is True
    pick.assert_awaited_once_with("ai engineers", GROUPS, model="gpt-5.6-luna", reasoning_effort="high")


@pytest.mark.parametrize("groups,error", [([], None), (GROUPS, RuntimeError("synthetic failure"))])
def test_fallback_keeps_agent_keywords_and_strips_levels(groups, error):
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock, side_effect=error) as pick:
        result = pipeline._apply_title_clusters(payload(bm25_queries=["Chief of Staff"]), groups)
    filters = result["role_search_filters"]
    assert filters["bm25_queries"] == ["Chief of Staff"]
    assert filters["role_core_patterns"] == [{"regex": r"\b(of\ staff)\b", "examples": ["of Staff"]}]
    assert filters["local_title_clustering_status"]["model_used"] is False
    assert filters["local_title_clustering_status"]["fallback"] == "role_agent"
    assert pick.await_count == bool(groups)


def test_no_model_keywords_uses_selected_display_titles_and_injections():
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                           return_value=title_selection.TitleSelection(["appli scientist", "chief staff"], [])):
        result = pipeline._apply_title_clusters(payload(role_agent_ids=["ai_researcher"]), GROUPS)
    filters = result["role_search_filters"]
    assert filters["bm25_queries"] == ["Senior AI Engineer", "Applied Scientist", "Chief of Staff", "Research Fellow", "Research Scientist"]
    assert len(filters["role_core_patterns"]) == 1
    assert "of Staff" in filters["role_core_patterns"][0]["examples"]


def test_display_title_keywords_strip_level_words_from_pattern():
    groups = [{"stemmed": "senior softwar engin", "count": 1,
               "raw_titles": ["Senior Software Engineer"], "display_title": "Senior Software Engineer"}]
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                           return_value=title_selection.TitleSelection(["senior softwar engin"], [])):
        result = pipeline._apply_title_clusters(payload(), groups)
    filters = result["role_search_filters"]
    assert filters["bm25_queries"] == ["Senior AI Engineer", "Senior Software Engineer"]
    assert filters["role_core_patterns"] == [{
        "regex": r"\b(ai\ engineer|software\ engineer)\b", "examples": ["AI Engineer", "Software Engineer"],
    }]
    assert re.search(filters["role_core_patterns"][0]["regex"], "Software Engineer", re.I)


@pytest.mark.parametrize("filters", [
    {"role_function": "founder", "role_ids": ["founder"]},
    {"csuite_shortcut_role_id": "chief_technology_officer", "role_ids": ["chief_technology_officer"]},
])
def test_shortcuts_skip_pick(filters, tmp_path):
    source = payload(**filters)
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock) as pick:
        assert pipeline._apply_title_clusters(source, GROUPS, adjacent_clusters=GROUPS)["role_search_filters"]["bm25_queries"] == ["Senior AI Engineer"]
        pipeline.apply_local_title_clustering(source, tmp_path / "absent.duckdb")
    pick.assert_not_called()


@pytest.mark.parametrize("filters", [{"company_ids": ["co1"]}, {"education_ids": ["school1"]}, {}])
def test_no_role_makes_no_pick(filters, tmp_path):
    source = {"intent_type": "person_lookup" if not filters else "role_search", "role_search_filters": filters}
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock) as pick:
        result = pipeline._apply_title_clusters(source, GROUPS, adjacent_clusters=GROUPS)
        pipeline.apply_local_title_clustering(source, tmp_path / "absent.duckdb")
    pick.assert_not_called()
    assert not result["role_search_filters"].get("role_core_patterns")


def test_card_edit_keeps_level_words_and_one_pattern(tmp_path):
    result = pipeline.apply_card_edits(payload(), {"filters": {
        "bm25_queries": ["Chief of Staff", "Senior Applied Scientist"],
    }}, tmp_path)
    assert result["role_search_filters"]["role_core_patterns"] == [{
        "regex": r"\b(chief\ of\ staff|senior\ applied\ scientist)\b",
        "examples": ["Chief of Staff", "Senior Applied Scientist"],
    }]


@pytest.mark.parametrize("model", [None, "gpt-5.6-luna"])
@pytest.mark.parametrize("adjacent", [False, True])
def test_picker_uses_api_input_top_100_role_model_and_records_usage(tmp_path, monkeypatch, model, adjacent):
    usage_log = tmp_path / "usage.jsonl"
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("POWERPACKS_USAGE_LOG", str(usage_log))
    monkeypatch.setenv("POWERPACKS_USAGE_STAGE", "previous")
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
            "selected": ["chief staff"], "keywords": ["Chief of Staff"],
        })))], model="gpt-5.6-luna", usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
    )
    create = mock.AsyncMock(return_value=response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    groups = [{"stem": f"title {i}", "count": 120 - i,
               "titles": [f"Title {i}", "Second Title", "Third Title", "Fourth Title"]} for i in range(101)]
    with mock.patch.object(openai_client.openai, "AsyncOpenAI", return_value=client):
        result = asyncio.run(title_selection.pick_title_clusters("chief of staff", groups,
                                                               model=model, reasoning_effort="high",
                                                               **({"system_prompt": title_selection.ADJACENT_TITLE_SELECTION_PROMPT}
                                                                  if adjacent else {})))
    assert result.keywords == ["Chief of Staff"]
    request = create.call_args.kwargs
    assert request["model"] == (model or title_selection.EXTRACTOR_MODELS["role"])
    if model:
        assert request["reasoning_effort"] == "high"
    else:
        assert request["temperature"] == 0.0
        assert "reasoning_effort" not in request
    assert request["messages"][0]["content"] == (title_selection.ADJACENT_TITLE_SELECTION_PROMPT if adjacent
                                                  else title_selection.TITLE_SELECTION_PROMPT)
    content = request["messages"][1]["content"]
    assert content.startswith('Query: "chief of staff"\n\nTitle clusters (stemmed form, count, raw examples):\n')
    assert '  "title 0" (count=120, e.g.: Title 0, Second Title, Third Title)' in content
    assert '"title 99"' in content
    assert '"title 100"' not in content
    assert 'Fourth Title' not in content
    assert json.loads(usage_log.read_text())["stage"] == "expand"
    assert pipeline.os.environ["POWERPACKS_USAGE_STAGE"] == "previous"


@pytest.mark.parametrize("prepare", [False, True])
def test_local_title_read_uses_neighbouring_role_ids_with_other_filters(tmp_path, prepare):
    db = tmp_path / "local.duckdb"
    with duckdb.connect(str(db)) as conn:
        conn.execute("CREATE TABLE local_people_positions (id VARCHAR, person_id VARCHAR, "
                     "position_title VARCHAR, role_ids VARCHAR[], city VARCHAR)")
        conn.executemany("INSERT INTO local_people_positions VALUES (?, ?, ?, ?, ?)", [
            ("1", "p1", "AI Engineer", ["ai_engineer"], "Austin"),
            ("2", "p2", "Applied Scientist", ["ai_researcher"], "Austin"),
            ("3", "p3", "Sales Manager", ["sales_manager"], "Austin"),
            ("4", "p4", "Research Scientist", ["ai_researcher"], "Boston"),
        ])
    source = payload(cities=["Austin"])
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                           return_value=title_selection.TitleSelection(["appli scientist"], ["Applied Scientist"])) as pick, \
            mock.patch.object(pipeline.cloud_search.powerset_api, "call", side_effect=AssertionError("no API")):
        if prepare:
            args = pipeline.build_parser().parse_args([
                "prepare", "--backend", "local", "--db", str(db), "--query", "ai engineers",
                "--output-dir", str(tmp_path), "--expand-model", "synthetic-model",
                "--expand-reasoning-effort", "high",
            ])
            with mock.patch.object(pipeline, "_expand_query", return_value={"status": "completed", **source}), \
                    mock.patch.dict("os.environ"):
                assert pipeline.cmd_prepare(args) == 0
            result = json.loads((tmp_path / "expand_search_request.json").read_text())
        else:
            result = pipeline.apply_local_title_clustering(source, db, model="synthetic-model", reasoning_effort="high")
    groups = pick.call_args_list[0].args[1]
    assert {group["display_title"] for group in groups} == {"AI Engineer", "Applied Scientist"}
    assert pick.call_args_list[0].kwargs == {"model": "synthetic-model", "reasoning_effort": "high"}
    adjacent = pick.call_args_list[1]
    assert {group["display_title"] for group in adjacent.args[1]} == {"Applied Scientist"}
    assert adjacent.kwargs == {"model": "synthetic-model", "reasoning_effort": "high",
                               "system_prompt": title_selection.ADJACENT_TITLE_SELECTION_PROMPT}
    assert result["role_search_filters"]["bm25_queries"] == ["Senior AI Engineer", "Applied Scientist"]
    assert source == payload(cities=["Austin"])


def test_set_prepare_passes_resolved_groups_and_applies_edits_after_pick(tmp_path):
    expansion = {"status": "completed", **payload()}
    groups = [{"stem": "appli scientist", "count": 10, "titles": ["Applied Scientist"]}]
    adjacent_groups = [{"stem": "softwar engin", "count": 5, "titles": ["Software Engineer"]}]
    edits = tmp_path / "edits.json"
    edits.write_text(json.dumps({"filters": {"bm25_queries": ["Chief of Staff"]}}))
    args = pipeline.build_parser().parse_args([
        "prepare", "--query", "ai engineers", "--set-id", "s1", "--output-dir", str(tmp_path),
        "--expand-model", "synthetic-model", "--expand-reasoning-effort", "high",
        "--preserve-query-semantic", "--current-role", "--seniority-bands", "senior,staff",
        "--edits-json", str(edits),
    ])
    with mock.patch.object(pipeline, "_expand_query", return_value=expansion), \
            mock.patch.object(pipeline.cloud_search.powerset_api, "call", return_value={"title_clusters": groups,
                                                                                      "adjacent_title_clusters": adjacent_groups}), \
            mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                              side_effect=[title_selection.TitleSelection(["appli scientist"], ["Applied Scientist"]),
                                           title_selection.TitleSelection(["softwar engin"], ["Ignored"])]) as pick, \
            mock.patch.object(pipeline, "emit"), mock.patch.dict("os.environ"):
        assert pipeline.cmd_prepare(args) == 0
    assert pick.await_args_list == [
        mock.call("ai engineers", [{**groups[0], "display_title": "Applied Scientist"}],
                  model="synthetic-model", reasoning_effort="high"),
        mock.call("ai engineers", [{**adjacent_groups[0], "display_title": "Software Engineer"}],
                  model="synthetic-model", reasoning_effort="high",
                  system_prompt=title_selection.ADJACENT_TITLE_SELECTION_PROMPT),
    ]
    prepared = json.loads((tmp_path / "expand_search_request.json").read_text())
    filters = prepared["role_search_filters"]
    assert filters["bm25_queries"] == ["Chief of Staff"]
    assert filters["role_core_patterns"] == [{"regex": r"\b(chief\ of\ staff)\b", "examples": ["Chief of Staff"]}]
    assert filters["semantic_query"] == "ai engineers"
    assert filters["seniority_bands"] == ["senior", "staff"]
    assert filters["is_current_role"] is True
    assert filters["role_adjacent_patterns"] == [{
        "regex": r"\bsoftware\ engineer\b", "examples": ["Software Engineer"],
        "display_value": "Software Engineer",
    }]
    with mock.patch.object(pipeline.cloud_search.powerset_api, "call", side_effect=AssertionError("no lookup")):
        body = pipeline.cloud_search.api_request(None, prepared, query="ai engineers", set_id="s1", limit=0)
    assert body["role_adjacent_patterns"] == [{
        "regex": r"\bsoftware\ engineer\b", "examples": ["Software Engineer"],
    }]


def test_model_error_uses_agent_keywords_through_real_picker(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    create = mock.AsyncMock(side_effect=RuntimeError("synthetic failure"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with mock.patch.object(openai_client.openai, "AsyncOpenAI", return_value=client):
        result = pipeline._apply_title_clusters(payload(), GROUPS)
    filters = result["role_search_filters"]
    assert filters["bm25_queries"] == ["Senior AI Engineer"]
    assert filters["role_core_patterns"] == [{"regex": r"\b(ai\ engineer)\b", "examples": ["AI Engineer"]}]
    assert filters["local_title_clustering_status"]["fallback"] == "role_agent"
    assert filters["local_title_clustering_status"]["status"] == "error"
    create.assert_awaited_once()


ADJACENT_GROUPS = [{"stem": f"related {i}", "count": 20 - i,
                    "titles": [f"Related Title {i}", "Second Title", "Third Title", "Fourth Title"],
                    "display_title": f"Related Title {i}"} for i in range(17)]


def test_adjacent_patterns_use_selected_titles_cap_15_and_ignore_keywords():
    async def pick(query, groups, **kwargs):
        if "system_prompt" in kwargs:
            return title_selection.TitleSelection([g["stem"] for g in groups] + ["unknown"], ["Ignored Keyword"])
        return title_selection.TitleSelection(["appli scientist"], ["Applied Scientist"])

    with mock.patch.object(title_selection, "pick_title_clusters", side_effect=pick):
        result = pipeline._apply_title_clusters(payload(), GROUPS, adjacent_clusters=ADJACENT_GROUPS)
    filters = result["role_search_filters"]
    assert filters["role_adjacent_patterns"] == [{
        "regex": rf"\b{re.escape(group['display_title'].lower())}\b",
        "examples": group["titles"][:3], "display_value": group["display_title"],
    } for group in ADJACENT_GROUPS[:15]]
    assert filters["bm25_queries"] == ["Senior AI Engineer", "Applied Scientist"]
    assert re.search(filters["role_adjacent_patterns"][0]["regex"], "Related Title 0", re.I)


@pytest.mark.parametrize("failed", ["direct", "adjacent"])
def test_picks_run_concurrently_and_failed_calls_leave_no_adjacent_patterns(failed):
    started = set()

    async def pick(query, groups, **kwargs):
        kind = "adjacent" if "system_prompt" in kwargs else "direct"
        started.add(kind)
        for _ in range(5):
            await asyncio.sleep(0)
            if len(started) == 2:
                break
        assert started == {"direct", "adjacent"}
        if kind == failed:
            raise RuntimeError("synthetic failure")
        return title_selection.TitleSelection(
            ["related 0"] if kind == "adjacent" else ["appli scientist"], ["Applied Scientist"])

    with mock.patch.object(title_selection, "pick_title_clusters", side_effect=pick):
        result = pipeline._apply_title_clusters(payload(), GROUPS, adjacent_clusters=ADJACENT_GROUPS)
    filters = result["role_search_filters"]
    assert filters["role_adjacent_patterns"] == []
    assert filters["bm25_queries"] == (["Senior AI Engineer"] if failed == "direct"
                                        else ["Senior AI Engineer", "Applied Scientist"])


def test_adjacent_pick_runs_without_direct_groups():
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                           return_value=title_selection.TitleSelection(["related 0"], ["Ignored"])) as pick:
        result = pipeline._apply_title_clusters(payload(), [], adjacent_clusters=ADJACENT_GROUPS)
    pick.assert_awaited_once()
    assert pick.call_args.kwargs["system_prompt"] == title_selection.ADJACENT_TITLE_SELECTION_PROMPT
    assert len(result["role_search_filters"]["role_adjacent_patterns"]) == 1
    assert result["role_search_filters"]["bm25_queries"] == ["Senior AI Engineer"]


def test_no_adjacent_groups_means_no_adjacent_patterns():
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                           return_value=title_selection.TitleSelection([], ["Applied Scientist"])) as pick:
        result = pipeline._apply_title_clusters(payload(), GROUPS)
    pick.assert_awaited_once()
    assert result["role_search_filters"]["role_adjacent_patterns"] == []


def test_keyword_merge_deduplicates_case_insensitively_and_appends_fixed_titles():
    source = payload(role_agent_ids=["ai_researcher"], bm25_queries=["Chief of Staff", "CHIEF OF STAFF"])
    with mock.patch.object(title_selection, "pick_title_clusters", new_callable=mock.AsyncMock,
                           return_value=title_selection.TitleSelection([], ["chief of staff", "Applied Scientist"])):
        result = pipeline._apply_title_clusters(source, GROUPS)
    assert result["role_search_filters"]["bm25_queries"] == [
        "Chief of Staff", "Applied Scientist", "Research Fellow", "Research Scientist",
    ]

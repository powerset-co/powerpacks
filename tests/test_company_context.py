import tempfile
import unittest
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import company_context


def _response(name="Acme", headcount=120, stage="SERIES_A", amount="50000000"):
    return {"data": {
        "name": name, "staffCount": headcount, "universalName": name.casefold(),
        "website": f"https://{name.casefold()}.example",
        "fundingData": {"lastFundingRound": {
            "fundingType": stage,
            "moneyRaised": {"amount": amount, "currencyCode": "USD"},
        }},
    }}


class CompanyContextTests(unittest.TestCase):
    def test_hiring_company_ref_never_treats_a_job_board_org_as_linkedin_slug(self) -> None:
        self.assertEqual(
            company_context.hiring_company_ref(
                "Lovable", "https://jobs.ashbyhq.com/lovable/posting-id"),
            {"name": "Lovable", "slug": "", "company_id": "", "domain": ""},
        )
        self.assertEqual(
            company_context.hiring_company_ref(
                "Lovable", "https://lovable.dev/careers/design-engineer"),
            {"name": "Lovable", "slug": "", "company_id": "", "domain": "lovable.dev"},
        )

    def test_extracts_rapidapi_headcount_stage_and_latest_round(self) -> None:
        context = company_context.company_facts(_response())
        self.assertEqual(context["headcount"], 120)
        self.assertEqual(context["stage"], "SERIES_A")
        self.assertEqual(context["funding"], 50_000_000.0)

    def test_cache_first_then_live_miss_is_cached(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            company_context.rapidapi._write_cache(  # noqa: SLF001
                company_context.rapidapi._slug_cache_key("cached"), _response("Cached"), cache)  # noqa: SLF001
            refs = [
                {"name": "Cached", "slug": "cached", "company_id": "", "domain": ""},
                {"name": "Live", "slug": "live", "company_id": "", "domain": ""},
                {"name": "Live", "slug": "live", "company_id": "", "domain": ""},
            ]
            with mock.patch.object(company_context.rapidapi, "fetch_company_details_by_slug",
                                   return_value=_response("Live")) as fetch:
                contexts, stats = company_context.resolve_company_contexts(
                    refs, cache_dir=cache, api_key="key", unit_cost_usd=.01)

        fetch.assert_called_once()
        self.assertEqual([row["name"] for row in contexts], ["Cached", "Live", "Live"])
        self.assertEqual(stats, {"cache_hits": 1, "cache_misses": 1, "live_lookups": 1,
                                 "unresolved": 0, "cost_usd": .01, "unit_cost_usd": .01,
                                 "billing_basis": "configured_per_lookup"})

    def test_unknown_company_is_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            contexts, stats = company_context.resolve_company_contexts(
                [{"name": "Ambiguous Name", "slug": "", "company_id": "", "domain": ""}],
                cache_dir=raw, api_key="key")
        self.assertEqual(contexts, [{}])
        self.assertEqual(stats["live_lookups"], 0)
        self.assertEqual(stats["unresolved"], 1)

    def test_ambiguous_cached_name_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            company_context.rapidapi._write_cache("1", _response("Shared", 10), cache)  # noqa: SLF001
            second = _response("Shared", 500)
            second["data"]["universalName"] = "shared-two"
            company_context.rapidapi._write_cache("2", second, cache)  # noqa: SLF001
            contexts, _stats = company_context.resolve_company_contexts(
                [{"name": "Shared", "slug": "", "company_id": "", "domain": ""}],
                cache_dir=cache, api_key="")
        self.assertEqual(contexts, [{}])

    def test_display_labels_do_not_need_a_score(self) -> None:
        self.assertEqual(company_context.company_move({"headcount": 500}, {"headcount": 40}), "step-up")
        self.assertEqual(company_context.fit_label("Senior Engineer", "staff_ic"), "promising step-up")
        self.assertEqual(company_context.fit_label("Junior Software Engineer", "staff_ic"),
                         "junior — could grow")
        self.assertEqual(company_context.fit_label("Director of Engineering", "staff_ic"), "too-senior")

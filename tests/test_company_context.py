import json
import tempfile
import unittest
from datetime import date
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
        self.assertEqual(context["funding_basis"], "last_round")

    def test_uses_total_raised_when_rapidapi_supplies_it(self) -> None:
        response = _response()
        response["data"]["fundingData"]["totalFunding"] = {
            "amount": "386000000", "currencyCode": "USD"}
        context = company_context.company_facts(response)
        self.assertEqual(context["funding"], 386_000_000.0)
        self.assertEqual(context["funding_basis"], "total_raised")
        self.assertIn("total raised", company_context.pull_note(context))

    def test_between_jobs_company_is_labeled_last_known(self) -> None:
        ref = company_context.current_company_ref({
            "positions": [{"company_name": "Prior Co", "title": "Engineer"}],
        })
        self.assertEqual(ref["name"], "Prior Co")
        self.assertEqual(ref["company_timing"], "last-known")
        self.assertEqual(company_context.current_company_ref({}, "Prior Co")["company_timing"],
                         "last-known")

    def test_current_company_ref_includes_start_date_and_months_in_seat(self) -> None:
        ref = company_context.current_company_ref({"positions": [{
            "company_name": "Strong Co", "is_current": True,
            "start_date": "2026-01-01T00:00:00Z",
        }]}, as_of=date(2026, 8, 22))
        self.assertEqual(ref["current_position_start_date"], "2026-01-01T00:00:00Z")
        self.assertEqual(ref["months_in_seat"], 8)

    def test_name_resolution_accepts_only_an_exact_returned_name(self) -> None:
        exact = [{"company_name": "Firecrawl", "linkedin_url":
                  "https://www.linkedin.com/company/firecrawl"}]
        with mock.patch.object(company_context.company_search, "exact_name_lookup",
                               new=mock.AsyncMock(return_value=exact)):
            ref = company_context.resolve_hiring_company_ref(
                {"name": "Firecrawl", "website_url": None})
        self.assertEqual(ref["slug"], "firecrawl")
        self.assertEqual(ref["resolution_basis"], "verified_name")

    def test_source_domain_overrides_job_board_company_link(self) -> None:
        exact = [{"company_name": "Lovable", "website_domain": "lovable.dev",
                  "linkedin_url": "https://www.linkedin.com/company/lovable-dev"}]
        with mock.patch.object(company_context.company_search, "exact_domain_lookup",
                               new=mock.AsyncMock(return_value=exact)):
            ref = company_context.resolve_hiring_company_ref(
                {"name": "Lovable", "website_url": "https://jobs.ashbyhq.com/lovable/id"},
                "https://lovable.dev/careers/design-engineer")

        self.assertEqual(ref["domain"], "lovable.dev")
        self.assertEqual(ref["verified_domain"], "lovable.dev")
        self.assertEqual(ref["slug"], "lovable-dev")

    def test_known_domain_uses_company_site_link_when_directory_has_no_slug(self) -> None:
        html = '<a href="https://www.linkedin.com/company/lovable-dev">LinkedIn</a>'
        with mock.patch.object(company_context.company_search, "exact_domain_lookup",
                               new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(company_context, "fetch",
                                  return_value=(html, "https://lovable.dev")) as fetch:
            ref = company_context.resolve_hiring_company_ref(
                {"name": "Lovable", "website_url": "https://lovable.dev"})

        fetch.assert_called_once_with("https://lovable.dev")
        self.assertEqual(ref["slug"], "lovable-dev")
        self.assertEqual(ref["verified_domain"], "lovable.dev")

    def test_company_site_bridge_rejects_redirect_to_another_domain(self) -> None:
        html = '<a href="https://www.linkedin.com/company/wrong-company">LinkedIn</a>'
        with mock.patch.object(company_context.company_search, "exact_domain_lookup",
                               new=mock.AsyncMock(return_value=[])), \
                mock.patch.object(company_context, "fetch",
                                  return_value=(html, "https://other.example")):
            ref = company_context.resolve_hiring_company_ref(
                {"name": "Lovable", "website_url": "https://lovable.dev"})

        self.assertEqual(ref["slug"], "")

    def test_known_domain_rejects_same_name_company_on_another_domain(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            company_context.rapidapi._write_cache(  # noqa: SLF001
                company_context.rapidapi._slug_cache_key("lovable-solutions"),  # noqa: SLF001
                {"data": {"name": "Lovable", "universalName": "lovable-solutions",
                          "website": "https://lovable.solutions", "staffCount": 6}},
                cache,
            )
            contexts, stats = company_context.resolve_company_contexts([{
                "name": "Lovable", "slug": "lovable-solutions", "company_id": "",
                "domain": "lovable.dev", "verified_domain": "lovable.dev",
            }], cache_dir=cache, api_key="")

        self.assertEqual(contexts, [{}])
        self.assertEqual(stats["unresolved"], 1)

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

    def test_model_annotations_preserve_candidate_order_and_scores(self) -> None:
        candidates = [{"person": "p1", "score": .91}, {"person": "p2", "score": .72}]
        raw = '{"candidates":[' \
              '{"candidate_index":1,"level_read":"senior","move_plausibility":"too-senior","why":"Large step down.","pedigree_prior":"weak","pedigree_why":"Weak role-family evidence."},' \
              '{"candidate_index":0,"level_read":"mid","move_plausibility":"promising step-up","why":"Good scope step.","pedigree_prior":"strong","pedigree_why":"Strong role-family hiring bar."}]}'
        annotated = company_context.apply_company_fit_response(candidates, raw)
        self.assertEqual([row["person"] for row in annotated], ["p1", "p2"])
        self.assertEqual([row["score"] for row in annotated], [.91, .72])
        self.assertEqual(annotated[0]["move_plausibility"], "promising step-up")
        self.assertEqual(annotated[0]["pedigree_prior"], "strong")
        self.assertEqual(annotated[1]["pedigree_prior"], "weak")

    def test_company_fit_prompt_includes_tenure_and_wrong_timing(self) -> None:
        messages = company_context.company_fit_messages(
            jd="Synthetic JD", target_level="senior_ic",
            comp_band={"currency": "USD", "minimum": 140000, "maximum": 220000,
                       "period": "year", "evidence_quote": "Synthetic salary quote."},
            hiring_company={}, role_family="synthetic engineering",
            company_taste_precedents=[{
                "company": "Synthetic Product Co", "pedigree_prior": "strong",
                "why": "Hard role-relevant hiring bar.",
            }], candidates=[{
                "current_position_start_date": "2026-01-01T00:00:00Z",
                "months_in_seat": 8,
            }])
        self.assertIn('"months_in_seat": 8', messages[1]["content"])
        self.assertIn('"minimum": 140000', messages[1]["content"])
        self.assertIn("materially exceeds the posted band", messages[0]["content"])
        self.assertIn("wrong-timing", messages[0]["content"])
        self.assertIn('"role_family": "synthetic engineering"', messages[1]["content"])
        self.assertIn('"company_taste_precedents"', messages[1]["content"])
        self.assertIn("role-family-conditional", messages[0]["content"])
        self.assertIn("not merely by\nindustry overlap", messages[0]["content"])
        for company in ("Roche", "Coinbase", "Stripe"):
            self.assertNotIn(company, company_context.COMPANY_FIT_PROMPT)

    def test_reviewed_pedigree_override_stays_separate_from_move_label(self) -> None:
        candidates = [{
            "person": "p1", "company_taste_override": {
                "reviewed": True, "pedigree_prior": "weak", "why": "Human-reviewed role prior.",
            },
        }]
        raw = json.dumps({"candidates": [{
            "candidate_index": 0, "level_read": "senior", "move_plausibility": "in-band",
            "why": "Level fits.", "pedigree_prior": "strong",
            "pedigree_why": "Model prior.",
        }]})

        annotated = company_context.apply_company_fit_response(candidates, raw)

        self.assertEqual(annotated[0]["move_plausibility"], "in-band")
        self.assertEqual(annotated[0]["pedigree_prior"], "weak")
        self.assertEqual(annotated[0]["pedigree_annotation_source"], "human")

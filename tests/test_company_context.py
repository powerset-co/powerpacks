import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from packs.search.primitives.deep_search import company_context
from packs.search.primitives.deep_search.fit_contract import FitDimension


def _response(name="Acme", headcount=120, stage="SERIES_A", amount="50000000"):
    return {"data": {
        "name": name, "staffCount": headcount, "universalName": name.casefold(),
        "industries": ["Software Development"],
        "website": f"https://{name.casefold()}.example",
        "fundingData": {"lastFundingRound": {
            "fundingType": stage,
            "moneyRaised": {"amount": amount, "currencyCode": "USD"},
        }},
    }}


def _fit_experts(role_move="strong-fit", move="plausible", craft="strong"):
    return {
        FitDimension.ROLE_FIT.value: {
            "label": role_move,
            "why": "Level and role evidence line up.",
            "applied_precedent_ids": [],
        },
        FitDimension.COMPANY_TASTE.value: {
            "label": "strong", "why": "High-bar role-family employers.",
            "applied_precedent_ids": [],
        },
        FitDimension.CRAFT_AND_POTENTIAL.value: {
            "label": craft, "why": "Repeated high-quality individual work.",
            "applied_precedent_ids": [],
        },
        FitDimension.MOVE_FEASIBILITY.value: {
            "label": move, "why": "The move is plausible now.",
            "applied_precedent_ids": [],
        },
    }


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
        self.assertNotIn("industries", context)

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

    def test_verified_domain_does_not_use_same_name_cache_before_live_slug(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cache = Path(raw)
            company_context.rapidapi._write_cache(  # noqa: SLF001
                company_context.rapidapi._slug_cache_key("lovable-solutions"),  # noqa: SLF001
                {"data": {"name": "Lovable", "universalName": "lovable-solutions",
                          "website": "https://lovable.solutions", "staffCount": 6}},
                cache,
            )
            correct = {"data": {"name": "Lovable", "universalName": "lovable-dev",
                                "website": "https://lovable.dev", "staffCount": 137}}
            with mock.patch.object(company_context.rapidapi, "fetch_company_details_by_slug",
                                   return_value=correct) as fetch:
                contexts, stats = company_context.resolve_company_contexts([{
                    "name": "Lovable", "slug": "lovable-dev", "company_id": "",
                    "domain": "lovable.dev", "verified_domain": "lovable.dev",
                }], cache_dir=cache, api_key="key")

        fetch.assert_called_once()
        self.assertEqual(contexts[0]["domain"], "lovable.dev")
        self.assertEqual(contexts[0]["headcount"], 137)
        self.assertEqual(stats["live_lookups"], 1)

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

    def test_fallback_does_not_invent_fit_without_a_model_read(self) -> None:
        self.assertEqual(company_context.company_move({"headcount": 500}, {"headcount": 40}), "step-up")
        fallback = company_context.fallback_company_fit({"title": "Director of Engineering"})
        self.assertEqual(
            {row["label"] for row in fallback["fit_experts"].values()}, {"unclear"})

    def test_model_annotation_preserves_candidate_and_adds_fit(self) -> None:
        candidate = {"person": "p1", "score": .91}
        annotated = company_context.apply_company_fit_response(
            candidate, _fit_experts(),
            {"group": "send_worthy",
             "why": "Strong evidence and pedigree make the move plausible."})
        self.assertEqual(annotated["person"], "p1")
        self.assertEqual(annotated["score"], .91)
        self.assertEqual(annotated["group"], "send_worthy")
        self.assertEqual(annotated["fit_experts"], _fit_experts())

    def test_company_fit_panel_splits_independent_judgments(self) -> None:
        kwargs = {
            "jd": "Synthetic JD", "target_level": "senior_ic",
            "comp_band": {"currency": "USD", "minimum": 140000, "maximum": 220000,
                          "period": "year", "evidence_quote": "Synthetic salary quote."},
            "hiring_company": {},
            "brief": {"occupation": "synthetic engineering", "defining_capability": "systems"},
            "fit_precedents": [{
                "id": "selective-product", "dimension": "company_taste",
                "candidate_context": "Selective product environment",
                "judgment": {"label": "strong"},
                "reason": "Hard role-relevant hiring bar.",
            }],
            "candidate": {
                "current_role_ids": ["software_engineer"],
                "current_company_description": "Canonical company description.",
                "current_company_sector_types": ["infra_devtools"],
                "current_company_entity_types": ["venture_backed_startup"],
                "current_position_start_date": "2026-01-01T00:00:00Z",
                "months_in_seat": 8,
                "recent_roles": [{"title": "Engineer", "start_date": "2024-01-01"}],
                "education": [{"school": "Example University", "degree": "BS"}],
                "trait_scores": {"Software Engineer": {"score": .9, "reason": "Built systems."}},
            },
        }
        panels = {expert: company_context.company_fit_expert_messages(
            expert=expert, **kwargs) for expert in company_context.FIT_EXPERTS}
        role = panels[FitDimension.ROLE_FIT]
        company = panels[FitDimension.COMPANY_TASTE]
        craft = panels[FitDimension.CRAFT_AND_POTENTIAL]
        move = panels[FitDimension.MOVE_FEASIBILITY]
        self.assertIn('"months_in_seat": 8', move[1]["content"])
        self.assertIn('"minimum": 140000', move[1]["content"])
        self.assertIn("comp-mismatch", move[0]["content"])
        self.assertIn("wrong-timing", move[0]["content"])
        self.assertIn("destination-pull", move[0]["content"])
        self.assertIn("Missing compensation", move[0]["content"])
        self.assertIn("defining work", role[0]["content"])
        self.assertIn("seniority", role[0]["content"])
        self.assertIn("actual function", company[0]["content"])
        self.assertIn("industry overlap", company[0]["content"])
        self.assertIn("individual", craft[0]["content"])
        self.assertIn("specific JD and job family", craft[0]["content"])
        self.assertIn("role-appropriate", craft[0]["content"])
        self.assertIn("promising", craft[0]["content"])
        self.assertIn('"school": "Example University"', craft[1]["content"])
        self.assertIn('"start_date": "2024-01-01"', craft[1]["content"])
        self.assertIn('"occupation": "synthetic engineering"', role[1]["content"])
        self.assertIn('"fit_precedents"', company[1]["content"])
        self.assertIn('"pond_trait_scores"', role[1]["content"])
        company_payload = json.loads(company[1]["content"])
        self.assertEqual(company_payload["candidate"]["current_role_ids"], ["software_engineer"])
        self.assertEqual(company_payload["candidate"]["company_sector_types"], ["infra_devtools"])
        decision = company_context.company_fit_decision_messages(
            fit_experts=_fit_experts())
        self.assertEqual(set(json.loads(decision[1]["content"])),
                         {"fit_experts", "fit_precedents"})
        self.assertIn("never substitute", decision[0]["content"])
        for company in ("Roche", "Coinbase", "Stripe"):
            self.assertNotIn(company, "\n".join([
                company_context.ROLE_FIT_PROMPT, company_context.COMPANY_TASTE_PROMPT,
                company_context.CRAFT_POTENTIAL_PROMPT,
                company_context.MOVE_FEASIBILITY_PROMPT, company_context.COMPANY_FIT_PROMPT]))

    def test_applied_company_precedent_binds_the_label_and_group_gate(self) -> None:
        experts = _fit_experts()
        experts[FitDimension.COMPANY_TASTE.value]["applied_precedent_ids"] = [
            "support-function-software"]
        annotated = company_context.apply_company_fit_response(
            {"person": "p1"}, experts,
            {"group": "send_worthy", "why": "The candidate otherwise fits.",
             "applied_precedent_ids": []},
            {"company_taste": [{
                "id": "support-function-software", "dimension": "company_taste",
                "judgment": {"label": "weak"},
                "reason": "The employer is a weak role-family prior.",
                "retrieval_score": .72,
            }]})

        self.assertEqual(annotated["fit_experts"]["company_taste"]["label"], "weak")
        self.assertEqual(annotated["group"], "chat_worthy")
        self.assertEqual(annotated["applied_precedent_ids"], ["support-function-software"])

    def test_craft_evidence_gates_send_and_relationship_groups(self) -> None:
        weak = company_context.apply_company_fit_response(
            {"person": "p1"}, _fit_experts(craft="weak"),
            {"group": "send_worthy", "why": "The candidate otherwise fits."})
        unclear = company_context.apply_company_fit_response(
            {"person": "p2"}, _fit_experts(craft="unclear"),
            {"group": "wrong_timing_relationship", "why": "Build the relationship."})

        self.assertEqual(weak["group"], "passed")
        self.assertEqual(weak["why"], "Repeated high-quality individual work.")
        self.assertEqual(unclear["group"], "chat_worthy")
        self.assertEqual(unclear["fit_experts"]["craft_and_potential"]["label"], "unclear")

    def test_destination_pull_relationship_label_is_valid(self) -> None:
        annotated = company_context.apply_company_fit_response(
            {"person": "p1"}, _fit_experts(move="destination-pull"),
            {"group": "wrong_timing_relationship",
             "why": "The destination cannot pull this candidate today."})
        self.assertEqual(annotated["fit_experts"]["move_feasibility"]["label"],
                         "destination-pull")

    def test_role_vetoes_override_the_decision_group(self) -> None:
        too_senior = company_context.apply_company_fit_response(
            {"person": "p1"}, _fit_experts(role_move="too-senior", move="wrong-timing"),
            {"group": "wrong_timing_relationship", "why": "The move is poorly timed."})
        wrong_role = company_context.apply_company_fit_response(
            {"person": "p2"}, _fit_experts(role_move="wrong-role"),
            {"group": "send_worthy", "why": "The employer prior is strong."})

        self.assertEqual(too_senior["group"], "passed")
        self.assertEqual(too_senior["why"], "Level and role evidence line up.")
        self.assertEqual(wrong_role["group"], "passed")

    def test_generic_role_evidence_cannot_become_send_or_relationship(self) -> None:
        for decision_group in ("send_worthy", "wrong_timing_relationship"):
            annotated = company_context.apply_company_fit_response(
                {"person": "p1"}, _fit_experts(role_move="junior-could-grow"),
                {"group": decision_group, "why": "The employer prior is strong."})
            self.assertEqual(annotated["group"], "chat_worthy")
            self.assertEqual(annotated["why"], "Level and role evidence line up.")

    def test_missing_hiring_company_facts_are_not_sent_to_experts(self) -> None:
        messages = company_context.company_fit_expert_messages(
            expert=FitDimension.MOVE_FEASIBILITY, jd="Synthetic JD",
            target_level="senior_ic", comp_band=None,
            hiring_company={"name": "Acme", "stage": None,
                            "funding": None, "pull_note": "stage unavailable"},
            candidate={}, brief={})
        payload = json.loads(messages[1]["content"])
        self.assertEqual(payload["hiring_company"], {"name": "Acme"})

    def test_reviewed_fit_override_replaces_model_group(self) -> None:
        candidate = {
            "person": "p1", "fit_override": {
                "reviewed": True, "group": "passed",
                "why": "Human reviewed this as the wrong fit.",
            },
        }
        annotated = company_context.apply_company_fit_response(
            candidate, _fit_experts(),
            {"group": "send_worthy", "why": "The candidate has direct role evidence."})

        self.assertEqual(annotated["fit_experts"]["role_fit"]["label"], "strong-fit")
        self.assertEqual(annotated["group"], "passed")
        self.assertEqual(annotated["fit_annotation_source"], "human")

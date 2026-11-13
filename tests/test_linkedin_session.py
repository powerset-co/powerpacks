"""linkedin_session: the budget, the cache, the request shapes and the parsers, with no browser."""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from packs.ingestion.primitives.discover.linkedin import linkedin_session as ls
from packs.ingestion.primitives.setup.automations.shell import CommandResult

MODULE = "packs.ingestion.primitives.discover.linkedin.linkedin_session"
PROFILE_URN = "urn:li:fsd_profile:ACoAAAeoARABWDyz1EU75HBBvc_rK3ACMCPCkVc"


def clusters(total, names):
    return json.dumps({"data": {"paging": {"total": total}}, "included": [
        {"$type": "com.linkedin.voyager.dash.search.EntityResultViewModel", "title": {"text": name},
         "primarySubtitle": {"text": "Builder"}, "secondarySubtitle": {"text": "SF"}, "navigationUrl": f"https://www.linkedin.com/in/{name.lower()}?x=1"}
        for name in names]})


class FakeBrowser:
    """Answers each plan request from `answers` by name prefix; counts requests like the runner."""

    def __init__(self, answers):
        self.answers, self.plans = answers, []

    def __call__(self, cmd, *, timeout, env=None, on_progress=None):
        plan_path, out_path = Path(cmd[cmd.index("--plan") + 1]), Path(cmd[cmd.index("--out") + 1])
        plan = json.loads(plan_path.read_text())
        self.plans.append([request["name"] for request in plan])
        results = [{"name": request["name"], **self.answers(request)} for request in plan]
        out_path.write_text(json.dumps(results))
        return CommandResult(returncode=0, stdout=json.dumps({"status": "ok", "requests": len(results)}), stderr="", ok=True)


def with_folder(test):
    def wrapped(self):
        with tempfile.TemporaryDirectory() as raw:
            folder = Path(raw) / "linkedin"
            folder.mkdir()
            profile = Path(raw) / "profile"
            profile.mkdir()
            (profile / "Default").mkdir()
            with patch(f"{MODULE}.ensure_chromium", return_value={"status": "ok", "node_path": "/tmp/node_modules"}), \
                    patch(f"{MODULE}.USAGE", folder / "usage.json"):
                test(self, folder, profile)
    return wrapped


class MutualsTests(unittest.TestCase):
    @with_folder
    def test_looks_up_ids_then_counts_and_caches(self, folder, profile):
        (folder / "Connections.csv").write_text("First Name,Last Name,URL\nA,B,https://www.linkedin.com/in/friend\n")
        browser = FakeBrowser(lambda r: {"status": 200, "text": PROFILE_URN} if r["name"].startswith("profile")
                              else {"status": 200, "text": clusters(3, ["Ann", "Bob", "Cy"])})
        out = ls.mutuals(["https://www.linkedin.com/in/friend", "https://www.linkedin.com/in/stranger/", "nonsense"],
                         folder=folder, usage_path=folder / "usage.json", profile_dir=profile, runner=browser)
        by = {row["url"]: row for row in out["rows"]}
        self.assertEqual(by["https://www.linkedin.com/in/friend"]["status"], "connected")
        self.assertEqual(by["nonsense"]["status"], "no_slug")
        stranger = by["https://www.linkedin.com/in/stranger/"]
        self.assertEqual((stranger["status"], stranger["count"], [p["name"] for p in stranger["people"]]), ("looked_up", 3, ["Ann", "Bob", "Cy"]))
        self.assertEqual(stranger["people"][0]["url"], "https://www.linkedin.com/in/ann")
        self.assertEqual(browser.plans, [["profile stranger"], ["mutuals stranger"]])
        self.assertEqual(out["requests_today"], 2)
        # The second ask costs nothing: the answer is cached and the id is kept.
        again = ls.mutuals(["https://www.linkedin.com/in/stranger"], folder=folder, usage_path=folder / "usage.json", profile_dir=profile, runner=browser)
        self.assertEqual((again["rows"][0]["status"], again["rows"][0]["count"], again["requests_today"]), ("cached", 3, 2))
        self.assertEqual(len(browser.plans), 2)

    @with_folder
    def test_budget_bounds_the_batch(self, folder, profile):
        ls.spend(ls.DAY_CAP - 3, folder / "usage.json")
        browser = FakeBrowser(lambda r: {"status": 200, "text": PROFILE_URN} if r["name"].startswith("profile") else {"status": 200, "text": clusters(1, ["Ann"])})
        out = ls.mutuals([f"https://www.linkedin.com/in/p{i}" for i in range(4)], folder=folder, usage_path=folder / "usage.json", profile_dir=profile, runner=browser)
        statuses = [row["status"] for row in out["rows"]]
        # 3 requests left = one person at 2 requests; the rest wait for tomorrow.
        self.assertEqual(statuses, ["looked_up", "over_budget", "over_budget", "over_budget"])
        self.assertEqual(out["requests_today"], ls.DAY_CAP - 1)

    @with_folder
    def test_slow_down_keeps_what_came_back_and_spends_it(self, folder, profile):
        def answers(cmd, *, timeout, env=None, on_progress=None):
            out_path = Path(cmd[cmd.index("--out") + 1])
            out_path.write_text(json.dumps([{"name": "profile p0", "status": 999, "text": ""}]))
            return CommandResult(returncode=0, stdout=json.dumps({"status": "slow_down", "requests": 1, "stopped_at": "profile p0"}), stderr="", ok=True)
        out = ls.mutuals(["https://www.linkedin.com/in/p0", "https://www.linkedin.com/in/p1"], folder=folder, usage_path=folder / "usage.json", profile_dir=profile, runner=answers)
        self.assertTrue(out["status"].startswith("slow_down"))
        # The day is over: the budget is spent, so the next ask sends nothing.
        self.assertEqual(out["requests_today"], ls.DAY_CAP)
        self.assertEqual([row["status"] for row in out["rows"]], ["not_answered", "not_answered"])
        self.assertEqual(out["rows"][0]["error"], "profile HTTP 999")
        # The error is not kept as the person's answer: nothing is cached.
        self.assertEqual(json.loads((folder / "mutuals.json").read_text()), {})
        again = ls.mutuals(["https://www.linkedin.com/in/p0"], folder=folder, usage_path=folder / "usage.json", profile_dir=profile,
                           runner=FakeBrowser(lambda r: self.fail("no request after a slow down")))
        self.assertEqual(again["rows"][0]["status"], "over_budget")

    @with_folder
    def test_a_run_that_reports_nothing_spends_its_whole_plan(self, folder, profile):
        def timed_out(cmd, *, timeout, env=None, on_progress=None):
            return CommandResult(returncode=124, stdout="", stderr="node timed out", ok=False)
        with self.assertRaises(RuntimeError):
            ls.run_plan([{"name": "a", "url": "u"}, {"name": "b", "url": "u"}], profile_dir=profile, usage_path=folder / "usage.json", runner=timed_out)
        self.assertEqual(ls.spent_today(folder / "usage.json"), 2)

    def test_the_budget_is_one_file_in_the_home_folder(self):
        self.assertEqual(ls.USAGE, Path.home() / ".powerpacks/linkedin-usage.json")
        self.assertTrue(ls.FOLDER.is_absolute())

    @with_folder
    def test_open_profile_refuses(self, folder, profile):
        (profile / "SingletonLock").symlink_to("host-123")  # as Chrome writes it: a link to no file
        with self.assertRaisesRegex(RuntimeError, "open"):
            ls.run_plan([{"name": "x", "url": "u"}], profile_dir=profile, usage_path=folder / "usage.json", runner=FakeBrowser(lambda r: {}))


class SalesNavTests(unittest.TestCase):
    def test_urls_match_the_cloud_client(self):
        url = ls.lead_search_url([ls.relationship_filter("S"), ls.company_filter([(2135371, "Stripe")]), ls.title_filter([(35, "Founder")])],
                                 start=25, count=99, keywords="ai & ml")
        self.assertIn("(type:RELATIONSHIP,values:List((id:S,text:2nd%20degree%20connections,selectionType:INCLUDED)))", url)
        self.assertIn("(type:CURRENT_COMPANY,values:List((id:urn%3Ali%3Aorganization%3A2135371,text:Stripe,selectionType:INCLUDED,parent:(id:0))))", url)
        self.assertIn("(type:CURRENT_TITLE,values:List((id:35,text:Founder,selectionType:INCLUDED)))", url)
        self.assertIn(",keywords:ai%20%26%20ml)&start=25&count=25&decorationId=com.linkedin.sales.deco.desktop.searchv2.LeadSearchResult-14", url)
        self.assertEqual(ls.enum_filter("SENIORITY_LEVEL", ls.SENIORITY_LEVELS, ["310"]), "(type:SENIORITY_LEVEL,values:List((id:310,text:CXO,selectionType:INCLUDED)))")
        with self.assertRaisesRegex(ValueError, "unknown function"):
            ls.enum_filter("FUNCTION", ls.FUNCTIONS, ["99"])

    def test_member_id_decodes_like_the_cloud(self):
        self.assertEqual(ls.decode_member_id("ACwAAARv15kBchRDx6eACJ-o_62vF3lvBgTmSKg"), 74438553)
        self.assertEqual(ls.profile_id_in("urn:li:fs_salesProfile:(ACwAAARv15kBchRDx6eACJ-o_62vF3lvBgTmSKg,NAME_SEARCH,abc)"), "ACwAAARv15kBchRDx6eACJ-o_62vF3lvBgTmSKg")
        self.assertIsNone(ls.member_id_in("urn:li:fs_salesProfile:(nope,x)"))

    def test_lead_parses_with_mutual_badge(self):
        mutual = base64.urlsafe_b64encode(b"\x00\x2c" + (4242).to_bytes(6, "big") + b"\x01" + b"h" * 20).decode().rstrip("=")
        lead = ls.parse_lead({"firstName": "Ada", "lastName": "L", "degree": 2, "geoRegion": "SF Bay Area",
                              "entityUrn": "urn:li:fs_salesProfile:(ACwAAARv15kBchRDx6eACJ-o_62vF3lvBgTmSKg,NAME_SEARCH,x)",
                              "objectUrn": "urn:li:member:74438553",
                              "currentPositions": [{"title": "Investor", "companyName": "Stripe", "companyUrnResolutionResult": {"entityUrn": "urn:li:fs_salesCompany:2135371"}}],
                              "spotlightBadges": [{"id": "SECOND_DEGREE_CONNECTION", "displayValue": "1,204 mutual connections",
                                                   "popup": {"message": {"text": "You both know Sam"}},
                                                   "associatedEntityUrnsUnions": [{"profileUrn": f"urn:li:fs_salesProfile:({mutual},x,y)"}]}]})
        self.assertEqual((lead["member_id"], lead["profile_id"][:6], lead["title"], lead["company_name"], lead["total_mutual_count"], lead["mutual_member_ids"], lead["mutual_intro_text"]),
                         (74438553, "ACwAAA", "Investor", "Stripe", 1204, [4242], "You both know Sam"))

    def test_typeahead_parses_both_shapes(self):
        companies = ls.parse_typeahead("COMPANY_WITH_LIST", {"text": json.dumps({"elements": [{"displayValue": "Companies", "children": [
            {"id": "urn:li:organization:2135371", "displayValue": "Stripe", "employeeCountRange": "5001-10,000 employees", "address": {"city": "South San Francisco"}}]}]})})
        self.assertEqual(companies, [{"id": 2135371, "name": "Stripe", "headcount": "5001-10,000 employees", "location": "South San Francisco"}])
        titles = ls.parse_typeahead("TITLE", {"text": json.dumps({"elements": [{"id": 35, "displayValue": "Founder"}]})})
        self.assertEqual(titles[0], {"id": 35, "name": "Founder", "headcount": None, "location": None})

    @with_folder
    def test_search_goes_through_one_request_and_returns_the_cloud_shape(self, folder, profile):
        browser = FakeBrowser(lambda r: {"status": 200, "text": json.dumps({"paging": {"total": 430}, "elements": [
            {"firstName": "Ada", "lastName": "L", "degree": 2, "entityUrn": "urn:li:fs_salesProfile:(ACwAAARv15kBchRDx6eACJ-o_62vF3lvBgTmSKg,x,y)", "currentPositions": []}]})})
        args = Namespace(degree="2nd", company_id=[2135371], company_name=["Stripe"], title_id=None, title_name=None, region_id=None, region_name=None,
                         seniority=None, function=["8"], keywords="engineer", start=0, count=10)
        out = ls.sales_nav("search", args, usage_path=folder / "usage.json", profile_dir=profile, runner=browser)
        self.assertEqual((out["total"], out["count"], out["leads"][0]["member_id"], out["requests_today"]), (430, 1, 74438553, 1))
        self.assertEqual(browser.plans, [["lead search 0"]])
        self.assertIn("(type:FUNCTION,values:List((id:8,text:Engineering,selectionType:INCLUDED)))", out["filters"][2])

    @with_folder
    def test_several_searches_share_one_browser_run(self, folder, profile):
        browser = FakeBrowser(lambda r: {"status": 200, "text": json.dumps({"paging": {"total": 7}, "elements": []})})
        base = dict(degree="2nd", company_id=[2135371], company_name=["Stripe"], title_id=None, title_name=None, region_id=None,
                    region_name=None, seniority=None, function=None, keywords=None, start=0, count=25)
        out = ls.sales_nav_searches([Namespace(**base), Namespace(**{**base, "seniority": ["310", "320"]})], usage_path=folder / "usage.json", profile_dir=profile, runner=browser)
        self.assertEqual(browser.plans, [["lead search 0", "lead search 1"]])
        self.assertEqual([o["total"] for o in out], [7, 7])
        self.assertIn("SENIORITY_LEVEL", out[1]["filters"][2])
        self.assertEqual(out[1]["requests_today"], 2)

    def test_cli_reports_a_spent_budget_as_failed(self):
        with tempfile.TemporaryDirectory() as raw:
            with patch(f"{MODULE}.emit") as emitted, patch(f"{MODULE}.budget", return_value=0):
                code = ls.main(["--folder", raw, "sales-nav", "companies", "Stripe"])
            self.assertEqual(code, 1)
            self.assertIn("budget", emitted.call_args.args[0]["message"])


if __name__ == "__main__":
    unittest.main()

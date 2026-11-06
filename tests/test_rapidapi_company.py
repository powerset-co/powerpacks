import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from packs.indexing.primitives.enrich_companies_checkpointed import rapidapi_company


def _fake_response(status: int, body: str, headers: dict[str, str] | None = None) -> mock.Mock:
    res = mock.Mock()
    res.status = status
    res.read.return_value = body.encode("utf-8")
    res.getheader.side_effect = lambda name, default=None: (headers or {}).get(name, default)
    return res


def _fake_connection(response: mock.Mock) -> mock.Mock:
    conn = mock.Mock()
    conn.getresponse.return_value = response
    return conn


class FetchCompanyDetailsRetryTests(unittest.TestCase):
    MODULE = "packs.indexing.primitives.enrich_companies_checkpointed.rapidapi_company"

    def _fetch(self, responses: list[mock.Mock], cache_dir: Path, **kwargs) -> tuple[dict, mock.Mock, mock.Mock]:
        connections = [_fake_connection(res) for res in responses]
        with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", side_effect=connections) as conn_cls, \
                mock.patch(f"{self.MODULE}.time.sleep") as sleep:
            result = rapidapi_company.fetch_company_details(
                "123", api_key="test-key", cache_dir=cache_dir, **kwargs
            )
        for conn in connections[: conn_cls.call_count]:
            conn.close.assert_called_once()
        return result, conn_cls, sleep

    def test_retries_on_429_then_succeeds_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            responses = [
                _fake_response(429, "rate limited"),
                _fake_response(200, json.dumps({"data": {"name": "Acme"}})),
            ]
            result, conn_cls, sleep = self._fetch(responses, cache_dir)
            self.assertEqual(result, {"data": {"name": "Acme"}})
            self.assertEqual(conn_cls.call_count, 2)
            self.assertEqual(sleep.call_count, 1)
            cached = json.loads((cache_dir / "123.json").read_text(encoding="utf-8"))
            self.assertEqual(cached, {"data": {"name": "Acme"}})

    def test_does_not_retry_on_404_and_does_not_cache(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            responses = [_fake_response(404, "not found")]
            result, conn_cls, sleep = self._fetch(responses, cache_dir)
            self.assertEqual(result, {"error": "HTTP 404", "body": "not found"})
            self.assertEqual(conn_cls.call_count, 1)
            sleep.assert_not_called()
            self.assertFalse((cache_dir / "123.json").exists())

    def test_exhausts_attempts_on_5xx_and_returns_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            responses = [_fake_response(500, "boom")] * 3
            result, conn_cls, sleep = self._fetch(responses, cache_dir)
            self.assertEqual(result, {"error": "HTTP 500", "body": "boom"})
            self.assertEqual(conn_cls.call_count, 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertFalse((cache_dir / "123.json").exists())

    def test_retries_on_connection_exception_then_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            failing_conn = mock.Mock()
            failing_conn.request.side_effect = OSError("connection reset")
            ok_conn = _fake_connection(_fake_response(200, json.dumps({"data": {"name": "Acme"}})))
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", side_effect=[failing_conn, ok_conn]) as conn_cls, \
                    mock.patch(f"{self.MODULE}.time.sleep") as sleep:
                result = rapidapi_company.fetch_company_details("123", api_key="test-key", cache_dir=cache_dir)
            self.assertEqual(result, {"data": {"name": "Acme"}})
            self.assertEqual(conn_cls.call_count, 2)
            self.assertEqual(sleep.call_count, 1)
            failing_conn.close.assert_called_once()
            ok_conn.close.assert_called_once()

    def test_429_honors_numeric_retry_after_header_capped(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            responses = [
                _fake_response(429, "rate limited", headers={"Retry-After": "30"}),
                _fake_response(200, json.dumps({"data": {}})),
            ]
            result, _, sleep = self._fetch(responses, cache_dir)
            self.assertEqual(result, {"data": {}})
            sleep.assert_called_once_with(10.0)

    def test_cache_hit_skips_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "123.json").write_text(json.dumps({"data": {"name": "Cached"}}), encoding="utf-8")
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection") as conn_cls:
                result = rapidapi_company.fetch_company_details("123", api_key="test-key", cache_dir=cache_dir)
            self.assertEqual(result, {"data": {"name": "Cached"}})
            conn_cls.assert_not_called()

    def test_uses_powerset_gateway_and_one_year_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            response = _fake_response(200, json.dumps({"data": {"name": "Acme"}}))
            conn = _fake_connection(response)
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", return_value=conn) as conn_cls:
                rapidapi_company.fetch_company_details("123", api_key="powerset-key", cache_dir=Path(td))

        conn_cls.assert_called_once_with("proxy.powerset.dev", timeout=rapidapi_company.DEFAULT_TIMEOUT)
        self.assertEqual(
            conn.request.call_args.args[1],
            "/vendor/professional-network-data/get-company-details-by-id?id=123",
        )
        headers = conn.request.call_args.kwargs["headers"]
        self.assertEqual(headers["x-powerset-key"], "powerset-key")
        self.assertEqual(headers["X-Freshness"], "31536000")
        self.assertNotIn("x-rapidapi-key", headers)
        self.assertNotIn("x-rapidapi-host", headers)


class FetchCompanyDetailsBySlugTests(unittest.TestCase):
    MODULE = "packs.indexing.primitives.enrich_companies_checkpointed.rapidapi_company"

    def test_fetch_by_slug_hits_username_endpoint_and_caches_namespaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            conn = _fake_connection(_fake_response(200, json.dumps({"data": {"name": "Acme"}})))
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", return_value=conn):
                result = rapidapi_company.fetch_company_details_by_slug("acme-inc", api_key="k", cache_dir=cache_dir)
            self.assertEqual(result, {"data": {"name": "Acme"}})
            self.assertEqual(
                conn.request.call_args.args[1],
                "/vendor/professional-network-data/get-company-details?username=acme-inc",
            )
            headers = conn.request.call_args.kwargs["headers"]
            self.assertEqual(headers["x-powerset-key"], "k")
            self.assertEqual(headers["X-Freshness"], "31536000")
            # cached under a slug-namespaced key so it never collides with ids
            self.assertTrue((cache_dir / "slug__acme-inc.json").exists())

    def test_by_slug_cache_hit_skips_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "slug__acme-inc.json").write_text(json.dumps({"data": {"name": "Cached"}}), encoding="utf-8")
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection") as conn_cls:
                result = rapidapi_company.fetch_company_details_by_slug("acme-inc", api_key="k", cache_dir=cache_dir)
            self.assertEqual(result, {"data": {"name": "Cached"}})
            conn_cls.assert_not_called()

    def test_no_key_returns_error_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict("os.environ", {"POWERSET_API_KEY": ""}, clear=True), \
                    mock.patch(f"{self.MODULE}.http.client.HTTPSConnection") as conn_cls:
                result = rapidapi_company.fetch_company_details_by_slug("acme-inc", api_key="", cache_dir=Path(td))
            self.assertIn("error", result)
            conn_cls.assert_not_called()


class CachedCompanyBatchTests(unittest.TestCase):
    def test_cached_id_reads_overlap_and_deduplicate(self) -> None:
        active = 0
        max_active = 0
        lock = threading.Lock()

        def delayed_read(key: str, _cache_dir: object = None) -> dict:
            nonlocal active, max_active
            with lock:
                active += 1
                max_active = max(max_active, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"data": {"name": key}}

        with mock.patch.object(rapidapi_company, "_read_cache", side_effect=delayed_read):
            result = rapidapi_company.load_cached_company_details(["1", "2", "1", "3"])

        self.assertGreater(max_active, 1)
        self.assertEqual(list(result), ["1", "2", "3"])
        self.assertEqual(result["2"], {"data": {"name": "2"}})

    def test_cached_slug_reads_return_original_slug_keys(self) -> None:
        def read_slug_key(key: str, _cache_dir: object = None) -> dict:
            return {"data": {"cache_key": key}}

        with mock.patch.object(rapidapi_company, "_read_cache", side_effect=read_slug_key):
            result = rapidapi_company.load_cached_company_details_by_slug(["Acme", "Beta"])

        self.assertEqual(list(result), ["Acme", "Beta"])
        self.assertEqual(result["Acme"]["data"]["cache_key"], "slug__acme")

    def test_fetch_batch_only_sends_cache_misses_to_network(self) -> None:
        def read_cache(key: str, _cache_dir: object = None) -> dict | None:
            return {"data": {"name": "Cached"}} if key in {"cached", "slug__cached"} else None

        for batch, fetch_name in (
            (rapidapi_company.fetch_company_details_batch, "fetch_company_details"),
            (rapidapi_company.fetch_company_details_batch_by_slug, "fetch_company_details_by_slug"),
        ):
            with self.subTest(batch=batch.__name__), \
                    mock.patch.object(rapidapi_company, "_read_cache", side_effect=read_cache), \
                    mock.patch.object(rapidapi_company._RATE_LIMITER, "wait"), \
                    mock.patch.object(rapidapi_company.sys, "stderr", new_callable=io.StringIO) as log, \
                    mock.patch.object(rapidapi_company, fetch_name,
                                      return_value={"data": {"name": "Fetched"}}) as fetch:
                result = batch(["cached", "missing", "missing"], api_key="test-key", max_workers=2)

            self.assertEqual(set(result), {"cached", "missing"})
            fetch.assert_called_once()
            self.assertEqual(fetch.call_args.args[0], "missing")
            self.assertEqual(log.getvalue().splitlines(), [
                f"[enrich-companies] fetching 1 company profiles at {rapidapi_company.DEFAULT_COMPANY_MAX_RPM:g} rpm",
                "[enrich-companies] fetched 1/1 company profiles",
            ])


class FailedLookupTests(unittest.TestCase):
    """The API answers a failed lookup with HTTP 200 and ``success: false``. That is
    not an answer about the company, so it is never cached and a cached one is a miss."""

    MODULE = FetchCompanyDetailsRetryTests.MODULE
    FAILED = {"data": None, "message": "The request failed. You will not be charged for this request", "success": False}

    def test_a_failed_lookup_is_returned_but_never_cached(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            conn = _fake_connection(_fake_response(200, json.dumps(self.FAILED)))
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", return_value=conn):
                result = rapidapi_company.fetch_company_details("123", api_key="test-key", cache_dir=cache_dir)
                by_slug = rapidapi_company.fetch_company_details_by_slug("acme", api_key="test-key", cache_dir=cache_dir)
            self.assertEqual(result, self.FAILED)
            self.assertEqual(by_slug, self.FAILED)
            self.assertEqual(list(cache_dir.iterdir()), [])

    def test_a_cached_failure_is_a_miss_and_is_fetched_again(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "123.json").write_text(json.dumps(self.FAILED), encoding="utf-8")
            self.assertIsNone(rapidapi_company._read_cache("123", cache_dir))
            self.assertEqual(rapidapi_company.load_cached_company_details(["123"], cache_dir=cache_dir), {})
            conn = _fake_connection(_fake_response(200, json.dumps({"data": {"name": "Acme"}})))
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", return_value=conn):
                result = rapidapi_company.fetch_company_details("123", api_key="test-key", cache_dir=cache_dir)
            self.assertEqual(result, {"data": {"name": "Acme"}})
            self.assertEqual(json.loads((cache_dir / "123.json").read_text(encoding="utf-8")), {"data": {"name": "Acme"}})

    def test_a_company_gone_for_good_is_cached_and_never_bought_again(self) -> None:
        gone_id = {"data": None, "message": "This company is no longer exists. Not valid LinkedIn profile", "success": False}
        gone_slug = {"data": None, "message": "Not valid LinkedIn company username provided", "success": False}
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            conns = [_fake_connection(_fake_response(200, json.dumps(gone_id))),
                     _fake_connection(_fake_response(200, json.dumps(gone_slug)))]
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", side_effect=conns):
                rapidapi_company.fetch_company_details_batch(["123"], api_key="test-key", cache_dir=cache_dir)
                rapidapi_company.fetch_company_details_batch_by_slug(["gone"], api_key="test-key", cache_dir=cache_dir)
            # Readers still see no payload; the next run asks for neither.
            self.assertEqual(rapidapi_company.load_cached_company_details(["123"], cache_dir=cache_dir), {})
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection") as conn_cls:
                self.assertEqual(rapidapi_company.fetch_company_details_batch(["123"], api_key="test-key", cache_dir=cache_dir), {})
                self.assertEqual(rapidapi_company.fetch_company_details_batch_by_slug(["gone"], api_key="test-key", cache_dir=cache_dir), {})
            conn_cls.assert_not_called()


class CompanyLogoTests(unittest.TestCase):
    def test_prefers_the_200px_logo_then_images_logo(self) -> None:
        details = {"data": {"name": "Acme", "logos": [
            {"url": "https://media.licdn.com/x/company-logo_100_100/0/1", "width": 100, "height": 100},
            {"url": "https://media.licdn.com/x/company-logo_200_200/0/1", "width": 200, "height": 200},
            {"url": "https://media.licdn.com/x/company-logo_400_400/0/1", "width": 400, "height": 400},
        ]}}
        self.assertEqual(rapidapi_company.extract_company_context(details)["logo_url"], "https://media.licdn.com/x/company-logo_200_200/0/1")
        search_shape = {"logo": [{"url": "https://media.licdn.com/x/company-logo_400_400/0/2"}]}
        self.assertEqual(rapidapi_company.company_logo_url(search_shape), "https://media.licdn.com/x/company-logo_400_400/0/2")
        self.assertEqual(rapidapi_company.company_logo_url({"Images": {"logo": "https://media.licdn.com/x/company-logo_200_200/0/3"}}),
                         "https://media.licdn.com/x/company-logo_200_200/0/3")
        self.assertNotIn("logo_url", rapidapi_company.extract_company_context({"data": {"name": "No logo", "logos": [], "Images": {}}}))
        self.assertEqual(rapidapi_company.extract_company_context({"error": "nope"}), {})

    def test_an_expired_logo_link_is_no_logo(self) -> None:
        past = int(time.time()) - 86400
        future = int(time.time()) + 86400
        expired = f"https://media.licdn.com/x/company-logo_200_200/0/1/logo?e={past}&v=beta&t=sig"
        live = f"https://media.licdn.com/x/company-logo_200_200/0/1/logo?e={future}&v=beta&t=sig"
        self.assertEqual(rapidapi_company.live_logo_url(expired), "")
        self.assertEqual(rapidapi_company.live_logo_url(live), live)
        self.assertEqual(rapidapi_company.live_logo_url("https://x/own.png"), "https://x/own.png")
        self.assertEqual(rapidapi_company.live_logo_url(None), "")
        self.assertNotIn("logo_url", rapidapi_company.extract_company_context({"data": {"logos": [{"url": expired, "width": 200}]}}))
        self.assertTrue(rapidapi_company.logo_expired({"data": {"logos": [{"url": expired, "width": 200}]}}))
        self.assertFalse(rapidapi_company.logo_expired({"data": {"logos": [{"url": live, "width": 200}]}}))
        self.assertFalse(rapidapi_company.logo_expired({"data": {"logos": [], "Images": {"logo": ""}}}))

    def test_a_cached_payload_with_an_expired_logo_is_bought_again(self) -> None:
        past = int(time.time()) - 86400
        future = int(time.time()) + 86400
        stale = {"success": True, "data": {"staffCount": 7, "logos": [{"url": f"https://media.licdn.com/x/old?e={past}", "width": 200}]}}
        fresh = {"success": True, "data": {"staffCount": 8, "logos": [{"url": f"https://media.licdn.com/x/new?e={future}", "width": 200}]}}
        live = {"success": True, "data": {"staffCount": 9, "logos": [{"url": f"https://media.licdn.com/x/live?e={future}", "width": 200}]}}
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "1.json").write_text(json.dumps(stale), encoding="utf-8")
            (cache_dir / "2.json").write_text(json.dumps(live), encoding="utf-8")
            (cache_dir / "slug__acme.json").write_text(json.dumps(stale), encoding="utf-8")
            # Still read free, with its context intact apart from the dead link.
            self.assertEqual(rapidapi_company.load_cached_company_details(["1", "2"], cache_dir=cache_dir), {"1": stale, "2": live})
            conns = [_fake_connection(_fake_response(200, json.dumps(fresh))), _fake_connection(_fake_response(200, json.dumps(fresh)))]
            module = FetchCompanyDetailsRetryTests.MODULE
            with mock.patch(f"{module}.http.client.HTTPSConnection", side_effect=conns) as conn_cls, \
                    mock.patch.object(rapidapi_company._RATE_LIMITER, "wait"):
                by_id = rapidapi_company.fetch_company_details_batch(["1", "2"], api_key="test-key", cache_dir=cache_dir)
                by_slug = rapidapi_company.fetch_company_details_batch_by_slug(["acme"], api_key="test-key", cache_dir=cache_dir)
            self.assertEqual(conn_cls.call_count, 2)
            self.assertEqual(by_id, {"1": fresh, "2": live})
            self.assertEqual(by_slug, {"acme": fresh})
            self.assertEqual(json.loads((cache_dir / "1.json").read_text(encoding="utf-8")), fresh)
            self.assertEqual(json.loads((cache_dir / "slug__acme.json").read_text(encoding="utf-8")), fresh)


class GatewayFreshnessTests(unittest.TestCase):
    """The gateway answers from its own copy for a year, so a payload whose logo link has
    expired is asked for ``live``: when the local copy is stale, and when the gateway's is."""

    MODULE = FetchCompanyDetailsRetryTests.MODULE

    @staticmethod
    def _payload(staff: int, expires_at: int) -> dict:
        return {"success": True, "data": {"staffCount": staff, "logos": [{"url": f"https://media.licdn.com/x/logo?e={expires_at}", "width": 200}]}}

    def test_a_stale_local_payload_is_asked_for_live(self) -> None:
        stale = self._payload(7, int(time.time()) - 86400)
        fresh = self._payload(8, int(time.time()) + 86400)
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            (cache_dir / "123.json").write_text(json.dumps(stale), encoding="utf-8")
            (cache_dir / "slug__acme.json").write_text(json.dumps(stale), encoding="utf-8")
            conns = [_fake_connection(_fake_response(200, json.dumps(fresh))), _fake_connection(_fake_response(200, json.dumps(fresh)))]
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", side_effect=conns):
                by_id = rapidapi_company.fetch_company_details("123", api_key="test-key", cache_dir=cache_dir)
                by_slug = rapidapi_company.fetch_company_details_by_slug("acme", api_key="test-key", cache_dir=cache_dir)
            self.assertEqual((by_id, by_slug), (fresh, fresh))
            for conn in conns:
                self.assertEqual(conn.request.call_args.kwargs["headers"]["X-Freshness"], "live")
            self.assertEqual(json.loads((cache_dir / "123.json").read_text(encoding="utf-8")), fresh)

    def test_a_stale_gateway_copy_is_asked_for_once_more_live(self) -> None:
        stale = self._payload(7, int(time.time()) - 86400)
        fresh = self._payload(8, int(time.time()) + 86400)
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            conns = [_fake_connection(_fake_response(200, json.dumps(stale))), _fake_connection(_fake_response(200, json.dumps(fresh)))]
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", side_effect=conns), \
                    mock.patch(f"{self.MODULE}.time.sleep") as slept:
                result = rapidapi_company.fetch_company_details("123", api_key="test-key", cache_dir=cache_dir)
            self.assertEqual(result, fresh)
            self.assertEqual([c.request.call_args.kwargs["headers"]["X-Freshness"] for c in conns], ["31536000", "live"])
            slept.assert_not_called()
            self.assertEqual(json.loads((cache_dir / "123.json").read_text(encoding="utf-8")), fresh)

    def test_a_live_payload_is_asked_for_with_the_year_and_kept(self) -> None:
        fresh = self._payload(8, int(time.time()) + 86400)
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            conn = _fake_connection(_fake_response(200, json.dumps(fresh)))
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", return_value=conn):
                self.assertEqual(rapidapi_company.fetch_company_details("123", api_key="test-key", cache_dir=cache_dir), fresh)
            self.assertEqual(conn.request.call_count, 1)
            self.assertEqual(conn.request.call_args.kwargs["headers"]["X-Freshness"], "31536000")


if __name__ == "__main__":
    unittest.main()

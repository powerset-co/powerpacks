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


class FetchCompanyDetailsBySlugTests(unittest.TestCase):
    MODULE = "packs.indexing.primitives.enrich_companies_checkpointed.rapidapi_company"

    def test_fetch_by_slug_hits_username_endpoint_and_caches_namespaced(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            cache_dir = Path(td)
            conn = _fake_connection(_fake_response(200, json.dumps({"data": {"name": "Acme"}})))
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection", return_value=conn):
                result = rapidapi_company.fetch_company_details_by_slug("acme-inc", api_key="k", cache_dir=cache_dir)
            self.assertEqual(result, {"data": {"name": "Acme"}})
            # hit the username endpoint, not the by-id one
            path = conn.request.call_args.args[1]
            self.assertIn("/get-company-details?username=acme-inc", path)
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
            with mock.patch(f"{self.MODULE}.http.client.HTTPSConnection") as conn_cls:
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
            return {"data": {"name": "Cached"}} if key == "cached" else None

        with mock.patch.object(rapidapi_company, "_read_cache", side_effect=read_cache), \
                mock.patch.object(rapidapi_company._RATE_LIMITER, "wait"), \
                mock.patch.object(
                    rapidapi_company,
                    "fetch_company_details",
                    return_value={"data": {"name": "Fetched"}},
                ) as fetch:
            result = rapidapi_company.fetch_company_details_batch(
                ["cached", "missing", "missing"], api_key="test-key", max_workers=2
            )

        self.assertEqual(set(result), {"cached", "missing"})
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0], "missing")


if __name__ == "__main__":
    unittest.main()

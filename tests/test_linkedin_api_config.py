import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from packs.ingestion.primitives.enrich.rapidapi_client import RapidApiClient
from packs.indexing.primitives.enrich_companies_checkpointed import rapidapi_company


DIRECT_HOST = "professional-network-data.p.rapidapi.com"


class LinkedInApiConfigTests(unittest.TestCase):
    def configurations(self):
        return (
            ({"RAPIDAPI_LINKEDIN_KEY": "direct-key"}, None, DIRECT_HOST, "", {
                "x-rapidapi-key": "direct-key", "x-rapidapi-host": DIRECT_HOST,
            }),
            ({"POWERSET_API_KEY": "gateway-key", "RAPIDAPI_LINKEDIN_KEY": "direct-key"}, None,
             "proxy.powerset.dev", "/vendor/professional-network-data", {"x-powerset-key": "gateway-key"}),
            ({"RAPIDAPI_LINKEDIN_KEY": "direct-key"}, "explicit-key", "proxy.powerset.dev",
             "/vendor/professional-network-data", {"x-powerset-key": "explicit-key"}),
        )

    def test_person_selects_matching_url_and_authentication(self):
        for env, api_key, host, prefix, auth in self.configurations():
            with self.subTest(env=tuple(env), explicit=bool(api_key)), patch.dict(os.environ, env, clear=True), \
                    tempfile.TemporaryDirectory() as directory, \
                    patch.object(RapidApiClient, "http_json", return_value=(404, None, "gone")) as http:
                client = RapidApiClient(api_key=api_key)
                client.get_profile("jordan-bravo", "https://www.linkedin.com/in/jordan-bravo", cache_dir=directory)
                self.assertEqual(http.call_args.args[1], f"https://{host}{prefix}/get-profile-data-by-url")
                headers = http.call_args.kwargs["headers"]
                self.assertEqual({key: value for key, value in headers.items() if key != "X-Freshness"}, auth)
                self.assertEqual(headers.get("X-Freshness"), "7776000" if prefix else None)

    def test_company_selects_matching_url_and_authentication_for_single_and_batch(self):
        methods = (
            (rapidapi_company.fetch_company_details, "123", "/get-company-details-by-id?id=123"),
            (rapidapi_company.fetch_company_details_by_slug, "bravo", "/get-company-details?username=bravo"),
            (rapidapi_company.fetch_company_details_batch, ["123"], "/get-company-details-by-id?id=123"),
            (rapidapi_company.fetch_company_details_batch_by_slug, ["bravo"], "/get-company-details?username=bravo"),
        )
        for env, api_key, host, prefix, auth in self.configurations():
            for fetch, identifier, endpoint in methods:
                response = Mock(status=200)
                response.read.return_value = json.dumps({"data": {"name": "Bravo"}}).encode()
                connection = Mock()
                connection.getresponse.return_value = response
                with self.subTest(env=tuple(env), explicit=bool(api_key), method=fetch.__name__), \
                        patch.dict(os.environ, env, clear=True), tempfile.TemporaryDirectory() as directory, \
                        patch.object(rapidapi_company.http.client, "HTTPSConnection", return_value=connection) as http, \
                        patch.object(rapidapi_company._RATE_LIMITER, "wait"):
                    fetch(identifier, api_key=api_key, cache_dir=Path(directory))
                    http.assert_called_once_with(host, timeout=rapidapi_company.DEFAULT_TIMEOUT)
                    self.assertEqual(connection.request.call_args.args[1], prefix + endpoint)
                    headers = connection.request.call_args.kwargs["headers"]
                    self.assertEqual({key: value for key, value in headers.items()
                                      if key not in {"X-Freshness", "Content-Type"}}, auth)
                    self.assertEqual(headers.get("X-Freshness"), "31536000" if prefix else None)

    def test_explicit_empty_person_key_stays_keyless(self):
        with patch.dict(os.environ, {"RAPIDAPI_LINKEDIN_KEY": "direct-key"}, clear=True):
            self.assertEqual(RapidApiClient(api_key="").api_key, "")


if __name__ == "__main__":
    unittest.main()

"""The wacli JSON boundary: `wacli/payloads.py` parsers against recorded-shape JSON.

wacli is an external Go binary, so these fixtures are written the way its
`--json` output actually looks (envelope, `data` nesting, string-ish numbers,
omitted keys) and then fed through `json.loads` — the parsers must survive every
shape without the callers re-guarding anything. Every identifier here is
synthetic.
"""

from __future__ import annotations

import importlib
import json
import unittest

payloads = importlib.import_module(
    "packs.ingestion.primitives.discover.messages.wacli.payloads"
)


AUTH_STATUS_LINKED = """
{"success": true, "data": {"authenticated": true, "linked_jid": "15550100@s.whatsapp.net"}}
"""
AUTH_STATUS_UNLINKED = """
{"success": false, "error": "not authenticated", "data": {"authenticated": false}}
"""

GROUP_INFO = """
{"success": true, "data": {
  "JID": "120363000000000001@g.us",
  "Name": "  Bravo   Family  ",
  "ParticipantCount": 4,
  "Participants": [
    {"JID": "15550100@s.whatsapp.net", "PhoneNumber": "+1 555 0100", "DisplayName": "Jordan Bravo"},
    {"JID": "15550101@s.whatsapp.net", "PhoneNumber": "", "DisplayName": "Casey Bravo"},
    {"JID": "84391000000001@lid", "PhoneNumber": "", "DisplayName": "Hidden Member"},
    "not-an-object"
  ]
}}
"""
GROUP_INFO_FLAT = """
{"JID": "120363000000000002@g.us", "Name": "Trip", "Participants": [
  {"JID": "15550102@s.whatsapp.net", "PhoneNumber": "15550102", "DisplayName": ""}
]}
"""
GROUP_INFO_NO_JID = """
{"success": true, "data": {"Name": "Nameless", "Participants": []}}
"""

BACKFILL_BATCH = """
{"success": true, "data": {"chats": [
  {"chat": "15550100@s.whatsapp.net", "requests_sent": 4, "responses_seen": 4,
   "messages_received": 37, "end_type": "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY", "error": ""},
  {"chat": "15550101@s.whatsapp.net", "requests_sent": "2", "responses_seen": null,
   "error": "timed out waiting for on-demand history sync response"},
  {"chat": "", "requests_sent": 9},
  "not-an-object"
]}}
"""
BACKFILL_BATCH_FLAT = """
{"chats": [{"chat": "15550102@s.whatsapp.net", "requests_sent": 1, "responses_seen": 1}]}
"""

PRIOR_DEPTH_MANIFEST = """
{"status": "partial",
 "policy": {"version": 4, "selection": "changed_recent_shallow"},
 "counts": {"eligible": 3, "source_total_messages": 1200},
 "source": {"dm_state_sha256": "%s"}}
""" % ("a1" * 32)


class AuthStatusTests(unittest.TestCase):
    def test_linked_store_carries_the_linked_jid(self) -> None:
        status = payloads.AuthStatus.from_payload(json.loads(AUTH_STATUS_LINKED))
        self.assertTrue(status.authenticated)
        self.assertEqual(status.linked_jid, "15550100@s.whatsapp.net")
        self.assertIs(status.raw_success, True)
        self.assertIsNone(status.error)

    def test_unlinked_store_keeps_the_error_verbatim(self) -> None:
        status = payloads.AuthStatus.from_payload(json.loads(AUTH_STATUS_UNLINKED))
        self.assertFalse(status.authenticated)
        self.assertEqual(status.linked_jid, "")
        self.assertEqual(status.error, "not authenticated")

    def test_missing_or_wrong_shaped_data_is_not_authenticated(self) -> None:
        for payload in ({}, {"data": None}, {"data": ["authenticated"]}, {"success": True}):
            with self.subTest(payload=payload):
                status = payloads.AuthStatus.from_payload(payload)
                self.assertFalse(status.authenticated)
                self.assertEqual(status.linked_jid, "")


class PairingMarkerTests(unittest.TestCase):
    def test_marker_written_by_our_flow(self) -> None:
        marker = payloads.PairingMarker.from_payload({
            "full_sync": True,
            "full_sync_days": "3650",
            "wacli_version": "v0.14.0-fullsync",
            "paired_at": "2026-07-30T00:00:00Z",
        })
        assert marker is not None
        self.assertTrue(marker.full_sync)
        self.assertEqual(marker.wacli_version, "v0.14.0-fullsync")
        self.assertEqual(marker.paired_at, "2026-07-30T00:00:00Z")

    def test_a_non_object_marker_is_no_marker(self) -> None:
        for payload in (None, [], "full_sync", 3650):
            with self.subTest(payload=payload):
                self.assertIsNone(payloads.PairingMarker.from_payload(payload))

    def test_missing_keys_stay_none_so_the_status_payload_is_unchanged(self) -> None:
        marker = payloads.PairingMarker.from_payload({"full_sync": True})
        assert marker is not None
        self.assertIsNone(marker.wacli_version)
        self.assertIsNone(marker.paired_at)


class GroupInfoTests(unittest.TestCase):
    def test_participants_resolve_by_phone_then_jid_and_drop_the_rest(self) -> None:
        group = payloads.GroupInfo.from_payload(json.loads(GROUP_INFO))
        assert group is not None
        self.assertEqual(group.jid, "120363000000000001@g.us")
        self.assertEqual(group.name, "Bravo Family")  # whitespace squeezed
        self.assertEqual(group.participant_count, 4)  # what wacli reported
        self.assertEqual(
            [(participant.phone, participant.name) for participant in group.participants],
            [("+15550100", "Jordan Bravo"), ("+15550101", "Casey Bravo")],
        )

    def test_cache_entry_is_the_shape_the_sidecar_stores(self) -> None:
        group = payloads.GroupInfo.from_payload(json.loads(GROUP_INFO))
        assert group is not None
        self.assertEqual(group.as_cache_entry(), {
            "jid": "120363000000000001@g.us",
            "name": "Bravo Family",
            "participant_count": 4,
            "participants": [
                {"phone": "+15550100", "name": "Jordan Bravo"},
                {"phone": "+15550101", "name": "Casey Bravo"},
            ],
        })

    def test_flat_payload_and_absent_count_fall_back_to_usable_members(self) -> None:
        group = payloads.GroupInfo.from_payload(json.loads(GROUP_INFO_FLAT))
        assert group is not None
        self.assertEqual(group.jid, "120363000000000002@g.us")
        self.assertEqual(group.participant_count, 1)
        self.assertEqual(group.participants[0].phone, "+15550102")
        self.assertEqual(group.participants[0].name, "")

    def test_a_group_without_a_jid_is_not_cacheable(self) -> None:
        self.assertIsNone(payloads.GroupInfo.from_payload(json.loads(GROUP_INFO_NO_JID)))
        self.assertIsNone(payloads.GroupInfo.from_payload({"data": "nope"}))


class BackfillBatchResultTests(unittest.TestCase):
    def test_per_chat_results_are_keyed_and_coerced(self) -> None:
        batch = payloads.BackfillBatchResult.from_command_json(json.loads(BACKFILL_BATCH))
        self.assertEqual(
            sorted(batch.chats),
            ["15550100@s.whatsapp.net", "15550101@s.whatsapp.net"],
        )
        deep = batch.chat("15550100@s.whatsapp.net")
        self.assertTrue(deep.present)
        self.assertEqual(deep.requests_sent, 4)
        self.assertEqual(deep.messages_received, 37)
        self.assertEqual(deep.end_type, "COMPLETE_AND_NO_MORE_MESSAGE_REMAIN_ON_PRIMARY")
        self.assertEqual(deep.error, "")

    def test_stringy_and_null_counts_become_ints(self) -> None:
        batch = payloads.BackfillBatchResult.from_command_json(json.loads(BACKFILL_BATCH))
        stalled = batch.chat("15550101@s.whatsapp.net")
        self.assertEqual(stalled.requests_sent, 2)
        self.assertEqual(stalled.responses_seen, 0)
        self.assertEqual(stalled.messages_received, 0)
        self.assertIn("timed out", stalled.error)
        self.assertEqual(stalled.end_type, "")

    def test_a_chat_wacli_never_reported_is_the_missing_result(self) -> None:
        batch = payloads.BackfillBatchResult.from_command_json(json.loads(BACKFILL_BATCH))
        missing = batch.chat("15550999@s.whatsapp.net")
        self.assertFalse(missing.present)
        self.assertEqual(missing.requests_sent, 0)
        self.assertEqual(missing.responses_seen, 0)
        self.assertEqual(missing.error, "")

    def test_flat_result_parses_and_unusable_output_reports_no_chats(self) -> None:
        flat = payloads.BackfillBatchResult.from_command_json(json.loads(BACKFILL_BATCH_FLAT))
        self.assertEqual(flat.chat("15550102@s.whatsapp.net").responses_seen, 1)
        for payload in (None, [], "wacli: connection closed", {}, {"data": {"chats": "none"}}):
            with self.subTest(payload=payload):
                batch = payloads.BackfillBatchResult.from_command_json(payload)
                self.assertEqual(batch.chats, {})
                self.assertFalse(batch.chat("15550100@s.whatsapp.net").present)


class PriorDepthManifestTests(unittest.TestCase):
    def test_a_current_manifest_reports_its_watermark_and_policy(self) -> None:
        prior = payloads.PriorDepthManifest.from_payload(json.loads(PRIOR_DEPTH_MANIFEST))
        self.assertEqual(prior.source_total_messages, 1200)
        self.assertEqual(prior.policy_version, 4)
        self.assertTrue(prior.has_source_total)
        self.assertTrue(prior.has_source_digest)

    def test_an_absent_watermark_is_none_but_a_zero_watermark_is_zero(self) -> None:
        absent = payloads.PriorDepthManifest.from_payload({"counts": {"eligible": 0}})
        self.assertIsNone(absent.source_total_messages)
        self.assertFalse(absent.has_source_total)

        empty_store = payloads.PriorDepthManifest.from_payload(
            {"counts": {"source_total_messages": 0}}
        )
        self.assertEqual(empty_store.source_total_messages, 0)
        self.assertTrue(empty_store.has_source_total)

    def test_a_truncated_digest_does_not_count_as_a_digest(self) -> None:
        prior = payloads.PriorDepthManifest.from_payload({"source": {"dm_state_sha256": "a1b2"}})
        self.assertEqual(prior.dm_state_sha256, "a1b2")
        self.assertFalse(prior.has_source_digest)

    def test_missing_or_wrong_shaped_sections_degrade_to_a_bootstrap(self) -> None:
        for payload in (None, [], "{}", {}, {"counts": [], "policy": None, "source": 7}):
            with self.subTest(payload=payload):
                prior = payloads.PriorDepthManifest.from_payload(payload)
                self.assertIsNone(prior.source_total_messages)
                self.assertEqual(prior.dm_state_sha256, "")
                self.assertEqual(prior.policy_version, 0)


if __name__ == "__main__":
    unittest.main()

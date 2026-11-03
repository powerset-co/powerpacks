"""Named group evidence and unchanged direct-message payloads; synthetic contacts only."""
from __future__ import annotations

import hashlib
import json
import unittest
from dataclasses import replace

from packs.ingestion.primitives.deep_context_v2.collect.bundle import (
    CollectionBundle, MessageChannel, MessageDirection, MessageEntry, Person,
)
from packs.ingestion.primitives.deep_context_v2.db.owner import owner_from_payload
from packs.ingestion.primitives.deep_context_v2.synthesize.facts import OwnedIdentifiers, SynthesizedFacts
from packs.ingestion.primitives.deep_context_v2.synthesize.prompt import SYNTHESIS_VERSION, render_batch
from packs.ingestion.primitives.deep_context_v2.worth import luna
from packs.ingestion.primitives.share.models import GROUP_CHANNELS
from packs.ingestion.primitives.share.web.model import _families, _family_counts


def bundle(messages: list[MessageEntry], groups: list[str] | None = None) -> CollectionBundle:
    return CollectionBundle.of(
        Person("candidate", "Casey Candidate", (), ("+15550100",), ("whatsapp",)),
        messages=messages, groups=groups or [], thread_participants=(), available=len(messages),
    )


class GroupRenderTests(unittest.TestCase):
    def test_both_group_channels_render_named_speakers_and_directions(self) -> None:
        for channel in (MessageChannel.IMESSAGE_GROUP, MessageChannel.WHATSAPP_GROUP):
            with self.subTest(channel=channel):
                messages = [
                    MessageEntry(channel, "2026-10-01T12:00:00Z", direction, "Project", "Hello", sender=sender)
                    for direction, sender in (
                        (MessageDirection.FROM_ME, "Jordan Owner"),
                        (MessageDirection.FROM_THEM, "Casey Candidate"),
                        (MessageDirection.FROM_OTHER, "Taylor Other"),
                        (MessageDirection.FROM_OTHER, "+15550102 (no saved name)"),
                    )
                ]
                rendered = render_batch(bundle(messages, ["Project"]), messages)
                self.assertIn("The contact's own facts come only from (them) lines.", rendered)
                self.assertEqual([line for line in rendered.splitlines() if line.startswith("[")], [
                    f"[Project({channel}) 2026-10-01 (me) Jordan Owner]: Hello",
                    f"[Project({channel}) 2026-10-01 (them) Casey Candidate]: Hello",
                    f"[Project({channel}) 2026-10-01 (other) Taylor Other]: Hello",
                    f"[Project({channel}) 2026-10-01 (other) +15550102 (no saved name)]: Hello",
                ])

    def test_group_sender_roundtrip_and_stable_order(self) -> None:
        message = MessageEntry(MessageChannel.WHATSAPP_GROUP, "2026-10-01", MessageDirection.FROM_OTHER,
                               "Project", "Hello", sender="Taylor Other")
        self.assertEqual(message.to_payload()["sender"], "Taylor Other")
        self.assertEqual(MessageEntry.from_payload(message.to_payload()), message)
        renamed = replace(message, sender="Morgan Other")
        self.assertLess(renamed.content_order_key(), message.content_order_key())

    def test_worth_uses_same_group_lines_with_its_direct_message_directions(self) -> None:
        messages = [
            MessageEntry(channel, "2026-10-01", MessageDirection.FROM_OTHER, "Project", "Hello",
                         sender="Taylor Other")
            for channel in (MessageChannel.IMESSAGE_GROUP, MessageChannel.WHATSAPP_GROUP)
        ]
        messages.append(MessageEntry.of(MessageChannel.WHATSAPP, "2026-10-02", from_me=True, text="Direct"))
        facts = SynthesizedFacts("Casey Candidate", (), (), "", "", "", "", "", "", (), (), (),
                                 OwnedIdentifiers((), (), ()), (), 0.9, False)
        owner = owner_from_payload({"name": "Jordan Owner"})
        channels = luna.ChannelSummary(("whatsapp",), {}, "2026-10-01", "2026-10-02", 1, 0, 1)
        request = luna.build_request(facts, [bundle(messages, ["Project"])], channels, owner, "2026-10-02")
        rendered = json.loads(request["input"])["messages"]
        for channel in (MessageChannel.IMESSAGE_GROUP, MessageChannel.WHATSAPP_GROUP):
            self.assertIn(f"[Project({channel}) 2026-10-01 (other) Taylor Other]: Hello", rendered)
        self.assertIn("[whatsapp 2026-10-02 from Jordan Owner (owner) to Casey Candidate]: Direct", rendered)

    def test_whatsapp_group_is_group_evidence_and_shares_whatsapp_channel(self) -> None:
        self.assertIn("whatsapp_group", GROUP_CHANNELS)
        self.assertEqual(_families(("whatsapp_group", "whatsapp", "imessage_group")), ("whatsapp", "imessage"))
        self.assertEqual(_family_counts({"whatsapp": 2, "whatsapp_group": 3}), {"whatsapp": 5})

    def test_without_groups_payload_fingerprint_and_direct_lines_are_unchanged(self) -> None:
        messages = [MessageEntry.of(channel, "2026-10-01", from_me=False, text="Hello", subject="Topic")
                    for channel in (MessageChannel.GMAIL, MessageChannel.IMESSAGE, MessageChannel.WHATSAPP)]
        person = bundle(messages)
        payload = person.to_payload()
        expected = {
            "person_id": "candidate", "full_name": "Casey Candidate", "emails": [], "phones": ["+15550100"],
            "source_channels": ["whatsapp"], "groups": [], "thread_participants": [],
            "messages": [{"channel": channel, "at": "2026-10-01", "direction": "from_them", "subject": "Topic",
                          "text": "Hello"} for channel in ("gmail", "imessage", "whatsapp")],
            "messages_available": 3, "capped": False,
        }
        self.assertEqual(payload, expected)
        actual_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected_json = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(hashlib.sha256(actual_json.encode()).hexdigest(),
                         hashlib.sha256(expected_json.encode()).hexdigest())
        self.assertEqual(CollectionBundle.from_payload(expected), person)
        self.assertEqual([line for line in render_batch(person, messages).splitlines() if line.startswith("[")], [
            "[gmail 2026-10-01 THEM; recipients unavailable] Topic: Hello",
            "[imessage 2026-10-01 THEM] Topic: Hello",
            "[whatsapp 2026-10-01 THEM] Topic: Hello",
        ])
        self.assertEqual(luna.REQUEST_VERSION, "deep-context-luna-detectors-20261028")
        self.assertEqual(SYNTHESIS_VERSION, "b2374192ab73")


if __name__ == "__main__":
    unittest.main()

"""The share vocabulary: the three-way decision, its reasons, and its flags.

The share row itself is a table row — `deep_context.db.models.ShareDecisionRow`
in the canonical SQLite store — so this module owns only the words.

Flow: the share policy picks one ShareValue and one reason; the upload reads
both back from the store.

Changelog:
  2026-10-13: Luna's share call decides a worth-yes person: would_vouch, unsure, not_professional.
    Family, partner and minor stay home; the other JEV flags are gone.
  2026-09-24: the share list became a SQLite table; ShareRow moved to db/models.
  2026-09-24: share became three-way (yes | no | confirm); reasons follow worth
    and the JEV rules became confirm flags.
  2026-09-24: created.
"""

from __future__ import annotations

from typing import Literal

ShareValue = Literal["yes", "no", "confirm"]
SHARE_YES: ShareValue = "yes"
SHARE_NO: ShareValue = "no"
# A worth-yes person Luna is unsure about: the human decides, nothing is uploaded.
SHARE_CONFIRM: ShareValue = "confirm"
SHARE_VALUES = frozenset({SHARE_YES, SHARE_NO, SHARE_CONFIRM})

# Reasons, first rule wins; the rule name is the reason.
OWNER = "owner"
HUMAN_PRIVATE = "human_private"
HUMAN_SHARE = "human_share"
WORTH_NO = "worth_no"
WORTH_MAYBE = "worth_maybe"

# Flags keep a worth-yes person home (labels.flag owns the order). A flag is a reason too.
FAMILY = "family"
ROMANTIC_PARTNER = "romantic_partner"
MINOR = "minor"

# Luna's share call on everyone else (labels.share_decision).
WOULD_VOUCH = "would_vouch"
UNSURE = "unsure"
NOT_PROFESSIONAL = "not_professional"

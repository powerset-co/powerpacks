"""Messages discovery package (module path preserved for import consumers).

The channel classes and their owned path constants live in channels/; they are
re-exported here for the package's public API. Test patches import the concrete
modules (channels/i_message_channel.py, channels/whats_app_channel.py,
messages/discover.py), never these package re-exports.

Changelog:
  2026-07-23 (dead accounts.json registry): dropped the DEFAULT_ACCOUNTS re-export —
    the accounts.json registry was deleted, and nothing imported the constant from
    this package.
  2026-07-23 (explicit-selection): dropped the messages_discovery_inputs re-export —
    the accounts.json-linkage resolver was deleted when channel selection became
    explicit --include-* only.
  2026-07-23 (terse): dropped the discover()/resolve() re-exports — those wrapper
    functions were folded into MessagesDiscovery (construct + run()).
  2026-07-23 (channels split): the channel classes and their IMESSAGE_*/WHATSAPP_*
    path constants (and the wacli max-messages/sync/depth defaults) moved out of
    discover.py into channels/; the re-exports below now source them from those
    concrete modules.
"""

from packs.ingestion.schemas.message_contacts import CSV_HEADERS  # noqa: F401
from packs.ingestion.primitives.discover.messages.channels.message_channel_base import (  # noqa: F401
    MessageChannel,
)
from packs.ingestion.primitives.discover.messages.channels.i_message_channel import (  # noqa: F401
    IMESSAGE_CONTACTS,
    IMESSAGE_MANIFEST,
    IMESSAGE_NORMALIZED_JSONL,
    IMESSAGE_NORMALIZED_MANIFEST,
    IMESSAGE_RAW_JSONL,
    IMessageChannel,
)
from packs.ingestion.primitives.discover.messages.channels.whats_app_channel import (  # noqa: F401
    DEFAULT_WACLI_DISCOVERY_MAX_MESSAGES,
    DEFAULT_WACLI_SYNC_TIMEOUT,
    WHATSAPP_CONTACTS,
    WHATSAPP_MANIFEST,
    WHATSAPP_NORMALIZED_JSONL,
    WHATSAPP_NORMALIZED_MANIFEST,
    WHATSAPP_PROGRESS_JSONL,
    WHATSAPP_RAW_JSONL,
    WhatsAppChannel,
)
from packs.ingestion.primitives.discover.messages.discover import (  # noqa: F401
    DEFAULT_MESSAGES_OUTPUT_DIR,
    MERGED_CONTACTS,
    MERGED_CONTACTS_MANIFEST,
    MESSAGES_DIR,
    MessagesDiscovery,
    build_parser,
    main,
)

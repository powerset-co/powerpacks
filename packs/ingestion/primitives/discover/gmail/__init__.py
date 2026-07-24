"""Gmail discovery package: util (parsers), discover (CLI), extract_gmail
(in-process extractor + CLI), and the `msgvault/` subpackage
(`store` = MsgvaultStore + SQL, `util` = pure helpers, `sync` = msgvault sync).

The package name preserves `from ...discover import gmail` for
module consumers; file invocations use gmail/discover.py. The `msgvault/`
submodules and extract_gmail are deliberately NOT re-exported here — consumers
(and test patches) import the concrete submodules.
"""

from packs.ingestion.primitives.discover.gmail.util import (  # noqa: F401
    GmailDiscoveryInputs,
    resolve_discovery_inputs,
    GMAIL_DISCOVERY_COLUMNS,
    DEFAULT_GMAIL_ESTIMATE_MAX_PAGES,
    GMAIL_CALCULATION_FULL_RECOUNT,
    GMAIL_CALCULATION_INCREMENTAL_DELTA,
    gmail_incremental_input_id,
    gmail_discovery_merge_plan,
    extract_gmail_base_dir,
)
from packs.ingestion.primitives.discover.gmail.msgvault.sync import (  # noqa: F401
    MSGVAULT_REAUTH_ERROR_MARKERS,
    parse_msgvault_sync_date,
    sqlite_table_columns,
    infer_msgvault_sync_after,
    msgvault_sync_supports_no_attachments,
    msgvault_reauthorization_required,
    msgvault_reauthorize_command,
    sync_msgvault_account,
    normalize_label_names,
    gmail_sync_query,
    gmail_sync_after,
    gmail_excluded_labels,
)
from packs.ingestion.primitives.discover.gmail.discover import (  # noqa: F401
    build_parser,
    main,
)

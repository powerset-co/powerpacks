"""Gmail discovery package: util (parsers), sync (msgvault), discover (CLI).

The package name preserves `from ...discover_contacts_pipeline import gmail` for
module consumers; file invocations use gmail/discover.py.
"""

from packs.ingestion.primitives.discover_contacts_pipeline.gmail.util import (  # noqa: F401
    GmailDiscoveryInputs,
    resolve_discovery_inputs,
    GMAIL_DISCOVERY_COLUMNS,
    DEFAULT_GMAIL_ESTIMATE_MAX_PAGES,
    GMAIL_CALCULATION_FULL_RECOUNT,
    GMAIL_CALCULATION_INCREMENTAL_DELTA,
    gmail_incremental_input_id,
    gmail_discovery_merge_plan,
    gmail_network_import_base_dir,
    inputs,
)
from packs.ingestion.primitives.discover_contacts_pipeline.gmail.sync import (  # noqa: F401
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
from packs.ingestion.primitives.discover_contacts_pipeline.gmail.discover import (  # noqa: F401
    build_parser,
    discover,
    main,
    run_gmail_msgvault,
)

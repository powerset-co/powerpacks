"""Gmail import vertical (package path preserved for import consumers)."""

from packs.ingestion.primitives.import_contacts_pipeline.gmail.importer import (  # noqa: F401
    GMAIL_IMPORT_CONTRACT,
    build_parser,
    main,
    run,
)
from packs.ingestion.primitives.import_contacts_pipeline.gmail.util import (  # noqa: F401
    gmail_artifacts_from_discovery,
    queue_row_to_candidate,
    write_gmail_candidates,
)

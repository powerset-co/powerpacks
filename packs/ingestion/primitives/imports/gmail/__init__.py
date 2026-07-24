"""Gmail import vertical (package path preserved for import consumers).

`importer.py` is THE entry: the `GmailImport` orchestrator + CLI. Its two steps
live in `steps/` and its shared helpers in `util.py`.
"""

from packs.ingestion.primitives.imports.gmail.importer import (  # noqa: F401
    GMAIL_IMPORT_CONTRACT,
    GmailImport,
    build_parser,
    main,
    run,
)
from packs.ingestion.primitives.imports.gmail.util import (  # noqa: F401
    gmail_artifacts_from_discovery,
    queue_row_to_candidate,
    write_gmail_candidates,
)

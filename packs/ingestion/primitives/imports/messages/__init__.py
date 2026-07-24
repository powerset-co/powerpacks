"""Messages import vertical (package path preserved for import consumers)."""

from packs.ingestion.primitives.imports.messages.importer import (  # noqa: F401
    MATCH_MANIFEST_JSON,
    MESSAGES_IMPORT_CONTRACT,
    WORKING_CONTACTS_CSV,
    build_parser,
    contact_row_to_candidate,
    contact_row_to_messages_people,
    existing_csv_column,
    main,
    merge_matched_people_rows,
    messages_import_diff,
    people_csv_schema_stale,
    replace_messages_directory_rows,
    run,
    selected_contacts_people,
)

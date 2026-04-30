# powerset_contacts_harness

Locate and run the `contact-exporter` / `powerset-contacts` CLI as an external
local extraction backend.

Examples:

```bash
python packs/messages/primitives/powerset_contacts_harness/powerset_contacts_harness.py check
python packs/messages/primitives/powerset_contacts_harness/powerset_contacts_harness.py run --channel imessage --output .powerpacks/messages/contacts.csv --dry-run
python packs/messages/primitives/powerset_contacts_harness/powerset_contacts_harness.py run --channel imessage --output .powerpacks/messages/contacts.csv
```

Upload requires a separate flag:

```bash
python packs/messages/primitives/powerset_contacts_harness/powerset_contacts_harness.py run --channel upload --input .powerpacks/messages/contacts.csv --confirm-upload
```

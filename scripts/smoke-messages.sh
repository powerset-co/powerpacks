#!/usr/bin/env bash
# Drive the full messages pack end-to-end against synthetic data.
#
# Exercises:
#   - extract_imessage_contacts (against a synthetic chat.db + AddressBook)
#   - normalize_message_contacts
#   - waha_runtime check (real Docker)
#   - waha_session status (expects no WAHA running)
#   - extract_whatsapp_contacts (against synthetic CSV — WAHA-less mode)
#   - powerset_auth whoami (likely anonymous on a fresh box)
#   - sync_powerset_candidates --use-cached (validates a hand-rolled candidate CSV)
#   - match_local_candidates (synthetic catalog)
#   - llm_review_contacts estimate (no spend, no API key)
#
# No real network calls. No money. Safe to run repeatedly.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d -t powerpacks-smoke-XXXX)"
trap 'echo; echo "[smoke] artifacts: $TMP"' EXIT

PY=python3
PRIMS="$ROOT/packs/messages/primitives"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
step "1. extract_imessage_contacts (synthetic SQLite fixture)"
# ---------------------------------------------------------------------------
mkdir -p "$TMP/AddressBook/Sources/fixture"
$PY - <<EOF
import sqlite3
from pathlib import Path
chat_db = Path("$TMP/chat.db")
ab_db = Path("$TMP/AddressBook/Sources/fixture/AddressBook-v22.abcddb")
with sqlite3.connect(chat_db) as c:
    c.executescript("""
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, handle_id INTEGER, date INTEGER, associated_message_type INTEGER);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT, room_name TEXT);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
        INSERT INTO handle VALUES
            (1, '+14155550101'),
            (2, '+14155550202'),
            (3, '+14155550303'),
            (4, '+14155550404');
        INSERT INTO message (handle_id, date, associated_message_type)
            VALUES (1, 725846400000000000, NULL),
                   (1, 725946400000000000, NULL),
                   (2, 725846500000000000, NULL),
                   (3, 725846600000000000, NULL),
                   (3, 725846700000000000, NULL),
                   (3, 725846800000000000, NULL),
                   (4, 725846900000000000, NULL),
                   (4, 725947000000000000, NULL);
        INSERT INTO chat (chat_identifier, display_name, room_name) VALUES ('chat1', 'Founders', NULL);
        INSERT INTO chat_handle_join VALUES (1, 1), (1, 2);
    """)
with sqlite3.connect(ab_db) as c:
    c.executescript("""
        CREATE TABLE ZABCDRECORD (Z_PK INTEGER PRIMARY KEY, ZFIRSTNAME TEXT, ZLASTNAME TEXT);
        CREATE TABLE ZABCDPHONENUMBER (ZOWNER INTEGER, ZFULLNUMBER TEXT);
        INSERT INTO ZABCDRECORD VALUES
            (1, 'Jane', 'Doe'),
            (2, 'Bob', 'Smith'),
            (3, 'Carol', 'Lopez'),
            (4, 'Plumber', 'Mike');
        INSERT INTO ZABCDPHONENUMBER VALUES
            (1, '(415) 555-0101'),
            (2, '+14155550202'),
            (3, '4155550303'),
            (4, '+14155550404');
    """)
print(f"chat.db rows: 6 messages, AddressBook rows: 3")
EOF
$PY "$PRIMS/extract_imessage_contacts/extract_imessage_contacts.py" \
    --chat-db "$TMP/chat.db" \
    --addressbook-glob "$TMP/AddressBook/Sources/*/AddressBook-v22.abcddb" \
    extract \
    --output-csv "$TMP/contacts.csv" \
    --output-jsonl "$TMP/contacts.jsonl" \
    --manifest "$TMP/imessage.manifest.json" \
    --run-id smoke-imessage > "$TMP/imessage.stdout.json"
$PY -c "import json; d=json.load(open('$TMP/imessage.stdout.json')); assert d['status']=='completed'; print(f'  contacts={d[\"counts\"][\"contacts\"]} with_messages={d[\"counts\"][\"with_messages\"]} with_group_context={d[\"counts\"][\"with_group_context\"]}')"
ok "imessage CSV written"

# ---------------------------------------------------------------------------
step "2. normalize_message_contacts"
# ---------------------------------------------------------------------------
$PY "$PRIMS/normalize_message_contacts/normalize_message_contacts.py" normalize \
    --input "$TMP/contacts.csv" \
    --out-jsonl "$TMP/contacts.normalized.jsonl" \
    --manifest "$TMP/normalize.manifest.json" \
    --run-id smoke-normalize > "$TMP/normalize.stdout.json"
$PY -c "import json; d=json.load(open('$TMP/normalize.stdout.json')); print(f'  normalized_rows={d[\"counts\"][\"normalized_rows\"]} imessage={d[\"counts\"][\"imessage_rows\"]}')"
ok "normalize manifest written"

# ---------------------------------------------------------------------------
step "3. waha_runtime check (against real Docker if available)"
# ---------------------------------------------------------------------------
$PY "$PRIMS/waha_runtime/waha_runtime.py" check > "$TMP/waha_check.json" || true
$PY -c "import json; d=json.load(open('$TMP/waha_check.json')); print(f'  docker.installed={d[\"docker\"][\"installed\"]} docker.daemon_ok={d[\"docker\"][\"daemon_ok\"]} ready={d[\"ready_to_start\"]}')"
ok "waha_runtime emitted JSON manifest with install hints"

# ---------------------------------------------------------------------------
step "4. waha_session status (no WAHA running, expects unreachable)"
# ---------------------------------------------------------------------------
$PY "$PRIMS/waha_session/waha_session.py" status \
    --base-url http://127.0.0.1:65530 \
    --api-key smoke > "$TMP/waha_status.json" || true
$PY -c "import json; d=json.load(open('$TMP/waha_status.json')); print(f'  reachable={d[\"state\"].get(\"reachable\", False)}')"
ok "waha_session degrades cleanly when WAHA is down"

# ---------------------------------------------------------------------------
step "5. extract_whatsapp_contacts check (no session yet)"
# ---------------------------------------------------------------------------
$PY "$PRIMS/extract_whatsapp_contacts/extract_whatsapp_contacts.py" check \
    --base-url http://127.0.0.1:65530 \
    --api-key smoke > "$TMP/wa_check.json" || true
$PY -c "import json; d=json.load(open('$TMP/wa_check.json')); print(f'  ready={d[\"ready\"]} working={d[\"session_state\"][\"working\"]}')"
ok "extract_whatsapp_contacts gates on session WORKING"

# ---------------------------------------------------------------------------
step "6. powerset_auth whoami (no creds expected)"
# ---------------------------------------------------------------------------
$PY "$PRIMS/powerset_auth/powerset_auth.py" whoami \
    --credentials-path "$TMP/credentials.json" > "$TMP/whoami.json" || true
$PY -c "import json; d=json.load(open('$TMP/whoami.json')); print(f'  status={d[\"status\"]}')"
ok "powerset_auth.whoami reports anonymous cleanly"

# ---------------------------------------------------------------------------
step "7. sync_powerset_candidates --use-cached (synthetic catalog)"
# ---------------------------------------------------------------------------
cat > "$TMP/powerset_contacts.csv" <<'EOF'
id,name,linkedin_url,phone_number,emails,public_identifier
p1,Jane Doe,https://www.linkedin.com/in/jane-doe,+14155550101,jane@example.com,jane-doe
p2,Bobby Smith,https://www.linkedin.com/in/bobby-smith,,,bobby-smith
p3,Bob Smith,https://www.linkedin.com/in/bob-smith,,,bob-smith
p4,Carol Lopez,https://www.linkedin.com/in/carol-lopez,+14155550303,,carol-lopez
EOF
$PY "$PRIMS/sync_powerset_candidates/sync_powerset_candidates.py" sync \
    --use-cached \
    --output "$TMP/powerset_contacts.csv" \
    --manifest "$TMP/sync.manifest.json" > "$TMP/sync.stdout.json"
$PY -c "import json; d=json.load(open('$TMP/sync.stdout.json')); print(f'  rows={d[\"rows\"]} status={d[\"status\"]}')"
ok "sync_powerset_candidates honored cached catalog"

# ---------------------------------------------------------------------------
step "8. match_local_candidates (Jane/Bob/Carol exact match, Plumber Mike unmatched)"
# ---------------------------------------------------------------------------
$PY "$PRIMS/match_local_candidates/match_local_candidates.py" match \
    --contacts "$TMP/contacts.csv" \
    --candidates "$TMP/powerset_contacts.csv" \
    --manifest "$TMP/match.manifest.json" > "$TMP/match.stdout.json"
$PY -c "
import csv, json
d = json.load(open('$TMP/match.stdout.json'))
print(f'  stats={d[\"stats\"]}')
rows = list(csv.DictReader(open('$TMP/contacts.csv')))
for r in rows:
    print(f'    {r[\"phone\"]:>14}  {r[\"name\"]:<14}  status={r[\"match_status\"]:<10}  method={r[\"match_method\"]}')
"
ok "match_local_candidates wrote match_* columns"

# ---------------------------------------------------------------------------
step "9. llm_review_contacts estimate (no API key, no spend)"
# ---------------------------------------------------------------------------
$PY "$PRIMS/llm_review_contacts/llm_review_contacts.py" estimate \
    --input "$TMP/contacts.csv" \
    --model openai/gpt-4.1-mini > "$TMP/estimate.json"
$PY -c "import json; d=json.load(open('$TMP/estimate.json')); print(f'  candidates={d[\"candidates\"]} batches={d[\"estimate\"][\"batches\"]} est_usd={d[\"estimate\"][\"estimated_usd\"]}')"
ok "llm_review_contacts.estimate computed cost without API"

# ---------------------------------------------------------------------------
step "10. llm_review_contacts review --dry-run"
# ---------------------------------------------------------------------------
$PY "$PRIMS/llm_review_contacts/llm_review_contacts.py" review \
    --input "$TMP/contacts.csv" \
    --model openai/gpt-4.1-mini \
    --dry-run > "$TMP/dryrun.json"
$PY -c "import json; d=json.load(open('$TMP/dryrun.json')); print(f'  status={d[\"status\"]} candidates={d[\"candidate_count\"]}')"
ok "llm_review_contacts review --dry-run honored"

# ---------------------------------------------------------------------------
step "11. llm_review_contacts review against fake OpenRouter (no real spend)"
# ---------------------------------------------------------------------------
# Background a tiny Python HTTP server that mimics OpenRouter and returns a
# deterministic SKIP for the unmatched contact. Then point the primitive at it.
cat > "$TMP/fake_openrouter.py" <<'PYEOF'
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self,*a,**k): return
    def do_POST(self):
        n=int(self.headers.get("Content-Length") or 0)
        body=json.loads(self.rfile.read(n)) if n else {}
        content=body.get("messages",[{}])[0].get("content","")
        try:
            j=content.split("Contacts to evaluate:\n",1)[1].split("\n\nRespond",1)[0]
            cs=json.loads(j)
        except Exception:
            cs=[]
        out=[{"idx":c["idx"],"name":c["name"],
              "verdict":("SKIP" if "plumber" in c["name"].lower() else "ENRICH"),
              "reason":"smoke test"} for c in cs]
        resp=json.dumps({"choices":[{"message":{"content":json.dumps({"results":out})}}],
                         "usage":{"prompt_tokens":100,"completion_tokens":50}}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(resp))); self.end_headers(); self.wfile.write(resp)
HTTPServer(("127.0.0.1",int(sys.argv[1])),H).serve_forever()
PYEOF
FAKE_PORT=$($PY -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')
$PY "$TMP/fake_openrouter.py" "$FAKE_PORT" > "$TMP/fake_or.log" 2>&1 &
FAKE_PID=$!
sleep 0.5
trap 'kill $FAKE_PID 2>/dev/null || true; echo; echo "[smoke] artifacts: $TMP"' EXIT
POWERPACKS_OPENROUTER_BASE="http://127.0.0.1:$FAKE_PORT/api/v1" \
    $PY "$PRIMS/llm_review_contacts/llm_review_contacts.py" review \
    --input "$TMP/contacts.csv" \
    --api-key smoke \
    --model openai/gpt-4.1-mini \
    --results "$TMP/llm.results.jsonl" \
    --manifest "$TMP/llm.manifest.json" > "$TMP/llm.stdout.json" 2>"$TMP/llm.stderr" || true
kill $FAKE_PID 2>/dev/null || true
$PY -c "
import json
d = json.load(open('$TMP/llm.stdout.json'))
print(f'  status={d[\"status\"]} verdicts={d[\"counts\"][\"verdicts\"]} skip={d[\"counts\"][\"skip\"]} enrich={d[\"counts\"][\"enrich\"]}')
"
ok "llm_review_contacts updated CSV via fake OpenRouter"

printf '\n\033[1;32mall smoke checks passed\033[0m\n'
echo "tempdir: $TMP"

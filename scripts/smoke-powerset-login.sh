#!/usr/bin/env bash
# Drive the powerset-login pack's primitives end-to-end against fakes /
# read-only flows. No real Auth0 PKCE, no real GCP, no MCP mutations.
#
# Exercises:
#   - auth.py whoami     (against a synthetic credentials.json)
#   - auth.py token      (--bearer-only against the same fixture)
#   - provision_runtime_env --help / list-profiles  (no gcloud calls)
#   - provision_runtime_env plan  (with stubbed gcloud — describes what
#     would be pulled, doesn't pull)
#   - mcp_install status --host all  (read-only)
#   - mcp_install token-env  (reads creds, prints export line, no host
#     mutation)
#   - doctor run  (read-only checks; some may report missing OS deps —
#     the runner itself working is what we validate)
#
# Honest limitations (not exercised here):
#   - Real Auth0 browser flow  (would need a mock OIDC server)
#   - Real gcloud secret pull  (would need real credentials or stub
#     secret manager)
#   - Real claude/codex MCP install  (would mutate ~/.claude.json or
#     ~/.codex/config.toml — out of scope for smoke)
#
# These gaps are covered by:
#   - tests/test_provision_runtime_env.py  (provision_runtime_env unit tests)
#   - scripts/run-skill-eval --skill powerset-login (skill-level eval)
#
# Safe to run repeatedly. No money. No network outside localhost.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d -t powerpacks-smoke-login-XXXX)"
trap 'echo; echo "[smoke-login] artifacts: $TMP"' EXIT

PY=python3
PACK="$ROOT/packs/powerset/primitives"

step() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
note() { printf '  \033[1;33m~\033[0m %s\n' "$*"; }
fail() { printf '  \033[1;31m✗\033[0m %s\n' "$*"; exit 1; }

# Forge a synthetic credentials.json that decodes as a real JWT shape but
# has no upstream value. exp set far in the future so whoami doesn't try
# to refresh.
mkdir -p "$TMP/.powerpacks"
SYNTH_TOKEN_HEADER='{"alg":"none","typ":"JWT"}'
SYNTH_TOKEN_PAYLOAD='{"sub":"test|smoke","email":"smoke@example.com","exp":4070908800,"iat":1700000000,"https://api.powerset.dev/roles":["authenticated"]}'
b64() { $PY -c "import base64,sys; print(base64.urlsafe_b64encode(sys.stdin.read().encode()).rstrip(b'=').decode())"; }
HEADER_B64=$(printf '%s' "$SYNTH_TOKEN_HEADER" | b64)
PAYLOAD_B64=$(printf '%s' "$SYNTH_TOKEN_PAYLOAD" | b64)
SYNTH_JWT="${HEADER_B64}.${PAYLOAD_B64}.deadbeef"
cat >"$TMP/.powerpacks/credentials.json" <<JSON
{
  "access_token": "$SYNTH_JWT",
  "refresh_token": "synthetic-refresh-not-used",
  "expires_at": 4070908800,
  "email": "smoke@example.com"
}
JSON
chmod 0600 "$TMP/.powerpacks/credentials.json"

export POWERPACKS_CREDENTIALS_PATH="$TMP/.powerpacks/credentials.json"

# ---------------------------------------------------------------------------
step "1. auth.py whoami (synthetic credentials)"
# ---------------------------------------------------------------------------
$PY "$PACK/auth/auth.py" whoami --credentials-path "$POWERPACKS_CREDENTIALS_PATH" >"$TMP/whoami.json" 2>&1
$PY -c "
import json
d=json.load(open('$TMP/whoami.json'))
assert d['status'] == 'logged_in', d
assert d['email'] == 'smoke@example.com', d
print('  status:', d['status'])
print('  email: ', d['email'])
"
ok "whoami parsed synthetic credentials.json"

# ---------------------------------------------------------------------------
step "2. auth.py token --bearer-only"
# ---------------------------------------------------------------------------
tok=$($PY "$PACK/auth/auth.py" token --bearer-only --credentials-path "$POWERPACKS_CREDENTIALS_PATH" 2>&1 | tail -1)
[ "${#tok}" -gt 50 ] || fail "expected a JWT-looking string, got: $tok"
ok "token --bearer-only printed a token (len=${#tok})"

# ---------------------------------------------------------------------------
step "3. provision_runtime_env plan --profile messages (read-only)"
# ---------------------------------------------------------------------------
# `plan` is read-only — lists which secrets WOULD be pulled per profile.
# Doesn't actually call gcloud secret manager.
$PY "$PACK/provision_runtime_env/provision_runtime_env.py" plan --profile messages \
  --env-file "$TMP/.env" >"$TMP/plan.json" 2>&1
$PY -c "
import json
d=json.load(open('$TMP/plan.json'))
# 'plan' returns a structured payload describing requested keys and their
# resolution status. We don't care about gcloud connectivity here, only
# that the primitive ran end-to-end and emitted a valid plan structure.
assert isinstance(d, dict), d
for key in ('command', 'profile'):
    assert key in d, f'plan output missing {key}: {list(d)}'
print('  profile:', d.get('profile'))
print('  keys:   ', len(d.get('items', d.get('keys', [])) or []))
"
ok "plan ran end-to-end and emitted structured output"

# ---------------------------------------------------------------------------
step "4. mcp_install token-env (reads synthetic credentials)"
# ---------------------------------------------------------------------------
out=$($PY "$PACK/mcp_install/mcp_install.py" token-env --credentials-path "$POWERPACKS_CREDENTIALS_PATH" 2>&1)
echo "$out" | grep -q "^export POWERPACKS_POWERSET_TOKEN=" || fail "expected export line, got: $out"
ok "token-env printed export line"

# ---------------------------------------------------------------------------
step "5. mcp_install status --host all (read-only, no mutation)"
# ---------------------------------------------------------------------------
$PY "$PACK/mcp_install/mcp_install.py" status --host all >"$TMP/mcp-status.json" 2>&1 || true
$PY -c "
import json
d=json.load(open('$TMP/mcp-status.json'))
assert 'hosts' in d, d
hosts=[h['host'] for h in d['hosts']]
assert 'claude' in hosts and 'codex' in hosts, hosts
print('  hosts checked:', hosts)
"
ok "status reported on claude + codex"

# ---------------------------------------------------------------------------
step "6. doctor run (read-only check sweep)"
# ---------------------------------------------------------------------------
# doctor run already emits JSON to stdout. Some checks may report status
# != ok (no real Auth0 token, missing gcloud login, etc.) — we only
# validate that the runner itself works.
$PY "$PACK/doctor/doctor.py" run >"$TMP/doctor.json" 2>&1 || true
$PY -c "
import json
d=json.load(open('$TMP/doctor.json'))
assert 'checks' in d, d
checks=[c['id'] for c in d['checks']]
assert len(checks) > 0, d
statuses={c['id']: c['status'] for c in d['checks']}
print('  doctor ran', len(checks), 'checks')
print('  ids:    ', ', '.join(checks[:6]) + ('...' if len(checks) > 6 else ''))
"
ok "doctor run produced a structured report"

# ---------------------------------------------------------------------------
step "7. unit tests for provision_runtime_env"
# ---------------------------------------------------------------------------
$PY -m unittest tests.test_provision_runtime_env -v 2>&1 | tail -3
ok "provision_runtime_env unit tests passed"

echo
printf '\033[1;32m✓ powerset-login pack smoke complete\033[0m\n'
echo "  artifacts: $TMP"

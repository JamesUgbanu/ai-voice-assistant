#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# health-check.sh — reusable, provider-neutral verification for the voice
# service. Works against any host that runs the same application.
#
# It verifies:
#   1. the server is reachable over the network,
#   2. the public health endpoint responds 200 with status "ok" / db true,
#   3. TLS is valid (when scheme is https and verification is not disabled),
#   4. (optional) the secured remote RAG API is reachable using a bearer token
#      read from a file — the token is never printed or placed in argv.
#
# It does NOT prove STT/TTS/voice-cloning work; those require a real browser
# turn (see infra/README.md and the repo's manual checklist).
#
# Usage:
#   ./health-check.sh --host <fqdn-or-ip> [options]
#
# Options:
#   --host H            hostname or IP                              [required]
#   --scheme S          http | https (default: https)
#   --port N            TCP port (default: 443 for https, 80 for http)
#   --path P            health path (default: /healthz)
#   --timeout N         curl timeout seconds (default: 10)
#   --require-warm      fail unless the model warm flag is true
#   --rag-url URL       remote RAG endpoint to probe (optional)
#   --rag-token-file P  file containing the RAG bearer token (optional)
#   --insecure          do not verify TLS (local testing only)
#   -h, --help          show this help
#
# Exit codes: 0 all checks passed; 1 a check failed; 2 usage error.
# ---------------------------------------------------------------------------
set -euo pipefail

log()  { printf '[health] %s\n' "$*"; }
pass() { printf '[health] PASS: %s\n' "$*"; }
fail() { printf '[health] FAIL: %s\n' "$*" >&2; exit 1; }

HOST=""
SCHEME="https"
PORT=""
PATH_="/healthz"
TIMEOUT="10"
REQUIRE_WARM=0
RAG_URL=""
RAG_TOKEN_FILE=""
INSECURE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --host)           HOST="${2:-}"; shift 2 ;;
    --scheme)         SCHEME="${2:-}"; shift 2 ;;
    --port)           PORT="${2:-}"; shift 2 ;;
    --path)           PATH_="${2:-}"; shift 2 ;;
    --timeout)        TIMEOUT="${2:-}"; shift 2 ;;
    --require-warm)   REQUIRE_WARM=1; shift ;;
    --rag-url)        RAG_URL="${2:-}"; shift 2 ;;
    --rag-token-file) RAG_TOKEN_FILE="${2:-}"; shift 2 ;;
    --insecure)       INSECURE=1; shift ;;
    -h|--help)        sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)                fail "unknown argument: $1 (use --help)" ;;
  esac
done

[ -n "$HOST" ] || { printf 'usage error: --host is required\n' >&2; exit 2; }

command -v curl >/dev/null 2>&1 || fail "curl not found"

case "$SCHEME" in
  https) [ -n "$PORT" ] || PORT=443 ;;
  http)  [ -n "$PORT" ] || PORT=80 ;;
  *)     fail "--scheme must be http or https" ;;
esac

CURL_OPTS=(--silent --show-error --connect-timeout "$TIMEOUT" --max-time "$TIMEOUT")
[ "$INSECURE" -eq 1 ] && CURL_OPTS+=(--insecure)
[ "$INSECURE" -eq 1 ] && log "TLS verification DISABLED (--insecure) — do not use against production"

BASE="${SCHEME}://${HOST}"
if { [ "$SCHEME" = "https" ] && [ "$PORT" != "443" ]; } || { [ "$SCHEME" = "http" ] && [ "$PORT" != "80" ]; }; then
  BASE="${SCHEME}://${HOST}:${PORT}"
fi
URL="${BASE}${PATH_}"

# ---------------------------------------------------------------------------
# 1. Reachability + HTTP status + TLS (curl verifies the cert unless --insecure)
# ---------------------------------------------------------------------------
log "GET ${URL}"
HTTP_CODE="$(curl "${CURL_OPTS[@]}" -o /tmp/voice-health-body.$$ -w '%{http_code}' "$URL" || true)"
BODY="$(cat /tmp/voice-health-body.$$ 2>/dev/null || true)"
rm -f /tmp/voice-health-body.$$

if [ "$HTTP_CODE" != "200" ]; then
  fail "health endpoint returned HTTP '${HTTP_CODE:-<none>}' (expected 200)"
fi
pass "reachable, HTTP 200"
[ "$SCHEME" = "https" ] && [ "$INSECURE" -eq 0 ] && pass "TLS certificate verified"

# ---------------------------------------------------------------------------
# 2. Parse the health payload with python3 (available on host and locally).
# ---------------------------------------------------------------------------
command -v python3 >/dev/null 2>&1 || fail "python3 not found (needed to parse the health JSON)"

if ! STATUS="$(printf '%s' "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("status",""))' 2>/dev/null)"; then
  fail "health response was not valid JSON"
fi
DB="$(printf '%s' "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(str(d.get("db",False)).lower())')"
WARM="$(printf '%s' "$BODY" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(str(d.get("warm",False)).lower())')"

[ "$STATUS" = "ok" ] || fail "health status is '${STATUS}' (expected 'ok')"
pass "status=ok"
[ "$DB" = "true" ] || fail "database check failed (db=${DB})"
pass "db=true"
if [ "$REQUIRE_WARM" -eq 1 ]; then
  [ "$WARM" = "true" ] || fail "models not warm (warm=${WARM})"
  pass "warm=true"
else
  log "warm=${WARM} (informational; use --require-warm to enforce)"
fi

# ---------------------------------------------------------------------------
# 3. Optional: secured remote RAG connectivity (token never printed).
# ---------------------------------------------------------------------------
if [ -n "$RAG_URL" ]; then
  [ -n "$RAG_TOKEN_FILE" ] || fail "--rag-url requires --rag-token-file"
  [ -f "$RAG_TOKEN_FILE" ] || fail "RAG token file not found: $RAG_TOKEN_FILE"
  RAG_TOKEN="$(tr -d '\r\n' < "$RAG_TOKEN_FILE")"
  [ -n "$RAG_TOKEN" ] || fail "RAG token file is empty"

  log "probing remote RAG endpoint (authorization redacted)"
  # Pass the header via curl config on stdin so the token never appears in argv.
  RAG_CODE="$(
    printf 'header = "Authorization: Bearer %s"\n' "$RAG_TOKEN" \
      | curl "${CURL_OPTS[@]}" -o /dev/null -w '%{http_code}' -K - \
          -X POST -H 'Content-Type: application/json' \
          --data '{"question":"connectivity probe","history":[],"debug":false}' \
          "$RAG_URL" || true
  )"
  unset RAG_TOKEN
  case "$RAG_CODE" in
    200|201|204) pass "RAG reachable (HTTP ${RAG_CODE})" ;;
    401|403)     fail "RAG rejected the bearer token (HTTP ${RAG_CODE}) — check VOICE_RAG_SERVICE_TOKEN" ;;
    000|"")      fail "RAG unreachable (no response)" ;;
    *)           fail "RAG returned HTTP ${RAG_CODE}" ;;
  esac
else
  log "RAG probe skipped (no --rag-url supplied)"
fi

log "all checks passed"

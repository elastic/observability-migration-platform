#!/usr/bin/env bash
# Bootstraps agent-browser for the verifier framework.
#
#   - ensures the binary is installed
#   - provisions Chrome for Testing
#   - reserves a dedicated profile directory
#   - opens Kibana in headed mode so the operator can SAML once
#   - saves the resulting auth state for headless reuse
#
# Run once per cluster. Subsequent verifier invocations consume
# ${VERIFIER_STATE_FILE} without going through SAML again.

set -euo pipefail

KIBANA_URL="${KIBANA_URL:?KIBANA_URL is required (e.g. https://<cluster>.kb.us-central1.gcp.staging.elastic.cloud)}"
PROFILE_DIR="${VERIFIER_PROFILE_DIR:-$HOME/.agent-browser/profiles/mig-to-kbn-verifier}"
STATE_FILE="${VERIFIER_STATE_FILE:-$HOME/.agent-browser/state/mig-to-kbn-verifier.json}"
WAIT_SECONDS="${VERIFIER_LOGIN_WAIT_SECONDS:-120}"

mkdir -p "$PROFILE_DIR" "$(dirname "$STATE_FILE")"

if ! command -v agent-browser >/dev/null 2>&1; then
  echo "agent-browser not on PATH; install with: npm install -g agent-browser" >&2
  exit 1
fi

echo "==> agent-browser doctor (quick)"
agent-browser doctor --quick || true

echo
echo "==> ensuring Chrome for Testing is installed"
agent-browser install >/dev/null || true

echo
echo "==> opening Kibana in headed mode against profile: $PROFILE_DIR"
echo "    you will see a Chrome window; complete the SAML login there."
echo "    leave the window open until this script tells you to close it."

agent-browser close --all >/dev/null 2>&1 || true
agent-browser --profile "$PROFILE_DIR" --headed open "$KIBANA_URL/app/home" >/dev/null

echo
echo "Waiting up to ${WAIT_SECONDS}s for the URL to settle inside /app/* (i.e. SAML complete)."
deadline=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < deadline )); do
  current_url="$(agent-browser get url 2>/dev/null | tail -1 || true)"
  case "$current_url" in
    *"/app/"*)
      echo "Detected logged-in URL: $current_url"
      break
      ;;
  esac
  sleep 3
done

current_url="$(agent-browser get url 2>/dev/null | tail -1 || true)"
case "$current_url" in
  *"/app/"*) : ;;
  *)
    echo "Did not reach /app/* after ${WAIT_SECONDS}s; aborting (still at: $current_url)" >&2
    exit 2
    ;;
esac

echo
echo "==> saving auth state to $STATE_FILE"
agent-browser state save "$STATE_FILE"

echo
echo "Bootstrap complete."
echo "  PROFILE_DIR=$PROFILE_DIR"
echo "  STATE_FILE=$STATE_FILE"
echo
echo "Future headless runs:"
echo "  agent-browser --state \"$STATE_FILE\" open \"$KIBANA_URL/app/dashboards\""

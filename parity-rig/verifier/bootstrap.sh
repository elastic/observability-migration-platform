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

# Derive the bare Kibana host so we don't false-positive on
# upstream SAML redirects whose URL happens to contain "/app/"
# (e.g. elastic.okta.com/app/google/.../sso/saml).
KIBANA_HOST="$(echo "$KIBANA_URL" | awk -F[/:] '{print $4}')"
if [[ -z "$KIBANA_HOST" ]]; then
  echo "Could not parse host from KIBANA_URL: $KIBANA_URL" >&2
  exit 1
fi

is_logged_in() {
  local url="$1"
  # Must be hosted on the Kibana origin AND inside /app/* AND NOT
  # the security capture-url interstitial.
  case "$url" in
    *"${KIBANA_HOST}/app/"*)
      case "$url" in
        *"/internal/security/capture-url"*) return 1 ;;
        *"auth_provider_hint"*)             return 1 ;;
        *)                                   return 0 ;;
      esac
      ;;
  esac
  return 1
}

echo
echo "Waiting up to ${WAIT_SECONDS}s for browser to settle on https://${KIBANA_HOST}/app/* (SAML complete)."
deadline=$((SECONDS + WAIT_SECONDS))
while (( SECONDS < deadline )); do
  current_url="$(agent-browser get url 2>/dev/null | tail -1 || true)"
  if is_logged_in "$current_url"; then
    echo "Detected logged-in URL: $current_url"
    break
  fi
  sleep 3
done

current_url="$(agent-browser get url 2>/dev/null | tail -1 || true)"
if ! is_logged_in "$current_url"; then
  echo "Did not land on https://${KIBANA_HOST}/app/* after ${WAIT_SECONDS}s; aborting" >&2
  echo "  current URL: $current_url" >&2
  exit 2
fi

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

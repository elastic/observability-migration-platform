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
  url="$1"
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

# Enumerate the URLs of EVERY open tab/target, one per line.
#
# An agent-browser session frequently carries multiple tabs — Kibana tabs PLUS
# unrelated ones such as Chrome's Gemini "glic" side-panel
# (https://gemini.google.com/glic) or staging.found.no. The *active* target is
# often the wrong one, so `agent-browser get url` alone reads the wrong page and
# never sees the Kibana /app/* URL even after SAML completed in the Kibana tab.
# We therefore scan all tabs (`tab list`) and fall back to the active URL.
list_all_tab_urls() {
  agent-browser tab list 2>/dev/null \
    | grep -oE 'https?://[^[:space:]]+' || true
  # Belt-and-suspenders: include the active tab's URL too.
  agent-browser get url 2>/dev/null | grep -oE 'https?://[^[:space:]]+' || true
}

# True (0) when ANY open tab is a logged-in Kibana /app/* page. Echoes the
# matching URL so the caller can report it.
any_tab_logged_in() {
  found=""
  while IFS= read -r url; do
    [ -n "$url" ] || continue
    if is_logged_in "$url"; then
      found="$url"
      break
    fi
  done <<EOF
$(list_all_tab_urls)
EOF
  if [ -n "$found" ]; then
    printf '%s\n' "$found"
    return 0
  fi
  return 1
}

echo
echo "Waiting up to ${WAIT_SECONDS}s for ANY tab to settle on https://${KIBANA_HOST}/app/* (SAML complete)."
echo "    (tolerant of extra tabs: gemini-glic side-panel, staging.found.no, SSO interstitials)"
deadline=$((SECONDS + WAIT_SECONDS))
logged_in_url=""
while (( SECONDS < deadline )); do
  if logged_in_url="$(any_tab_logged_in)"; then
    echo "Detected logged-in Kibana tab: $logged_in_url"
    break
  fi
  sleep 3
done

if ! logged_in_url="$(any_tab_logged_in)"; then
  echo "No tab landed on https://${KIBANA_HOST}/app/* after ${WAIT_SECONDS}s; aborting" >&2
  echo "  open tabs:" >&2
  list_all_tab_urls | sort -u | sed 's/^/    /' >&2 || true
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

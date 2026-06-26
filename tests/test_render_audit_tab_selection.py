# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

"""Unit tests for the pure Kibana-tab/page selection used by the render audit.

The render audit drives a logged-in ``agent-browser`` session on Serverless.
That session can carry MULTIPLE tabs/targets — Kibana dashboard tabs *plus*
unrelated ones such as Chrome's Gemini "glic" side-panel
(``https://gemini.google.com/glic``). The active target is frequently the wrong
tab, so URL checks and login-detection read the wrong page.

``select_kibana_page_url`` is the pure rule that, given the URLs of every open
tab, picks the Kibana ``/app/*`` page that matches the target host (and, when
known, the target dashboard id) while ignoring non-Kibana tabs and Elastic SSO
interstitials. It is unit-tested here WITHOUT a browser; the browser-driving
parts are integration-only.
"""

from __future__ import annotations

import json

from observability_migration.targets.kibana.render_audit_driver import (
    activate_kibana_tab,
    parse_agent_browser_tabs,
    select_kibana_page,
    select_kibana_page_url,
    select_kibana_tab_id,
)

KIBANA_HOST = "my-cluster.kb.us-central1.gcp.staging.elastic.cloud"
DASH_ID = "da9dfb0d-52e8-2f88-541f-fab582497dc9"
OTHER_DASH_ID = "11111111-2222-3333-4444-555555555555"

DASHBOARD_VIEW_URL = (
    f"https://{KIBANA_HOST}/app/dashboards#/view/{DASH_ID}?embed=true"
    "&_g=(time:(from:now-24h,to:now))"
)
KIBANA_HOME_URL = f"https://{KIBANA_HOST}/app/home"
GEMINI_GLIC_URL = "https://gemini.google.com/glic"
CAPTURE_URL_INTERSTITIAL = (
    f"https://{KIBANA_HOST}/internal/security/capture-url"
    "?next=%2Fapp%2Fdashboards&auth_provider_hint=cloud-saml"
)
FOUND_NO_SSO_URL = "https://staging.found.no/login?some=thing"
OKTA_SAML_URL = "https://elastic.okta.com/app/google/abc123/sso/saml"


def test_picks_dashboard_view_tab_over_gemini_home_and_interstitial():
    urls = [
        GEMINI_GLIC_URL,
        KIBANA_HOME_URL,
        DASHBOARD_VIEW_URL,
        FOUND_NO_SSO_URL,
        CAPTURE_URL_INTERSTITIAL,
    ]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) == DASHBOARD_VIEW_URL


def test_ignores_gemini_glic_side_panel():
    # gemini.google.com/glic must never be selected even when it is the only
    # extra tab next to a real Kibana tab.
    urls = [GEMINI_GLIC_URL, DASHBOARD_VIEW_URL]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) == DASHBOARD_VIEW_URL


def test_ignores_capture_url_and_auth_provider_hint_interstitials():
    # The only Kibana-host tab is the security capture-url interstitial — it is
    # NOT a logged-in page, so there is no usable Kibana tab.
    urls = [GEMINI_GLIC_URL, CAPTURE_URL_INTERSTITIAL]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) is None


def test_ignores_upstream_okta_saml_app_url():
    # Upstream IdP URLs contain "/app/" but are NOT on the Kibana host.
    urls = [OKTA_SAML_URL, GEMINI_GLIC_URL]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) is None


def test_returns_none_when_no_kibana_tab():
    urls = [GEMINI_GLIC_URL, FOUND_NO_SSO_URL, "about:blank", ""]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) is None


def test_prefers_dashboard_id_match_among_multiple_kibana_tabs():
    other_view = f"https://{KIBANA_HOST}/app/dashboards#/view/{OTHER_DASH_ID}"
    urls = [KIBANA_HOME_URL, other_view, DASHBOARD_VIEW_URL]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) == DASHBOARD_VIEW_URL


def test_falls_back_to_any_kibana_app_tab_when_dashboard_id_unknown():
    # No dashboard id supplied -> any logged-in Kibana /app/* tab is acceptable.
    urls = [GEMINI_GLIC_URL, KIBANA_HOME_URL]
    assert select_kibana_page_url(urls, KIBANA_HOST, None) == KIBANA_HOME_URL


def test_dashboard_id_not_open_falls_back_to_other_kibana_app_tab():
    # The requested dashboard tab is not open, but another Kibana /app tab is;
    # we still return a Kibana page rather than None so login is detected.
    urls = [GEMINI_GLIC_URL, KIBANA_HOME_URL]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) == KIBANA_HOME_URL


def test_host_match_is_exact_not_substring():
    # An attacker-ish look-alike host that merely *contains* the Kibana host as a
    # substring must not be treated as the Kibana origin.
    evil = f"https://{KIBANA_HOST}.evil.example/app/home"
    urls = [evil]
    assert select_kibana_page_url(urls, KIBANA_HOST, None) is None


def test_kibana_host_may_be_passed_with_scheme_or_trailing_slash():
    # Callers may hand us a full KIBANA_URL; normalize it to the bare host.
    urls = [GEMINI_GLIC_URL, DASHBOARD_VIEW_URL]
    assert (
        select_kibana_page_url(urls, f"https://{KIBANA_HOST}/", DASH_ID)
        == DASHBOARD_VIEW_URL
    )


def test_space_scoped_app_url_is_a_valid_kibana_tab():
    space_view = (
        f"https://{KIBANA_HOST}/s/team-a/app/dashboards#/view/{DASH_ID}?embed=true"
    )
    urls = [GEMINI_GLIC_URL, space_view]
    assert select_kibana_page_url(urls, KIBANA_HOST, DASH_ID) == space_view


def test_select_kibana_page_operates_on_objects_with_url_attr():
    class _Page:
        def __init__(self, url: str) -> None:
            self.url = url

        def __eq__(self, other: object) -> bool:
            return isinstance(other, _Page) and other.url == self.url

    pages = [_Page(GEMINI_GLIC_URL), _Page(KIBANA_HOME_URL), _Page(DASHBOARD_VIEW_URL)]
    chosen = select_kibana_page(pages, KIBANA_HOST, DASH_ID)
    assert chosen is not None
    assert chosen.url == DASHBOARD_VIEW_URL


def test_select_kibana_page_returns_none_with_no_kibana_pages():
    class _Page:
        def __init__(self, url: str) -> None:
            self.url = url

    pages = [_Page(GEMINI_GLIC_URL), _Page(FOUND_NO_SSO_URL)]
    assert select_kibana_page(pages, KIBANA_HOST, DASH_ID) is None


# ----- agent-browser ``tab list --json`` integration (injected driver) ----- #

# Verbatim shape captured from a real, multi-tab agent-browser session
# (t1/t2/t4 = Kibana tabs, t3 = staging.found.no, t5 = gemini glic webview).
_LIVE_TAB_LIST_JSON = json.dumps(
    {
        "success": True,
        "data": {
            "tabs": [
                {
                    "active": True,
                    "tabId": "t1",
                    "type": "page",
                    "title": "Other dashboard - Elastic",
                    "url": f"https://{KIBANA_HOST}/app/dashboards#/view/{OTHER_DASH_ID}",
                },
                {
                    "active": False,
                    "tabId": "t2",
                    "type": "page",
                    "title": "Elastic",
                    "url": f"https://{KIBANA_HOST}/app/home#/",
                },
                {
                    "active": False,
                    "tabId": "t3",
                    "type": "page",
                    "title": "...",
                    "url": "https://staging.found.no/home#uTargetOrganizationId=1",
                },
                {
                    "active": False,
                    "tabId": "t4",
                    "type": "page",
                    "title": "Elastic",
                    "url": f"https://{KIBANA_HOST}/app/dashboards#/view/{DASH_ID}?embed=true",
                },
                {
                    "active": False,
                    "tabId": "t5",
                    "type": "webview",
                    "title": "Google Gemini",
                    "url": GEMINI_GLIC_URL,
                },
            ]
        },
        "error": None,
    }
)


def test_parse_agent_browser_tabs_extracts_id_url_pairs():
    pairs = parse_agent_browser_tabs(_LIVE_TAB_LIST_JSON)
    ids = [tab_id for tab_id, _ in pairs]
    assert ids == ["t1", "t2", "t3", "t4", "t5"]


def test_parse_agent_browser_tabs_tolerates_garbage():
    assert parse_agent_browser_tabs("not json") == []
    assert parse_agent_browser_tabs("") == []
    assert parse_agent_browser_tabs(json.dumps({"data": {}})) == []


def test_select_kibana_tab_id_picks_dashboard_tab_among_live_tabs():
    pairs = parse_agent_browser_tabs(_LIVE_TAB_LIST_JSON)
    # t4 is the tab whose URL carries the requested dashboard id.
    assert select_kibana_tab_id(pairs, KIBANA_HOST, DASH_ID) == "t4"


def test_select_kibana_tab_id_none_when_no_kibana_tab():
    pairs = [("t1", GEMINI_GLIC_URL), ("t2", FOUND_NO_SSO_URL)]
    assert select_kibana_tab_id(pairs, KIBANA_HOST, DASH_ID) is None


def test_activate_kibana_tab_switches_to_matching_tab():
    calls: list[list[str]] = []

    def driver(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["tab", "list"]:
            return _LIVE_TAB_LIST_JSON
        return ""

    tab_id = activate_kibana_tab(
        f"https://{KIBANA_HOST}", DASH_ID, tab_driver=driver
    )
    assert tab_id == "t4"
    # It listed tabs, then switched to t4.
    assert calls[0] == ["tab", "list", "--json"]
    assert ["tab", "t4"] in calls


def test_activate_kibana_tab_returns_none_and_does_not_switch_without_kibana_tab():
    calls: list[list[str]] = []
    only_gemini = json.dumps(
        {"data": {"tabs": [{"tabId": "t1", "type": "webview", "url": GEMINI_GLIC_URL}]}}
    )

    def driver(args: list[str]) -> str:
        calls.append(args)
        return only_gemini if args[:2] == ["tab", "list"] else ""

    tab_id = activate_kibana_tab(f"https://{KIBANA_HOST}", DASH_ID, tab_driver=driver)
    assert tab_id is None
    # Only the list call happened; we never switched to a non-Kibana tab.
    assert calls == [["tab", "list", "--json"]]


def test_run_audit_cli_activates_tab_before_fetch():
    import argparse

    from observability_migration.targets.kibana.render_audit_driver import run_audit_cli

    events: list[str] = []

    def driver(args: list[str]) -> str:
        if args[:2] == ["tab", "list"]:
            events.append("list")
            return _LIVE_TAB_LIST_JSON
        if args[:1] == ["tab"]:
            events.append(f"switch:{args[1]}")
        return ""

    def fetch(_url: str) -> str:
        events.append("fetch")
        return '<div class="lnsExpressionRenderer">chart rendered</div>'

    args = argparse.Namespace(
        kibana_url=f"https://{KIBANA_HOST}", dashboard_id=DASH_ID, space="",
        user_data_dir="", time_from="now-1h", time_to="now", fail_on_error=True,
        elements=False, migration_out="", agent_browser=True,
    )
    rc = run_audit_cli(args, dom_fetcher=fetch, tab_driver=driver)
    assert rc == 0
    # The tab is selected+activated strictly before the DOM is captured.
    assert events == ["list", "switch:t4", "fetch"]

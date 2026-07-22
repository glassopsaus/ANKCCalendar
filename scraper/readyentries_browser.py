#!/usr/bin/env python3
"""
Ready Entries scraper via headless browser (Playwright).

WHY THIS EXISTS: readyentries.com is a Bubble.io single-page app. Its event
data is fetched through Bubble's /api/1.1/init/... search API using an
ENCRYPTED, session-bound request payload ({x, y, z}) that we cannot construct
or replay server-side. The only way to obtain the data is to let the page's
own JavaScript run in a real browser, which builds the encrypted query and
receives a plain-JSON response.

STRATEGY: rather than parse the rendered DOM (fragile — Bubble markup is
opaque and changes), we INTERCEPT the network responses. When the page loads
view-events, Bubble fetches event objects and the browser receives clean JSON
with fields like name_text / start_date_date / state_option_state /
entry_status_text. We capture those response bodies and return the raw event
objects for scrape.py to normalise.

The GitHub Action installs Playwright + Chromium (already required by Top Dog):
    pip install playwright
    python -m playwright install --with-deps chromium

If Playwright/browser is unavailable, get_readyentries_events() returns None so
the caller can skip Ready Entries without crashing. This is a best-effort,
FAIL-SAFE source: any error yields None/empty rather than breaking the run.
"""

import json
import sys
import time

VIEW_EVENTS_URL = "https://readyentries.com/view-events"
NAV_TIMEOUT_MS = 45000
# How long to wait (seconds) after load for the event data calls to arrive.
COLLECT_SECONDS = 12
# Bubble returns event objects with _type like "custom.event".
EVENT_TYPE_HINT = "event"


def get_readyentries_events():
    """Return a list of raw Ready Entries event objects (dicts with Bubble
    field names), or None if the browser can't run / nothing was captured.

    We attach a response listener BEFORE navigating, so we catch the data
    calls Bubble fires on page load. Each qualifying response body is scanned
    for event records, which are collected and de-duplicated by _id.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception as e:
        print(f"[readyentries] Playwright unavailable: {e}", file=sys.stderr)
        return None

    collected = {}  # _id -> raw event object (dedupe across responses)

    def _harvest_json(obj):
        """Recursively find Bubble event records in a decoded JSON structure.
        A record looks like {"_id":..., "_type":"custom.event", ...} or is
        wrapped as {"data": {...}, "type": "custom.event"}. We accept anything
        whose type/_type contains 'event' and that carries a name/date field."""
        found = []

        def _looks_like_event(d):
            if not isinstance(d, dict):
                return False
            t = str(d.get("_type") or d.get("type") or "").lower()
            if EVENT_TYPE_HINT not in t:
                return False
            # must have at least a name and a start date to be useful
            keys = set(d.keys())
            has_name = any(k.startswith("name_") for k in keys) or "name_text" in keys
            has_date = any("start_date" in k for k in keys)
            return has_name and has_date

        def _walk(node):
            if isinstance(node, dict):
                # Bubble sometimes wraps the record under "data"
                inner = node.get("data") if isinstance(node.get("data"), dict) else None
                if inner is not None and _looks_like_event(inner):
                    found.append(inner)
                elif _looks_like_event(node):
                    found.append(node)
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(obj)
        return found

    def _on_response(response):
        try:
            url = response.url
            # Only look at Bubble API / data responses; skip assets.
            if "/api/1.1/" not in url and "elasticsearch" not in url \
                    and "/msearch" not in url:
                return
            ctype = (response.headers or {}).get("content-type", "")
            if "json" not in ctype.lower():
                # some Bubble responses are json without the header; try anyway
                pass
            try:
                body = response.json()
            except Exception:
                try:
                    body = json.loads(response.text())
                except Exception:
                    return
            for ev in _harvest_json(body):
                _id = ev.get("_id") or ev.get("id")
                if _id and _id not in collected:
                    collected[_id] = ev
        except Exception:
            # never let a listener error interrupt the walk
            return

    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"[readyentries] Chromium launch failed: {e}",
                      file=sys.stderr)
                return None
            ctx = browser.new_context(
                user_agent="TrackingCalendarBot/1.0 (+combined tracking events)")
            page = ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)
            page.on("response", _on_response)

            try:
                page.goto(VIEW_EVENTS_URL, wait_until="networkidle")
            except PWTimeout:
                pass  # data calls may still have landed; fall through

            # Give lazy/secondary data calls time to arrive, and nudge the page
            # (scroll) in case the list lazy-loads more on scroll.
            deadline = time.time() + COLLECT_SECONDS
            last_count = -1
            while time.time() < deadline:
                page.wait_for_timeout(1500)
                try:
                    page.mouse.wheel(0, 4000)
                except Exception:
                    pass
                if len(collected) == last_count:
                    # no growth for a cycle after we've got something → stop early
                    if collected:
                        break
                last_count = len(collected)

            browser.close()
    except Exception as e:
        print(f"[readyentries] session error: {e}", file=sys.stderr)
        return list(collected.values()) or None

    print(f"[readyentries] captured {len(collected)} event object(s)",
          file=sys.stderr)
    return list(collected.values()) or None


if __name__ == "__main__":
    evs = get_readyentries_events()
    if evs is None:
        print("no events (browser unavailable or nothing captured)")
    else:
        print(f"got {len(evs)} events")
        # Print a compact sample so we can confirm field names on first run.
        for e in evs[:3]:
            keys = sorted(e.keys())
            print("  fields:", keys)
            print("  sample:", {k: e.get(k) for k in
                  ("name_text", "start_date_date", "end_date_date",
                   "state_option_state", "entry_status_text",
                   "entries_close_date", "year_number") if k in e})

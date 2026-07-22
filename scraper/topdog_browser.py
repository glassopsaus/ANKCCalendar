#!/usr/bin/env python3
"""
Top Dog Events scraper via headless browser (Playwright).

WHY THIS EXISTS: topdogevents.com.au/trials paginates entirely in JavaScript.
Plain HTTP (requests) always returns page 1 regardless of ?upcoming_page=N, so
events past the first page are invisible — which is why real trials (e.g. the
North East Tracking & Scent Club trial, 23-25 May 2026) were being missed.

This module renders the page in a real browser, walks BOTH the "Upcoming" and
"Past" tabs, clicks through every pagination page, and returns the fully
rendered HTML of each distinct page. The caller (scrape.py) parses those pages
with the EXISTING, proven `_topdog_parse_rows` logic — we only replace the
fetch mechanism, not the parsing.

The GitHub Action installs Playwright + Chromium:
    pip install playwright
    python -m playwright install --with-deps chromium

If Playwright or the browser is unavailable, get_topdog_pages() returns None so
the caller can fall back to the old plain-HTTP path without crashing.
"""

import re
import sys

TRIALS_URL = "https://www.topdogevents.com.au/trials"

# Safety cap: the Past list has historically been ~75 pages. Never loop forever.
MAX_PAGES_PER_SECTION = 120
NAV_TIMEOUT_MS = 30000
# CSS selectors are kept broad + fallbacked because the site markup can shift.
# We locate the "next page" control by common patterns and stop when it's gone
# or disabled, or when the rendered table stops changing.


def get_topdog_pages(sections=("upcoming", "past")):
    """Return a list of (section, html) tuples — one per distinct rendered page
    across the requested sections — or None if the browser can't run. `section`
    is "upcoming" (enterable) or "past", which lets callers tell whether a
    trial's entries are still open.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except Exception as e:
        print(f"[topdog-browser] Playwright unavailable: {e}", file=sys.stderr)
        return None

    pages_html = []
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"[topdog-browser] Chromium launch failed: {e}",
                      file=sys.stderr)
                return None

            ctx = browser.new_context(
                user_agent="TrackingCalendarBot/1.0 (+combined tracking events)")
            page = ctx.new_page()
            page.set_default_timeout(NAV_TIMEOUT_MS)

            for section in sections:
                try:
                    _walk_section(page, section, pages_html, PWTimeout)
                except Exception as e:
                    print(f"[topdog-browser] {section} walk error: {e}",
                          file=sys.stderr)
                    # keep whatever we already collected; try the next section

            browser.close()
    except Exception as e:
        print(f"[topdog-browser] session error: {e}", file=sys.stderr)
        return pages_html or None

    print(f"[topdog-browser] collected {len(pages_html)} rendered page(s)",
          file=sys.stderr)
    return pages_html


def _walk_section(page, section, pages_html, PWTimeout):
    """Navigate through every page of a section by URL.

    Top Dog's pagination is plain server-side paging with directly addressable
    URLs (?f=SECTION&SECTION_page=N), NOT JavaScript click-paging. So we render
    each page URL in the browser (the browser is needed only to render the row
    content, which is JS-built) and read it. This is deterministic and reaches
    all pages, unlike clicking a "next" control which could stall mid-list.

    We still stop early when a page's rows repeat the previous page (we've run
    past the last real page) or a page has no rows.
    """
    page_param = f"{section}_page"
    prev_signature = None
    for page_num in range(1, MAX_PAGES_PER_SECTION + 1):
        url = f"{TRIALS_URL}?f={section}&{page_param}={page_num}"
        try:
            page.goto(url, wait_until="networkidle")
        except PWTimeout:
            # networkidle can time out on a heavy page; the content may still be
            # there, so fall through and read what rendered.
            pass
        page.wait_for_timeout(400)

        html = page.content()
        signature = _table_signature(html)

        if not signature:
            print(f"[topdog-browser] {section} p{page_num}: empty, stopping",
                  file=sys.stderr)
            break
        if signature == prev_signature:
            print(f"[topdog-browser] {section} p{page_num}: unchanged "
                  f"(past last page), stopping", file=sys.stderr)
            break
        prev_signature = signature
        pages_html.append((section, html))
        # Diagnostic: how are per-event trial IDs represented (if at all) in the
        # captured HTML? Per-event links (/trials/<id>) tell us if IDs are
        # harvestable as hrefs; data-* attributes with digits tell us if the ID
        # is carried some other way (e.g. a click handler's data-trial-id).
        _nlinks = len(re.findall(r"/trials/\d+", html))
        extra = ""
        if page_num == 1 and section == "upcoming":
            # One-off structural probe: dump the HTML context around the FIRST
            # per-event link so we can see how the trial title relates to the
            # link (which container, heading, siblings) and build matching that
            # actually works — rather than inferring the structure.
            try:
                from bs4 import BeautifulSoup as _BS
                _soup = _BS(html, "html.parser")
                _a = None
                for _cand in _soup.find_all("a", href=True):
                    if not re.search(r"/trials/\d+", _cand["href"]):
                        continue
                    # Skip the "Running now" rail card — we want a LISTING link,
                    # which uses a different structure and is what we must match.
                    _anc = _cand
                    _in_rail = False
                    for _ in range(5):
                        _anc = getattr(_anc, "parent", None)
                        if _anc is None:
                            break
                        _cls = " ".join(_anc.get("class", [])) if hasattr(_anc, "get") else ""
                        if "trial-rail" in _cls:
                            _in_rail = True
                            break
                    if _in_rail:
                        continue
                    _a = _cand
                    break
                if _a is not None:
                    _ctx = _a
                    for _ in range(4):
                        if _ctx.parent is not None:
                            _ctx = _ctx.parent
                    snippet = str(_ctx)[:2000]
                    print(f"[topdog-browser] SAMPLE LISTING LINK CONTEXT:\n{snippet}\n"
                          f"[topdog-browser] END SAMPLE", file=sys.stderr)
            except Exception as _e:
                print(f"[topdog-browser] sample dump failed: {_e}",
                      file=sys.stderr)
            data_attrs = re.findall(r'(data-[a-z0-9\-]*(?:id|trial|event)[a-z0-9\-]*)\s*=\s*"(\d+)"', html, re.I)
            has_json = bool(re.search(r'/trials\.json|/trials\?[^"]*format=json|"trials_url"', html, re.I))
            extra = (f"; data-id-attrs={len(data_attrs)}"
                     f"{' e.g. '+data_attrs[0][0] if data_attrs else ''}"
                     f"; json_hint={has_json}")
        print(f"[topdog-browser] {section} p{page_num}: captured "
              f"({len(signature)} rows, {_nlinks} per-event links in HTML{extra})",
              file=sys.stderr)


def _table_signature(html):
    """A cheap fingerprint of the trial rows on a page: list of (date,trial)
    first-cell texts. Used to detect when pagination stops advancing.
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return html[:2000]  # fallback: raw prefix
    soup = BeautifulSoup(html, "html.parser")
    sig = []
    for tr in soup.select("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) >= 2:
            d = cells[0].get_text(" ", strip=True)
            t = cells[1].get_text(" ", strip=True)
            if d or t:
                sig.append((d, t))
    return tuple(sig)


if __name__ == "__main__":
    pages = get_topdog_pages()
    if pages is None:
        print("no pages (browser unavailable)")
    else:
        print(f"got {len(pages)} pages")

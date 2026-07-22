#!/usr/bin/env python3
"""
Combine 2027 Tracking (and Track & Search) events from:
  - Dogs ACT        (dogsact.org.au)      -> The Events Calendar (WordPress)
  - Dogs Victoria   (vicdog.com)          -> The Events Calendar (WordPress), Tracking category
  - Dogs Tasmania   (tasdogs.com)         -> WordPress, Tracking events category

Strategy: prefer iCal (.ics) feeds exposed by "The Events Calendar" plugin,
fall back to HTML list parsing. Filter to events whose title/category looks
like Tracking or Track & Search, and to the target year (default 2027).

Output: events.json  (a flat list the front-end reads)

This script is deliberately defensive: each source is wrapped in try/except so
one site being down or changing markup never kills the whole run. Selectors are
centralised in SOURCES so they're easy to fix when a site changes.
"""

import json
import os
import re
import sys
import datetime as dt
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from icalendar import Calendar  # pip install icalendar
    HAVE_ICAL = True
except Exception:
    HAVE_ICAL = False

# Rebuild modules (source-of-truth + entry-status cross-check). Imported
# defensively so a problem in one doesn't break the whole scraper.
try:
    import nsw_pdf
    HAVE_NSW = True
except Exception as _e:
    HAVE_NSW = False
    print(f"[init] nsw_pdf import failed: {_e}", file=sys.stderr)
try:
    import dv_calendar
    HAVE_DV = True
except Exception as _e:
    HAVE_DV = False
    print(f"[init] dv_calendar import failed: {_e}", file=sys.stderr)
try:
    import qld_calendar
    HAVE_QLD = True
except Exception as _e:
    HAVE_QLD = False
    print(f"[init] qld_calendar import failed: {_e}", file=sys.stderr)
try:
    import wa_calendar
    HAVE_WA = True
except Exception as _e:
    HAVE_WA = False
    print(f"[init] wa_calendar import failed: {_e}", file=sys.stderr)
try:
    import show_manager
    HAVE_SM = True
except Exception as _e:
    HAVE_SM = False
    print(f"[init] show_manager import failed: {_e}", file=sys.stderr)
try:
    import matcher
    HAVE_MATCHER = True
except Exception as _e:
    HAVE_MATCHER = False
    print(f"[init] matcher import failed: {_e}", file=sys.stderr)
# Dogz Online is RETIRED (2026-07): the site IP-blocks the GitHub Actions
# runner (persistent 403 despite browser headers + session warm-up), which no
# header/browser change can fix. Its unique value was small (most events
# overlap Show Manager). The module and its cross-check/source wiring below are
# left intact but dormant. To revive it (e.g. if run from an unblocked network
# or via a proxy), set DOGZ_ENABLED = True.
DOGZ_ENABLED = False
if DOGZ_ENABLED:
    try:
        import dogz_online
        HAVE_DZ = True
    except Exception as _e:
        HAVE_DZ = False
        print(f"[init] dogz_online import failed: {_e}", file=sys.stderr)
else:
    HAVE_DZ = False
try:
    import national_events
    HAVE_NE = True
except Exception as _e:
    HAVE_NE = False
    print(f"[init] national_events import failed: {_e}", file=sys.stderr)

# This calendar covers a single calendar year. Override with the YEAR env var.
YEAR = int(os.environ.get("YEAR", "2026"))
OUTPUT = Path(__file__).resolve().parent.parent / "docs" / "events.json"

HEADERS = {
    "User-Agent": "TrackingCalendarBot/1.0 (+combined tracking events; contact: you@example.com)"
}
TIMEOUT = 30

# Match "tracking" and "track & search" / "track and search" but NOT "trackless" etc.
TRACKING_RE = re.compile(r"\b(tracking|track\s*(&|and)\s*search)\b", re.I)

# ---------------------------------------------------------------------------
# Source definitions. Each has an iCal feed (tried first) and an HTML fallback.
# The .ics URLs are the standard "The Events Calendar" export endpoints.
# If a site changes, edit the URLs / selectors here only.
# ---------------------------------------------------------------------------
SOURCES = [
    {
        "id": "dogsact",
        "name": "Dogs ACT",
        "region": "ACT",
        "color": "#1d6fb8",
        # The Events Calendar iCal feed for the whole calendar:
        "ical": "https://dogsact.org.au/events/?ical=1",
        # HTML fallback: the events list page
        "html": "https://dogsact.org.au/events/list/",
        "source_url": "https://dogsact.org.au/events/",
    },
    # ------------------------------------------------------------------------
    # vicdog.com ("Dogs Victoria (Vic Dog Trials)") is NOT a primary SOURCES
    # entry. It is wired as a VERIFY + GAP-FILL listing source (like Show
    # Manager): scrape_vicdog_listings() produces all-discipline listings that
    # are layered onto the DV-PDF events via matcher.match_events (additive) and
    # events_from_unmatched_listings (gap-fill), down in the cross-check block.
    # This corroborates DV-PDF events (helping VIC verification) and fills any
    # gaps the PDF misses, without duplicating the comprehensive DV PDF feed.
    # ------------------------------------------------------------------------
    {
        "id": "tasdogs",
        "name": "Dogs Tasmania",
        "region": "TAS",
        "color": "#c8632a",
        # PRIMARY: the /dates/ page is a complete all-discipline year table
        # (Month/Date/Affiliate/Event-code/Location) with a code legend — the
        # authoritative TAS calendar. parse_tasdogs() reads it, year-guarded.
        # FALLBACK: if /dates/ yields nothing for the target year (e.g. the page
        # still shows the previous year), it walks the per-discipline
        # /category/events/ post archives so TAS still gets (conformation) events.
        "parser": "tasdogs",
        "source_url": "https://tasdogs.com/dates/",
    },
    {
        "id": "topdog",
        "name": "Top Dog Events",
        # Multi-region: the region is read PER EVENT from each row's state column,
        # so there's no single region/color here (see parse_topdog / TOPDOG_*).
        "region": None,
        "color": None,
        "ical": None,
        "html": "https://www.topdogevents.com.au/trials",
        "source_url": "https://www.topdogevents.com.au/trials",
        "parser": "topdog",
    },
]

# Region colours reused when a source (Top Dog Events) supplies events for
# multiple states. Keep these in sync with the single-region sources above.
REGION_COLOR = {"ACT": "#1d6fb8", "VIC": "#3aa657", "TAS": "#c8632a",
                "NSW": "#7a3ea6", "SA": "#c9a227",
                "QLD": "#c0392b", "WA": "#16887a", "NT": "#b8621d"}
# Only these states are kept from the national Top Dog Events feed.
TOPDOG_REGIONS = {"ACT", "VIC", "TAS", "NSW", "SA", "QLD", "WA", "NT"}


def fetch(url, retries=3, backoff=2.0):
    """GET a URL with retries. Transient network errors (DNS, connection reset,
    timeouts, 5xx) are common on CI runners and shouldn't cause a whole source
    to silently return zero events, so we retry those a few times with backoff.

    4xx client errors (esp. 404) are DEFINITIVE — the page isn't there — so we
    raise immediately without retrying. This matters for parsers that probe
    URLs they expect may not exist (e.g. tasdogs sub-category pages / pagination
    past the last page): retrying a 404 three times with backoff would waste
    ~6s per dead URL for no benefit.
    """
    import time
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            # Client errors (4xx) won't change on retry — fail fast.
            status = getattr(e.response, "status_code", None)
            if status is not None and 400 <= status < 500:
                raise
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"[fetch] {url} attempt {attempt}/{retries} failed "
                      f"({e}); retrying in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
        except Exception as e:
            last_err = e
            if attempt < retries:
                wait = backoff * attempt
                print(f"[fetch] {url} attempt {attempt}/{retries} failed "
                      f"({e}); retrying in {wait:.0f}s", file=sys.stderr)
                time.sleep(wait)
    raise last_err


def looks_like_tracking(*texts):
    return any(t and TRACKING_RE.search(t) for t in texts)


def classify_feed_discipline(*texts):
    """Classify an ical/HTML feed event (Dogs ACT / Dogs Tasmania) into a
    canonical discipline across ALL ANKC sports, or return None if it looks like
    a non-competition entry (meeting/training/etc.). Reuses the DV parser's
    discipline rules and skip patterns so classification is consistent."""
    hay = " ".join(t for t in texts if t)
    if not hay.strip():
        return None
    try:
        import dv_calendar
        # Drop obvious non-competition rows unless a real fixture is present.
        if dv_calendar._DV_SKIP_RE.search(hay) and not any(
                rx.search(hay) for rx, _ in dv_calendar._DV_DISCIPLINE_RULES):
            return None
        for rx, canon in dv_calendar._DV_DISCIPLINE_RULES:
            if rx.search(hay):
                return canon
    except Exception:
        # Fallback: at least keep tracking/scent if dv_calendar unavailable.
        if re.search(r"track\s*(&|and)\s*search", hay, re.I):
            return "Track & Search"
        if re.search(r"\btracking\b", hay, re.I):
            return "Tracking"
        if re.search(r"scent", hay, re.I):
            return "Scent Work"
    return None


def in_target_year(d):
    return d is not None and d.year == YEAR


def norm_date(value):
    """Return an ISO date string (YYYY-MM-DD) or None from a date/datetime."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return None


# ---------------------------------------------------------------------------
# iCal parsing (preferred)
# ---------------------------------------------------------------------------
def parse_ical(source):
    if not HAVE_ICAL or not source.get("ical"):
        return None
    try:
        resp = fetch(source["ical"])
    except Exception as e:
        print(f"[{source['id']}] ical fetch failed: {e}", file=sys.stderr)
        return None

    ctype = resp.headers.get("Content-Type", "")
    body = resp.text
    if "html" in ctype.lower() and "BEGIN:VCALENDAR" not in body:
        # Server returned a webpage, not a feed.
        print(f"[{source['id']}] ical endpoint returned HTML, not a feed", file=sys.stderr)
        return None

    try:
        cal = Calendar.from_ical(body)
    except Exception as e:
        print(f"[{source['id']}] ical parse failed: {e}", file=sys.stderr)
        return None

    events = []
    for comp in cal.walk("VEVENT"):
        title = str(comp.get("summary", "")).strip()
        categories = comp.get("categories")
        cat_text = ""
        if categories is not None:
            try:
                cat_text = ", ".join(str(c) for c in categories.cats)
            except Exception:
                cat_text = str(categories)

        # Classify the discipline across ALL ANKC sports. Drop only if nothing
        # recognisable (non-event rows / unrelated posts).
        discipline = classify_feed_discipline(title, cat_text)
        if not discipline:
            continue

        dtstart = comp.get("dtstart")
        dtend = comp.get("dtend")
        start = norm_date(dtstart.dt) if dtstart else None
        end = norm_date(dtend.dt) if dtend else start
        if not (start and dt.date.fromisoformat(start).year == YEAR):
            continue

        events.append({
            "title": title or f"{discipline} event",
            "start": start,
            "end": end,
            "location": str(comp.get("location", "")).strip(),
            "url": str(comp.get("url", "")).strip() or source["source_url"],
            "category": discipline,
        })
    from collections import Counter
    _bd = Counter(e["category"] for e in events)
    print(f"[{source['id']}] ical parsed {len(events)} events across disciplines "
          f"{dict(_bd)}", file=sys.stderr)
    return events


# ---------------------------------------------------------------------------
# HTML fallback parsing (The Events Calendar list markup + generic WP archive)
# ---------------------------------------------------------------------------
def parse_html(source):
    try:
        resp = fetch(source["html"])
    except Exception as e:
        print(f"[{source['id']}] html fetch failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    events = []

    # 1) The Events Calendar list markup: <div class="tribe-events-calendar-list__event-row">
    rows = soup.select("[class*=tribe-events-calendar-list__event]")
    for row in rows:
        title_el = row.select_one("[class*=event-title] a, h3 a")
        title = title_el.get_text(strip=True) if title_el else ""
        url = title_el["href"] if title_el and title_el.has_attr("href") else source["source_url"]

        # start datetime from the <time datetime="..."> element
        time_el = row.select_one("time[datetime]")
        start = None
        if time_el and time_el.has_attr("datetime"):
            m = re.match(r"(\d{4}-\d{2}-\d{2})", time_el["datetime"])
            if m:
                start = m.group(1)

        loc_el = row.select_one("[class*=venue], [class*=location]")
        location = loc_el.get_text(" ", strip=True) if loc_el else ""

        discipline = classify_feed_discipline(title, location)
        if not discipline:
            continue
        if not (start and dt.date.fromisoformat(start).year == YEAR):
            continue

        events.append({
            "title": title, "start": start, "end": start,
            "location": location, "url": url, "category": discipline,
        })

    # 2) Generic WordPress archive fallback (e.g. tasdogs.com posts):
    if not events:
        for art in soup.select("article, .post, .type-post"):
            title_el = art.select_one("h1 a, h2 a, h3 a, .entry-title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            text = art.get_text(" ", strip=True)
            discipline = classify_feed_discipline(title, text[:300])
            if not discipline:
                continue
            url = title_el.get("href", source["source_url"])
            start = extract_date_from_text(text)
            if not (start and start.year == YEAR):
                continue
            events.append({
                "title": title, "start": start.isoformat(), "end": start.isoformat(),
                "location": "", "url": url, "category": discipline,
            })

    from collections import Counter
    _bd = Counter(e["category"] for e in events)
    print(f"[{source['id']}] html parsed {len(events)} events across disciplines "
          f"{dict(_bd)}", file=sys.stderr)
    return events


DATE_TEXT_RE = re.compile(
    r"(\d{1,2})\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+"
    r"(\d{4})", re.I)
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}


def extract_date_from_text(text):
    m = DATE_TEXT_RE.search(text or "")
    if not m:
        return None
    day, mon, year = int(m.group(1)), MONTHS[m.group(2).lower()[:3]], int(m.group(3))
    try:
        return dt.date(year, mon, day)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# vicdog.com dedicated parser  (two-stage)
#
# WHY: vicdog.com's /events-page/ calendar is JavaScript-rendered. A plain HTTP
# GET only ever returns the CURRENT month/year (2026) — future years load via
# "Load More"/year-dropdown JS that we can't execute. So we never see 2027 there.
#
# WHAT WORKS: individual event pages are fully STATIC. Each lives at a
# year-encoded slug /events/YYYYMMDD[-DD]-slug/ and renders a clean block:
#     ### Date     21 - 23/Aug/2026
#     ### Labels   Tracking
#     # <clean title>
#     + iCal / Outlook export -> https://vicdog.com/?method=ical&id=<n>
#
# STRATEGY:
#   Stage 1 (enumerate): crawl the STATIC, paginated listing pages
#     (/category/tracking/ + /tracking-and-track-search/) and collect every
#     /events/YYYYMMDD-... link whose slug year == YEAR.
#   Stage 2 (detail): fetch each event page directly and parse its Date/Labels/
#     title. The slug date is the fallback if the Date block is unreadable.
#
# This sidesteps the JS calendar entirely and yields clean titles + real dates.
# ---------------------------------------------------------------------------
VICDOG_SLUG_RE = re.compile(r"/events/(\d{8})(?:-(\d{1,2}))?-", re.I)

# Static listing pages to crawl for event links (paginated; we follow /page/N/).
#
# We crawl BOTH the tracking-specific feeds AND the site-wide "latest updates"
# feed. The site-wide feed matters because some clubs' tracking events are
# categorised elsewhere: e.g. "North East Tracking & Scent Club" also runs scent
# work, and its Track & Search post can be filed under Scent Work, so it never
# appears in the tracking category feeds. Discovery must not depend on how a post
# was categorised — we collect every /events/ URL here and let the per-event
# Labels check (in _vicdog_parse_event_page) decide what's actually tracking.
VICDOG_LISTING_PAGES = [
    # The all-discipline "latest updates" feed is the widest net. The former
    # tracking-only category URLs were leftovers from when vicdog was a
    # tracking source and are removed — they over-weighted tracking and wasted
    # fetches. NOTE: this feed does not paginate over plain HTTP (it serves the
    # same first page for every /page/N/), so vicdog only sees its most recent
    # page of posts. It therefore acts as a RECENT-events verify/gap-fill layer
    # for VIC, not a full-year source; the DV PDF remains VIC's comprehensive
    # feed.
    "https://vicdog.com/latest-update-posts/",
]
VICDOG_MAX_PAGES = 25  # safety cap (the signature guard stops earlier anyway)

# "21 - 23/Aug/2026"  or  "23/Aug/2026"
VICDOG_DATE_RE = re.compile(
    r"(?:(\d{1,2})\s*-\s*)?(\d{1,2})\s*/\s*"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*/\s*(\d{4})",
    re.I)


def _vicdog_dates_from_href(href):
    """(start_iso, end_iso) from the slug, or (None, None). Fallback only."""
    m = VICDOG_SLUG_RE.search(href or "")
    if not m:
        return None, None
    ymd = m.group(1)
    try:
        start = dt.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
    except ValueError:
        return None, None
    end = start
    if m.group(2):
        try:
            end = dt.date(start.year, start.month, int(m.group(2)))
            if end < start:
                end = start
        except ValueError:
            end = start
    return start.isoformat(), end.isoformat()


def _vicdog_slug_year(href):
    m = VICDOG_SLUG_RE.search(href or "")
    if not m:
        return None
    try:
        return int(m.group(1)[:4])
    except ValueError:
        return None


def _vicdog_enumerate_event_urls(year):
    """Stage 1: collect unique /events/... URLs dated within `year`.

    Listing pages are newest-first, so once an entire page's events are older
    than `year`, everything further back is older too and we can stop. We crawl
    several feeds (see VICDOG_LISTING_PAGES) and union the results, so an event
    missed by one feed can still be found via another.
    """
    urls = {}
    for base in VICDOG_LISTING_PAGES:
        prev_signature = None
        for page in range(1, VICDOG_MAX_PAGES + 1):
            url = base if page == 1 else f"{base.rstrip('/')}/page/{page}/"
            try:
                resp = fetch(url)
            except Exception as e:
                # 404 => past the last page; anything else we just stop this list.
                print(f"[vicdog] listing stop {url}: {e}", file=sys.stderr)
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            anchors = [a.get("href", "") for a in soup.select('a[href*="/events/"]')]
            event_anchors = [h for h in anchors if VICDOG_SLUG_RE.search(h)]
            if not event_anchors:
                # No dated event links at all => past the end of this archive.
                print(f"[vicdog] {url}: no event links, stopping", file=sys.stderr)
                break
            # Pagination guard: if this page's event links are identical to the
            # previous page's, the archive isn't actually paginating (e.g. it's a
            # JS "load more" feed that serves the same page for every /page/N/).
            # Stop rather than re-fetch the same content up to the page cap.
            signature = tuple(sorted(set(event_anchors)))
            if signature == prev_signature:
                print(f"[vicdog] {url}: same as previous page "
                      f"(pagination not advancing), stopping", file=sys.stderr)
                break
            prev_signature = signature
            found_this_page = 0
            years_this_page = set()
            for href in event_anchors:
                yr = _vicdog_slug_year(href)
                if yr:
                    years_this_page.add(yr)
                if yr == year:
                    full = href if href.startswith("http") else "https://vicdog.com" + href
                    urls[full.split("?")[0].rstrip("/") + "/"] = True
                    found_this_page += 1
            print(f"[vicdog] {url}: +{found_this_page} {year} events "
                  f"(years seen: {sorted(years_this_page)})", file=sys.stderr)
            # Stop paging once the whole page is older than the target year
            # (posts are newest-first, so nothing newer lies further back).
            if years_this_page and max(years_this_page) < year:
                break
    return list(urls.keys())


def _vicdog_discipline(haystack):
    """Classify a vicdog event page into a canonical discipline using the same
    prose rules as the DV PDF parser (all ANKC disciplines). Returns None only
    if nothing recognisable is found."""
    try:
        import dv_calendar
        for rx, canon in dv_calendar._DV_DISCIPLINE_RULES:
            if rx.search(haystack):
                return canon
    except Exception:
        pass
    # Fallback minimal map if dv_calendar import fails.
    if re.search(r"track\s*(&|and)\s*search", haystack, re.I):
        return "Track & Search"
    if re.search(r"tracking", haystack, re.I):
        return "Tracking"
    return None


def _vicdog_parse_event_page(url):
    """Stage 2: fetch a single event page and return a LISTING dict (matcher-
    shaped) for ANY discipline, or None. vicdog is now a verify/gap-fill source,
    so it emits listings ({club,date,discipline,region,status,...}) rather than
    primary events."""
    try:
        resp = fetch(url)
    except Exception as e:
        print(f"[vicdog] event fetch failed {url}: {e}", file=sys.stderr)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text("\n", strip=True)

    # Title: the event <h1> (falls back to <title> minus site suffix).
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = re.sub(r"\s*[\u2013-]\s*Vic Dog Trials\s*$", "", soup.title.get_text(strip=True))
    title = title.strip() or "Dogs Victoria event"

    # Discipline label(s): the "Labels" section, plus title/body as fallback.
    label_text = ""
    for h in soup.find_all(["h2", "h3", "h4"]):
        if h.get_text(strip=True).lower() == "labels":
            nxt = h.find_next()
            if nxt:
                label_text = nxt.get_text(" ", strip=True)
            break
    haystack = " ".join([title, label_text, text[:400]])
    discipline = _vicdog_discipline(haystack)
    if not discipline:
        return None  # not a recognisable competition discipline

    # Dates: prefer the "21 - 23/Aug/2026" block; fall back to the slug.
    start = end = None
    dm = VICDOG_DATE_RE.search(text)
    if dm:
        d1, d2, mon, yr = dm.group(1), dm.group(2), dm.group(3), dm.group(4)
        mon_n = MONTHS[mon.lower()[:3]]
        yr = int(yr)
        try:
            end = dt.date(yr, mon_n, int(d2)).isoformat()
            start = (dt.date(yr, mon_n, int(d1)).isoformat() if d1 else end)
            if start > end:
                start, end = None, None
        except ValueError:
            start = end = None
    if not start:
        start, end = _vicdog_dates_from_href(url)
    if not start:
        return None

    cancelled = bool(re.search(r"\bcancel", haystack, re.I))

    # Listing shape consumed by matcher.match_events / gap-fill.
    return {
        "club": title,
        "date": start,
        "discipline": discipline,
        "region": "VIC",
        "status": "cancelled" if cancelled else "listed",
        "closes": None,
        "detail_url": url,
        "event_id": None,
    }


TASDOGS_DATES_URL = "https://tasdogs.com/dates/"
# Code -> canonical discipline, from the /dates/ page legend. Order/specificity
# handled by longest-key-first matching in _tasdogs_map_code().
TASDOGS_CODE_MAP = {
    "CH": "Conformation", "S": "Conformation", "O/S": "Conformation",
    "P": "Conformation", "G/S": "Conformation", "G/OS": "Conformation",
    "S/W": "Conformation", "G/P": "Conformation", "S/S": "Conformation",
    "S/P": "Conformation", "N/S": "Conformation", "PARADE": "Conformation",
    "GERMAN BREEDS": "Conformation", "COMPANION BREED": "Conformation",
    "BREED EXH": "Conformation",
    "O/T": "Obedience", "OT": "Obedience",
    "ROT": "Rally Obedience", "RO": "Rally Obedience", "RA/T": "Rally Obedience",
    "J/T": "Agility", "JT": "Agility", "A/G": "Agility", "AG": "Agility",
    "A/T": "Agility", "AT": "Agility", "A/J": "Agility",
    "R/T": "Retrieving", "RT": "Retrieving",
    "RATG": "Retrieving", "SP/RET": "Retrieving",
    "S/R FT": "Field Trial", "UGFT": "Field Trial", "FT": "Field Trial",
    "E/T": "Endurance", "ET": "Endurance",
    "T/S": "Track & Search", "T & S": "Track & Search", "T&S": "Track & Search",
    "ED/T": "Earthdog", "EDT": "Earthdog",
    "T/T": "Tracking", "TR/K": "Tracking", "TR/KG": "Tracking",
    "H": "Herding", "HT": "Herding", "H TEST": "Herding", "H TRIAL": "Herding",
    "SC/W": "Scent Work", "SC/W CONTAINER": "Scent Work",
    "LCT": "Lure Coursing", "LC": "Lure Coursing",
    "SPRINTDOG": "Sprint", "SP": "Sprint",
    "DWD": "Dances with Dogs",
}
_TAS_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def _tasdogs_map_code(raw):
    """Map a /dates/ EVENT-cell code to a canonical discipline. Strips 'x N'
    multipliers and am/pm/twilight suffixes, then matches the leading code
    token(s) against TASDOGS_CODE_MAP (longest key first)."""
    if not raw:
        return None
    s = raw.upper()
    s = re.sub(r"\bX\s*\d+\b", " ", s)             # drop "x 2" multipliers
    s = re.sub(r"\b(AM|PM|TWILIGHT)\b", " ", s)     # drop session markers
    s = re.sub(r"\s+", " ", s).strip(" /")
    if not s:
        return None
    # Try longest legend keys first so "T & S" beats "S", "A/T" beats "T", etc.
    for key in sorted(TASDOGS_CODE_MAP, key=len, reverse=True):
        if re.search(r"(^|[^A-Z])" + re.escape(key) + r"([^A-Z]|$)", s):
            return TASDOGS_CODE_MAP[key]
    # Fall back to prose classification (handles spelled-out fixtures).
    return classify_feed_discipline(raw)


def _parse_tasdogs_dates(year):
    """PRIMARY: parse the /dates/ year table. Rows are grouped under a bold year
    header (e.g. '2025'); MONTH rows set the current month; event rows are
    DATE | AFFILIATE | EVENT-code | LOCATION. Only rows under the target-year
    header are emitted (year-guard), so a page still showing last year yields
    []. Returns a list of event dicts."""
    try:
        resp = fetch(TASDOGS_DATES_URL)
    except Exception as e:
        print(f"[tasdogs] dates page fetch failed: {e}", file=sys.stderr)
        return []
    soup = BeautifulSoup(resp.text, "html.parser")

    events = []
    seen = set()
    current_year = None
    current_month = None

    # Walk the document top-to-bottom. Year headers appear as bold text; the
    # calendar itself is one or more tables of rows.
    for el in soup.find_all(["h1", "h2", "h3", "h4", "p", "strong", "b", "tr"]):
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        # Year header (a standalone 4-digit year 20xx).
        ym = re.fullmatch(r"(20\d{2})", text)
        if ym:
            current_year = int(ym.group(1))
            current_month = None
            continue
        if el.name != "tr":
            continue
        cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])]
        if not cells:
            continue
        joined = " ".join(cells)
        # A month-name row sets the current month (MONTH col populated, rest empty).
        mtxt = cells[0].strip().lower()
        if mtxt in _TAS_MONTHS:
            current_month = _TAS_MONTHS[mtxt]
            continue
        # Some rows carry the month in the first cell inline; detect anywhere.
        for name, num in _TAS_MONTHS.items():
            if re.fullmatch(name, cells[0].strip(), re.I):
                current_month = num
                break
        if current_year != year or not current_month:
            continue
        # Event row: find the DATE (a day number, possibly a range "9 - 11"),
        # AFFILIATE (club), EVENT (code), LOCATION.
        # Columns can be [MONTH, DATE, AFFILIATE, EVENT, LOCATION] or shifted, so
        # locate the day-number cell.
        nums = [(i, c) for i, c in enumerate(cells)
                if re.match(r"^\d{1,2}(\s*-\s*\d{1,2})?$", c.strip())]
        if not nums:
            continue
        di, daycell = nums[0]
        day = int(re.match(r"^(\d{1,2})", daycell.strip()).group(1))
        rest = cells[di + 1:]
        if len(rest) < 2:
            continue
        affiliate = rest[0].strip()
        event_code = rest[1].strip() if len(rest) > 1 else ""
        location = rest[2].strip() if len(rest) > 2 else ""
        # Skip office-closure / admin rows.
        if re.search(r"office clos|re-opens|exams?$|closed", joined, re.I):
            continue
        if not affiliate or not event_code:
            continue
        discipline = _tasdogs_map_code(event_code)
        if not discipline:
            continue
        try:
            edate = dt.date(year, current_month, day)
        except ValueError:
            continue
        title = f"{affiliate} \u2013 {discipline}"
        key = (title.lower(), edate.isoformat())
        if key in seen:
            continue
        seen.add(key)
        events.append({
            "title": title,
            "start": edate.isoformat(),
            "end": edate.isoformat(),
            "location": location,
            "url": TASDOGS_DATES_URL,
            "category": discipline,
            "region": "TAS",
            "source": "Dogs Tasmania",
            "color": REGION_COLOR.get("TAS"),
            "cancelled": bool(re.search(r"cancel", joined, re.I)),
        })
    return events


def parse_tasdogs(source):
    """Dogs Tasmania: try the comprehensive /dates/ table first (year-guarded);
    if it yields nothing for the target year (e.g. the page still shows last
    year), fall back to the per-discipline category post archives."""
    events = _parse_tasdogs_dates(YEAR)
    if events:
        from collections import Counter
        by_disc = Counter(e["category"] for e in events)
        print(f"[tasdogs] /dates/ parsed {len(events)} events across "
              f"disciplines {dict(by_disc)}", file=sys.stderr)
        return events
    print("[tasdogs] /dates/ had no events for the target year "
          "(page may still show a prior year); falling back to category crawl",
          file=sys.stderr)
    return _parse_tasdogs_categories(source)


TASDOGS_SUBCATEGORIES = [
    # Confirmed-existing slugs return events; ones that 404 are pruned. Dogs
    # Tasmania files rally under obedience and track & search under tracking,
    # so those separate slugs don't exist. The per-event classify_feed_
    # discipline() still refines the discipline from the title where possible.
    ("dog-shows", "Conformation"),
    ("obedience", "Obedience"),
    ("agility", "Agility"),
    ("tracking", "Tracking"),
    ("scent-work", "Scent Work"),
    ("herding", "Herding"),
    ("endurance", "Endurance"),
    ("retrieving", "Retrieving"),
    ("lure-coursing", "Lure Coursing"),
    ("dances-with-dogs", "Dances with Dogs"),
]
TASDOGS_BASE = "https://tasdogs.com/category/events"
TASDOGS_MAX_PAGES = 6
# Permalink date: /YYYY/MM/DD/slug/
_TASDOGS_DATE_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/")
# Admin/non-event posts to skip (title-based).
_TASDOGS_SKIP_RE = re.compile(
    r"change of judge|survey|premise|proposed champion|minutes|meeting|"
    r"agenda|newsletter|gazette|vale\b|obituary|reminder|notice to|"
    r"expression of interest|nomination|vacanc|position", re.I)


def _parse_tasdogs_categories(source):
    """FALLBACK: walk each Dogs Tasmania events sub-category and build events. Each
    sub-category maps to a discipline (the category is a more reliable signal
    than the post title, which is often 'Breed Numbers – <club> – <date>').
    Dates come from the post permalink (/YYYY/MM/DD/). Admin posts are skipped.
    """
    events = []
    seen = set()
    for slug, discipline in TASDOGS_SUBCATEGORIES:
        cat_url = f"{TASDOGS_BASE}/{slug}/"
        prev_sig = None
        for page in range(1, TASDOGS_MAX_PAGES + 1):
            url = cat_url if page == 1 else f"{cat_url}page/{page}/"
            try:
                resp = fetch(url)
            except Exception:
                break  # 404 => past last page / no such sub-category
            soup = BeautifulSoup(resp.text, "html.parser")
            # Post title links point at dated permalinks.
            links = []
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if _TASDOGS_DATE_RE.search(href) and a.get_text(strip=True):
                    links.append((href, a.get_text(" ", strip=True)))
            if not links:
                break
            sig = tuple(sorted({h for h, _ in links}))
            if sig == prev_sig:
                break  # pagination not advancing
            prev_sig = sig
            for href, title in links:
                dm = _TASDOGS_DATE_RE.search(href)
                if not dm:
                    continue
                try:
                    post_date = dt.date(int(dm.group(1)), int(dm.group(2)),
                                        int(dm.group(3)))
                except ValueError:
                    continue
                if _TASDOGS_SKIP_RE.search(title):
                    continue
                # Prefer an actual event date parsed from the title (e.g.
                # "Sat 27-Jun-2026"); fall back to the post/permalink date.
                title_date = extract_date_from_text(title)
                pdate = title_date or post_date
                if pdate.year != YEAR:
                    continue
                # Prefer a discipline named in the title; else the category's.
                disc = classify_feed_discipline(title) or discipline
                key = (title.lower(), pdate.isoformat(), disc)
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "title": title,
                    "start": pdate.isoformat(),
                    "end": pdate.isoformat(),
                    "location": "",
                    "url": href,
                    "category": disc,
                    "region": "TAS",
                    "source": "Dogs Tasmania",
                    "color": REGION_COLOR.get("TAS"),
                    "cancelled": bool(re.search(r"cancel", title, re.I)),
                })
    from collections import Counter
    by_disc = Counter(e["category"] for e in events)
    print(f"[tasdogs] parsed {len(events)} events across sub-categories "
          f"{dict(by_disc)}", file=sys.stderr)
    return events


def scrape_vicdog_listings(year=None):
    """Verify/gap-fill source: return vicdog.com listings (all disciplines) in
    matcher-listing shape. vicdog is NOT a primary event source anymore; its
    output is layered onto the DV-PDF events via matcher.match_events (to verify
    and add schedule/close data) and events_from_unmatched_listings (to gap-fill
    anything the DV PDF missed)."""
    year = year or YEAR
    urls = _vicdog_enumerate_event_urls(year)
    print(f"[vicdog] enumerated {len(urls)} candidate {year} event pages",
          file=sys.stderr)
    listings = []
    seen = set()
    for url in urls:
        L = _vicdog_parse_event_page(url)
        if not L:
            continue
        if dt.date.fromisoformat(L["date"]).year != year:
            continue
        key = (L["club"].lower(), L["date"], L["discipline"])
        if key in seen:
            continue
        seen.add(key)
        listings.append(L)
    from collections import Counter
    by_disc = Counter(x["discipline"] for x in listings)
    print(f"[vicdog] kept {len(listings)} listings across all disciplines "
          f"({sum(1 for x in listings if x['status']=='cancelled')} cancelled)",
          file=sys.stderr)
    print(f"[vicdog]   by discipline: {dict(by_disc)}", file=sys.stderr)
    return listings


# ---------------------------------------------------------------------------
# Top Dog Events parser  (topdogevents.com.au)
#
# WHY: some clubs advertise and take entries only through Top Dog Events, not
# vicdog/Dogs ACT/Dogs Tasmania — e.g. the North East Tracking & Scent Club's
# May 2026 tracking trial appeared here and nowhere in the vicdog feeds. This is
# a NATIONAL source: each trial row carries its own state, so we filter to our
# regions (VIC/ACT/TAS) and stamp region per event.
#
# The trials page is a static HTML table with two sections, "Upcoming" and
# "Past", each paginated via ?f=upcoming&upcoming_page=N / ?f=past&past_page=N.
# Rows are newest-first within each section. Each row's cells give:
#   - a date or date range ("Sat 23rd May – Mon 25th May 2026")
#   - the trial name with discipline tag(s) and status appended
#   - "Club Name · STATE"
# We keep rows whose discipline is Tracking or Track and Search, in-region,
# dated within YEAR.
# ---------------------------------------------------------------------------
TOPDOG_MAX_PAGES = 75  # past list is ~71 pages; upcoming far fewer

# "23rd May 2026" -> (23, May, 2026); also matches the end of a range.
TOPDOG_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})", re.I)
# Discipline detection on the row text. Top Dog trial names are free text, so
# we map from prose to the same canonical categories used across the calendar.
# Order matters: more specific patterns first (Track & Search before Tracking).
TOPDOG_DISCIPLINE_RULES = [
    (re.compile(r"track\s*(?:and|&)\s*search", re.I), "Track & Search"),
    (re.compile(r"\btracking\b", re.I), "Tracking"),
    (re.compile(r"scent\s*work|\bscentwork\b", re.I), "Scent Work"),
    (re.compile(r"rally", re.I), "Rally Obedience"),
    (re.compile(r"obedience", re.I), "Obedience"),
    (re.compile(r"trick", re.I), "Trick Dog"),
    (re.compile(r"jumping|games|\bagility\b", re.I), "Agility"),
    (re.compile(r"dances\s*with\s*dogs|\bdwd\b", re.I), "Dances with Dogs"),
    (re.compile(r"herding", re.I), "Herding"),
    (re.compile(r"endurance", re.I), "Endurance"),
    (re.compile(r"lure\s*coursing", re.I), "Lure Coursing"),
    (re.compile(r"field\s*trial|retriev", re.I), "Retrieving"),
    (re.compile(r"\bsprint", re.I), "Sprint"),
    (re.compile(r"earth\s*dog", re.I), "Earthdog"),
    (re.compile(r"weight\s*pull", re.I), "Weight Pull"),
    (re.compile(r"sled", re.I), "Sled Sports"),
    (re.compile(r"back\s*pack|hiking", re.I), "Backpacking"),
    (re.compile(r"conformation|championship show|open show", re.I), "Conformation"),
]


def _topdog_disciplines(row_text):
    """Return the list of canonical disciplines named in a Top Dog row (a row
    can list more than one). Empty if none recognised."""
    out = []
    for rx, canon in TOPDOG_DISCIPLINE_RULES:
        if rx.search(row_text) and canon not in out:
            out.append(canon)
    return out


# state is the trailing token after the final "·" in the club cell
TOPDOG_STATE_RE = re.compile(r"\b(ACT|NSW|NT|QLD|SA|TAS|VIC|WA)\b")


def _topdog_parse_dates(text):
    """Return (start_iso, end_iso) from a row's date text, or (None, None).

    Handles single dates and ranges. A range often omits the month/year on the
    first date ("Sat 23rd May – Mon 25th May 2026" or "23rd – 25th May 2026");
    the end date is always fully specified, so we take month/year from it and,
    if the start day has no month of its own, apply the end's month/year.
    """
    # First, a range where the START day may lack month/year. Capture the start
    # day, an optional start month, then the fully-specified end date.
    rng = re.search(
        r"(\d{1,2})(?:st|nd|rd|th)?"
        r"(?:\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)?"
        r"(?:\s+\d{4})?"
        r"\s*[–-]\s*"
        r"(?:\w{3,9}\s+)?"                      # optional weekday before end day
        r"(\d{1,2})(?:st|nd|rd|th)?\s+"
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+(\d{4})",
        text, re.I)
    if rng:
        end_mon = MONTHS[rng.group(4).lower()[:3]]
        end_yr = int(rng.group(5))
        start_mon = MONTHS[rng.group(2).lower()[:3]] if rng.group(2) else end_mon
        try:
            end = dt.date(end_yr, end_mon, int(rng.group(3)))
            start = dt.date(end_yr, start_mon, int(rng.group(1)))
            if start > end:  # start month rolled into a new year rarely; clamp
                start = dt.date(end_yr, end_mon, int(rng.group(3)))
            return start.isoformat(), end.isoformat()
        except ValueError:
            pass

    # Otherwise, a single fully-specified date (take the last if several).
    matches = list(TOPDOG_DATE_RE.finditer(text))
    if matches:
        last = matches[-1]
        d = dt.date(int(last.group(3)), MONTHS[last.group(2).lower()[:3]],
                    int(last.group(1)))
        return d.isoformat(), d.isoformat()
    return None, None


def _topdog_clean_title(raw):
    """Strip trailing discipline tags / status words the table appends."""
    title = raw
    # cut trailing status + discipline tokens (they follow the trial name)
    title = re.split(
        r"\s+(?:Open|Closed(?:\s+can\s+edit)?|Running now)\b", title)[0]
    title = re.sub(
        r"\s+(?:Tracking|Track\s*(?:and|&)\s*Search|Scent\s*Work|Agility|"
        r"Obedience|Rally|Trick\s*Dog|Dances\s*With\s*Dogs|Herding|"
        r"Lure\s*Coursing|Sled\s*Sport\s*Events?|Retrieving|Earthdog|"
        r"Endurance\s*Test|Mondioring|Canine\s*Hoopers|Miscellaneous|"
        r"Products|SprintDog\u2122?|CASSA\s*Scent\s*Work)+\s*$", "", title)
    return title.strip(" -–—·")


def _topdog_parse_rows(soup, year):
    """Yield event dicts from every qualifying table row on one page."""
    out = []
    for tr in soup.select("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 3:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        name_text = cells[1].get_text(" ", strip=True)
        club_text = cells[2].get_text(" ", strip=True)

        row_text = " ".join([date_text, name_text, club_text])
        # discipline(s): map from the row's free-text trial name
        disciplines = _topdog_disciplines(row_text)
        if not disciplines:
            continue

        # region from the club cell's state token
        sm = TOPDOG_STATE_RE.search(club_text)
        region = sm.group(1) if sm else None
        if region not in TOPDOG_REGIONS:
            continue

        start, end = _topdog_parse_dates(date_text)
        if not start or dt.date.fromisoformat(start).year != year:
            continue

        # Link to the specific trial page. Top Dog per-event pages have URLs of
        # the form /trials/<id> (e.g. /trials/664). The anchor isn't reliably in
        # the name cell — the row may link from any cell or wrap the whole row —
        # so search the ENTIRE row for the first href matching that pattern, and
        # only fall back to the bare listing page if none is found.
        url = "https://www.topdogevents.com.au/trials"
        row_el = tr if hasattr(tr, "find_all") else None
        best_href = None
        anchors = (row_el.find_all("a", href=True) if row_el
                   else [a for c in cells if hasattr(c, "find_all")
                         for a in c.find_all("a", href=True)])
        for a in anchors:
            href = a["href"]
            if re.search(r"/trials/\d+", href):
                best_href = href
                break
            # remember a first non-listing link as a weaker fallback
            if best_href is None and href not in (
                    "/trials", "https://www.topdogevents.com.au/trials"):
                best_href = href
        if best_href:
            url = best_href
        if url.startswith("/"):
            url = "https://www.topdogevents.com.au" + url

        cancelled = bool(re.search(r"cancel", row_text, re.I))

        for category in disciplines:
            out.append({
                "title": _topdog_clean_title(name_text) or f"{category} Trial",
                "start": start,
                "end": end or start,
                "location": re.split(r"\s*·\s*", club_text)[0].strip(),
                "url": url,
                "category": category,
                "cancelled": cancelled,
                "region": region,
                "color": REGION_COLOR.get(region),
            })
    return out


def parse_topdog(source):
    """Prefer the headless-browser scraper (renders JS pagination, so it sees
    ALL pages). Fall back to the plain-HTTP path if the browser is unavailable.
    """
    try:
        import topdog_browser
        pages = topdog_browser.get_topdog_pages()
    except Exception as e:
        print(f"[topdog] browser module error: {e}", file=sys.stderr)
        pages = None

    if pages:
        events = []
        seen = set()
        for item in pages:
            # Support both the new (section, html) tuples and, defensively, a
            # bare html string (older return shape).
            if isinstance(item, tuple):
                section, html = item
            else:
                section, html = "upcoming", item
            enterable = (section == "upcoming")
            soup = BeautifulSoup(html, "html.parser")
            for ev in _topdog_parse_rows(soup, YEAR):
                key = (ev["title"].lower(), ev["start"], ev["region"])
                if key in seen:
                    continue
                seen.add(key)
                # An event listed in Top Dog's UPCOMING section has an active
                # entry link -> entries are open. Past-section events do not.
                ev["topdog_open"] = enterable
                events.append(ev)
        print(f"[topdog] browser path kept {len(events)} tracking events "
              f"across {sorted(TOPDOG_REGIONS)} "
              f"({sum(e['cancelled'] for e in events)} cancelled)",
              file=sys.stderr)
        if events:
            return events
        # If the browser returned pages but zero in-region tracking events,
        # fall through to HTTP as a sanity backstop.
        print("[topdog] browser path found 0 events; trying HTTP fallback",
              file=sys.stderr)

    return parse_topdog_http(source)


def parse_topdog_http(source):
    events = []
    seen = set()
    for section, page_param in (("upcoming", "upcoming_page"),
                                ("past", "past_page")):
        prev_signature = None
        repeat_count = 0
        for page in range(1, TOPDOG_MAX_PAGES + 1):
            url = f"{source['html']}?f={section}&{page_param}={page}"
            try:
                resp = fetch(url)
            except Exception as e:
                print(f"[topdog] {section} stop p{page}: {e}", file=sys.stderr)
                break
            soup = BeautifulSoup(resp.text, "html.parser")
            rows = _topdog_parse_rows(soup, YEAR)

            # Detect non-advancing pagination: Top Dog's list is JS-paginated,
            # so plain HTTP often returns the SAME page regardless of the page
            # number. If the row signature repeats, stop early instead of
            # fetching all ~75 identical pages.
            signature = tuple(sorted((r["title"], r["start"]) for r in rows))
            if signature and signature == prev_signature:
                repeat_count += 1
                if repeat_count >= 2:
                    print(f"[topdog] {section} p{page}: repeated page, "
                          f"stopping (JS pagination not advancing)", file=sys.stderr)
                    break
            else:
                repeat_count = 0
            prev_signature = signature

            # Also scan all dated text on the page to decide whether to stop.
            page_years = set()
            for m in TOPDOG_DATE_RE.finditer(soup.get_text(" ", strip=True)):
                page_years.add(int(m.group(3)))

            new_here = 0
            for ev in rows:
                key = (ev["title"].lower(), ev["start"], ev["region"])
                if key in seen:
                    continue
                seen.add(key)
                events.append(ev)
                new_here += 1
            print(f"[topdog] {section} p{page}: +{new_here} in-region {YEAR} "
                  f"(years on page: {sorted(page_years) or '—'})", file=sys.stderr)

            if not page_years:
                break  # no dated rows => past the end
            # Past list is newest-first: once the whole page predates YEAR, stop.
            if section == "past" and max(page_years) < YEAR:
                break
            # Upcoming list is oldest-first: once the whole page is beyond YEAR, stop.
            if section == "upcoming" and min(page_years) > YEAR:
                break
    print(f"[topdog] kept {len(events)} tracking events across VIC/ACT/TAS "
          f"({sum(e['cancelled'] for e in events)} cancelled)", file=sys.stderr)
    return events


def scrape_source(source):
    parser = source.get("parser")
    if parser == "topdog":
        events = parse_topdog(source)
    elif parser == "tasdogs":
        events = parse_tasdogs(source)
    else:
        events = parse_ical(source) or []
        if not events:
            events = parse_html(source)
    for e in events:
        e["source"] = source["name"]
        e.setdefault("cancelled", False)
        # Single-region sources stamp their region/color here. Multi-region
        # sources (Top Dog Events) set region/color per event, so only fill in
        # from the source when the parser didn't already provide them.
        if source.get("region") is not None:
            e["region"] = source["region"]
            e["color"] = source["color"]
        else:
            e.setdefault("region", None)
            e.setdefault("color", REGION_COLOR.get(e.get("region")))
    return events


_CLUB_WORD_RE = re.compile(
    r"club|association|societ|kennel|canine|obedience|training|dog\s*sports?|"
    r"\bk9\b|academy|centre|center|group|committee", re.I)


# Generic listing/calendar pages that are NOT per-event entry links — an
# "Enter / details" link pointing here is useless, so we never treat these as
# an entry_url and always prefer a specific per-event link over them.
_GENERIC_LINK_RES = (
    re.compile(r"topdogevents\.com\.au/trials/?$", re.I),
    re.compile(r"dogsvictoria\.org\.au/events/shows-and-trials-calendar", re.I),
    re.compile(r"vicdog\.com/events-?page", re.I),
    re.compile(r"dogsvictoria\.org\.au/events/?$", re.I),
)


def _is_specific_event_link(url):
    """True if `url` is a specific per-event page (e.g. Top Dog /trials/<id> or
    a Show Manager event Details page), as opposed to a generic listing or
    governing-body calendar page."""
    if not url or not isinstance(url, str):
        return False
    if any(rx.search(url) for rx in _GENERIC_LINK_RES):
        return False
    # Positive signals of a per-event page.
    if re.search(r"topdogevents\.com\.au/trials/\d+", url, re.I):
        return True
    if re.search(r"showmanager\.com\.au/.*(Details|events/PublicEvents)", url, re.I):
        return True
    if re.search(r"vicdog\.com/events/\d", url, re.I):
        return True
    # A non-generic http(s) link with a path deeper than the site root is
    # probably event-specific; accept it rather than lose a usable link.
    m = re.match(r"https?://[^/]+(/.*)?$", url)
    return bool(m and m.group(1) and len(m.group(1).strip("/")) > 0)


def _looks_like_club(text):
    """Does this text look like a club/organisation name (vs a trial-type
    descriptor or marketing name)?"""
    return bool(text and _CLUB_WORD_RE.search(text))


def _derive_club(e):
    """Set e['club'] — the club/organisation name — for display on the card's
    info line (between State and Source). Sources put the club in different
    fields: Top Dog often in `location` with a trial-type descriptor as the
    `title`; DV/NSW in the `title`. We pick whichever field holds an
    org-looking name, preferring location, then title, then any merged
    alternate text. The event `title` is left untouched as the headline."""
    title = (e.get("title") or "").strip()
    loc = (e.get("location") or "").strip()
    alts = [t for t in (e.get("_alt_text") or []) if t]
    _STATES = ("victoria", "queensland", "new south wales", "western australia",
               "south australia", "tasmania", "act", "northern territory")

    club = ""
    # Prefer location (Top Dog's club field), then title, then merged alternates.
    for c in [loc, title] + alts:
        cl = c.strip().lower() if c else ""
        if c and cl not in _STATES and _looks_like_club(c):
            club = c.strip()
            break
    # Fallback: a non-empty location that isn't a bare state name; else title.
    if not club:
        if loc and loc.lower() not in _STATES:
            club = loc
        else:
            club = title

    # Strip a trailing discipline suffix from the club name
    # (e.g. "Oxley Dog Training Club Inc – Scent Work" -> "...Club Inc").
    club = re.sub(r"\s*[\u2013-]\s*(scent\s*work|scentwork|tracking|"
                  r"track\s*&?\s*search|obedience|rally|agility)\s*$", "",
                  club, flags=re.I).strip()

    e["club"] = club or title or "Event"
    e.pop("_alt_text", None)  # internal scratch, don't ship it


def canonical_category(cat):
    """Normalise the many discipline labels the parsers emit into a fixed set
    the front-end colour-codes and filters on:
        "Tracking"                   (a Tracking trial, distinct)
        "Track & Search"             (a Track & Search trial, distinct)
        "Tracking / Track & Search"  (source can't distinguish, e.g. Dogs NSW TT)
        "Scent Work"
    We preserve the Tracking vs Track & Search distinction wherever the source
    provides it, and only fall back to the combined label when the source truly
    doesn't tell them apart (so we never overstate which one it is).
    Anything unrecognised is left as-is so we notice it.
    """
    c = (cat or "").strip().lower()
    if "scent" in c:
        return "Scent Work"
    # Explicit combined label FIRST (Dogs NSW "TT", or "Tracking / Track &
    # Search") — it contains "track & search" as a substring, so it must be
    # caught before the Track & Search test below.
    if "/" in c or c == "tt":
        return "Tracking / Track & Search"
    # Track & Search must be tested before plain "tracking".
    if re.search(r"track\s*(?:&|and)\s*search", c) or "t&search" in c \
            or c in ("t&s", "track & search", "track and search"):
        return "Track & Search"
    if "track" in c:  # plain tracking
        return "Tracking"
    return cat or "Tracking / Track & Search"


def _disambiguate_nsw_tracking(events, sm_listings, _sm_module=None):
    """Split NSW's combined "Tracking / Track & Search" events into their
    specific discipline where a distinguishing source confirms which one it is.

    Sources that DO separate the two: Show Manager listings (`discipline` is
    "Tracking" or "Track & Search"), Top Dog events (already in `events`, with
    those specific categories), and — for events still unresolved — the Show
    Manager DETAIL page's full event name (via `_sm_module.fetch_event_detail`).
    Events with no confirmation are LEFT combined — we never guess. Mutates
    `events` in place; returns the count reclassified.
    """
    COMBINED = "Tracking / Track & Search"
    SPECIFIC = {"Tracking", "Track & Search"}

    def norm_club(name):
        # distinctive lowercase word tokens, so "NETSC Tracking Trial" and
        # "North East ... Club" still key loosely; keep >=4-char words.
        toks = re.findall(r"[a-z]+", (name or "").lower())
        return frozenset(w for w in toks if len(w) >= 4
                         and w not in ("trial", "trials", "club", "tracking",
                                       "track", "search", "test", "open"))

    # Build (region, date, club-token-set) -> specific discipline from
    # distinguishing sources.
    signals = []  # list of (region, date_iso, club_tokens, discipline)
    for L in sm_listings or []:
        disc = canonical_category(L.get("discipline"))
        if disc in SPECIFIC and L.get("region") == "NSW":
            signals.append(("NSW", L.get("date"), norm_club(L.get("club")), disc))
    for e in events:
        if e.get("region") == "NSW" and e.get("category") in SPECIFIC \
                and (e.get("source") or "").startswith(("Top Dog", "Show Manager")):
            signals.append(("NSW", e.get("start"),
                            norm_club(e.get("club") or e.get("title")),
                            e["category"]))

    def find_signal(region, date_iso, club_tokens):
        best = None
        for (r, d, toks, disc) in signals:
            if r != region or d != date_iso or not toks:
                continue
            overlap = len(club_tokens & toks)
            if overlap >= 1 and (best is None or overlap > best[0]):
                best = (overlap, disc)
        return best[1] if best else None

    n = 0
    for e in events:
        if e.get("region") != "NSW" or e.get("category") != COMBINED:
            continue
        club_tokens = norm_club(e.get("club") or e.get("title"))
        disc = find_signal("NSW", e.get("start"), club_tokens)
        if disc:
            e["category"] = disc
            # keep the title's trailing label in step if it named the combined form
            if e.get("title"):
                e["title"] = re.sub(r"Tracking\s*/\s*Track & Search\s*$", disc,
                                    e["title"])
            n += 1

    # Second pass: for NSW events STILL combined, consult the Show Manager
    # DETAIL page's full event name (e.g. "... Track & Search Trial"), which
    # distinguishes the two where the listing-level discipline was generic. We
    # only fetch details for the specific unresolved NSW events (a small set),
    # matching them to a candidate SM listing in the tracking family first.
    n_detail = 0
    if _sm_module is not None:
        # Index candidate SM listings by (date, club-tokens) for NSW tracking-
        # family listings that carry an event_id.
        tracking_family = SPECIFIC | {COMBINED}
        cand = []
        for L in sm_listings or []:
            if L.get("region") != "NSW" or not L.get("event_id"):
                continue
            if canonical_category(L.get("discipline")) in tracking_family:
                cand.append((L.get("date"), norm_club(L.get("club")),
                             L.get("event_id")))
        for e in events:
            if e.get("region") != "NSW" or e.get("category") != COMBINED:
                continue
            etoks = norm_club(e.get("club") or e.get("title"))
            eid = None
            for (d, toks, event_id) in cand:
                if d == e.get("start") and toks and len(etoks & toks) >= 1:
                    eid = event_id
                    break
            if not eid:
                continue
            try:
                detail = _sm_module.fetch_event_detail(eid)
            except Exception:
                detail = {}
            name = (detail or {}).get("event_name") or ""
            low = name.lower()
            disc = None
            if re.search(r"track\s*(?:&|and)\s*search|t\s*&\s*s\b", low):
                disc = "Track & Search"
            elif "tracking" in low:
                disc = "Tracking"
            if disc:
                e["category"] = disc
                if e.get("title"):
                    e["title"] = re.sub(r"Tracking\s*/\s*Track & Search\s*$",
                                        disc, e["title"])
                n += 1
                n_detail += 1

    if n:
        print(f"[nsw-disambig] reclassified {n} NSW Tracking/T&S events via "
              f"cross-reference ({n_detail} via SM detail pages; rest left "
              f"combined)", file=sys.stderr)
    else:
        print("[nsw-disambig] no NSW Tracking/T&S events confirmed by another "
              "source; all left combined", file=sys.stderr)
    return n


def collapse_consecutive_days(events):
    """Merge consecutive-day events with the SAME club and SAME discipline into
    a single multi-day event. NSW's PDF lists each day of a trial on its own
    row; this rebuilds them into one "23-25 May" style entry. Only merges when
    club (title) and category match and dates are adjacent (gap <= 1 day).
    """
    if not events:
        return events
    # group by (region, source, title, category)
    from collections import defaultdict
    groups = defaultdict(list)
    passthrough = []
    for e in events:
        # Only collapse events that came from the NSW PDF (per requirement it's
        # the one that lists per-day); others keep their own multi-day handling.
        if e.get("source") == "Dogs NSW":
            groups[(e.get("region"), e.get("title"), e.get("category"))].append(e)
        else:
            passthrough.append(e)

    merged = []
    for key, evs in groups.items():
        evs.sort(key=lambda x: x.get("start") or "")
        run = [evs[0]]
        for cur in evs[1:]:
            try:
                prev_end = dt.date.fromisoformat(run[-1].get("end") or run[-1]["start"])
                cur_start = dt.date.fromisoformat(cur["start"])
                adjacent = (cur_start - prev_end).days <= 1
            except (ValueError, TypeError):
                adjacent = False
            if adjacent:
                run.append(cur)
            else:
                merged.append(_merge_run(run))
                run = [cur]
        merged.append(_merge_run(run))
    return passthrough + merged


def _merge_run(run):
    """Collapse a run of same-club/discipline events into one spanning event."""
    first = dict(run[0])
    first["start"] = run[0]["start"]
    first["end"] = run[-1].get("end") or run[-1]["start"]
    # keep a provider if any day had one
    for e in run:
        if e.get("provider") and not first.get("provider"):
            first["provider"] = e["provider"]
    return first


def build_year():
    """Build and write ONE year's calendar. Reads the module globals YEAR and
    OUTPUT (the multi-year main() reassigns them before each call), so all the
    parsers/helpers that read YEAR pick up the right year without threading it
    through every call site. Returns the event count written (or None if the
    write-guard refused)."""
    all_events = []
    for source in SOURCES:
        try:
            all_events.extend(scrape_source(source))
        except Exception as e:
            print(f"[{source['id']}] FAILED: {e}", file=sys.stderr)

    # --- Dogs NSW (PDF, source of truth for NSW) -----------------------------
    if HAVE_NSW:
        try:
            nsw_events = nsw_pdf.parse_nsw_pdf(YEAR)
            for e in nsw_events:
                e.setdefault("source", "Dogs NSW")
                e["color"] = REGION_COLOR.get("NSW", "#7a3ea6")
            all_events.extend(nsw_events)
        except Exception as e:
            print(f"[nsw] FAILED: {e}", file=sys.stderr)

    # --- Dogs Victoria official calendar (PDF, VIC cross-check / gap-fill) ----
    # The governing body's authoritative master calendar. Added to the pool so
    # the cross-source dedup below merges entries we already have from vicdog /
    # Top Dog, while DV-only tracking trials (e.g. events never published to the
    # website) are kept. Failures return [] and never break the run.
    if HAVE_DV:
        try:
            dv_events = dv_calendar.parse_dv_calendar(YEAR)
            for e in dv_events:
                e.setdefault("source", "Dogs Victoria (official calendar)")
                e["color"] = REGION_COLOR.get("VIC", "#3aa657")
            all_events.extend(dv_events)
        except Exception as e:
            print(f"[dv] FAILED: {e}", file=sys.stderr)

    # --- Dogs Queensland master trial calendar (PDF, QLD primary source) -----
    # Covers all sport disciplines for QLD. No entry status in the PDF, so these
    # are governing-body "approved" listings, cross-checked downstream against
    # Show Manager for entry status. Failures return [] and never break the run.
    if HAVE_QLD:
        try:
            qld_events = qld_calendar.parse_qld_calendar(YEAR)
            for e in qld_events:
                e.setdefault("source", "Dogs Queensland (trial calendar)")
                e["color"] = REGION_COLOR.get("QLD", "#c0392b")
            all_events.extend(qld_events)
        except Exception as e:
            print(f"[qld] FAILED: {e}", file=sys.stderr)

    # --- Dogs West (WA calendar) (PDF, WA primary source) --------------------
    # Yearly calendar of all WA events. Free-text fixtures mapped to disciplines;
    # no entry status (cross-checked downstream). Fails safe to [].
    if HAVE_WA:
        try:
            wa_events = wa_calendar.parse_wa_calendar(YEAR)
            for e in wa_events:
                e.setdefault("source", "Dogs West (WA calendar)")
                e["color"] = REGION_COLOR.get("WA", "#16887a")
            all_events.extend(wa_events)
        except Exception as e:
            print(f"[wa] FAILED: {e}", file=sys.stderr)

    # National Events (Dogs Australia): supplementary feed of national-title and
    # major breed championship events. Adds events; verifies nothing; dedups
    # like any other source.
    if HAVE_NE:
        try:
            ne_events = national_events.parse_national_events(YEAR)
            for e in ne_events:
                e.setdefault("source", "Dogs Australia (National Events)")
                if e.get("region"):
                    e["color"] = REGION_COLOR.get(e["region"])
            all_events.extend(ne_events)
        except Exception as e:
            print(f"[ne] FAILED: {e}", file=sys.stderr)

    # Normalise the assorted discipline labels from all parsers into the fixed
    # canonical set BEFORE dedup/collapse/matching depend on them.
    for e in all_events:
        e["category"] = canonical_category(e.get("category"))
        # Seed each event's `sources` list from its origin source. Dedup will
        # merge these so a surviving event records EVERY source that listed it,
        # which drives the "verified = corroborated by 2+ sources" rule.
        if not e.get("sources"):
            e["sources"] = [e["source"]] if e.get("source") else []

    # Collapse NSW per-day rows into multi-day events (same club + discipline).
    all_events = collapse_consecutive_days(all_events)

    # De-duplicate. Within a source, exact (title, start, region) is enough.
    # Across sources the same trial can have different titles (e.g. vicdog names
    # the club, Top Dog names the event), so we also collapse events that share
    # (start, end, region) AND clearly refer to the same trial via strong word
    # overlap in their titles. Conservative: only merge on high overlap so two
    # genuinely different same-day same-state trials stay separate.
    # State-name variants that appear inside club titles and vary by source
    # ("... of Vic" vs "... of Victoria"). Normalise them to one token so they
    # count as overlap rather than being dropped (Vic is <4 chars) or mismatched.
    _STATE_WORDS = {
        "vic": "victoria", "victoria": "victoria",
        "nsw": "newsouthwales", "qld": "queensland", "queensland": "queensland",
        "sa": "southaustralia", "wa": "westaustralia",
        "tas": "tasmania", "tasmania": "tasmania",
        "act": "act", "nt": "northernterritory",
    }
    _TITLE_STOP = {"trial", "trials", "test", "tests", "club", "dog", "dogs",
                   "obedience", "tracking", "track", "search", "scent",
                   "inc", "the", "of", "and", "open"}

    def title_tokens(t):
        # Normalise state words first, then keep distinctive tokens. State words
        # are mapped to a canonical form and kept (they can be the only thing
        # distinguishing/uniting two source titles, e.g. Vic vs Victoria).
        # Tokens are drawn from alphanumeric words so short-but-distinctive
        # identifiers survive (e.g. "K9", "SSDC") — dropping everything <4 chars
        # reduced names like "K9 Scent Club" to nothing, which broke dedup.
        raw = re.findall(r"[a-z0-9&]+", (t or "").lower())
        toks = set()
        for w in raw:
            if w in _STATE_WORDS:
                toks.add(_STATE_WORDS[w])
            elif w in _TITLE_STOP:
                continue
            elif len(w) >= 4:
                toks.add(w)
            elif len(w) >= 2 and any(c.isdigit() for c in w):
                # short alphanumeric identifier like "k9" — distinctive, keep it
                toks.add(w)
        return toks

    def norm_core(t):
        """The distinctive-token core of a title, used to detect that two
        differently-worded source titles are the same club/event."""
        return title_tokens(t)

    # Stopwords that are not part of a club's distinctive name, so they don't
    # count toward its initials when matching an acronym.
    _ACR_STOP = {"inc", "the", "of", "and", "&", "open", "trial", "trials",
                 "test", "tests", "dog", "dogs"}

    def name_initials(t):
        """Initials of the significant words in a title, e.g.
        'North East Tracking & Scent Club' -> 'NETSC'."""
        words = [w for w in re.findall(r"[A-Za-z&]+", t or "")
                 if w.lower() not in _ACR_STOP and w != "&"]
        return "".join(w[0] for w in words).upper()

    def acronym_tokens(t):
        """All-caps tokens of length >=3 in a title that look like an acronym
        (e.g. 'NETSC'). These are candidate abbreviations of a club name."""
        return {w for w in re.findall(r"\b[A-Z]{3,}\b", t or "")}

    def same_club_by_acronym(a, b):
        """True if an acronym token in one title equals the leading initials of
        the other title's significant words (club abbreviated vs spelled out)."""
        for ta, tb in ((a, b), (b, a)):
            init = name_initials(tb)
            if not init:
                continue
            for acr in acronym_tokens(ta):
                # exact, or acronym is a prefix of the full initials (handles a
                # trailing 'Inc' etc. that survived), min length 3 to be safe.
                if len(acr) >= 3 and (acr == init or init.startswith(acr)):
                    return True
        return False

    # Words dropped from the ORDERED name used for prefix matching: only true
    # fillers and generic trial-type words — NOT discipline words or 'club',
    # which are part of a club's actual name (e.g. "K9 Scent Club") and give
    # short names enough words to form a reliable multi-word prefix.
    _NAME_FILLER = {"inc", "the", "of", "and", "open", "trial", "trials",
                    "test", "tests"}

    def norm_name(t):
        """Ordered list of a title's name words (only true fillers/trial-type
        words removed; discipline words and 'club' kept, unlike title_tokens).
        Used for prefix-containment: one source's club name being the leading
        part of the other's is a strong same-event signal that token-set overlap
        misses for short names like 'K9 Scent Club'."""
        raw = re.findall(r"[a-z0-9&]+", (t or "").lower())
        out = []
        for w in raw:
            if w in _STATE_WORDS:
                out.append(_STATE_WORDS[w])
            elif w in _NAME_FILLER or w == "&":
                continue
            elif len(w) >= 3 or (len(w) >= 2 and any(c.isdigit() for c in w)):
                out.append(w)
        return out

    def name_prefix_match(a, b):
        """True if one normalised name is a (non-trivial) leading prefix of the
        other — e.g. 'k9 scent club' vs 'k9 scent club geelong'. Requires the
        shorter to be >=2 significant words so a single shared generic token
        (e.g. just 'k9') can't trigger a merge."""
        na, nb = norm_name(a), norm_name(b)
        short, long = (na, nb) if len(na) <= len(nb) else (nb, na)
        if len(short) < 2:
            return False
        return long[:len(short)] == short

    seen_exact = set()
    kept = []
    for e in sorted(all_events, key=lambda x: (x.get("start") or "", x["title"])):
        key = (e["title"].lower(), e.get("start"), e.get("region"))
        if key in seen_exact:
            continue
        seen_exact.add(key)
        kept.append(e)

    unique = []

    # Generic club-name scaffolding that is NOT distinctive: every "X Club of
    # Vic Inc" shares these, so they must be excluded from club-identity
    # matching or unrelated breed clubs on the same day would all collapse.
    _CLUB_GENERIC = ({"club", "association", "society", "kennel", "canine",
                      "region", "regional", "districts", "district", "county",
                      "state", "australian", "australia", "royal"}
                     | set(_STATE_WORDS.values()) | set(_STATE_WORDS.keys()))

    def club_id_tokens(ev):
        """DISTINCTIVE club-name tokens from title AND location combined. Drops
        both the aggressive title-stops (discipline words) AND generic club
        scaffolding ('club', 'kennel', state names, etc.), leaving only what
        actually identifies the club — 'k9' for "K9 Scent Club", {'afghan',
        'hound'} for "Afghan Hound Club of Vic". Sources put the club name in
        different fields (DV: title; Top Dog: location), so both are combined."""
        parts = [ev.get("title", "")]
        loc = ev.get("location", "")
        if loc and loc.strip().lower() not in _STATE_WORDS:
            parts.append(loc)
        toks = title_tokens(" ".join(parts))
        return {t for t in toks if t not in _CLUB_GENERIC}

    for e in kept:
        dup = False
        toks = title_tokens(e["title"])
        e_club = club_id_tokens(e)
        for k in unique:
            # Anchor on the START date + region. The END may differ between
            # sources for the SAME trial (one lists a single day, another the
            # full weekend span), so we don't require identical ends here —
            # the discipline + club-name checks below do the discriminating, and
            # start+region+category+club is enough to identify one trial.
            if (k.get("start") == e.get("start")
                    and k.get("region") == e.get("region")):
                # Same trial must be the same discipline. Without this, a club
                # running (say) a Tracking AND a Scent Work trial on the same day
                # could be wrongly merged — especially via the acronym match,
                # where both share the club's initials.
                if k.get("category") != e.get("category"):
                    continue
                ktoks = title_tokens(k["title"])
                overlap = len(toks & ktoks)
                # A subset match is only trustworthy if the smaller set has >=2
                # tokens; a single shared token (e.g. just "k9") is too weak and
                # could merge different clubs — those cases are instead handled
                # by name_prefix_match, which requires an ordered 2+ word prefix.
                subset_ok = ((toks and toks <= ktoks and len(toks) >= 2)
                             or (ktoks and ktoks <= toks and len(ktoks) >= 2))
                # Club-identity match across title+location (handles the DV-vs-
                # Top Dog field-placement difference). Merge when the distinctive
                # club tokens of one are a non-empty subset of the other's — this
                # matches "K9 Scent Club" {k9} inside Top Dog's {k9, geelong},
                # while keeping unrelated breed clubs apart (e.g. {afghan,hound}
                # vs {beagle} are not subsets, and {dachshund} vs {dalmatian}
                # share nothing).
                k_club = club_id_tokens(k)
                club_match = bool(e_club and k_club) and (
                    e_club <= k_club or k_club <= e_club)
                if (overlap >= 2 or subset_ok or club_match
                        or same_club_by_acronym(e["title"], k["title"])
                        or name_prefix_match(e["title"], k["title"])):
                    dup = True
                    # Merge the duplicate's source(s) into the survivor so it
                    # records every source that corroborated this event.
                    for s in e.get("sources", []):
                        if s and s not in k["sources"]:
                            k["sources"].append(s)
                    # Preserve a Top Dog "enterable" flag from either copy.
                    if e.get("topdog_open"):
                        k["topdog_open"] = True
                    # Keep the fuller date span (sources may disagree on whether
                    # a weekend trial is 1 or 2 days; show the longer end).
                    if (e.get("end") or "") > (k.get("end") or ""):
                        k["end"] = e["end"]
                    # Fill any field the survivor lacks from the duplicate, so
                    # merging keeps the best available info (location, links...).
                    for fld in ("location", "entry_url", "schedule_url",
                                "closes", "address"):
                        if not k.get(fld) and e.get(fld):
                            k[fld] = e[fld]
                    # A SPECIFIC per-event link (Top Dog /trials/<id> or a Show
                    # Manager detail page) from EITHER copy should win over a
                    # generic listing/calendar page — otherwise a DV survivor
                    # keeps its generic calendar URL and the real Top Dog entry
                    # link is lost. Stash the best specific link seen.
                    for cand in (e.get("url"), e.get("entry_url")):
                        if _is_specific_event_link(cand):
                            k["_best_link"] = cand
                            break
                    # Stash the duplicate's title/location text so the later
                    # club/detail derivation can draw the club name from one
                    # source and the trial-type descriptor from the other.
                    alt = k.setdefault("_alt_text", [])
                    if e.get("title"):
                        alt.append(e["title"])
                    if e.get("location"):
                        alt.append(e["location"])
                    break
        if not dup:
            unique.append(e)

    # --- Entry-status cross-check --------------------------------------------
    # Scrape Show Manager (reliable, verifiable) and annotate each event with a
    # status + verification level. Never claims open/closed unless confirmed.
    if HAVE_SM and HAVE_MATCHER:
        try:
            sm_listings = show_manager.scrape_show_manager(YEAR)
            # Disambiguate NSW's combined "Tracking / Track & Search" events into
            # their specific discipline where a distinguishing source (Show
            # Manager listing or Top Dog event) confirms which one it is. Events
            # with no such confirmation stay combined (we don't guess).
            _disambiguate_nsw_tracking(unique, sm_listings, _sm_module=show_manager)
            matcher.match_events(unique, sm_listings)
            # Fill gaps: any Show Manager listing in our regions that no
            # governing-body event matched becomes its own event (per the
            # "only where no governing-body source exists" rule). This is what
            # gives SA coverage and tops up thin TAS/ACT.
            matched_ids = getattr(matcher.match_events, "last_matched_ids", set())
            gap_events = matcher.events_from_unmatched_listings(
                sm_listings, matched_ids, region_color=REGION_COLOR,
                existing_events=unique)
            for ge in gap_events:
                ge["category"] = canonical_category(ge.get("category"))
            unique.extend(gap_events)

            # --- Dogz Online: second verification/cross-check source ----------
            # Dogz Online's Event Diary also carries closing dates and
            # cancellation flags, so we run its listings through the SAME
            # matcher: it can confirm/annotate events Show Manager didn't cover,
            # and gap-fill any Dogz-only events. Most will already be present
            # (many Dogz schedule links point at the same Show Manager events),
            # so the gap-fill collision check keeps duplicates out.
            if HAVE_DZ:
                try:
                    dz_listings = dogz_online.scrape_dogz_online(YEAR)
                    matcher.match_events(unique, dz_listings, additive=True,
                                         source_label="Dogz Online")
                    dz_matched = getattr(matcher.match_events,
                                         "last_matched_ids", set())
                    dz_gap = matcher.events_from_unmatched_listings(
                        dz_listings, dz_matched, region_color=REGION_COLOR,
                        existing_events=unique,
                        source_name="Dogz Online",
                        default_url="https://www.dogzonline.com.au/event-diary/list.asp")
                    for ge in dz_gap:
                        ge["category"] = canonical_category(ge.get("category"))
                    unique.extend(dz_gap)
                    print(f"[dz] added {len(dz_gap)} Dogz-only events "
                          f"(after dedup)", file=sys.stderr)
                except Exception as e:
                    print(f"[dz] cross-check FAILED (skipping): {e}",
                          file=sys.stderr)

            # --- vicdog.com: VIC verify + gap-fill source --------------------
            # vicdog listings (all disciplines) are layered additively onto the
            # DV-PDF events: match_events corroborates/annotates existing VIC
            # events (raising them to verified and adding cancellation/detail),
            # and events_from_unmatched_listings adds any vicdog-only VIC events
            # the DV PDF missed. Additive mode means it never downgrades an
            # already-verified event.
            try:
                vicdog_listings = scrape_vicdog_listings(YEAR)
                if vicdog_listings:
                    matcher.match_events(unique, vicdog_listings, additive=True,
                                         source_label="Dogs Victoria (Vic Dog Trials)")
                    vd_matched = getattr(matcher.match_events,
                                         "last_matched_ids", set())
                    vd_gap = matcher.events_from_unmatched_listings(
                        vicdog_listings, vd_matched, region_color=REGION_COLOR,
                        existing_events=unique,
                        source_name="Dogs Victoria (Vic Dog Trials)",
                        default_url="https://vicdog.com/events-page/")
                    for ge in vd_gap:
                        ge["category"] = canonical_category(ge.get("category"))
                    unique.extend(vd_gap)
                    print(f"[vicdog] added {len(vd_gap)} vicdog-only events "
                          f"(after dedup)", file=sys.stderr)
            except Exception as e:
                print(f"[vicdog] cross-check FAILED (skipping): {e}",
                      file=sys.stderr)
        except Exception as e:
            print(f"[crosscheck] FAILED: {e}", file=sys.stderr)
            # Fall back to provider-only labels so the page still renders.
            for e2 in unique:
                if "status" not in e2:
                    prov = (e2.get("provider") or "").strip()
                    if prov:
                        e2["status"] = "entries_via_provider"
                        e2["status_label"] = f"Entries via {prov} (unverified)"
                    else:
                        e2["status"] = "approved_not_open"
                        e2["status_label"] = "Approved; not open (unverified)"
                    e2["verified"] = False
    else:
        for e2 in unique:
            e2.setdefault("status", "unknown")
            e2.setdefault("status_label", "")
            e2.setdefault("verified", False)

    # ---- Trust model: verified (real) + open (enterable) --------------------
    # Two INDEPENDENT properties, per the agreed rules:
    #   verified (real)  = corroborated by 2+ sources, OR present on any single
    #                      entry platform (Show Manager / Top Dog). A governing-
    #                      calendar-only event with no other source is NOT
    #                      verified.
    #   open (enterable) = Show Manager reports "open", OR the event is in Top
    #                      Dog's UPCOMING section (topdog_open=True).
    # "Approved; not open" now strictly means: on a governing (ANKC) source
    # only, no entry platform, not otherwise corroborated.
    ENTRY_PLATFORMS = {"Show Manager", "Dogz Online", "Top Dog Events"}
    today = dt.date.today()

    def _closes_passed(ev):
        """True if we KNOW the entry closing date and it is before today. A
        passed closing date is unambiguous, so it overrides a stale 'open'
        verdict from a source that was slow to flip the event to closed."""
        c = ev.get("closes")
        if not c:
            return False
        try:
            return dt.date.fromisoformat(str(c)[:10]) < today
        except (ValueError, TypeError):
            return False

    for e2 in unique:
        srcs = set(e2.get("sources") or ([e2["source"]] if e2.get("source") else []))
        on_entry_platform = bool(srcs & ENTRY_PLATFORMS)
        # Show Manager's own verdict, captured by the matcher earlier.
        sm_open = (e2.get("status") == "open")
        sm_closed = (e2.get("status") == "closed")
        sm_cancelled = (e2.get("status") == "cancelled") or e2.get("cancelled")
        topdog_open = bool(e2.get("topdog_open"))
        closes_passed = _closes_passed(e2)

        # Entry link: prefer a SPECIFIC per-event page over any generic
        # listing/calendar page, drawing from (a) a per-event link stashed
        # during merge (e.g. Top Dog's /trials/<id> when a DV copy was the
        # survivor), then (b) the event's own url/entry_url if specific.
        # A generic page (DV calendar, bare Top Dog /trials) is never used —
        # better no "Enter" link than one that dumps the user on an index.
        # Don't overwrite a Show Manager entry_url the matcher already set.
        sm_entry = e2.get("entry_url")
        if not _is_specific_event_link(sm_entry):
            best = e2.get("_best_link")
            if not _is_specific_event_link(best):
                best = e2.get("url") if _is_specific_event_link(e2.get("url")) else None
            e2["entry_url"] = best  # may be None -> no Enter link shown
        e2.pop("_best_link", None)

        # ---- verified (real) ----
        e2["verified"] = (len(srcs) >= 2) or on_entry_platform

        # ---- open (enterable) ----  distinct from verified
        # A source may be slow to flip open->closed, so if we KNOW the closing
        # date has passed, the event is not open regardless of the source verdict.
        e2["open_now"] = (sm_open or topdog_open) and not sm_cancelled \
            and not closes_passed

        # ---- headline status + label ----
        if sm_cancelled:
            e2["status"] = "cancelled"
            e2["status_label"] = "Cancelled"
        elif e2["open_now"]:
            e2["status"] = "open"
            e2["status_label"] = "Open" + (" (verified)" if e2["verified"] else "")
        elif sm_closed or closes_passed:
            # Either the source said closed, OR we know the closing date passed.
            e2["status"] = "closed"
            e2["status_label"] = "Entries closed"
        elif on_entry_platform:
            # listed on an entry platform but not currently open/closed-known
            e2["status"] = "listed"
            e2["status_label"] = "Listed" + (" (verified)" if e2["verified"] else "")
        else:
            # governing-source only, no entry platform
            e2["status"] = "approved_not_open"
            e2["status_label"] = "Approved; not open (unverified)"

    # ---- Past-event pass ----------------------------------------------------
    # "Past" (the event's last day has gone by) is INDEPENDENT of entry-state
    # (open / entries-closed / cancelled / approved). They can co-occur:
    #   - cancelled + past  -> allowed (a cancelled event whose date has passed)
    #   - closed   + past   -> allowed (entries closed AND the event has run)
    #   - open     + past   -> NOT allowed: once it's over it isn't "open", so a
    #                          past event that was "open" is downgraded to closed
    #   - open     + cancelled -> impossible (entry-state is a single value)
    # We therefore set a separate `is_past` flag rather than overwriting status,
    # so the entry-state survives and the UI can show both badges.
    #   - Uses the END date, so a multi-day trial stays current until its last
    #     day has passed.
    today = dt.date.today()
    for e2 in unique:
        end_iso = e2.get("end") or e2.get("start")
        try:
            past = dt.date.fromisoformat(end_iso) < today
        except (ValueError, TypeError):
            past = False
        e2["is_past"] = past
        if past:
            # A past event can't still be taking entries: open -> closed.
            if e2.get("status") == "open" and not (
                    e2.get("status") == "cancelled" or e2.get("cancelled")):
                e2["status"] = "closed"
                e2["status_label"] = "Entries closed"
                e2["open_now"] = False

    # Source list for the UI. Top Dog Events is multi-region; report it once.
    source_meta = []
    for s in SOURCES:
        source_meta.append({
            "name": s["name"],
            "region": s.get("region"),  # None for multi-region sources
            "color": s.get("color") or "#8a8172",
            "url": s["source_url"],
        })
    if HAVE_NSW:
        source_meta.append({
            "name": "Dogs NSW",
            "region": "NSW",
            "color": REGION_COLOR.get("NSW", "#7a3ea6"),
            "url": "https://www.dogsnsw.org.au/events/show-and-trials-guide/",
        })
    if HAVE_DV:
        source_meta.append({
            "name": "Dogs Victoria (official calendar)",
            "region": "VIC",
            "color": REGION_COLOR.get("VIC", "#3aa657"),
            "url": "https://dogsvictoria.org.au/events/dogs-victoria-events-calendar/",
        })
    if HAVE_QLD:
        source_meta.append({
            "name": "Dogs Queensland (trial calendar)",
            "region": "QLD",
            "color": REGION_COLOR.get("QLD", "#c0392b"),
            "url": "https://dogsqueensland.org.au/events/showtrial-dates/",
        })
    if HAVE_WA:
        source_meta.append({
            "name": "Dogs West (WA calendar)",
            "region": "WA",
            "color": REGION_COLOR.get("WA", "#16887a"),
            "url": "https://dogswest.com/dogswest/Members-Yearly_Show_Date_Calendars.htm",
        })
    if HAVE_SM:
        source_meta.append({
            "name": "Show Manager",
            "region": None,  # multi-region gap-fill source
            "color": "#8a8172",
            "url": "https://www.showmanager.com.au/events/publicevents?g=2",
        })
    if HAVE_DZ:
        source_meta.append({
            "name": "Dogz Online",
            "region": None,  # multi-region verification/gap-fill source
            "color": "#6d7f9c",
            "url": "https://www.dogzonline.com.au/event-diary/list.asp",
        })
    if HAVE_NE:
        source_meta.append({
            "name": "Dogs Australia (National Events)",
            "region": None,  # multi-region supplementary feed
            "color": "#9c6d7f",
            "url": "https://dogsaustralia.org.au/members/events/national-events/",
        })
    # vicdog is a VIC verify/gap-fill layer (not a SOURCES primary). List it only
    # if it actually corroborated or added any events this run.
    if any("Dogs Victoria (Vic Dog Trials)" in (e.get("sources") or [])
           for e in unique):
        source_meta.append({
            "name": "Dogs Victoria (Vic Dog Trials)",
            "region": "VIC",
            "color": "#3aa657",
            "url": "https://vicdog.com/events-page/",
        })

    # Derive the club/organisation name over EVERY event (for the info-line tag
    # between State and Source). Sources place the club in different fields; this
    # picks it out. The event title is left as-is for the headline. Also strip
    # any internal scratch fields so they never ship in the JSON.
    for e in unique:
        _derive_club(e)
        e.pop("_best_link", None)
        e.pop("_alt_text", None)

    payload = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "year": YEAR,
        "count": len(unique),
        "sources": source_meta,
        "events": unique,
    }

    # ---- Safeguard: don't let a transient source failure wipe the live data.
    # This calendar now aggregates ~1,800 events from 8+ independent sources
    # (governing-body PDFs, Show Manager, Top Dog). The dominant failure mode is
    # a SINGLE source silently dropping out (a PDF 404s, Show Manager times out,
    # the Top Dog browser crashes). A per-source comparison catches that most
    # directly — a failed source is exactly "this source's count went to ~0".
    #
    # Two-tier response (per the design decision):
    #   CATASTROPHIC -> refuse to publish (keep yesterday's file, exit non-zero):
    #       * overall total collapses (< 60% of before), OR
    #       * a source that had >=15 events last run drops to <20% this run
    #         (i.e. a whole source almost certainly failed), OR
    #       * a region that had >=15 events drops to <20%.
    #   NOTABLE -> publish, but log a loud WARNING for a human to eyeball:
    #       * total drops 15-40%, OR
    #       * any source/region with >=8 events drops by >50% (but not to ~0).
    # Override refusal with ALLOW_SHRINK=1 for a legitimate large reduction.
    allow_shrink = os.environ.get("ALLOW_SHRINK") == "1"
    try:
        if OUTPUT.exists():
            from collections import Counter
            old = json.loads(OUTPUT.read_text())
            old_events = old.get("events", [])
            old_count = len(old_events)
            new_count = len(unique)

            old_by_region = Counter(e.get("region") for e in old_events)
            new_by_region = Counter(e.get("region") for e in unique)
            old_by_source = Counter(e.get("source") for e in old_events)
            new_by_source = Counter(e.get("source") for e in unique)

            catastrophic = []   # -> refuse (unless ALLOW_SHRINK)
            notable = []        # -> warn but publish

            if old_count >= 40:
                ratio = new_count / old_count if old_count else 1.0
                if ratio < 0.6:
                    catastrophic.append(
                        f"total {old_count} -> {new_count} "
                        f"({(1-ratio)*100:.0f}% drop)")
                elif ratio < 0.85:
                    notable.append(
                        f"total {old_count} -> {new_count} "
                        f"({(1-ratio)*100:.0f}% drop)")

            def check_axis(old_counts, new_counts, label, min_base):
                for key, oc in old_counts.items():
                    if not key or oc < min_base:
                        continue
                    nc = new_counts.get(key, 0)
                    if nc < 0.2 * oc:
                        catastrophic.append(f"{label} '{key}' {oc} -> {nc} "
                                            f"(likely failed)")
                    elif nc < 0.5 * oc:
                        notable.append(f"{label} '{key}' {oc} -> {nc}")

            # A whole source vanishing is the clearest "source down" signal.
            check_axis(old_by_source, new_by_source, "source", 15)
            check_axis(old_by_region, new_by_region, "region", 15)

            if notable and not catastrophic:
                print("[guard] WARNING: notable reductions vs last publish "
                      "(publishing anyway - eyeball these):", file=sys.stderr)
                for p in notable:
                    print(f"[guard]   {p}", file=sys.stderr)

            if catastrophic and not allow_shrink:
                print(f"[guard] REFUSING to overwrite {OUTPUT.name} - result "
                      "looks like a source failure, not a real change:",
                      file=sys.stderr)
                for p in catastrophic:
                    print(f"[guard]   {p}", file=sys.stderr)
                if notable:
                    for p in notable:
                        print(f"[guard]   (also) {p}", file=sys.stderr)
                print(f"[guard] Keeping existing {OUTPUT.name}. If this reduction "
                      "is real, re-run with ALLOW_SHRINK=1.", file=sys.stderr)
                return None  # skip THIS year; multi-year loop continues
            elif catastrophic and allow_shrink:
                print("[guard] ALLOW_SHRINK set - publishing despite "
                      "catastrophic drop:", file=sys.stderr)
                for p in catastrophic:
                    print(f"[guard]   {p}", file=sys.stderr)
    except SystemExit:
        raise
    except Exception as e:
        # A guard failure must never crash the run; just log and proceed.
        print(f"[guard] check skipped ({e})", file=sys.stderr)

    # Print a breakdown to the log every run (in-process, so it can't be lost
    # between shell commands). Useful in both the dry-run and the daily job.
    from collections import Counter as _Counter
    _by_region = _Counter(e.get("region") for e in unique)
    _by_cat = _Counter(e.get("category") for e in unique)
    _by_source = _Counter(e.get("source") for e in unique)
    _by_status = _Counter(e.get("status_label") or e.get("status") for e in unique)
    print("==== SUMMARY ====", file=sys.stderr)
    print(f"total: {len(unique)}", file=sys.stderr)
    print(f"by region: {dict(_by_region)}", file=sys.stderr)
    print(f"by category: {dict(_by_cat)}", file=sys.stderr)
    print(f"by source: {dict(_by_source)}", file=sys.stderr)
    print("by status:", file=sys.stderr)
    for _k, _v in _by_status.most_common():
        print(f"  {_v:4}  {_k}", file=sys.stderr)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {len(unique)} events -> {OUTPUT}", file=sys.stderr)
    return len(unique)


def _output_path_for(year):
    """Per-year output file: docs/events-<year>.json. The current year is ALSO
    written to docs/events.json so the site has a stable default file."""
    return Path(__file__).resolve().parent.parent / "docs" / f"events-{year}.json"


# A past year whose scraped output is byte-identical (ignoring the run
# timestamp) for this many consecutive runs is considered settled and frozen.
FREEZE_AFTER = 5


def _year_signature(path):
    """Stable content hash of a year's output file, IGNORING the volatile
    `generated` timestamp (and the manifest-ish top-level meta), so two runs
    that produced the same events compare equal. Returns a hex digest, or None
    if the file can't be read. Events are sorted so ordering can't cause a false
    'changed' result."""
    try:
        data = json.loads(Path(path).read_text())
    except Exception:
        return None
    import hashlib
    events = data.get("events", [])
    # Canonicalise: sort events by a stable key, dump with sorted keys.
    try:
        events_sorted = sorted(
            events,
            key=lambda e: (str(e.get("start")), str(e.get("region")),
                           str(e.get("category")), str(e.get("title"))))
    except Exception:
        events_sorted = events
    blob = json.dumps(events_sorted, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def target_years():
    """The years to build this run. Defaults to a window around the current
    system year (previous, current, next) so the site is current-year-driven and
    keeps one past + one upcoming year for the year menu. Override with the YEARS
    env var (comma-separated), e.g. YEARS=2025,2026,2027."""
    env = os.environ.get("YEARS", "").strip()
    if env:
        out = []
        for tok in env.split(","):
            tok = tok.strip()
            if tok.isdigit():
                out.append(int(tok))
        if out:
            return sorted(set(out))
    this = dt.date.today().year
    return [this - 1, this, this + 1]


def main():
    """Build each target year into its own docs/events-<year>.json (and mirror
    the current year to docs/events.json). One year's guard refusal or error
    doesn't stop the others.

    FREEZE OPTIMISATION: a PAST year (before the current year) whose scraped
    output has been byte-identical (ignoring the run timestamp) for
    FREEZE_AFTER consecutive runs is considered settled — its sources have
    stopped changing it — and is thereafter SKIPPED entirely, keeping its
    existing file. This avoids re-running a whole year's expensive build (incl.
    the Top Dog browser walk) for data that no longer changes. The current and
    future years are never frozen. State (signature + stable-run count + frozen
    flag) is persisted per-year in years.json and read back each run.
    """
    global YEAR, OUTPUT
    years = target_years()
    this_year = dt.date.today().year
    print(f"==== MULTI-YEAR BUILD: {years} (current={this_year}) ====",
          file=sys.stderr)

    # Load the PRIOR manifest to recover each year's freeze state.
    man_path = Path(__file__).resolve().parent.parent / "docs" / "years.json"
    prior = {}
    try:
        if man_path.exists():
            pj = json.loads(man_path.read_text())
            for y in pj.get("years", []):
                if "year" in y:
                    prior[int(y["year"])] = y
    except Exception as e:
        print(f"[freeze] couldn't read prior manifest ({e}); starting fresh",
              file=sys.stderr)

    manifest = {"generated": dt.datetime.now(dt.timezone.utc)
                .isoformat(timespec="seconds"),
                "current_year": this_year, "years": []}
    for yr in years:
        prev = prior.get(yr, {})
        # Skip a past year that has already frozen.
        if yr < this_year and prev.get("frozen"):
            OUTPUT = _output_path_for(yr)
            n = None
            try:
                n = len(json.loads(OUTPUT.read_text()).get("events", [])) \
                    if OUTPUT.exists() else prev.get("count")
            except Exception:
                n = prev.get("count")
            print(f"\n==== YEAR {yr}: FROZEN (settled {prev.get('stable_runs')}"
                  f"x) — skipping rebuild, keeping {OUTPUT.name} ====",
                  file=sys.stderr)
            entry = {"year": yr, "count": n, "frozen": True,
                     "stable_runs": prev.get("stable_runs"),
                     "signature": prev.get("signature")}
            manifest["years"].append(entry)
            continue

        YEAR = yr
        OUTPUT = _output_path_for(yr)
        print(f"\n==== BUILDING YEAR {yr} -> {OUTPUT.name} ====", file=sys.stderr)
        try:
            count = build_year()
        except Exception as e:
            print(f"[year {yr}] build FAILED: {e}", file=sys.stderr)
            count = None
        if count is not None:
            entry = {"year": yr, "count": count}
            # Freeze bookkeeping (only meaningful for PAST years; current/future
            # never freeze because they legitimately keep changing).
            sig = _year_signature(OUTPUT)
            if yr < this_year and sig is not None:
                if sig == prev.get("signature"):
                    runs = int(prev.get("stable_runs") or 0) + 1
                else:
                    runs = 1  # changed this run -> reset the streak
                frozen = runs >= FREEZE_AFTER
                entry["signature"] = sig
                entry["stable_runs"] = runs
                entry["frozen"] = frozen
                if frozen:
                    print(f"[freeze] year {yr} now FROZEN — identical for "
                          f"{runs} consecutive runs; future runs will skip it",
                          file=sys.stderr)
                else:
                    print(f"[freeze] year {yr} stable-run streak {runs}/"
                          f"{FREEZE_AFTER}", file=sys.stderr)
            manifest["years"].append(entry)
            # Mirror the current year to the stable events.json default.
            if yr == this_year and OUTPUT.exists():
                stable = Path(__file__).resolve().parent.parent / "docs" / "events.json"
                stable.write_text(OUTPUT.read_text())
                print(f"[year {yr}] mirrored to {stable.name} (current year)",
                      file=sys.stderr)
        else:
            # Build failed/refused. Preserve the prior file's count AND carry
            # forward the prior freeze state (a failed run must NOT advance or
            # reset the freeze streak — it simply didn't produce a comparison).
            if OUTPUT.exists():
                try:
                    existing = json.loads(OUTPUT.read_text())
                    entry = {"year": yr,
                             "count": len(existing.get("events", []))}
                    for k in ("signature", "stable_runs", "frozen"):
                        if k in prev:
                            entry[k] = prev[k]
                    manifest["years"].append(entry)
                except Exception:
                    pass
    # Write a small manifest the front-end reads to build its year menu.
    manifest["years"].sort(key=lambda x: x["year"])
    man_path.write_text(json.dumps(manifest, indent=2))
    print(f"\nWrote year manifest ({[y['year'] for y in manifest['years']]}) "
          f"-> {man_path.name}", file=sys.stderr)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Show Manager Event Diary scraper  (stage 2a of the source-of-truth rebuild).

ROLE: Show Manager is the RELIABLE entry-status verification source. Unlike Top
Dog (JavaScript pagination) it exposes a public, server-rendered HTML Event
Diary that we can page through by month. We use it to answer, for a given
governing-body event: is it open, closed, or cancelled for entry?

URL (Dog Sports group = g=2), one month at a time:
  https://www.showmanager.com.au/events?y=2026&m=8&s=ALL&o=True&g=2&a=True&r=False&sl=ALL
  y=year, m=month(1-12), s=state(ALL/NSW/VIC/...), g=2 dog sports,
  o=True online-entry events. We fetch s=ALL and filter states ourselves.

STRUCTURE (verified against real July & August 2026 pages):
  A single month table. Rows are grouped under date-header rows like
  "01-Aug-2026 (Saturday)"; each following event row INHERITS that date until
  the next header. Each event row has cells:
    State | Event Name(+link) | Location | Event Type | Entries Closing | Files | Status
  Event Type is a clean discipline name ("Tracking", "Track & Search",
  "Scent Work", ...). The final status cell is one of:
    "Enter Online"  -> entries OPEN            (link .../AttendEvent/<id>)
    "Entries Closed"-> entries CLOSED
    "Cancelled"     -> event CANCELLED
  There is also an "Entries Closing" date column (e.g. "Mon 27-Jul 11:30 PM").

OUTPUT: list of dicts describing Show Manager listings (NOT calendar events):
    {club, date (ISO), discipline, region, status, closes (ISO or None),
     detail_url, event_id}
  status in {"open","closed","cancelled","unknown"}.

These are consumed by the cross-check in scrape.py, which matches them to
governing-body events by (region, date, discipline, fuzzy club name).
"""

import re
import sys
import datetime as dt

import requests

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

HEADERS = {"User-Agent": "TrackingCalendarBot/1.0 (+combined tracking events)"}
TIMEOUT = 60

SM_BASE = "https://www.showmanager.com.au/events"

# Show Manager's full Dog Sports discipline taxonomy, mapped to canonical
# category names used across the calendar. Keys are matched case-insensitively
# against the event-type cell. We keep the Tracking vs Track & Search vs Scent
# Work distinction (as before) and now also carry every other ANKC sport.
SM_DISCIPLINES = {
    "tracking": "Tracking",
    "track & search": "Track & Search",
    "track and search": "Track & Search",
    "scent work": "Scent Work",
    "scentwork": "Scent Work",
    "agility": "Agility",
    "jumping": "Agility",            # jumping/games are agility-family classes
    "games": "Agility",
    "obedience": "Obedience",
    "rally": "Rally Obedience",
    "rally obedience": "Rally Obedience",
    "dances with dogs": "Dances with Dogs",
    "dwd": "Dances with Dogs",
    "herding": "Herding",
    "earth dog": "Earthdog",
    "earthdog": "Earthdog",
    "endurance": "Endurance",
    "endurance test": "Endurance",
    "lure coursing": "Lure Coursing",
    "field trial": "Field Trial",
    "retrieve": "Retrieving",
    "retrieving": "Retrieving",
    "retrieving trial": "Retrieving",
    "sled dog": "Sled Sports",
    "sleddog": "Sled Sports",
    "sleddog race": "Sled Sports",
    "sled sports": "Sled Sports",
    "sprint": "Sprint",
    "sprintdog": "Sprint",
    "weight pull": "Weight Pull",
    "trick dog": "Trick Dog",
    "ratg": "RATG",
    "draft test": "Draft Test",
    "backpacking": "Backpacking",
}

# All eight ANKC state/territory jurisdictions.
SM_REGIONS = {"ACT", "NSW", "VIC", "TAS", "SA", "WA", "QLD", "NT"}

# "01-Aug-2026 (Saturday)" date-group header
SM_DATE_HEADER_RE = re.compile(
    r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
# "Mon 27-Jul 11:30 PM" or "Mon 27-Jul" closing datetime
SM_CLOSING_RE = re.compile(
    r"(\d{1,2})-([A-Za-z]{3})(?:-(\d{4}))?")

MONTHS3 = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

DETAIL_ID_RE = re.compile(r"/(?:PublicEvents/Details|AttendEvent)/(\d+)")


def _sm_url(year, month, state="ALL"):
    return (f"{SM_BASE}?y={year}&m={month}&s={state}"
            f"&o=True&g=2&a=True&r=False&sl=ALL")


def _parse_header_date(text, year):
    m = SM_DATE_HEADER_RE.search(text or "")
    if not m:
        return None
    try:
        return dt.date(int(m.group(3)), MONTHS3[m.group(2).lower()], int(m.group(1)))
    except (ValueError, KeyError):
        return None


def _parse_closing(text, year):
    """Parse the 'Entries Closing' cell into an ISO date (year inferred)."""
    if not text:
        return None
    m = SM_CLOSING_RE.search(text)
    if not m:
        return None
    yr = int(m.group(3)) if m.group(3) else year
    try:
        return dt.date(yr, MONTHS3[m.group(2).lower()], int(m.group(1))).isoformat()
    except (ValueError, KeyError):
        return None


_STATE_ALIASES = {
    "new south wales": "NSW", "nsw": "NSW",
    "victoria": "VIC", "vic": "VIC",
    "queensland": "QLD", "qld": "QLD",
    "south australia": "SA", "sa": "SA",
    "western australia": "WA", "wa": "WA",
    "tasmania": "TAS", "tas": "TAS",
    "australian capital territory": "ACT", "act": "ACT",
    "northern territory": "NT", "nt": "NT",
}


# Discipline keys sorted longest-first so multi-word keys ("track & search",
# "rally obedience") are tested before their shorter substrings ("tracking",
# "rally"). Used for token-based fallback matching.
_DISC_KEYS_BY_LEN = None


def _match_discipline(cell_texts):
    """Find the canonical discipline for a row from its cells.
    1) exact cell match (most reliable);
    2) else a whole-word/substring match of a known key within a cell,
       trying longest keys first so 'track & search' wins over 'tracking'.
    Returns the canonical name or None."""
    global _DISC_KEYS_BY_LEN
    if _DISC_KEYS_BY_LEN is None:
        _DISC_KEYS_BY_LEN = sorted(SM_DISCIPLINES.keys(), key=len, reverse=True)

    lows = [ct.strip().lower() for ct in cell_texts]
    # 1) exact match
    for low in lows:
        if low in SM_DISCIPLINES:
            return SM_DISCIPLINES[low]
    # 2) token/substring match, longest key first
    for key in _DISC_KEYS_BY_LEN:
        for low in lows:
            # word-boundary-ish check: key surrounded by start/end or non-alnum
            idx = low.find(key)
            if idx != -1:
                before = low[idx - 1] if idx > 0 else " "
                after_i = idx + len(key)
                after = low[after_i] if after_i < len(low) else " "
                if not before.isalnum() and not after.isalnum():
                    return SM_DISCIPLINES[key]
    return None


def _normalise_state(text):
    """Map a state cell (code or full name) to a canonical jurisdiction code,
    or "" if it isn't one of the eight ANKC jurisdictions. Robust to trailing
    text and casing so we never silently misclassify (e.g. 'New South Wales'
    must not become 'NEW')."""
    t = (text or "").strip().lower()
    if t in _STATE_ALIASES:
        return _STATE_ALIASES[t]
    # Try leading token / first word (e.g. "NSW - Sydney" or "QLD Brisbane").
    first = re.split(r"[\s,;/|-]+", t, 1)[0] if t else ""
    if first in _STATE_ALIASES:
        return _STATE_ALIASES[first]
    return ""


def _status_from_text(text):
    low = (text or "").lower()
    if "cancel" in low:
        return "cancelled"
    if "enter online" in low or "attendevent" in low:
        return "open"
    if "entries closed" in low or "closed" in low:
        return "closed"
    return "unknown"


# Detail-page fetch (OPTIONAL, off by default). Show Manager's per-event Details
# page exposes a full structured venue address and a link to the Schedule PDF.
# Fetching it costs one HTTP request PER EVENT (~1000+/run), so it is gated
# behind a toggle (fetch_details / env SM_FETCH_DETAILS=1) and used only when the
# extra address/schedule data is wanted.
_DETAIL_LABELS = ("Location", "Address", "Suburb", "Post Code", "State")


def _detail_url_for(event_id):
    return f"https://showmanager.com.au/events/publicevents/Details/{event_id}"


def fetch_event_detail(event_id):
    """Fetch one Show Manager event Details page and return
    {address, schedule_url, event_name} (any may be None). Best-effort; never
    raises. event_name is the full page heading (e.g. "... Track & Search
    Trial"), which distinguishes Tracking vs Track & Search where the listing's
    discipline field is generic."""
    if not event_id or not HAVE_BS4:
        return {}
    try:
        r = requests.get(_detail_url_for(event_id), headers=HEADERS,
                         timeout=TIMEOUT)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception as e:
        print(f"[sm-detail] {event_id} fetch failed: {e}", file=sys.stderr)
        return {}

    out = {"address": None, "schedule_url": None, "event_name": None}

    # Event name: the main <h1> heading on the details page (full event title).
    h1 = soup.find("h1")
    if h1:
        out["event_name"] = h1.get_text(" ", strip=True)
    if not out["event_name"] and soup.title:
        out["event_name"] = re.sub(r"\s*[-\u2013]\s*Show Manager.*$", "",
                                   soup.title.get_text(strip=True))

    # Schedule PDF link: an <a> whose href contains "_Schedule_" (not catalogue,
    # breed numbers, ring plan).
    for a in soup.find_all("a", href=True):
        href = a["href"]
        low = href.lower()
        if "schedule" in low and ".pdf" in low and "marked" not in low:
            out["schedule_url"] = href
            break

    # Address: the "Location Details" block renders as label/value pairs. Parse
    # them by walking the text for the known labels and taking the following
    # value. We reconstruct a single-line address from Location/Address/Suburb/
    # Post Code/State.
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    vals = {}
    for i, ln in enumerate(lines):
        if ln in _DETAIL_LABELS and i + 1 < len(lines):
            nxt = lines[i + 1]
            # value shouldn't itself be another label
            if nxt not in _DETAIL_LABELS:
                vals[ln] = nxt
    parts = [vals.get("Location"), vals.get("Address"), vals.get("Suburb"),
             vals.get("Post Code"), vals.get("State")]
    parts = [p for p in parts if p]
    if parts:
        # de-dup consecutive identical parts (Location == Address sometimes)
        dedup = []
        for p in parts:
            if not dedup or dedup[-1].lower() != p.lower():
                dedup.append(p)
        out["address"] = ", ".join(dedup)
    return out


def scrape_show_manager(year, months=range(1, 13), fetch_details=None):
    """Return a list of Show Manager tracking/scent-work listings for `year`.

    fetch_details: if True, also fetch each event's Details page to add a full
    venue `address` and `schedule_url`. This is ONE request per event (~1000+
    per run), so it defaults to OFF. When None, it reads the SM_FETCH_DETAILS
    environment variable ("1" = on) so it can be toggled from the workflow.
    """
    if not HAVE_BS4:
        print("[sm] bs4 not installed; skipping Show Manager", file=sys.stderr)
        return []

    if fetch_details is None:
        import os
        fetch_details = os.environ.get("SM_FETCH_DETAILS") == "1"

    listings = []
    seen_ids = set()

    for month in months:
        url = _sm_url(year, month)
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
        except Exception as e:
            print(f"[sm] fetch failed y{year} m{month}: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        current_date = None
        month_count = 0

        # Walk table rows in document order so date headers apply to the rows
        # that follow them.
        for tr in soup.select("tr"):
            row_text = tr.get_text(" ", strip=True)

            # Is this a date-group header row? (spans the row, no event cells)
            hdr = _parse_header_date(row_text, year)
            cells = tr.find_all(["td", "th"])
            # A header row is typically a single wide cell whose text is just a
            # date; detect by: parses as header AND has few data cells.
            if hdr and (len(cells) <= 2 or "back to top" in row_text.lower()):
                current_date = hdr
                continue

            if len(cells) < 4:
                continue

            state = _normalise_state(cells[0].get_text(" ", strip=True))
            # Event Type cell carries the discipline, sometimes decorated
            # ("Agility Trial", "Obedience Trial x2"). Match robustly.
            cell_texts = [c.get_text(" ", strip=True) for c in cells]
            discipline = _match_discipline(cell_texts)
            if not discipline:
                continue
            if state not in SM_REGIONS:
                continue

            # club/event name: first cell containing a details link
            name = ""
            detail_url = ""
            event_id = None
            for c in cells:
                a = c.find("a", href=True)
                if a and ("/events/PublicEvents/Details/" in a["href"]
                          or "/AttendEvent/" in a["href"]):
                    name = a.get_text(" ", strip=True)
                    detail_url = a["href"]
                    idm = DETAIL_ID_RE.search(a["href"])
                    if idm:
                        event_id = idm.group(1)
                    break
            if not name:
                # fall back to the 2nd cell text
                name = cell_texts[1] if len(cell_texts) > 1 else ""

            # closing date: the cell that looks like a weekday+date
            closes = None
            for ct in cell_texts:
                if re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\b", ct):
                    closes = _parse_closing(ct, year)
                    break

            status = _status_from_text(row_text)

            date_iso = current_date.isoformat() if current_date else None
            if not date_iso:
                continue

            # de-dup by event id (an event can be listed under multiple days)
            dedup_key = event_id or (name.lower(), date_iso, discipline)
            if dedup_key in seen_ids:
                continue
            seen_ids.add(dedup_key)

            listings.append({
                "club": name,
                "date": date_iso,
                "discipline": discipline,
                "region": state,
                "status": status,
                "closes": closes,
                "detail_url": ("https://www.showmanager.com.au" + detail_url
                               if detail_url.startswith("/") else detail_url),
                "event_id": event_id,
                "address": None,
                "schedule_url": None,
            })
            month_count += 1

        print(f"[sm] {year}-{month:02d}: {month_count} sport listings",
              file=sys.stderr)

    # Optional: enrich each listing with address + schedule link from its
    # Details page (one request per event). Off by default. To limit cost and
    # be a good citizen, we only fetch details for events that haven't happened
    # yet (past events don't need an address) and aren't cancelled.
    #
    # LOAD SPREADING: fetching every upcoming event's Details page daily would
    # be ~1000+ requests per run — a traffic surge on Show Manager and a
    # rate-limit/blocking risk to our biggest source. Instead we fetch only
    # 1/SM_DETAIL_CYCLE_DAYS of the targets each day, cycling which slice runs
    # by day-of-year, so every event is refreshed within a full cycle (default
    # a week) and any single run makes a fraction of the requests. Addresses
    # persist once fetched (stored on the event), so after the first cycle
    # coverage is complete and daily runs just refresh one slice. A small delay
    # between requests further avoids any burst.
    if fetch_details:
        import os
        import time
        today = dt.date.today()
        def _upcoming(x):
            try:
                return dt.date.fromisoformat(x["date"]) >= today
            except (ValueError, TypeError):
                return True
        targets = [x for x in listings
                   if x.get("event_id") and x["status"] != "cancelled"
                   and _upcoming(x)]

        # How many days to spread a full pass over (default 7 = a week).
        try:
            cycle_days = int(os.environ.get("SM_DETAIL_CYCLE_DAYS", "7"))
        except ValueError:
            cycle_days = 7
        cycle_days = max(1, cycle_days)
        # Which slice runs today: cycle through 0..cycle_days-1 by day-of-year,
        # so a different (deterministic) slice runs each day and all are covered
        # within any `cycle_days` window. Assign each target to a slice by a
        # STABLE hash of its event_id (hashlib, not built-in hash() which is
        # randomized per process and would reshuffle slices every run).
        import hashlib
        def _slice_of(event_id):
            h = hashlib.md5(str(event_id).encode("utf-8")).hexdigest()
            return int(h, 16) % cycle_days
        today_slice = today.timetuple().tm_yday % cycle_days
        todays_targets = [x for x in targets
                          if _slice_of(x["event_id"]) == today_slice]
        # Delay between requests (seconds) to smooth out the load. Small but
        # enough to avoid a tight burst; ~0.7s over e.g. 140 events ≈ 100s.
        try:
            req_delay = float(os.environ.get("SM_DETAIL_DELAY_SEC", "0.7"))
        except ValueError:
            req_delay = 0.7
        req_delay = max(0.0, req_delay)

        print(f"[sm-detail] slice {today_slice+1}/{cycle_days} today: "
              f"{len(todays_targets)} of {len(targets)} upcoming events "
              f"(~{req_delay:.1f}s apart)...", file=sys.stderr)
        n_addr = 0
        for i, x in enumerate(todays_targets, 1):
            info = fetch_event_detail(x["event_id"])
            if info.get("address"):
                x["address"] = info["address"]
                n_addr += 1
            if info.get("schedule_url"):
                x["schedule_url"] = info["schedule_url"]
            if req_delay and i < len(todays_targets):
                time.sleep(req_delay)
            if i % 100 == 0:
                print(f"[sm-detail]   {i}/{len(todays_targets)} done",
                      file=sys.stderr)
        print(f"[sm-detail] got address for {n_addr}/{len(todays_targets)} "
              f"events this slice", file=sys.stderr)

    from collections import Counter
    by_disc = Counter(x["discipline"] for x in listings)
    by_region = Counter(x["region"] for x in listings)
    op = sum(1 for x in listings if x["status"] == "open")
    print(f"[sm] TOTAL {len(listings)} listings across all disciplines "
          f"({op} currently open)", file=sys.stderr)
    print(f"[sm]   by region: {dict(by_region)}", file=sys.stderr)
    print(f"[sm]   by discipline: {dict(by_disc)}", file=sys.stderr)
    return listings


if __name__ == "__main__":
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    # default: test just a couple of months to keep the manual run quick
    if len(sys.argv) > 2:
        months = [int(sys.argv[2])]
    else:
        months = [7, 8]
    rows = scrape_show_manager(yr, months=months)
    for x in rows:
        print(f"{x['date']}  {x['region']:3}  {x['discipline']:26} "
              f"{x['status']:9} closes={x['closes'] or '-':10}  {x['club']}")
    print(f"\nTOTAL: {len(rows)}")

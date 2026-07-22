#!/usr/bin/env python3
"""
Dogs SA (South Australian Canine Association) governing-body calendar parser.

WHY THIS EXISTS: SA is the one mainland state with no governing-body source in
the pipeline, so SA events were only ever seen via entry platforms (Top Dog /
Show Manager) and therefore looked "unverified / single source". Dogs SA does
publish an authoritative Event Calendar, so this closes that gap.

SOURCE: https://dogssa.com.au/events/upcoming-events/ — a server-rendered HTML
table (no JS needed). Structure (verified against the live page 22 Jul 2026):

    | July            |   |                                               |   <- month header row
    | 25th (Saturday) |   | Club – Discipline – time – venue – Entries... |   <- dated row
    |                 |   | Another club – Discipline – ... – Via TopDog  |   <- continuation (same date)
    | August          |   |                                               |
    ...

Each event cell is ` – ` (en-dash) delimited and reliably starts with the CLUB,
then the DISCIPLINE ("... – Tracking Trial – ...", "... – Scent Work Trial – ...").
It usually also carries a venue and "Entries Close DD.MM.YY Via <platform>".

We keep only SPORT disciplines (Tracking, Track & Search, Scent Work, Obedience,
Rally, Agility, etc.) and drop conformation-only shows and non-trial social
items ("Funday", "catch-up"). Region is always "SA".

CAVEAT: the page is an UPCOMING window, not the whole year, so it naturally
won't include past SA events — acceptable for a "can I still enter?" calendar.
Fail-safe: any error returns [] so it can never break the run.

OUTPUT: list of event dicts compatible with the main scraper:
    {title, start, end, location, url, category, region, source, color,
     cancelled, closes?}
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

SA_EVENTS_URL = "https://dogssa.com.au/events/upcoming-events/"
SA_SOURCE_NAME = "Dogs SA"
SA_COLOR = "#c9a227"   # SA's region colour (matches REGION_COLOR in scrape.py)
HEADERS = {"User-Agent": "TrackingCalendarBot/1.0 (+combined tracking events)"}
TIMEOUT = 30

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

# Discipline classification from the event-cell text. Ordered: more specific
# first (Track & Search before Tracking; Rally before Obedience is irrelevant
# since we emit per matched discipline, but keep Scent/Track precedence).
_SA_DISCIPLINE_RULES = [
    (re.compile(r"track\s*(?:&|and)\s*search|t\s*&\s*s\b", re.I), "Track & Search"),
    (re.compile(r"\btracking\b", re.I), "Tracking"),
    (re.compile(r"scent\s*work|scentwork", re.I), "Scent Work"),
    (re.compile(r"\brally\b", re.I), "Rally Obedience"),
    (re.compile(r"\bobedience\b", re.I), "Obedience"),
    (re.compile(r"agility|jumping|\bgames\b", re.I), "Agility"),
    (re.compile(r"trick", re.I), "Trick Dog"),
    (re.compile(r"dances\s*with\s*dogs|\bdwd\b|freestyle|htm", re.I), "Dances with Dogs"),
    (re.compile(r"herding", re.I), "Herding"),
    (re.compile(r"sprint", re.I), "Sprint"),
    (re.compile(r"endurance", re.I), "Endurance"),
    (re.compile(r"lure\s*coursing", re.I), "Lure Coursing"),
    (re.compile(r"earthdog", re.I), "Earthdog"),
    (re.compile(r"back\s*pack", re.I), "Backpacking"),
    (re.compile(r"retriev|\brat[g]?\b|field\s*trial", re.I), "Retrieving"),
    (re.compile(r"weight\s*pull", re.I), "Weight Pull"),
    (re.compile(r"hoopers", re.I), "Agility"),
    # Conformation shows are ANKC events and belong in the calendar too (the
    # other sources classify them as "Conformation"). Tested LAST so a show
    # that also names a sport discipline is caught by the sport rules first.
    (re.compile(r"championship\s*show|open\s*show|parade|specialty|exhibition|"
                r"\ba2o\b|all\s*breeds|\bshow\b", re.I), "Conformation"),
]

# Entry platform mentioned in the cell -> canonical platform hint (for logging
# only; the entry link/status is resolved downstream via the platform sources).
_PLATFORM_RE = re.compile(r"via\s+(show\s*manager|top\s*dog[^.\n]*)", re.I)
# Entries-close date, e.g. "Entries Close 07.07.26" / "Entries close 07.07.2026".
_CLOSE_RE = re.compile(r"entries?\s*close[sd]?\s*[:\-]?\s*(?:paper\s*)?(\d{1,2})[./](\d{1,2})[./](\d{2,4})", re.I)
# A dated row's first cell: "25th (Saturday)".
_DATE_CELL_RE = re.compile(r"^\s*(\d{1,2})(?:st|nd|rd|th)?\s*\(", re.I)


def _classify(text):
    for rx, canon in _SA_DISCIPLINE_RULES:
        if rx.search(text):
            return canon
    return None


def _parse_close(text, default_year):
    m = _CLOSE_RE.search(text)
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    try:
        return dt.date(y, mo, d).isoformat()
    except ValueError:
        return None


def _clean_cell(text):
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _split_club_discipline(cell):
    """From a cell like 'Tracking Dog Club of SA Inc – Tracking Trial – 8.00am
    – Monarto – Entries Close 07.07.26 Via Show Manager', return (club, venue).
    Club = text before the first discipline/time segment; venue = a later
    segment that isn't a time or the entries-close tail (best-effort)."""
    # Normalise dash variants (en/em dash AND hyphen used as a separator) to a
    # single delimiter. Only treat " - " (spaced hyphen) as a separator so we
    # don't split hyphenated place names.
    norm = re.sub(r"\s*[–—]\s*", " – ", cell)
    norm = re.sub(r"\s+-\s+", " – ", norm)
    parts = [p.strip() for p in norm.split(" – ") if p.strip()]
    if not parts:
        return "", ""
    # Club = first segment, BUT if the first segment already has a discipline
    # word merged onto its END (e.g. hyphen-joined "Gawler Dog Training Club
    # Inc Games Trial"), trim that trailing discipline tail. Only trim when the
    # discipline word is NOT at the very start — many real clubs are named after
    # their sport ("Tracking Dog Club of SA", "Herding Dog Club of SA",
    # "Agility Dog Club of SA"); trimming those would wipe the club name.
    club = parts[0]
    for rx, _ in _SA_DISCIPLINE_RULES:
        m = rx.search(club)
        if m and m.start() > 0:
            # keep everything up to the discipline word only if what remains
            # still looks like a club name (has a club-ish word or 2+ words).
            head = club[:m.start()].strip(" -–—")
            if head and (len(head.split()) >= 2 or re.search(
                    r"club|kennel|society|association|committee", head, re.I)):
                club = head
            break
    venue = ""
    for p in parts[1:]:
        if re.search(r"\d{1,2}[:.]\d{2}\s*[ap]m|\bam\b|\bpm\b|not\s*bef", p, re.I):
            continue  # a time segment
        if re.search(r"entries?\s*close", p, re.I):
            continue  # the closing tail
        if _classify(p) and not venue:
            continue  # the discipline segment
        venue = p
        break
    return club, venue


def parse_sa_calendar(year):
    """Return Dogs SA sport-trial events for `year` from the upcoming-events
    HTML table. Region always 'SA'. Fail-safe: [] on any problem."""
    if not HAVE_BS4:
        print("[sa] bs4 not installed; skipping Dogs SA", file=sys.stderr)
        return []
    try:
        r = requests.get(SA_EVENTS_URL, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
    except Exception as e:
        print(f"[sa] fetch failed: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    events = []
    seen = set()
    cur_month = None
    cur_day = None

    # The calendar is a table; walk its rows in order. We find the table that
    # contains month names to avoid nav/layout tables.
    tables = soup.find_all("table")
    cal = None
    for t in tables:
        txt = t.get_text(" ", strip=True).lower()
        if "entries close" in txt or ("july" in txt and "august" in txt):
            cal = t
            break
    if cal is None:
        print("[sa] no calendar table found", file=sys.stderr)
        return []

    for tr in cal.find_all("tr"):
        cells = [_clean_cell(td.get_text(" ", strip=True))
                 for td in tr.find_all(["td", "th"])]
        if not cells:
            continue
        first = cells[0]
        # Month header row: first cell is just a month name.
        if first.lower() in _MONTHS:
            cur_month = _MONTHS[first.lower()]
            cur_day = None
            continue
        # Dated row: first cell like "25th (Saturday)".
        dm = _DATE_CELL_RE.match(first)
        if dm:
            cur_day = int(dm.group(1))
        # The event description is the last non-empty cell.
        desc = ""
        for c in reversed(cells):
            if c and not _DATE_CELL_RE.match(c) and c.lower() not in _MONTHS:
                desc = c
                break
        if not desc or cur_month is None or cur_day is None:
            continue

        discipline = _classify(desc)
        if not discipline:
            continue  # conformation-only show or non-trial social item

        try:
            start = dt.date(year, cur_month, cur_day).isoformat()
        except ValueError:
            continue

        club, venue = _split_club_discipline(desc)
        if not club:
            continue
        closes = _parse_close(desc, year)
        cancelled = bool(re.search(r"cancel", desc, re.I))

        key = (club.lower(), start, discipline)
        if key in seen:
            continue
        seen.add(key)

        events.append({
            "title": f"{club} \u2013 {discipline}",
            "club": club,
            "start": start,
            "end": start,
            "location": venue or "South Australia",
            "url": SA_EVENTS_URL,
            "category": discipline,
            "region": "SA",
            "source": SA_SOURCE_NAME,
            "sources": [SA_SOURCE_NAME],
            "color": SA_COLOR,
            "cancelled": cancelled,
        })
        if closes:
            events[-1]["closes"] = closes

    from collections import Counter
    _bd = Counter(e["category"] for e in events)
    print(f"[sa] parsed {len(events)} events across disciplines {dict(_bd)}",
          file=sys.stderr)
    return events


if __name__ == "__main__":
    import datetime
    y = datetime.date.today().year
    evs = parse_sa_calendar(y)
    print(f"got {len(evs)} SA events for {y}")
    for e in evs[:12]:
        print(f"  {e['start']} [{e['category']}] {e['club'][:40]!r} @ {e['location'][:30]!r} closes={e.get('closes')}")

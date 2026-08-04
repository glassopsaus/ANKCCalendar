#!/usr/bin/env python3
"""
OZentries entry-platform parser.

WHY THIS EXISTS: OZentries (dogs.ozentries.com.au) is one of the entry platforms
named in the Dogs NSW show & trial calendar, alongside Show Manager, Top Dog and
Ready Entries. We already scrape those three, but OZentries was uncovered — ~32
NSW events use it, and without it those events have no live entry status and no
per-event link. This closes that gap.

SOURCE: https://dogs.ozentries.com.au/shows.php — a PLAIN server-rendered HTML
table (no JavaScript, no login required to VIEW the list; login is only needed
to actually submit an entry). This makes it far simpler than Ready Entries or
Top Dog, which are JS SPAs needing a headless browser. Structure (verified
against the live page):

    ## Enter Online - Available Shows
    ### Approved Events                          <- these are OPEN for entry
        | NSW Shows      | | | |                 <- state header row
        | 15-Aug-2026 | <a>Werriwa All Breeds Obedience Trial ...</a> | 28-Jul-2026 | |
        | 16-Aug-2026 | <a>Border Terrier Club NSW Earthdog Tests ...</a> | 10-Aug-2026 | |
        ...
    ### Closed Shows                             <- these are CLOSED
        | 2-Aug-2026  | <a>Campbelltown & District Champ Show ...</a> | 30-Jul-2026 | |

Each event row:
  - col 0: event date (e.g. "15-Aug-2026")
  - col 1: event name, linked to its schedule PDF (/schedule/XXXX.pdf)
  - col 2: published entries-close date
The "Approved Events" vs "Closed Shows" section gives entry STATUS directly.
State ("NSW Shows", "VIC Shows", ...) comes from grouping header rows.

The event name carries the discipline ("... Obedience Trial", "... Champ Show",
"... Earthdog Tests"), classified with the same explicit-trial-phrase approach
used elsewhere so a club-name word can't drive the discipline.

ENTRY LINK: the public per-event link is the schedule PDF (the actual "Enter"
action is login-walled), so entry_url is the schedule PDF.

Fail-safe: any error returns [] so it can never break the run.

OUTPUT: list of event dicts compatible with the main scraper:
    {title, start, end, location, url, entry_url, category, region, source,
     color, cancelled, closes, status}
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

OZ_SHOWS_URL = "https://dogs.ozentries.com.au/shows.php"
OZ_SOURCE_NAME = "Ozentries"
HEADERS = {"User-Agent": "TrackingCalendarBot/1.0 (+combined tracking events)"}
TIMEOUT = 30

# State header -> region code. Headers on the page read "NSW Shows", "VIC Shows",
# etc. We map the leading state token.
_STATES = {"NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"}

# Discipline classification from the event NAME. Ordered longest/most-specific
# first. Conformation shows are the bulk of OZentries; trials appear too.
_OZ_DISCIPLINE_RULES = [
    (re.compile(r"track\s*&?\s*search|t\s*&\s*search", re.I), "Track & Search"),
    (re.compile(r"\btracking\b", re.I), "Tracking"),
    (re.compile(r"scent\s*work|scentwork", re.I), "Scent Work"),
    (re.compile(r"rally", re.I), "Rally Obedience"),
    (re.compile(r"obedience", re.I), "Obedience"),
    (re.compile(r"trick", re.I), "Trick Dog"),
    (re.compile(r"agility|jumping|\bgames\b", re.I), "Agility"),
    (re.compile(r"dances\s*with\s*dogs|\bdwd\b", re.I), "Dances with Dogs"),
    (re.compile(r"herding|\bherd\b", re.I), "Herding"),
    (re.compile(r"endurance", re.I), "Endurance"),
    (re.compile(r"lure\s*coursing|\blure\b", re.I), "Lure Coursing"),
    (re.compile(r"\bfield\s*trial\b|retriev", re.I), "Retrieving"),
    (re.compile(r"sprint", re.I), "Sprint"),
    (re.compile(r"earth\s*dog", re.I), "Earthdog"),
    (re.compile(r"weight\s*pull", re.I), "Weight Pull"),
    (re.compile(r"\bsled\b", re.I), "Sled Sports"),
    # Conformation shows: "Champ Show", "Open Show", "Parade", "Speciality",
    # "Neuter Show", plain "Show".
    (re.compile(r"champ(?:ionship)?\s*show|open\s*show|parade|"
                r"speciality|specialty|neuter\s*show|\bshow\b", re.I),
     "Conformation"),
]

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul",
     "Aug", "Sep", "Oct", "Nov", "Dec"], start=1)}

# Date like "15-Aug-2026".
_OZ_DATE_RE = re.compile(r"^\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*$")


def _parse_oz_date(text):
    m = _OZ_DATE_RE.match(text or "")
    if not m:
        return None
    day = int(m.group(1))
    mon = _MONTHS.get(m.group(2).title())
    yr = int(m.group(3))
    if not mon:
        return None
    try:
        return dt.date(yr, mon, day)
    except ValueError:
        return None


def _oz_discipline(name):
    """Classify the event name into a canonical discipline. Prefer an explicit
    trial/test/show phrase so a club-name word can't drive the result."""
    if not name:
        return None
    # Prefer the discipline named right before Trial/Test/Show/Tests.
    m = re.search(r"((?:[A-Za-z&/'-]+\s+){0,4}[A-Za-z&/'-]+)\s+"
                  r"(?:trial|test|show|tests|trials)\b", name, re.I)
    if m:
        phrase = m.group(1) + " " + name[m.end(1):]  # include the trailing kind
        for rx, canon in _OZ_DISCIPLINE_RULES:
            if rx.search(phrase):
                return canon
    for rx, canon in _OZ_DISCIPLINE_RULES:
        if rx.search(name):
            return canon
    return None


def _clean_title(name):
    """Trim the trailing weekday+date the event name usually carries, e.g.
    'Werriwa All Breeds Obedience Trial Sat 15 Aug 2026' -> drop the date tail."""
    if not name:
        return name
    name = re.sub(
        r"\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+"
        r"[A-Za-z]{3,}\s+\d{4}\s*$", "", name).strip()
    return name


def parse_ozentries(year, html=None):
    """Return a list of OZentries event dicts for `year`.

    Pass `html` for offline testing; otherwise fetches OZ_SHOWS_URL.
    """
    if not HAVE_BS4:
        print("[ozentries] bs4 not installed; skipping", file=sys.stderr)
        return []

    if html is None:
        try:
            r = requests.get(OZ_SHOWS_URL, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            print(f"[ozentries] fetch failed: {e}", file=sys.stderr)
            return []

    events = []
    seen = set()
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Walk the document in order, tracking the current SECTION (Approved =
        # open, Closed = closed) and current STATE (from "NSW Shows" headers).
        # The page is a table; iterate its rows in document order.
        status = None       # "open" | "closed"
        region = None

        # Find every table row; also honour section headings that appear as
        # <h3>/<h4>/text ("Approved Events" / "Closed Shows") interleaved.
        # We linearise: walk all descendants, updating status on heading text
        # and parsing <tr> rows.
        for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "td", "tr", "p"]):
            txt = el.get_text(" ", strip=True)
            if not txt:
                continue
            low = txt.lower()
            if el.name in ("h1", "h2", "h3", "h4", "h5", "p"):
                if "approved event" in low:
                    status = "open"
                elif "closed show" in low:
                    status = "closed"
                continue

            if el.name != "tr":
                continue
            cells = el.find_all(["td", "th"])
            if not cells:
                continue
            texts = [c.get_text(" ", strip=True) for c in cells]

            # A state header row: first cell like "NSW Shows", rest empty.
            first = texts[0] if texts else ""
            state_tok = first.split()[0].upper() if first else ""
            if state_tok in _STATES and (
                    "show" in first.lower() or len(first.split()) <= 2):
                if not any(texts[1:]):  # header rows have empty remaining cells
                    region = state_tok
                    continue

            # A section marker can also live inside a row cell.
            if "approved event" in first.lower():
                status = "open"
                continue
            if "closed show" in first.lower():
                status = "closed"
                continue

            # Otherwise: an event row needs a parseable date in col 0.
            edate = _parse_oz_date(first)
            if not edate:
                continue
            if edate.year != year:
                continue

            # col 1: the event name + schedule link.
            name_cell = cells[1] if len(cells) > 1 else None
            if name_cell is None:
                continue
            raw_name = name_cell.get_text(" ", strip=True)
            if not raw_name:
                continue
            a = name_cell.find("a", href=True)
            schedule_url = None
            if a and a["href"]:
                href = a["href"]
                if href.startswith("/"):
                    href = "https://dogs.ozentries.com.au" + href
                elif not href.startswith("http"):
                    href = "https://dogs.ozentries.com.au/" + href
                schedule_url = href

            # col 2: published entries-close date (optional).
            closes = None
            if len(texts) > 2:
                cd = _parse_oz_date(texts[2])
                if cd:
                    closes = cd.isoformat()

            title = _clean_title(raw_name)
            category = _oz_discipline(title) or _oz_discipline(raw_name)
            if not category:
                # Unknown/again-non-trial: skip rather than mis-file.
                continue

            key = (title.lower(), edate.isoformat(), category)
            if key in seen:
                continue
            seen.add(key)

            events.append({
                "title": title,
                "start": edate.isoformat(),
                "end": edate.isoformat(),
                "location": "",
                "url": OZ_SHOWS_URL,
                "entry_url": schedule_url or OZ_SHOWS_URL,
                "category": category,
                "region": region,          # may be None if no header seen yet
                "source": OZ_SOURCE_NAME,
                "color": None,             # set by the main scraper
                "cancelled": False,
                "closes": closes,
                "status": status or "open",
            })
    except Exception as e:
        print(f"[ozentries] parse error: {e}", file=sys.stderr)
        return events

    from collections import Counter
    by_disc = Counter(e["category"] for e in events)
    by_region = Counter(e["region"] for e in events)
    print(f"[ozentries] parsed {len(events)} events", file=sys.stderr)
    print(f"[ozentries]   by discipline: {dict(by_disc)}", file=sys.stderr)
    print(f"[ozentries]   by region: {dict(by_region)}", file=sys.stderr)
    return events


if __name__ == "__main__":
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    evs = parse_ozentries(yr)
    for e in evs[:40]:
        print(f"{e['start']}  {e['region'] or '-':4} {e['category']:14} "
              f"{e['status']:7} closes={e['closes'] or '-':11} {e['title']}")
    print(f"\nTOTAL: {len(evs)}")

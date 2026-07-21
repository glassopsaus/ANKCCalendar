#!/usr/bin/env python3
"""
Dogs Victoria official events-calendar PDF parser  (VIC cross-check / gap-fill).

WHY THIS EXISTS: Dogs Victoria publishes an official master events calendar as a
PDF on dogsvictoria.org.au, e.g.
  https://dogsvictoria.org.au/media/7101/2026-dogs-victoria-events-calendar-v44.pdf
This is the governing body's authoritative list. The main VIC source is the
vicdog.com website (parse_vicdog_calendar); Top Dog Events supplies entry-based
listings. This PDF is used as a THIRD, cross-check source: it confirms events we
already have and catches any Tracking / Track & Search trial that never made it
onto the website or an entry system.

IMPORTANT: this is a venue-BOOKING calendar, not a trials calendar. It lists
everything at Dogs Victoria grounds (champ shows, training, meetings, agility,
obedience, etc). We keep ONLY lines whose trailing discipline tag is Tracking or
Track & Search, and discard everything else. We skip lines marked "cancelled".

STRUCTURE (verified against the real 2026 PDF text):
  Date header lines:   "Saturday, 23 May 2026"
  Event lines:         "<VENUE> <Club / event text> <DISCIPLINE TAG>"
      VENUE is one of: KCC Park, Bulla, OTHER  (case varies)
      DISCIPLINE TAG (trailing) is e.g. TRACKING, T&SEARCH, TRACK & SEARCH,
      STATE TRACKING, SCENT, RETRIEVING, AGILITY, ...
  Multi-day trials appear as one line per day under consecutive date headers;
  we collapse consecutive same-club/same-discipline days into one event.

OUTPUT: list of event dicts compatible with the main scraper:
    {title, start, end, location, url, category, region, source, color,
     cancelled}
  category is "Tracking" or "Track & Search"; region is always "VIC".
  Entry status is applied later by the Show Manager cross-check, not here.

The URL can change when DV republishes an amended version (this is "v44"), so we
discover the current PDF link from the events page rather than hardcoding it,
falling back to a known URL if discovery fails.
"""

import io
import re
import sys
import datetime as dt

import requests

try:
    import pdfplumber
    HAVE_PDFPLUMBER = True
except Exception:
    HAVE_PDFPLUMBER = False

# The dogs-victoria-events-calendar page 500-errors; the Shows & Trials Calendar
# page loads reliably and links the current PDFs for BOTH the current and next
# year (with amendment dates), so it's the source for version discovery.
DV_CALENDAR_PAGE = "https://dogsvictoria.org.au/events/shows-and-trials-calendar/"
DV_FALLBACK_PDF = ("https://dogsvictoria.org.au/media/7101/"
                   "2026-dogs-victoria-events-calendar-v44.pdf")
DV_SOURCE_NAME = "Dogs Victoria (official calendar)"
DV_COLOR = "#3aa657"  # same green as the vicdog VIC source

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], start=1)}

# Date header, e.g. "Saturday, 23 May 2026" (may have trailing text after year).
_DATE_RE = re.compile(
    r"^\s*(?:mon|tues|wednes|thurs|fri|satur|sun)day,\s+"
    r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.I)

# Track & Search must be checked BEFORE plain tracking.
_TS_RE = re.compile(r"(track\s*&\s*search|t\s*&\s*search|t&search)", re.I)
_TRACK_RE = re.compile(r"TRACKING\b")            # uppercase discipline tag
_TRACK_TRAIL_RE = re.compile(r"tracking\s*$", re.I)  # or trailing word

# Full FIXTURE-tag -> canonical discipline map for the DV calendar. Order
# matters: specific before general (Track & Search before Tracking; Rally
# before Obedience). POSITIVE allowlist of genuine COMPETITION fixtures — the
# DV calendar is full of non-event rows (TRAINING/MEETING/EVENT/BREED SURVEY/
# BUMP IN/NO BOOKINGS/Private Event/exams) which must NOT become events.
_DV_DISCIPLINE_RULES = [
    (re.compile(r"track\s*&?\s*search|t\s*&\s*search|t&search|\bt\s*&\s*s\b", re.I), "Track & Search"),
    (re.compile(r"\btracking\b", re.I), "Tracking"),
    (re.compile(r"scent", re.I), "Scent Work"),
    (re.compile(r"rally|o&r|o\s*&\s*r", re.I), "Rally Obedience"),
    (re.compile(r"obedience", re.I), "Obedience"),
    (re.compile(r"trick", re.I), "Trick Dog"),
    (re.compile(r"agility|jumping|games", re.I), "Agility"),
    (re.compile(r"\bdwd\b|dances with dogs", re.I), "Dances with Dogs"),
    (re.compile(r"herd", re.I), "Herding"),
    (re.compile(r"endurance", re.I), "Endurance"),
    (re.compile(r"lure", re.I), "Lure Coursing"),
    (re.compile(r"point\s*/?\s*set|pointing|field\s+trial", re.I), "Field Trial"),
    (re.compile(r"retriev|\bratg\b|\brtb\b", re.I), "Retrieving"),
    (re.compile(r"sprint", re.I), "Sprint"),
    (re.compile(r"earth\s*dog", re.I), "Earthdog"),
    (re.compile(r"weight\s*pull", re.I), "Weight Pull"),
    (re.compile(r"\bsled\b", re.I), "Sled Sports"),
    (re.compile(r"back\s*pack|hiking", re.I), "Backpacking"),
    (re.compile(r"bale\s*(seek|hunt)|\bbsic\b|super\s*seven\s*snuffle", re.I), "Bale Seek"),
    (re.compile(r"canine\s*disc|disc\s*dog|toss\s*(&|and)\s*fetch|\bfrisbee\b", re.I), "Canine Disc"),
    (re.compile(r"champ|parade|\bopen\s+show\b", re.I), "Conformation"),
]
# Non-competition fixtures -> always skip (unless a real competition tag is
# also present on the line).
_DV_SKIP_RE = re.compile(
    r"training|meeting|\bbump\s*in\b|no\s+(further\s+)?booking|private\s+event|"
    r"breed\s+(survey|eval)|\bexam|state\s+breed\s+evaluation|"
    r"cancelled|on\s+hold", re.I)

_VENUE_PREFIX_RE = re.compile(r"^\s*(KCC Park|BULLA|Bulla|OTHER|Other)\s+", re.I)
# Leading amendment-date fragment the DV calendar sometimes prepends to a row
# when an entry was edited, e.g. "27.07.26 OTHER Tracking Club of Vic Inc".
# This is an editing artefact, never part of the club/event name, so strip it.
_LEADING_DATE_RE = re.compile(r"^\s*\d{1,2}\.\d{1,2}\.\d{2,4}\s+")
# Strip only the trailing discipline tag (and an "OPEN" trial-type prefix on it).
# NOTE: "RTG N" (Restricted To Group N — an eligibility qualifier, e.g. RTG3 =
# restricted to Group 3 dogs) is meaningful and is deliberately KEPT in the
# title, not stripped.
_TAG_STRIP_RE = re.compile(
    r"\s*(OPEN\s+|STATE\s+|NATIONAL\s+)?"
    r"(TRACK\s*&\s*SEARCH|T&SEARCH|T\s*&\s*SEARCH|T\s*&\s*S|STATE\s+TRACKING|"
    r"TRACKING|SCENT(\s*WORK)?|OPEN\s+RALLY|RALLY(\s+O&R)?|O&R|OBEDIENCE|"
    r"SPRINT\s*DOG|SPRINTDOG|DWD|DANCES\s+WITH\s+DOGS|AGILITY|JUMPING|"
    r"GAMES(\s+TRIAL)?|TRICK(\s+DOG)?|HERD(\s+TEST|\s+TRIAL)?|ENDURANCE(\s+TEST)?|"
    r"LURE(\s+COURSING)?|RETRIEVING|RATG|RTB|POINT\s*/?\s*SET(\s+NOVICE)?|"
    r"WEIGHT\s*PULL|SLED|BACK\s*PACK(ING)?|HIKING|EARTH\s*DOG|"
    r"ALL\s+BREEDS?\s+CHAMP(\.|ionship)?|CHAMP(\.|ionship)?|PARADE|OPEN\s+SHOW)"
    r"(\s*X\s*\d+)?\s*$", re.I)


def find_current_dv_pdf_url(year):
    """Discover the current calendar PDF for `year` from the Shows & Trials
    Calendar page (which links both the current 2026 and 2027 PDFs and, unlike
    the dogs-victoria-events-calendar page, actually loads).

    Matches links like `/media/NNNN/{year}-dogs-victoria-events-calendar-vNN.pdf`
    and returns the one with the HIGHEST version number for the target year, so
    we always follow amendments (v44 -> v45 ...) and the correct year without
    hardcoding. Returns (url, discovered_bool). Falls back to the pinned URL if
    discovery fails, flagging discovered=False so staleness is visible upstream.
    """
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(DV_CALENDAR_PAGE, timeout=20,
                            headers={"User-Agent": "TrackingCalendarBot/1.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        pat = re.compile(
            rf"{year}-dogs-victoria-events-calendar-v(\d+)\.pdf", re.I)
        best_url, best_ver = None, -1
        for a in soup.select('a[href]'):
            href = a.get("href", "")
            m = pat.search(href)
            if m:
                ver = int(m.group(1))
                if ver > best_ver:
                    best_ver = ver
                    best_url = href if href.startswith("http") else \
                        "https://dogsvictoria.org.au" + href
        if best_url:
            print(f"[dv] discovered {year} calendar v{best_ver}: {best_url}",
                  file=sys.stderr)
            return best_url, True
        print(f"[dv] no {year} calendar link found on page; using fallback",
              file=sys.stderr)
    except Exception as e:
        print(f"[dv] PDF discovery failed ({e}); using fallback", file=sys.stderr)
    return DV_FALLBACK_PDF, False


def _discipline(line):
    """Return the canonical competition discipline from a DV line's trailing
    FIXTURE tag, or None. The discipline lives in the trailing fixture tag; the
    CLUB NAME (which often contains words like 'Obedience' or 'Agility') must
    NOT drive classification. So we test the trailing portion of the line, and
    only fall back to a whole-line scan if the tail is inconclusive.
    """
    tail = line.strip()
    # The fixture tag is the trailing run of UPPERCASE words (DV prints fixtures
    # in caps, e.g. "... Bendigo Obedience Dog Club Inc TRICK"). Extract the
    # trailing all-caps/symbol tokens as the fixture.
    m = re.search(r"([A-Z][A-Z&/\s\.\dx]*?)\s*$", line)
    fixture = m.group(1).strip() if m else ""
    # If the trailing-caps run is too long, it likely swept up caps club words;
    # keep only the last few tokens.
    ftoks = fixture.split()
    if len(ftoks) > 4:
        fixture = " ".join(ftoks[-4:])

    # Decide on the fixture tag first.
    if fixture:
        if _DV_SKIP_RE.search(fixture) and not any(
                rx.search(fixture) for rx, _ in _DV_DISCIPLINE_RULES):
            return None
        for rx, canon in _DV_DISCIPLINE_RULES:
            if rx.search(fixture):
                return canon
    # Tail inconclusive: if the WHOLE line is clearly non-competition, skip.
    if _DV_SKIP_RE.search(line) and not any(
            rx.search(line) for rx, _ in _DV_DISCIPLINE_RULES):
        return None
    return None


def _clean_title(line):
    body = _LEADING_DATE_RE.sub("", line)        # drop amendment-date artefact
    body = _VENUE_PREFIX_RE.sub("", body).strip()
    title = _TAG_STRIP_RE.sub("", body).strip(" -\u2013\u2014\xa0")
    return title


def _collapse(events):
    """Merge consecutive-day rows for the same club+discipline into one event."""
    events.sort(key=lambda e: (e["title"].lower(), e["category"], e["start"]))
    out = []
    for e in events:
        if out:
            p = out[-1]
            if (p["title"].lower() == e["title"].lower()
                    and p["category"] == e["category"]):
                pe = dt.date.fromisoformat(p["end"])
                es = dt.date.fromisoformat(e["start"])
                if (es - pe).days in (0, 1):
                    p["end"] = max(p["end"], e["end"])
                    continue
        out.append(dict(e))
    out.sort(key=lambda e: (e["start"], e["title"]))
    return out


def parse_dv_text(text, year):
    """Parse already-extracted PDF text into VIC tracking/T&S event dicts."""
    events = []
    cur = None
    for raw in text.splitlines():
        line = raw.rstrip()
        m = _DATE_RE.match(line)
        if m:
            day = int(m.group(1))
            mon = _MONTHS.get(m.group(2).lower())
            yr = int(m.group(3))
            cur = dt.date(yr, mon, day) if (mon and yr == year) else None
            continue
        if not cur or not line.strip():
            continue
        if re.search(r"cancelled", line, re.I):
            continue  # skip cancelled bookings entirely
        disc = _discipline(line)
        if not disc:
            continue
        title = _clean_title(line)
        if not title:
            continue
        events.append({
            "title": title,
            "start": cur.isoformat(),
            "end": cur.isoformat(),
            "location": "Victoria",
            "url": DV_CALENDAR_PAGE,
            "category": disc,
            "region": "VIC",
            "source": DV_SOURCE_NAME,
            "color": DV_COLOR,
            "cancelled": False,
        })
    return _collapse(events)


def parse_dv_calendar(year, pdf_url=None, pdf_bytes=None):
    """Fetch + parse the DV calendar PDF for `year`. Returns [] on any failure
    so a problem here never breaks the rest of the scraper.

    Discovery is version- and year-aware (see find_current_dv_pdf_url). If it
    can't discover the current PDF it falls back to the pinned URL and prints a
    clear STALE warning, so a silent-stale source is visible in the logs.
    """
    if not HAVE_PDFPLUMBER:
        print("[dv] pdfplumber not installed; skipping DV calendar",
              file=sys.stderr)
        return []
    try:
        if pdf_bytes is None:
            if pdf_url:
                url, discovered = pdf_url, True
            else:
                url, discovered = find_current_dv_pdf_url(year)
            if not discovered:
                print(f"[dv] WARNING: using pinned FALLBACK PDF (discovery "
                      f"failed) - may be STALE: {url}", file=sys.stderr)
            else:
                print(f"[dv] using PDF: {url}", file=sys.stderr)
            resp = requests.get(url, timeout=40,
                                headers={"User-Agent": "TrackingCalendarBot/1.0"})
            resp.raise_for_status()
            pdf_bytes = resp.content
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        text = "\n".join(text_parts)
        # Safety: DV date headers carry the full year and parse_dv_text filters
        # to `year`, so wrong-year data can't be mis-stamped — but a wrong-year
        # PDF would silently yield 0 events. Warn if the target year isn't even
        # present in the document.
        if str(year) not in text[:6000] and str(year) not in (pdf_url or ""):
            print(f"[dv] WARNING: {year} not found in PDF header - may be the "
                  f"wrong year's file; VIC events may be missing", file=sys.stderr)
        events = parse_dv_text(text, year)
        if not events:
            print("[dv] WARNING: parsed 0 events - PDF format may have "
                  "changed; parser likely needs updating", file=sys.stderr)
        from collections import Counter
        by_disc = Counter(e["category"] for e in events)
        print(f"[dv] parsed {len(events)} VIC events across all disciplines",
              file=sys.stderr)
        print(f"[dv]   by discipline: {dict(by_disc)}", file=sys.stderr)
        return events
    except Exception as e:
        print(f"[dv] FAILED, skipping: {e}", file=sys.stderr)
        return []


if __name__ == "__main__":
    evs = parse_dv_calendar(2026)
    for e in evs:
        print(f"{e['start']}..{e['end']} {e['category']:15} {e['title']}")
    print(f"total {len(evs)}")

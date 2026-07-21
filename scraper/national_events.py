#!/usr/bin/env python3
"""
Dogs Australia "National Events" parser  (supplementary feed).

https://dogsaustralia.org.au/members/events/national-events/

This page lists national-championship events (the once-a-year national title for
each discipline) plus major breed-club Championship Shows, spanning several years
(2025-2029). It has NO closing dates and NO entry status, so it is a
SUPPLEMENTARY FEED, not a verification source: it adds a handful of prestige
national events that may not appear (framed as "national") elsewhere. Everything
here dedups against the main pipeline like any other event.

Two section shapes:

1. Discipline-headed sections, e.g.:
       1. AGILITY TRIALS
       2026: 24-28 June DOGS QUEENSLAND
       2028: 7-12 June DOGS SA
   -> "<YEAR>: <day-range> <Month> DOGS <STATE>"

2. A CHAMPIONSHIP SHOWS section, grouped by year:
       2026
       7-8 August Dalmatian Association of Queensland Inc DOGS QLD Showgrounds...
   -> "<day-range> <Month> <Club> <Venue..., STATE>"

OUTPUT: calendar-event dicts (same shape the other parsers emit).
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

NE_URL = "https://dogsaustralia.org.au/members/events/national-events/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-AU,en;q=0.9",
}
TIMEOUT = 30

_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}
_MON_RE = ("january|february|march|april|may|june|july|august|september|"
           "october|november|december")

# "DOGS QUEENSLAND" / "DOGS SA" / "DOGS WEST" -> region code.
_DOGS_STATE = {
    "dogs act": "ACT", "dogs nsw": "NSW", "dogs nt": "NT",
    "dogs queensland": "QLD", "dogs qld": "QLD", "dogs sa": "SA",
    "dogs tasmania": "TAS", "dogs victoria": "VIC", "dogs west": "WA",
    "dogs western australia": "WA",
}
# Trailing venue state, e.g. "..., QLD" or "... NSW".
_TRAIL_STATE_RE = re.compile(r"\b(ACT|NSW|NT|QLD|SA|TAS|VIC|WA)\b")

# Discipline section headers -> canonical category.
_SECTION_DISCIPLINE = [
    (re.compile(r"agility", re.I), "Agility"),
    (re.compile(r"dances with dogs", re.I), "Dances with Dogs"),
    (re.compile(r"herding", re.I), "Herding"),
    (re.compile(r"obedience", re.I), "Obedience"),
    (re.compile(r"rally", re.I), "Rally Obedience"),
    (re.compile(r"pointer & setter|field trial", re.I), "Field Trial"),
    (re.compile(r"retrieving", re.I), "Retrieving"),
    (re.compile(r"trick dog", re.I), "Trick Dog"),
    (re.compile(r"utility gundog", re.I), "Field Trial"),
    (re.compile(r"junior handler", re.I), "Junior Handler"),
]

# Line beginning "<year>: <day>[-<day>] <Month> DOGS <STATE>"
_DISC_LINE_RE = re.compile(
    r"^(\d{4}):\s*(\d{1,2})(?:\s*-\s*\d{1,2})?\s+(" + _MON_RE + r")\s+(DOGS[A-Za-z ]+)",
    re.I)
# Championship-show line "<day>[-<day>] <Month> <rest incl club + venue,state>"
_CH_LINE_RE = re.compile(
    r"^(\d{1,2})(?:\s*-\s*\d{1,2})?\s+(" + _MON_RE + r")\s+(.+)$", re.I)
# A bare year line (section grouping within CHAMPIONSHIP SHOWS)
_YEAR_LINE_RE = re.compile(r"^(20\d{2})$")


def _mk_date(year, day, month_name):
    mon = _MONTHS.get(month_name.lower())
    if not mon:
        return None
    try:
        return dt.date(int(year), mon, int(day))
    except ValueError:
        return None


def _section_discipline(header):
    for rx, canon in _SECTION_DISCIPLINE:
        if rx.search(header):
            return canon
    return None


def parse_national_events(year, html=None):
    """Return national events for `year`. Returns [] on any failure."""
    if html is None:
        if not HAVE_BS4:
            print("[ne] bs4 not installed; skipping National Events",
                  file=sys.stderr)
            return []
        try:
            r = requests.get(NE_URL, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            html = r.text
        except Exception as e:
            print(f"[ne] fetch failed: {e}", file=sys.stderr)
            return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        # The event lines may live in <li>, <p>, <td>, or be separated by <br>.
        # Gather text from all plausible block elements so we don't depend on one
        # exact structure. Also split on newlines within blocks.
        lines = []
        for el in soup.find_all(["li", "p", "td", "div"]):
            # get_text with newline separator preserves <br>-separated lines
            txt = el.get_text("\n", strip=True)
            for part in txt.split("\n"):
                part = part.strip()
                # keep only lines that look like a header, a year, or an event
                if part and len(part) < 200:
                    lines.append(part)
        # de-duplicate consecutive repeats (nested elements can double lines)
        deduped = []
        for ln in lines:
            if not deduped or deduped[-1] != ln:
                deduped.append(ln)
        lines = deduped
    except Exception as e:
        print(f"[ne] parse error: {e}", file=sys.stderr)
        return []

    events = []
    seen = set()
    current_discipline = None       # for discipline-headed sections
    in_champ_shows = False
    champ_year = None

    for ln in lines:
        if not ln:
            continue

        # Section header for a discipline (e.g. "1. AGILITY TRIALS").
        hdr = re.sub(r"^\d+\.\s*", "", ln).strip()
        if re.search(r"championship shows", hdr, re.I):
            in_champ_shows = True
            current_discipline = None
            continue
        disc = _section_discipline(hdr)
        if disc and ("trial" in hdr.lower() or "test" in hdr.lower()
                     or "competition" in hdr.lower() or "handler" in hdr.lower()):
            current_discipline = disc
            in_champ_shows = False
            continue

        # Championship-shows year grouping.
        ym = _YEAR_LINE_RE.match(ln)
        if ym:
            champ_year = int(ym.group(1))
            continue

        # Discipline-section event line: "2026: 24-28 June DOGS QUEENSLAND"
        dm = _DISC_LINE_RE.match(ln)
        if dm and current_discipline:
            yr = int(dm.group(1))
            if yr != year:
                continue
            date = _mk_date(yr, dm.group(2), dm.group(3))
            if not date:
                continue
            dogs_body = dm.group(4).strip().lower()
            region = None
            for k, v in _DOGS_STATE.items():
                if dogs_body.startswith(k):
                    region = v
                    break
            # Fallback: scan for a bare state token anywhere in the body
            # (handles spacing/format variants the startswith map misses).
            if not region:
                sm2 = _TRAIL_STATE_RE.search(dm.group(4).upper())
                if sm2:
                    region = sm2.group(1)
            if not region:
                # Can't place it on the map/state filter; skip rather than emit
                # an event with a None region (which shows as a "None" bucket).
                continue
            title = f"National {current_discipline} Championship"
            key = (title.lower(), date.isoformat(), region)
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "title": title,
                "start": date.isoformat(),
                "end": date.isoformat(),
                "location": dm.group(4).strip(),
                "url": NE_URL,
                "category": current_discipline,
                "region": region,
                "source": "Dogs Australia (National Events)",
                "color": None,
                "cancelled": False,
            })
            continue

        # Championship-shows event line.
        if in_champ_shows and champ_year == year:
            cm = _CH_LINE_RE.match(ln)
            if cm:
                date = _mk_date(champ_year, cm.group(1), cm.group(2))
                if not date:
                    continue
                rest = cm.group(3).strip()
                sm = _TRAIL_STATE_RE.search(rest)
                region = sm.group(1) if sm else None
                if not region:
                    continue  # skip unplaceable events (no None-region bucket)
                # Trim a trailing street address (from the first number that
                # looks like a street number) for readability, but keep the
                # club + ground name so different shows stay distinct.
                title = re.sub(r",?\s*\d{1,4}\s+[A-Z][a-z].*$", "", rest).strip()
                title = title or rest
                key = (title.lower(), date.isoformat(), region)
                if key in seen:
                    continue
                seen.add(key)
                events.append({
                    "title": title,
                    "start": date.isoformat(),
                    "end": date.isoformat(),
                    "location": rest,
                    "url": NE_URL,
                    "category": "Conformation",
                    "region": region,
                    "source": "Dogs Australia (National Events)",
                    "color": None,
                    "cancelled": False,
                })
                continue

    print(f"[ne] parsed {len(events)} National Events for {year}",
          file=sys.stderr)
    return events


if __name__ == "__main__":
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    for e in parse_national_events(yr):
        print(f"{e['start']} {e['region'] or '??':3} {e['category']:16} {e['title']}")

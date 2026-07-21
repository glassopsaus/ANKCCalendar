#!/usr/bin/env python3
"""
Dogz Online "Canine Event Diary" parser  (verification / cross-check source).

https://www.dogzonline.com.au/event-diary/list.asp  (optionally ?state=xx)

This is a clean, server-rendered HTML table covering all 8 states and all
disciplines. Rows are grouped under date headers ("24-Jul-2026 (Friday)") and
each row carries:
  - a type code cell (CH champ show / OP open show / DS dog sports / OE other)
  - the state (ACT/NSW/VIC/QLD/SA/WA/TAS/NT)
  - the event name (with the discipline in the name, e.g. "... Track & Search
    Trial"), sometimes followed by "Event Cancelled"
  - a closing date ("13-Jul-2026")
  - schedule/catalogue PDF links (ignored here)

Because it carries CLOSING DATES and CANCELLATION status, Dogz Online works as a
verification/cross-check source alongside Show Manager: it confirms an event is
real, supplies its entry-closing date, and flags cancellations. Many of its
events are the same ones Show Manager lists (schedule links point at the same
Show Manager blob store), so most will de-duplicate; its added value is
independent cancellation confirmation and any events Show Manager doesn't list.

OUTPUT: a list of listing dicts in the SAME shape Show Manager's scraper emits,
so the existing matcher/gap-fill machinery can consume it unchanged:
    {club, date (ISO), discipline, region, status, closes (ISO|None),
     detail_url, event_id}
status is "cancelled" when flagged, else "listed" (present, not entry-verified).
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

DZ_BASE = "https://www.dogzonline.com.au/event-diary/list.asp"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/126.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://www.dogzonline.com.au/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-User": "?1",
    "Connection": "keep-alive",
}
TIMEOUT = 30

_STATES = {"ACT", "NSW", "VIC", "QLD", "SA", "WA", "TAS", "NT"}

# Date header like "24-Jul-2026 (Friday)".
_DATE_HDR_RE = re.compile(
    r"(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*\((?:mon|tues|wednes|thurs|fri|satur|sun)day\)",
    re.I)
_CLOSING_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")
_MONTHS3 = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Discipline detection from the event NAME (prose), mapped to canonical names.
# Order matters: more specific first (Track & Search before Tracking).
_DISCIPLINE_RULES = [
    (re.compile(r"track\s*&?\s*search|track and search", re.I), "Track & Search"),
    (re.compile(r"\btracking\b", re.I), "Tracking"),
    (re.compile(r"scent\s*work", re.I), "Scent Work"),
    (re.compile(r"rally", re.I), "Rally Obedience"),
    (re.compile(r"obedience", re.I), "Obedience"),
    (re.compile(r"trick", re.I), "Trick Dog"),
    (re.compile(r"jumping|games|\bagility\b", re.I), "Agility"),
    (re.compile(r"dances with dogs|\bdwd\b", re.I), "Dances with Dogs"),
    (re.compile(r"herding", re.I), "Herding"),
    (re.compile(r"endurance", re.I), "Endurance"),
    (re.compile(r"lure\s*(coursing|pursuit)", re.I), "Lure Coursing"),
    (re.compile(r"field trial|pointer & setter|pointing breeds|spaniel", re.I), "Field Trial"),
    (re.compile(r"retriev", re.I), "Retrieving"),
    (re.compile(r"sprint\s*dog|sprintdog", re.I), "Sprint"),
    (re.compile(r"earth\s*dog", re.I), "Earthdog"),
    (re.compile(r"weight pull", re.I), "Weight Pull"),
    (re.compile(r"sled", re.I), "Sled Sports"),
    (re.compile(r"working pack dog|back\s*pack|hiking", re.I), "Backpacking"),
]
# Conformation type codes (from the row's leading code cell).
_CONFORMATION_CODES = {"CH", "OP"}


def _parse_date(day, mon3, year):
    mon = _MONTHS3.get(mon3.lower())
    if not mon:
        return None
    try:
        return dt.date(int(year), mon, int(day))
    except ValueError:
        return None


def _discipline_from_name(name, type_code):
    for rx, canon in _DISCIPLINE_RULES:
        if rx.search(name):
            return canon
    # No sport discipline matched. If it's a conformation show code, call it
    # Conformation; otherwise we don't recognise it, so skip.
    if type_code in _CONFORMATION_CODES:
        return "Conformation"
    return None


def scrape_dogz_online(year, state=None):
    """Return a list of Dogz Online listings for `year` (optionally one state).
    Returns [] on any failure so a problem here never breaks the run."""
    if not HAVE_BS4:
        print("[dz] bs4 not installed; skipping Dogz Online", file=sys.stderr)
        return []
    url = DZ_BASE + (f"?state={state.lower()}" if state else "")
    # Use a Session so any cookie the site sets is carried, and "warm up" by
    # hitting the homepage first (some sites 403 a cold, refererless request to
    # a deep page but allow it once a session/cookie exists). If we still get a
    # 403 after this, it is almost certainly IP-level blocking of the runner,
    # which no header change can fix.
    try:
        sess = requests.Session()
        sess.headers.update(HEADERS)
        try:
            sess.get("https://www.dogzonline.com.au/", timeout=TIMEOUT)
        except Exception:
            pass  # warmup is best-effort
        r = sess.get(url, timeout=TIMEOUT)
        if r.status_code == 403:
            print("[dz] 403 Forbidden even with browser headers + session - "
                  "this is likely IP-level blocking of the CI runner, not a "
                  "header problem; Dogz Online cannot be scraped from here",
                  file=sys.stderr)
            return []
        r.raise_for_status()
    except Exception as e:
        print(f"[dz] fetch failed: {e}", file=sys.stderr)
        return []

    listings = []
    seen = set()
    try:
        soup = BeautifulSoup(r.text, "html.parser")
        current_date = None
        for tr in soup.select("tr"):
            row_text = tr.get_text(" ", strip=True)
            if not row_text:
                continue
            # Date header row?
            hm = _DATE_HDR_RE.search(row_text)
            if hm and len(row_text) < 60:  # header rows are short
                current_date = _parse_date(hm.group(1), hm.group(2), hm.group(3))
                continue
            if not current_date:
                continue
            if current_date.year != year:
                continue

            cells = tr.find_all(["td", "th"])
            if len(cells) < 4:
                continue

            # type code is the first cell's text (CH/OP/DS/OE)
            type_code = cells[0].get_text(" ", strip=True).upper()[:2]

            # state: the bold cell whose text is a state code
            region = None
            for c in cells:
                t = c.get_text(" ", strip=True).upper()
                if t in _STATES:
                    region = t
                    break
            if region not in _STATES:
                continue

            # event name: the cell containing the display.asp link
            name_cell = None
            for c in cells:
                a = c.find("a", href=re.compile(r"display\.asp\?event=", re.I))
                if a:
                    name_cell = c
                    break
            if not name_cell:
                continue
            a = name_cell.find("a", href=re.compile(r"display\.asp\?event=", re.I))
            name = a.get_text(" ", strip=True)
            detail_url = a["href"]
            if detail_url.startswith("/"):
                detail_url = "https://www.dogzonline.com.au" + detail_url
            m_id = re.search(r"event=(\d+)", detail_url)
            event_id = m_id.group(1) if m_id else None

            # cancellation: "Event Cancelled" appears in the name cell text
            cell_text = name_cell.get_text(" ", strip=True)
            cancelled = bool(re.search(r"event cancelled", cell_text, re.I))

            discipline = _discipline_from_name(name, type_code)
            if not discipline:
                continue

            # Schedule PDF link: Dogz Online rows carry a direct link to the
            # event's Schedule document (in an early cell), which is where the
            # ground address and check-in/vetting time are published. Grab the
            # first PDF link that looks like a schedule (not a catalogue/ring
            # plan/breed-numbers doc).
            schedule_url = None
            for c in cells:
                for link in c.find_all("a", href=True):
                    href = link["href"]
                    low = href.lower()
                    if ".pdf" not in low and ".jpg" not in low:
                        continue
                    # skip catalogue / ring plan / breed number docs
                    if any(k in low for k in ("catalogue", "ringplan",
                                              "ring_plan", "breednumbers",
                                              "publicbreednumbers")):
                        continue
                    if "schedule" in low or "-schedule-" in low:
                        schedule_url = href
                        break
                if schedule_url:
                    break

            # closing date: the last cell that parses as a date
            closes = None
            for c in reversed(cells):
                ct = c.get_text(" ", strip=True)
                cm = _CLOSING_RE.search(ct)
                if cm:
                    cd = _parse_date(cm.group(1), cm.group(2), cm.group(3))
                    if cd:
                        closes = cd.isoformat()
                        break

            dedup_key = event_id or (name.lower(), current_date.isoformat())
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            listings.append({
                "club": re.sub(r"\s*Event Cancelled\s*$", "", name, flags=re.I).strip(),
                "date": current_date.isoformat(),
                "discipline": discipline,
                "region": region,
                "status": "cancelled" if cancelled else "listed",
                "closes": closes,
                "detail_url": detail_url,
                "schedule_url": schedule_url,
                "event_id": event_id,
            })
    except Exception as e:
        print(f"[dz] parse error: {e}", file=sys.stderr)
        return listings

    from collections import Counter
    by_region = Counter(x["region"] for x in listings)
    canc = sum(1 for x in listings if x["status"] == "cancelled")
    print(f"[dz] parsed {len(listings)} Dogz Online listings ({canc} cancelled)",
          file=sys.stderr)
    print(f"[dz]   by region: {dict(by_region)}", file=sys.stderr)
    return listings


if __name__ == "__main__":
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    rows = scrape_dogz_online(yr)
    for x in rows[:40]:
        print(f"{x['date']} {x['region']:3} {x['discipline']:16} "
              f"{x['status']:9} closes={x['closes'] or '-':11} {x['club']}")
    print(f"... total {len(rows)}")

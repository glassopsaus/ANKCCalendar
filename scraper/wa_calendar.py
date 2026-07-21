#!/usr/bin/env python3
"""
Dogs West (WA) Calendar of Events PDF parser  (WA primary source).

Dogs West publishes a yearly "Calendar of Events" PDF. Its layout is a
spreadsheet export, denser and messier than the NSW/DV/QLD calendars:

  [optional note] [W/E week#] DATE DAY FIXTURE ... CLUB  4mth  CanineNews
  e.g.  "31 Sat Agility, Jumping & Games Trial Cloverdale Canine Comp Oct Jan/Feb"
        "FEBRUARY 1 Sun Championship Show (All Breeds) Fremantle Dog Club Oct Jan/Feb"
        "16/20 Sat/Wed Tracking Trial Trackwest Jan Mar/Apr"

Parsing rules (derived from the real 2026 PDF):
  - Month comes from an ALL-CAPS month word that appears at the start of the
    first row of each month (sometimes on the same line as the first event).
  - DATE is a day number, or a multi-day form: "20/21", "27-2", "30/31/3",
    "16/20" (ranges use "-", lists use "/"). We take the first as start and the
    last as end; cross-month ranges are handled via the running month.
  - DAY is a weekday word or slash-list ("Sat", "Sat/Sun/Wed"); used to locate
    the boundary between date and fixture.
  - FIXTURE is free-text naming one or more disciplines; we map from prose.
  - The last two columns (a 3-letter "4-month due" month and a "Canine News"
    like "Jan/Feb") are stripped from the end before reading the club.
  - Rows that are clearly not competition events (grounds maintenance, private
    bookings, holiday markers, "No Events", reserved) are skipped.

Only rows whose fixture maps to a known ANKC sport discipline are emitted;
conformation-only shows ("Championship Show (All Breeds)") map to "Conformation".

OUTPUT: event dicts compatible with the main scraper. region always "WA".
No entry status in the PDF; cross-checked downstream against Show Manager.
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

WA_CALENDAR_PAGE = "https://dogswest.com/dogswest/Members-Yearly_Show_Date_Calendars.htm"
WA_FALLBACK_PDF = ("https://dogswest.com/dogswest/d/Members/Yearly_Show_Date_Calendars/"
                   "D6SXD0IL1XES52V1IFEE8Q2GKBW80F/M7HSQDDFTWV6O16.pdf/"
                   "2026+Calendar+Of+Events+(1).pdf")
WA_SOURCE_NAME = "Dogs West (WA calendar)"
WA_COLOR = "#16887a"

_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
           "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
           "november": 11, "december": 12}
_MONTH_WORD_RE = re.compile(r"\b(JANUARY|FEBRUARY|MARCH|APRIL|MAY|JUNE|JULY|"
                            r"AUGUST|SEPTEMBER|OCTOBER|NOVEMBER|DECEMBER)\b")
_DOW = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_MONTH_ABBR = (r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|"
               r"Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t|tember)?|Oct(?:ober)?|"
               r"Nov(?:ember)?|Dec(?:ember)?")
_CANINE_NEWS_RE = re.compile(
    rf"\s+(?:{_MONTH_ABBR})(?:[/ ](?:{_MONTH_ABBR}))?\s*$", re.I)
_FOURMONTH_RE = re.compile(rf"\s+(?:{_MONTH_ABBR})\s*$", re.I)

# Prose -> canonical discipline. Order matters: check more specific first.
# Each entry: (regex, canonical). A fixture may match several (multi-discipline).
_FIXTURE_RULES = [
    (r"track\s*&?\s*search|track and search", "Track & Search"),
    (r"tracking", "Tracking"),
    (r"scent\s*work", "Scent Work"),
    (r"rally", "Rally Obedience"),
    (r"obedience", "Obedience"),
    (r"trick", "Trick Dog"),
    (r"jumping|games|\bagility\b", "Agility"),
    (r"dances with dogs|\bdwd\b", "Dances with Dogs"),
    (r"herding", "Herding"),
    (r"endurance", "Endurance"),
    (r"lure coursing", "Lure Coursing"),
    (r"field trial|pointer & setter|pointing breeds|\bapb\b|spaniels", "Field Trial"),
    (r"retrieving", "Retrieving"),
    (r"sprintdog|sprint dog", "Sprint"),
    (r"earthdog|earth dog", "Earthdog"),
    (r"weight pull", "Weight Pull"),
    (r"sled", "Sled Sports"),
    (r"back\s*pack|hiking", "Backpacking"),
    (r"bale\s*(seek|hunt)|super\s*seven\s*snuffle", "Bale Seek"),
    (r"canine\s*disc|disc\s*dog|toss\s*(&|and)\s*fetch|frisbee", "Canine Disc"),
    (r"championship show|open show|champ show|parade|contest of winners|"
     r"breed exhibition|champ &|championship &|champ show|champ x", "Conformation"),
]

# Rows that are not competition events at all -> skip entirely.
_SKIP_RE = re.compile(
    r"no events|grounds maintenance|grounds free|private booking|reserved for|"
    r"ensure no|shouldn't start|reserve for practical|bump in|no bookings|"
    r"no further bookings|club reserved|diary booking", re.I)


def find_current_wa_pdf_url(year):
    """Discover the current WA calendar PDF from the Yearly Show Date Calendars
    page. Returns (url, discovered_bool); falls back to the pinned URL."""
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(WA_CALENDAR_PAGE, timeout=20,
                            headers={"User-Agent": "TrackingCalendarBot/1.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        best = None
        for a in soup.select('a[href]'):
            href = a.get("href", "")
            txt = (a.get_text(" ", strip=True) or "")
            # Prefer a link mentioning the target year and "calendar".
            if ".pdf" in href.lower() and (str(year) in href or str(year) in txt) \
               and re.search(r"calendar", href + " " + txt, re.I):
                best = href if href.startswith("http") else \
                    "https://dogswest.com" + href
        if best:
            print(f"[wa] discovered {year} calendar: {best}", file=sys.stderr)
            return best, True
        print(f"[wa] no {year} calendar link found on page; using fallback",
              file=sys.stderr)
    except Exception as e:
        print(f"[wa] PDF discovery failed ({e}); using fallback", file=sys.stderr)
    return WA_FALLBACK_PDF, False


def _disciplines_from_fixture(fixture):
    out = []
    low = fixture.lower()
    for pat, canon in _FIXTURE_RULES:
        if re.search(pat, low) and canon not in out:
            out.append(canon)
    return out


def _parse_date_field(datestr, cur_month, year):
    """Parse WA date forms into (start_date, end_date). Handles single '23',
    range '27-2', list '30/31/3', pair '20/21'. Cross-month via cur_month and
    a simple heuristic: if a later part's number is smaller, it's next month."""
    parts = re.split(r"[/\-]", datestr)
    nums = []
    for p in parts:
        p = p.strip()
        if p.isdigit():
            nums.append(int(p))
    if not nums or cur_month is None:
        return None, None
    start_day = nums[0]
    end_day = nums[-1]
    try:
        start = dt.date(year, cur_month, start_day)
    except ValueError:
        return None, None
    # end: same month unless the day rolled backwards (crossed into next month)
    end_month = cur_month
    if end_day < start_day:
        end_month = cur_month + 1 if cur_month < 12 else 1
        if end_month == 1:
            year = year  # keep same year for simplicity (Dec->Jan rare here)
    try:
        end = dt.date(year, end_month, end_day)
    except ValueError:
        end = start
    if end < start:
        end = start
    return start, end


_WE_LEAD_RE = re.compile(r"^\s*(\d{1,3})\s+")   # leading week-number column


def parse_wa_text(text, year):
    events = []
    seen = set()
    cur_month = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Update current month if a month word appears; strip it from the line.
        mw = _MONTH_WORD_RE.search(line)
        if mw:
            cur_month = _MONTHS[mw.group(1).lower()]
            line = (line[:mw.start()] + " " + line[mw.end():]).strip()
        if _SKIP_RE.search(line):
            continue
        # Strip a leading note phrase like "Grounds Free" / "ANZAC Day" is hard
        # to distinguish generically; rely on the date+DOW anchor below instead.

        # Find the DATE + DAY anchor: a date field (digits, optional /-) followed
        # by a weekday word or weekday slash-list.
        m = re.search(
            r"(?<![\d/])(\d{1,2}(?:[/\-]\d{1,2}){0,3})\s+"
            r"((?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)(?:[/\-](?:Mon|Tue|Wed|Thu|Fri|Sat|Sun))*)\b",
            line)
        if not m:
            continue
        datestr = m.group(1)
        fixture_and_club = line[m.end():].strip()
        if not fixture_and_club:
            continue

        # Strip the two trailing columns: Canine News then 4-month due.
        fc = _CANINE_NEWS_RE.sub("", fixture_and_club).strip()
        fc = _FOURMONTH_RE.sub("", fc).strip()

        # Split fixture from club is ambiguous (both free text). We don't need a
        # perfect club name; map disciplines from the whole remaining string,
        # which contains the fixture (and the club, which rarely contains sport
        # keywords). Use the full string for discipline detection.
        discs = _disciplines_from_fixture(fc)
        if not discs:
            continue

        start, end = _parse_date_field(datestr, cur_month, year)
        if not start:
            continue

        # Best-effort club: take trailing capitalised words after the fixture.
        # Heuristic: the club is usually the last 2-5 words. We keep the whole
        # remaining text as the title so information isn't lost.
        title_text = fc

        for disc in discs:
            key = (title_text.lower(), start.isoformat(), disc)
            if key in seen:
                continue
            seen.add(key)
            events.append({
                "title": title_text if disc in title_text else f"{title_text} \u2013 {disc}",
                "club": "",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "location": "Western Australia",
                "url": WA_CALENDAR_PAGE,
                "category": disc,
                "region": "WA",
                "source": WA_SOURCE_NAME,
                "color": WA_COLOR,
                "cancelled": False,
            })
    events.sort(key=lambda e: (e["start"], e["title"]))
    return events


def parse_wa_calendar(year, pdf_url=None, pdf_bytes=None):
    """Fetch + parse the WA calendar PDF. Returns [] on any failure so a problem
    here never breaks the rest of the scraper."""
    if not HAVE_PDFPLUMBER:
        print("[wa] pdfplumber not installed; skipping WA calendar",
              file=sys.stderr)
        return []
    try:
        resolved_url = pdf_url or ""
        if pdf_bytes is None:
            if pdf_url:
                url, discovered = pdf_url, True
            else:
                url, discovered = find_current_wa_pdf_url(year)
            resolved_url = url
            if not discovered:
                print(f"[wa] WARNING: using pinned FALLBACK PDF (discovery "
                      f"failed) - may be STALE: {url}", file=sys.stderr)
            else:
                print(f"[wa] using PDF: {url}", file=sys.stderr)
            resp = requests.get(url, timeout=40,
                                headers={"User-Agent": "TrackingCalendarBot/1.0"})
            resp.raise_for_status()
            pdf_bytes = resp.content
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        text = "\n".join(text_parts)
        # Year safety. The WA PDF DOES carry an in-body year ("2026 CALENDAR OF
        # EVENTS" header), but rows themselves are year-less, so we still stamp
        # YEAR. Accept if the target year appears in the body OR the filename;
        # reject if the body shows positive evidence of a DIFFERENT year.
        year_in_body = str(year) in text
        year_in_url = str(year) in resolved_url
        other_years = set(re.findall(r"\b(20\d{2})\b", text)) - {str(year)}
        if not (year_in_body or year_in_url):
            print(f"[wa] WARNING: {year} not found in PDF body or filename - "
                  f"may be the wrong year's file; skipping to avoid mis-dated "
                  f"events", file=sys.stderr)
            return []
        if other_years and not year_in_body:
            # filename says target year but body only shows other years
            print(f"[wa] WARNING: PDF body references other year(s) "
                  f"{sorted(other_years)} but not {year} - possible wrong file; "
                  f"skipping", file=sys.stderr)
            return []
        events = parse_wa_text(text, year)
        if not events:
            print("[wa] WARNING: parsed 0 events - PDF format may have changed; "
                  "parser likely needs updating", file=sys.stderr)
        from collections import Counter
        by_disc = Counter(e["category"] for e in events)
        print(f"[wa] parsed {len(events)} WA events", file=sys.stderr)
        print(f"[wa]   by discipline: {dict(by_disc)}", file=sys.stderr)
        return events
    except Exception as e:
        print(f"[wa] FAILED, skipping: {e}", file=sys.stderr)
        return []


if __name__ == "__main__":
    evs = parse_wa_calendar(2026)
    for e in evs[:50]:
        print(f"{e['start']}..{e['end']} {e['category']:16} {e['title']}")
    print(f"... total {len(evs)}")

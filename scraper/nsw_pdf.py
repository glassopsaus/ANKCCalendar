#!/usr/bin/env python3
"""
Dogs NSW PDF calendar parser  (stage 1 of the source-of-truth rebuild).

WHY A PDF: Dogs NSW does not publish a scrapable events calendar. The only
machine-readable source is the "Show & Trials Guide" PDF, e.g.
  https://dogsnsw.org.au/media/7610/2026-show-trial-calendar-website-with-entry-system.pdf

STRUCTURE (verified against the real 2026 PDF):
  A legend at the top maps discipline codes to names. The ones we care about:
      TT = Tracking / Track & Search
      SW = Scent Work
  (Note: TD = Trick Dog Trial — NOT tracking. Must not be confused with TT.)

  Each event is a row laid out as:
      <WeekNo>  <D/MM/YYYY>  <Club>  <TypeCodes>  <Venue>  [<Online Provider>]  <Contact>
  e.g.
      03/5 16/01/2026 Western Sydney Scent Work Club Inc SW Showground, ...
           Top Dog Events  Ms S Michael 0433 642 244

  The "Type" field can combine codes with '/', e.g. "CH/OT", "AT/JT/TD".
  We keep a row if its Type field contains TT or SW as a whole token.

  The "Online Provider" column names the entry system (Top Dog Events,
  Show Manager, K9entries, Ozentries, Easy Dogs, Ready Entries) or is blank.
  We capture it as `provider` for the later entry-status cross-check.

OUTPUT: a list of event dicts compatible with the main scraper:
    {title, start, end, location, url, category, region, source,
     color, cancelled, provider}
  category is "Tracking", "Track & Search", or "Scent Work".
  (TT covers both Tracking and Track & Search; the PDF doesn't distinguish
   them in the code, so TT is labelled "Tracking / Track & Search".)

STATUS: this module only extracts the authoritative list. The open-vs-approved
status is applied later by the entry-system cross-check, not here.

NOTE ON THE URL: Dogs NSW republishes this PDF at a new /media/NNNN/ path on
every amendment, so the "current" URL changes. We therefore discover it from
the calendar page rather than hardcoding it (see find_current_nsw_pdf_url).
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

HEADERS = {"User-Agent": "TrackingCalendarBot/1.0 (+combined tracking events)"}
TIMEOUT = 60

# The page that always links to the current PDF (stable URL).
NSW_CALENDAR_PAGE = "https://www.dogsnsw.org.au/events/show-and-trials-guide/"
# Fallback if discovery fails: the known 2026 PDF (may go stale on amendment).
NSW_PDF_FALLBACK = ("https://dogsnsw.org.au/media/7610/"
                    "2026-show-trial-calendar-website-with-entry-system.pdf")

# Full discipline-code -> canonical category map (ALL ANKC sports, aligned with
# the QLD legend and the NSW Show & Trial Guide key). Previously this kept only
# TT/SW (tracking-era scope); it now keeps every discipline. Codes not present
# here fall through and the row is skipped only if NO code maps.
NSW_DISCIPLINE_MAP = {
    "TT": "Tracking / Track & Search",   # NSW combines these under TT
    "T&S": "Track & Search",
    "SW": "Scent Work",
    "OT": "Obedience",
    "RT": "Rally Obedience",
    "R-OT": "Obedience",
    "AT": "Agility", "JT": "Agility", "GT": "Agility", "GAMES": "Agility",
    "TD": "Trick Dog",
    "DWD": "Dances with Dogs",
    "HT": "Herding",
    "ET": "Endurance",
    "LC": "Lure Coursing",
    "FT": "Field Trial",
    "RATG": "Retrieving", "RET": "Retrieving",
    "EDT": "Earthdog",
    "SD": "Sled Sports", "SS": "Sprint",
    # Newly ANKC-sanctioned disciplines (Bale Seek from 2026, Canine Disc from
    # 2025). NOTE: the exact NSW PDF codes for these are not yet confirmed from a
    # live calendar that includes them; these are the plausible/observed tokens.
    # If Dogs NSW uses a different short code, add it here once seen in the PDF.
    "BS": "Bale Seek", "BSK": "Bale Seek", "BALE": "Bale Seek", "BH": "Bale Seek",
    "CD": "Canine Disc", "DISC": "Canine Disc", "CDA": "Canine Disc",
    "CH": "Conformation", "OPEN": "Conformation", "PARADE": "Conformation",
}

# A row starts with a week-number token like "03/5" then a date "16/01/2026".
NSW_ROW_RE = re.compile(
    r"^\s*\d{1,2}/\d\s+(\d{1,2}/\d{1,2}/\d{4})\s+(.*)$")

# Known entry-provider strings that may appear in the Online Provider column.
NSW_PROVIDERS = [
    "Top Dog Events", "Show Manager", "K9entries", "K9 Entries",
    "Ozentries", "Easy Dogs", "Ready Entries", "EasyDogs",
]

# Type field: a run of uppercase discipline codes joined by '/', optionally
# with digits (e.g. RTG5) and hyphens (R-OT). We pull the whole token block.
NSW_TYPE_RE = re.compile(r"\b([A-Z][A-Z0-9\-]*(?:/[A-Z0-9\-]+)*)\b")


def find_current_nsw_pdf_url():
    """Scrape the calendar page for the current PDF link; fall back if needed."""
    try:
        r = requests.get(NSW_CALENDAR_PAGE, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        # Look for a media PDF link mentioning the show/trial calendar.
        candidates = re.findall(
            r'href="([^"]+\.pdf)"', r.text, re.I)
        for href in candidates:
            low = href.lower()
            if "show" in low and "trial" in low and "calendar" in low:
                if href.startswith("/"):
                    href = "https://www.dogsnsw.org.au" + href
                print(f"[nsw] discovered PDF: {href}", file=sys.stderr)
                return href
    except Exception as e:
        print(f"[nsw] PDF discovery failed ({e}); using fallback", file=sys.stderr)
    print(f"[nsw] using fallback PDF url", file=sys.stderr)
    return NSW_PDF_FALLBACK


def _type_codes(type_field):
    """Return the set of individual discipline codes in a Type field."""
    codes = set()
    for token in NSW_TYPE_RE.findall(type_field):
        for part in token.split("/"):
            codes.add(part)
    return codes


# The full discipline-code vocabulary from the PDF legend (verified against the
# real 2026 key). The Type field is the first token whose '/'-separated parts
# are ALL drawn from this vocabulary — that's what distinguishes it from a
# capitalised club word like "Sydney" or "Kennel".
NSW_CODE_VOCAB = {
    "AT", "JT", "GT", "HT", "CH", "LC", "DS", "OPEN", "SD", "PARADE",
    "DWD", "RATG", "EDT", "OT", "ET", "RT", "EV", "R-OT", "FB", "SANCTION",
    "FT", "SS", "TD", "TT", "SW",
    # composite/rare tokens seen in real data:
    "RTG5", "GAMES",
}


def _looks_like_type_token(tok):
    """True if `tok` is a discipline-code group (all parts in the vocab)."""
    parts = [p for p in tok.split("/") if p]
    if not parts:
        return False
    # Every part must be a known code (allows CH/DS, AT/JT/TD, OT/R-OT). A
    # single unknown part disqualifies it, keeping club words like "Sydney" out.
    return all(p in NSW_CODE_VOCAB for p in parts)


def _split_row(remainder):
    """From the text after the date, pull (club, type_field, provider).

    Layout: <Club words...> <TYPE-codes> <Venue...> [<Provider>] <Contact>
    The club name is one or more title-case words; the TYPE field is the first
    token whose '/'-separated parts are all known discipline codes.
    """
    tokens = remainder.split()
    type_idx = None
    for i, tok in enumerate(tokens):
        clean = tok.strip(",.")
        if i >= 1 and _looks_like_type_token(clean):
            type_idx = i
            break
    if type_idx is None:
        return None, None, None
    club = " ".join(tokens[:type_idx]).strip()
    type_field = tokens[type_idx].strip(",.")
    rest = " ".join(tokens[type_idx + 1:])
    provider = None
    for p in NSW_PROVIDERS:
        if re.search(re.escape(p), rest, re.I):
            if p.lower().startswith("k9"):
                provider = "K9 Entries"
            elif p.lower().replace(" ", "") == "easydogs":
                provider = "Easy Dogs"
            else:
                provider = p
            break
    return club, type_field, provider


def parse_nsw_pdf(year, pdf_url=None, pdf_bytes=None):
    """Return a list of NSW tracking/scent-work events for `year`.

    Either pass pdf_bytes (for offline testing) or let it fetch pdf_url
    (defaults to the discovered current PDF).
    """
    if not HAVE_PDFPLUMBER:
        print("[nsw] pdfplumber not installed; skipping NSW", file=sys.stderr)
        return []

    if pdf_bytes is None:
        pdf_url = pdf_url or find_current_nsw_pdf_url()
        try:
            r = requests.get(pdf_url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            pdf_bytes = r.content
        except Exception as e:
            print(f"[nsw] PDF fetch failed: {e}", file=sys.stderr)
            return []

    events = []
    seen = set()
    current_month_year = None

    try:
        year_seen_in_pdf = False
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                if str(year) in text:
                    year_seen_in_pdf = True
                for raw_line in text.split("\n"):
                    line = raw_line.strip()
                    m = NSW_ROW_RE.match(line)
                    if not m:
                        continue
                    date_str, remainder = m.group(1), m.group(2)
                    # parse date d/m/Y
                    try:
                        d, mo, y = [int(x) for x in date_str.split("/")]
                        edate = dt.date(y, mo, d)
                    except ValueError:
                        continue
                    if edate.year != year:
                        continue

                    club, type_field, provider = _split_row(remainder)
                    if not type_field:
                        continue
                    codes = _type_codes(type_field)
                    # Map every recognised code to its discipline. A row can list
                    # several (e.g. "OT/AT/JT") -> emit one event per distinct
                    # discipline so each shows under the right filter.
                    cats = []
                    for c in codes:
                        cat = NSW_DISCIPLINE_MAP.get(c)
                        if cat and cat not in cats:
                            cats.append(cat)
                    if not cats:
                        continue  # no recognised discipline in this row

                    for category in cats:
                        title = f"{club} \u2013 {category}" if club else category
                        key = (title.lower(), edate.isoformat())
                        if key in seen:
                            continue
                        seen.add(key)
                        events.append({
                            "title": title,
                            "start": edate.isoformat(),
                            "end": edate.isoformat(),
                            "location": "",
                            "url": NSW_CALENDAR_PAGE,
                            "category": category,
                            "region": "NSW",
                            "source": "Dogs NSW",
                            "color": None,          # set by main scraper
                            "cancelled": False,
                            "provider": provider,   # for entry-status cross-check
                        })
    except Exception as e:
        print(f"[nsw] PDF parse error: {e}", file=sys.stderr)
        return events

    # Safety: NSW rows carry full d/m/Y dates and we filter to `year`, so wrong-
    # year data can't be mis-stamped — but a wrong-year PDF would silently yield
    # 0 events, looking like "no NSW trials". Warn if the target year never even
    # appeared in the PDF, or if we parsed nothing.
    if not year_seen_in_pdf:
        print(f"[nsw] WARNING: {year} not found anywhere in the PDF - may be "
              f"the wrong year's file; NSW events may be missing", file=sys.stderr)
    if not events:
        print("[nsw] WARNING: parsed 0 events - PDF format may have changed or "
              "wrong file fetched", file=sys.stderr)

    from collections import Counter
    by_disc = Counter(e["category"] for e in events)
    print(f"[nsw] parsed {len(events)} events across all disciplines",
          file=sys.stderr)
    print(f"[nsw]   by discipline: {dict(by_disc)}", file=sys.stderr)
    return events


if __name__ == "__main__":
    # Manual test entrypoint: python nsw_pdf.py <year> [pdf_url]
    yr = int(sys.argv[1]) if len(sys.argv) > 1 else 2026
    url = sys.argv[2] if len(sys.argv) > 2 else None
    evs = parse_nsw_pdf(yr, pdf_url=url)
    for e in evs[:40]:
        print(f"{e['start']}  {e['category']:26} {e['provider'] or '-':16} {e['title']}")
    print(f"\nTOTAL: {len(evs)}")

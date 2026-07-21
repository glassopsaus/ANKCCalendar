#!/usr/bin/env python3
"""
Dogs Queensland master trial calendar PDF parser  (QLD primary source).

Dogs Queensland publishes a single master "Trial Calendar" PDF covering all
sport disciplines for the year, e.g.
  https://dogsqueensland.org.au/media/54908/trial-calendar-2026-master-dogs-qld.pdf

FORMAT (verified against the real 2026 PDF):
  A legend maps short codes to disciplines, then rows of:
      W/E | CLUB | DAY | DATE | TRIAL TYPE
  e.g.  "24 TOWNSVILLE SA 22-Aug T&S"
        "21 ROCKHAMPTON SA 23-May OT/RT/AT/JT/GTx2"
  - W/E is a week number or "MW" (mid-week); we ignore it.
  - DAY is a 2-3 letter weekday code (MO/TU/WE/TH/FR/SA/SU or WED/THU...).
  - DATE is "D-Mon" with no year (year comes from YEAR).
  - TRIAL TYPE is one or more discipline codes joined by "/", each optionally
    with an "xN" multiplier ("TKx2", "GT x 2"). One club-day row can list
    several disciplines; we emit ONE event per distinct discipline (so each
    shows under the right filter), de-duplicated per club-day-discipline.

There is no entry-status information here (like the DV calendar); these are
governing-body "approved" listings, cross-checked for entry status downstream
against Show Manager.

OUTPUT: list of event dicts compatible with the main scraper:
    {title, start, end, location, url, category, region, source, color, cancelled}
region is always "QLD".

The media-id URL changes when QLD republishes, so discovery reads the current
link from the Show/Trial Dates page, falling back to the pinned URL (flagged
visibly) if that fails.
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

QLD_DATES_PAGE = "https://dogsqueensland.org.au/events/showtrial-dates/"
QLD_FALLBACK_PDF = ("https://dogsqueensland.org.au/media/54908/"
                    "trial-calendar-2026-master-dogs-qld.pdf")
QLD_SOURCE_NAME = "Dogs Queensland (trial calendar)"
QLD_COLOR = "#c0392b"

_MONTHS3 = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Weekday codes that can appear in the DAY column (used to locate the date).
_DOW_CODES = {"mo", "tu", "we", "th", "fr", "sa", "su",
              "mon", "tue", "wed", "thu", "fri", "sat", "sun"}

# Map QLD discipline codes (and common free-text labels) to canonical names.
# Codes are matched case-insensitively after stripping any "xN" multiplier.
_CODE_MAP = {
    "AT": "Agility", "JT": "Agility", "GT": "Agility",
    "OT": "Obedience", "RT": "Rally Obedience",
    "DWD": "Dances with Dogs", "TK": "Trick Dog",  # NOTE: see caveat below
    "TT": "Tracking", "T&S": "Track & Search",
    "SW": "Scent Work",
    "ED": "Earthdog", "ET": "Endurance",
    "FT": "Field Trial", "F.T": "Field Trial",
    "HT": "Herding",
    "RATG": "Retrieving", "RET": "Retrieving",
    "BP": "Backpacking",
    "ROT": "Rally Obedience", "RRT": "Rally Obedience",
    # Newly sanctioned; codes not yet confirmed from a QLD calendar that lists
    # them, so add plausible tokens (revise if the real code differs).
    "BS": "Bale Seek", "BSK": "Bale Seek", "BH": "Bale Seek",
    "CD": "Canine Disc",
}
# Free-text (multi-word) labels that appear instead of codes.
_TEXT_MAP = [
    ("sprint dog", "Sprint"), ("sprintdog", "Sprint"),
    ("lure coursing", "Lure Coursing"),
    ("weight pull", "Weight Pull"),
    ("sled dog", "Sled Sports"), ("sled racing", "Sled Sports"),
    ("back packing", "Backpacking"), ("backpacking", "Backpacking"),
    ("hiking", "Backpacking"),
    ("endurance", "Endurance"),
    ("bale seek", "Bale Seek"), ("bale hunt", "Bale Seek"),
    ("super seven snuffle", "Bale Seek"),
    ("canine disc", "Canine Disc"), ("disc dog", "Canine Disc"),
    ("toss and fetch", "Canine Disc"), ("frisbee", "Canine Disc"),
]
# Obedience class-level strings (e.g. "UDX/UD/CDX/CD/CCD") -> Obedience.
_OBEDIENCE_LEVELS = {"udx", "ud", "cdx", "cd", "ccd", "ccdx"}
# Row trial-type values that are not real disciplines -> skip.
_SKIP_TYPES = {"on hold", "club reserved", "club choice", "club reserved",
               "purple trial", "sprintdog training day",
               "sprint dog training day"}

# CAVEAT on "TK": in the QLD legend, TK = "Trick Trial" (Trick Dog) while
# TT = "Tracking Trial". These are easy to conflate. We follow the legend:
# TK -> Trick Dog, TT -> Tracking. (This differs from some other sources where
# "TK" might be shorthand for tracking; QLD's own legend is authoritative here.)


def find_current_qld_pdf_url(year):
    """Discover the current trial-calendar PDF for `year` from the Show/Trial
    Dates page. Returns (url, discovered_bool); falls back to the pinned URL on
    failure. Pins the YEAR so we never grab a draft for a different year (the
    media library can contain e.g. 'copy-of-...-2027-...corals-copy.pdf')."""
    try:
        from bs4 import BeautifulSoup
        resp = requests.get(QLD_DATES_PAGE, timeout=20,
                            headers={"User-Agent": "TrackingCalendarBot/1.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        best = None
        for a in soup.select('a[href]'):
            href = a.get("href", "")
            low = href.lower()
            # Must be a trial-calendar PDF that mentions the TARGET year, and we
            # avoid obvious working drafts ("copy of", "draft").
            if ".pdf" not in low or "trial-calendar" not in low:
                continue
            if str(year) not in low:
                continue
            if "copy-of" in low or "draft" in low or "corals" in low:
                continue
            best = href if href.startswith("http") else \
                "https://dogsqueensland.org.au" + href
        if best:
            print(f"[qld] discovered {year} trial calendar: {best}",
                  file=sys.stderr)
            return best, True
        print(f"[qld] no clean {year} trial-calendar link found; using fallback",
              file=sys.stderr)
    except Exception as e:
        print(f"[qld] PDF discovery failed ({e}); using fallback", file=sys.stderr)
    return QLD_FALLBACK_PDF, False


_XN_RE = re.compile(r"\s*x\s*\d+\s*$", re.I)     # trailing "x2", " x 2"
_PAREN_RE = re.compile(r"\([^)]*\)")              # "(Level TSD1 only)", "(E)"


def _clean_code_token(tok):
    """Strip an xN multiplier and parenthetical qualifier from a single code."""
    t = _PAREN_RE.sub("", tok).strip()
    t = _XN_RE.sub("", t).strip()
    return t


def _disciplines_from_type(type_str):
    """Return the set of canonical disciplines named in a TRIAL TYPE cell.
    Handles code lists ('OT/RT/AT/JT/GTx2'), free text ('SPRINT DOG'),
    obedience levels, and mixed forms. Skips non-discipline placeholders."""
    s = (type_str or "").strip()
    low = s.lower()
    if not s or low in _SKIP_TYPES:
        return []

    found = []

    def add(canon):
        if canon and canon not in found:
            found.append(canon)

    # Free-text multi-word labels first (they contain spaces, not slashes).
    for needle, canon in _TEXT_MAP:
        if needle in low:
            add(canon)
    # If we matched a free-text label and there are no slash-codes, done.
    # Otherwise also parse slash-separated codes.
    # Obedience class levels like "UDX/UD/CDX/CD/CCD" -> Obedience.
    parts = re.split(r"[\/]", s)
    for raw in parts:
        tok = _clean_code_token(raw)
        if not tok:
            continue
        up = tok.upper().replace(" ", "")
        low_tok = tok.lower()
        if low_tok in _OBEDIENCE_LEVELS or up in {x.upper() for x in _OBEDIENCE_LEVELS}:
            add("Obedience")
            continue
        # exact code match (normalise "T & S" -> "T&S", "F.T" stays)
        code = up.replace("&", "&")
        # try a few normalisations
        candidates = {up, up.replace(".", ""), tok.upper().strip()}
        matched = None
        for c in candidates:
            if c in _CODE_MAP:
                matched = _CODE_MAP[c]
                break
        if matched:
            add(matched)
            continue
        # free-text token inside a slash list (e.g. ".../SPRINT DOG")
        for needle, canon in _TEXT_MAP:
            if needle in low_tok:
                add(canon)
                matched = canon
                break
        # otherwise: unrecognised token -> ignore (don't guess)
    return found


def parse_qld_text(text, year):
    """Parse extracted PDF text into QLD event dicts (one per discipline)."""
    events = []
    seen = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # A data row contains a "D-Mon" date token; find it.
        m = re.search(r"\b(\d{1,2})-([A-Za-z]{3})\b", line)
        if not m:
            continue
        day = int(m.group(1))
        mon = _MONTHS3.get(m.group(2).lower())
        if not mon:
            continue
        try:
            date = dt.date(year, mon, day)
        except ValueError:
            continue

        # Everything after the date token is the TRIAL TYPE; everything between
        # the leading W/E token and the DAY code is the CLUB.
        after = line[m.end():].strip()
        before = line[:m.start()].strip()
        # DAY code is the last token of `before`; club is the rest minus the
        # leading W/E column (first token, a number or "MW").
        btoks = before.split()
        if len(btoks) < 2:
            continue
        # drop trailing day-of-week code if present
        if btoks[-1].lower().strip(".") in _DOW_CODES:
            btoks = btoks[:-1]
        # drop leading W/E token (number or MW)
        if btoks and (btoks[0].isdigit() or btoks[0].upper() == "MW"):
            btoks = btoks[1:]
        club = " ".join(btoks).strip()
        if not club:
            continue

        for disc in _disciplines_from_type(after):
            key = (club.lower(), date.isoformat(), disc)
            if key in seen:
                continue
            seen.add(key)
            title = f"{club.title()} \u2013 {disc}"
            events.append({
                "title": title,
                "club": club.title(),
                "start": date.isoformat(),
                "end": date.isoformat(),
                "location": "Queensland",
                "url": QLD_DATES_PAGE,
                "category": disc,
                "region": "QLD",
                "source": QLD_SOURCE_NAME,
                "color": QLD_COLOR,
                "cancelled": False,
            })
    events.sort(key=lambda e: (e["start"], e["title"]))
    return _collapse_consecutive(events)


def _collapse_consecutive(events):
    """Merge consecutive-day rows for the same club+discipline into one event
    (e.g. a Sat+Sun Track & Search trial), matching NSW/DV behaviour."""
    events.sort(key=lambda e: (e["club"].lower(), e["category"], e["start"]))
    out = []
    for e in events:
        if out:
            p = out[-1]
            if (p["club"].lower() == e["club"].lower()
                    and p["category"] == e["category"]):
                try:
                    pe = dt.date.fromisoformat(p["end"])
                    es = dt.date.fromisoformat(e["start"])
                    if (es - pe).days in (0, 1):
                        p["end"] = max(p["end"], e["end"])
                        continue
                except (ValueError, TypeError):
                    pass
        out.append(dict(e))
    out.sort(key=lambda e: (e["start"], e["title"]))
    return out


def parse_qld_calendar(year, pdf_url=None, pdf_bytes=None):
    """Fetch + parse the QLD trial calendar PDF. Returns [] on any failure so a
    problem here never breaks the rest of the scraper."""
    if not HAVE_PDFPLUMBER:
        print("[qld] pdfplumber not installed; skipping QLD calendar",
              file=sys.stderr)
        return []
    try:
        resolved_url = pdf_url or ""
        if pdf_bytes is None:
            if pdf_url:
                url, discovered = pdf_url, True
            else:
                url, discovered = find_current_qld_pdf_url(year)
            resolved_url = url
            if not discovered:
                print(f"[qld] WARNING: using pinned FALLBACK PDF (discovery "
                      f"failed) - may be STALE: {url}", file=sys.stderr)
            else:
                print(f"[qld] using PDF: {url}", file=sys.stderr)
            resp = requests.get(url, timeout=40,
                                headers={"User-Agent": "TrackingCalendarBot/1.0"})
            resp.raise_for_status()
            pdf_bytes = resp.content
        text_parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text_parts.append(page.extract_text() or "")
        text = "\n".join(text_parts)
        # Year safety. IMPORTANT: the QLD PDF body carries NO year — rows are
        # "17-Jan" etc. and the only date marker is an "Updated D/M/YYYY" line.
        # So the trustworthy year signal is the FILENAME, which discovery pins
        # to the target year and screens for drafts ("copy-of"/"draft"/"corals").
        # We therefore:
        #   (a) require the resolved filename/URL to name the target year; and
        #   (b) reject only if the body shows POSITIVE evidence of a DIFFERENT
        #       year (e.g. a wrong-year file that still had a plausible name).
        # This avoids the false rejection we saw when requiring an in-body year
        # that legitimately isn't there.
        url_ok = str(year) in resolved_url
        other_years = set(re.findall(r"\b(20\d{2})\b", text)) - {str(year)}
        # drop amendment-stamp year(s): those appearing in an "Updated ...YYYY"
        stamp_years = set(re.findall(r"Updated[^\n]*?\b(20\d{2})\b", text, re.I))
        wrong_year_evidence = other_years - stamp_years
        if not url_ok:
            print(f"[qld] WARNING: resolved PDF filename does not name {year} "
                  f"({resolved_url}); skipping to avoid mis-dated events",
                  file=sys.stderr)
            return []
        if wrong_year_evidence:
            print(f"[qld] WARNING: PDF body references other year(s) "
                  f"{sorted(wrong_year_evidence)} - possible wrong file; "
                  f"skipping to avoid mis-dated events", file=sys.stderr)
            return []
        events = parse_qld_text(text, year)
        if not events:
            print("[qld] WARNING: parsed 0 events - PDF format may have changed; "
                  "parser likely needs updating", file=sys.stderr)
        from collections import Counter
        by_disc = Counter(e["category"] for e in events)
        print(f"[qld] parsed {len(events)} QLD events", file=sys.stderr)
        print(f"[qld]   by discipline: {dict(by_disc)}", file=sys.stderr)
        return events
    except Exception as e:
        print(f"[qld] FAILED, skipping: {e}", file=sys.stderr)
        return []


if __name__ == "__main__":
    evs = parse_qld_calendar(2026)
    for e in evs[:40]:
        print(f"{e['start']} {e['category']:16} {e['title']}")
    print(f"... total {len(evs)}")

#!/usr/bin/env python3
"""
TRDC (Tracking & Retrieving Dog Club NSW) tracking-calendar parser.

http://trackingclubnsw.org.au/Tracking_Calendar_2026_v<n>.pdf

WHY THIS EXISTS: the Dogs NSW governing PDF lumps Tracking and Track & Search
under a single "TT" code, so we can't tell the two apart from that source alone.
The TRDC club publishes a season calendar as a ruled table whose columns ARE the
signal:

    Week | Date | TRDC Tracking Trials | Other Tracking Trials | Other Trials | Holidays

Columns 3 and 4 ("TRDC Tracking Trials", "Other Tracking Trials") are the
tracking family; column 5 ("Other Trials") is everything else (SW/OT/ET/etc.)
and is ignored here. Within the tracking columns, a cell that says
"Track & Search" marks a T&S event.

CONSERVATIVE POLICY (important): TRDC does NOT reliably annotate every Track &
Search event — some genuine T&S trials appear with no "Track & Search" marker
(e.g. Grafton Dog Obedience Club's trials). An unlabelled cell is therefore NOT
proof that the event is plain Tracking. To avoid confidently-wrong labels we
emit a signal ONLY for cells EXPLICITLY marked "Track & Search":

    {"date": ISO, "club": <cell text>, "discipline": "Track & Search"}

Unlabelled tracking rows produce no signal, so the corresponding Dogs NSW "TT"
event is LEFT COMBINED ("Tracking / Track & Search") rather than asserted as
plain Tracking. We only ever refine toward a specific label when a source
positively confirms it.

FAIL-SAFE: any problem (no pdfplumber, network down, layout changed) returns []
so the run is never broken. This is a refinement source, not a primary one.
"""

import re
import sys
import datetime as dt

try:
    import requests
    HAVE_REQUESTS = True
except Exception:
    HAVE_REQUESTS = False

try:
    import pdfplumber
    HAVE_PDFPLUMBER = True
except Exception:
    HAVE_PDFPLUMBER = False

# The TRDC calendar PDF URL encodes both the YEAR and a VERSION that both change
# over time (a new file each season, bumped versions within a season). We build
# candidate URLs from a template so a rollover to 2027 — or a bump to _v3 — is
# found automatically, and keep the last-known-good full URL as a pin/fallback.
TRDC_URL_TEMPLATE = "http://trackingclubnsw.org.au/Tracking_Calendar_{year}_v{ver}.pdf"
TRDC_PDF_URL = TRDC_URL_TEMPLATE.format(year=2026, ver=2)  # last-known-good pin
# Some seasons may publish without a version suffix; try that shape too.
TRDC_URL_NOVER_TEMPLATE = "http://trackingclubnsw.org.au/Tracking_Calendar_{year}.pdf"
HEADERS = {"User-Agent": "ANKCEventCheck/1.0 (+https://github.com/glassopsaus/ANKCCalendar)"}
TIMEOUT = 30

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Column headers we treat as the tracking family (case/spacing-insensitive
# substring match, so header wording tweaks don't break detection).
_TRACKING_COL_HINTS = ("tracking trial",)  # matches "TRDC Tracking Trials" & "Other Tracking Trials"

_TS_RE = re.compile(r"track\s*(?:&|and)\s*search", re.I)
_DATE_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{2,4})")
# Cell values that aren't events (training markers, blanks).
_NON_EVENT_RE = re.compile(r"^\s*(training(?:\s+if\s+weather\s+ok)?)?\s*$", re.I)


def _parse_date(s, target_year):
    """Parse 'DD-MMM-YY' -> ISO. The source has at least one typo year
    (e.g. '11-May-06' for 2026); if the parsed year is far from the target,
    assume it was meant to be the target year."""
    m = _DATE_RE.search(s or "")
    if not m:
        return None
    day, mon3, yr = m.group(1), m.group(2).lower(), m.group(3)
    mon = _MONTHS.get(mon3)
    if not mon:
        return None
    year = int(yr)
    if year < 100:
        year += 2000
    # Guard against source typos like '-06' meaning '-26': if the two-digit
    # year is wildly off from the target season, snap to the target year.
    if target_year and abs(year - target_year) > 5:
        year = target_year
    try:
        return dt.date(year, mon, int(day)).isoformat()
    except ValueError:
        return None


def _clean_cell(text):
    """A cell may hold 'Club\\nJudgeName' or 'Event\\nTrack & Search\\nJudge'.
    Return the cell collapsed to a single line for club-token matching, with the
    'Track & Search' marker removed from the club text (it's a type flag, not
    part of the club name)."""
    one = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip()
    return one


TRDC_SITE_ROOT = "http://trackingclubnsw.org.au/"


def _discover_from_site(year=None):
    """Scan the club site's homepage for a link to the tracking calendar PDF.
    This is the most rename-proof discovery path: it doesn't assume the filename
    pattern, only that the link text/href mentions tracking/calendar. Returns a
    URL or None. Prefers a link whose href mentions the target year."""
    if not HAVE_REQUESTS:
        return None
    try:
        r = requests.get(TRDC_SITE_ROOT, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        html = r.text
    except Exception:
        return None
    # All PDF hrefs on the page.
    hrefs = re.findall(r'href=["\']([^"\']+\.pdf)["\']', html, re.I)
    if not hrefs:
        return None

    def _abs(u):
        if u.startswith("http"):
            return u
        if u.startswith("/"):
            return "http://trackingclubnsw.org.au" + u
        return TRDC_SITE_ROOT + u

    # Rank: must look like a tracking calendar; prefer target-year, then newest
    # version number, then anything mentioning calendar.
    cands = []
    for h in hrefs:
        low = h.lower()
        if "calendar" not in low and "tracking" not in low:
            continue
        yr_match = bool(year and str(year) in low)
        ver = 0
        mv = re.search(r"_v(\d+)", low)
        if mv:
            ver = int(mv.group(1))
        cands.append((yr_match, ver, _abs(h)))
    if not cands:
        return None
    # target-year first, then highest version.
    cands.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return cands[0][2]


def _candidate_urls(base_url, year=None):
    """Yield candidate TRDC PDF URLs to try, most-likely first.

    The filename encodes YEAR and VERSION, both of which change over time (a new
    file each season; bumped versions within a season). We probe:
      1. the explicit pin/base_url (last-known-good),
      2. for the TARGET year: versions high→low (newer first), plus a no-version
         shape,
      3. the SAME probes for the base_url's own year (covers the case where the
         target season's file isn't published yet and we should reuse last year's
         calendar as a stopgap).
    Dedupes while preserving order. This makes a rollover to 2027 — or a bump to
    _v3 — resolve automatically without a code change."""
    seen = set()

    def _emit(u):
        if u and u not in seen:
            seen.add(u)
            return u
        return None

    # Figure out which years to probe: the TARGET year first (so a new season is
    # preferred), then the pin's year as a fallback (last season's calendar may
    # still be the newest published file if this season's isn't out yet).
    years = []
    if year:
        years.append(int(year))
    m_pin = re.search(r"_(\d{4})_v\d+\.pdf$|_(\d{4})\.pdf$", base_url or "")
    pin_year = None
    if m_pin:
        pin_year = int(m_pin.group(1) or m_pin.group(2))
        if pin_year not in years:
            years.append(pin_year)

    # A small version window to probe per year. Start above the pin's version so
    # newer files are found first.
    m_ver = re.search(r"_v(\d+)\.pdf$", base_url or "")
    pin_ver = int(m_ver.group(1)) if m_ver else 2
    ver_order = list(range(pin_ver + 3, 0, -1))  # e.g. 5,4,3,2,1

    for y in years:
        for v in ver_order:
            got = _emit(TRDC_URL_TEMPLATE.format(year=y, ver=v))
            if got:
                yield got
        # no-version shape for this year
        got = _emit(TRDC_URL_NOVER_TEMPLATE.format(year=y))
        if got:
            yield got

    # Finally, the explicit pin/base_url as a last resort (usually already
    # covered by the year/version probes above, but kept for safety).
    got = _emit(base_url)
    if got:
        yield got


def _fetch_trdc_bytes(pdf_url, year):
    """Fetch the TRDC PDF, auto-discovering the right year/version if the pinned
    one is gone, and falling back to the last-known-good cached URL. Returns
    (bytes|None, resolved_url|None, discovered_bool)."""
    if not HAVE_REQUESTS:
        return None, None, False
    base = pdf_url or TRDC_PDF_URL
    first_hit = None  # any working PDF, even if not the target year
    for cand in _candidate_urls(base, year=year):
        try:
            r = requests.get(cand, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                discovered = (cand != base)
                # Prefer a URL whose filename carries the TARGET year. If this
                # candidate is the target year (or year unknown), take it now.
                if not year or f"_{year}" in cand or f"/{year}" in cand \
                        or f"Calendar_{year}" in cand:
                    if discovered:
                        print(f"[trdc] resolved to {cand}", file=sys.stderr)
                    return r.content, cand, discovered
                # Otherwise remember it as a stopgap but keep looking for the
                # target year's file.
                if first_hit is None:
                    first_hit = (r.content, cand, discovered)
        except Exception:
            pass
    # No target-year file found; use the best stopgap we saw (e.g. last season's
    # calendar still being the newest published file).
    if first_hit is not None:
        print(f"[trdc] target-year file not found; using {first_hit[1]}",
              file=sys.stderr)
        return first_hit
    # Pattern probing found nothing — the file may have been renamed. Discover
    # the link straight from the club homepage (rename-proof).
    discovered_url = _discover_from_site(year=year)
    if discovered_url and discovered_url != base:
        try:
            r = requests.get(discovered_url, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                print(f"[trdc] discovered via site link: {discovered_url}",
                      file=sys.stderr)
                return r.content, discovered_url, True
        except Exception:
            pass
    # Nothing worked via probing/discovery — try the auto-updating cache.
    try:
        import pdf_cache
        cached = pdf_cache.get_cached_url("trdc", year)
    except Exception:
        cached = None
    if cached:
        try:
            r = requests.get(cached, headers=HEADERS, timeout=TIMEOUT)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                print(f"[trdc] using cached last-known-good URL: {cached}",
                      file=sys.stderr)
                return r.content, cached, False
        except Exception:
            pass
    print("[trdc] could not fetch any TRDC PDF (pin, probe, or cache)",
          file=sys.stderr)
    return None, None, False


def parse_trdc_calendar(target_year=2026, pdf_bytes=None, pdf_url=None):
    """Return a list of tracking-family signal dicts:
        {"date": ISO, "club": str, "discipline": "Tracking"|"Track & Search"}
    Empty list on any failure (fail-safe)."""
    if not HAVE_PDFPLUMBER:
        print("[trdc] pdfplumber not installed; skipping", file=sys.stderr)
        return []

    resolved_url = None
    was_discovered = False
    if pdf_bytes is None:
        pdf_bytes, resolved_url, was_discovered = _fetch_trdc_bytes(
            pdf_url, target_year)
        if pdf_bytes is None:
            return []

    import io
    signals = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                tables = page.find_tables()
                if not tables:
                    continue
                for tbl in tables:
                    rows = tbl.extract()
                    if not rows:
                        continue
                    # Identify the tracking-family columns from the header row.
                    header = [(_clean_cell(c) or "").lower() for c in rows[0]]
                    date_col = None
                    tracking_cols = []
                    for i, h in enumerate(header):
                        if h == "date" or ("date" in h and date_col is None):
                            date_col = i
                        if any(hint in h for hint in _TRACKING_COL_HINTS):
                            tracking_cols.append(i)
                    # Fallbacks if header wording changed: date is usually col 1,
                    # tracking-family the two cols after it.
                    if date_col is None:
                        date_col = 1
                    if not tracking_cols:
                        tracking_cols = [date_col + 1, date_col + 2]

                    for row in rows[1:]:
                        if date_col >= len(row):
                            continue
                        date_iso = _parse_date(row[date_col], target_year)
                        if not date_iso:
                            continue
                        for ci in tracking_cols:
                            if ci >= len(row):
                                continue
                            raw = row[ci]
                            if raw is None:
                                continue
                            if _NON_EVENT_RE.match(raw or ""):
                                continue
                            club = _clean_cell(raw)
                            if not club:
                                continue
                            # CONSERVATIVE POLICY: TRDC does NOT reliably mark
                            # every Track & Search event (e.g. Grafton's T&S
                            # trials appear unlabelled), so an unlabelled cell is
                            # NOT proof of plain Tracking. We therefore emit a
                            # signal ONLY for cells explicitly marked
                            # "Track & Search"; unlabelled tracking rows are left
                            # to the disambiguator as no-signal, so the event
                            # stays combined ("Tracking / Track & Search") rather
                            # than being wrongly asserted as plain Tracking.
                            if not _TS_RE.search(raw):
                                continue
                            # strip the T&S marker words from the club text so
                            # club-token matching keys on the real club name
                            club_clean = _TS_RE.sub("", club).strip(" -|")
                            club_clean = re.sub(r"\s+", " ", club_clean).strip()
                            signals.append({
                                "date": date_iso,
                                "club": club_clean or club,
                                "discipline": "Track & Search",
                            })
    except Exception as e:
        print(f"[trdc] parse error ({e}); returning what we have",
              file=sys.stderr)

    # De-duplicate identical (date, club, discipline) rows.
    seen = set()
    uniq = []
    for s in signals:
        key = (s["date"], s["club"].lower(), s["discipline"])
        if key in seen:
            continue
        seen.add(key)
        uniq.append(s)

    from collections import Counter
    by_disc = Counter(s["discipline"] for s in uniq)
    print(f"[trdc] parsed {len(uniq)} tracking-family signals "
          f"({dict(by_disc)})", file=sys.stderr)
    # If we discovered a newer URL that yielded events, record it as the
    # last-known-good so future runs use it over the stale pin.
    if uniq and was_discovered and resolved_url:
        try:
            import pdf_cache
            pdf_cache.save_url("trdc", target_year, resolved_url)
        except Exception:
            pass
    return uniq


if __name__ == "__main__":
    import sys as _sys
    path = _sys.argv[1] if len(_sys.argv) > 1 else None
    if path:
        rows = parse_trdc_calendar(2026, pdf_bytes=open(path, "rb").read())
    else:
        rows = parse_trdc_calendar(2026)
    for s in rows:
        flag = "  <== T&S" if s["discipline"] == "Track & Search" else ""
        print(f"  {s['date']} {s['discipline']:14} {s['club']!r}{flag}")
    print(f"... total {len(rows)}")

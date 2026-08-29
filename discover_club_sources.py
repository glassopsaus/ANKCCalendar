#!/usr/bin/env python3
"""
Club-source discovery tool  (OFFLINE / one-off — NOT part of the daily scrape).

Goal: find dog clubs that expose a STRUCTURED, robustly-parseable events feed
(iCal/.ics or Google Calendar), so we can consider adding them as real sources.
It deliberately does NOT try to scrape prose, Facebook, or arbitrary HTML — only
machine-readable feeds are worth wiring into the pipeline.

WHAT IT DOES
  1. Enumerate affiliated clubs from each ANKC state body's public club
     directory. NOTE (learned from Dogs Victoria): these directories are often
     just Name/Phone/Email CONTACT tables with NO website links. Where a club's
     contact email is on its OWN domain (e.g. secretary@grcv.org.au) we derive a
     candidate website from that domain; clubs using free-host email
     (gmail/bigpond/etc.) give us no site to probe and are skipped.
  2. For each club with a derivable website, fetch its home page and look for a
     STRUCTURED feed signal:
       - an iCal/.ics link (webcal:// or *.ics)
       - a Google Calendar embed/ID (calendar.google.com/.../embed?src=...)
       - "The Events Calendar" WordPress plugin (exposes /events/?ical=1)
       - a linked schedule PDF (weaker signal, reported separately)
  3. Write a REPORT (JSON + CSV) ranking clubs by feed-signal strength, so a
     human can review the shortlist and decide which (if any) deserve a per-club
     parser.

HONEST EXPECTATION: only a minority of clubs have their own domain, and only
some of those expose a structured feed. The likely useful outcome is a short
list — most valuably, clubs on "The Events Calendar" plugin, since one parser
(the same shape we already use for Dogs ACT/Tasmania) would cover all of them.

WHY OFFLINE: this crawls hundreds of external sites, is slow, and its output is
a shortlist to review — not live event data. Run it occasionally, by hand:

    python discover_club_sources.py --out club_sources_report

Requires: requests, beautifulsoup4 (both already in the project). Fully
fail-safe: any site that errors is recorded as "unreachable" and skipped.
"""

import argparse
import csv
import json
import re
import sys
import time
from urllib.parse import urljoin, urlparse

try:
    import requests
    HAVE_REQUESTS = True
except Exception:
    HAVE_REQUESTS = False

try:
    from bs4 import BeautifulSoup
    HAVE_BS4 = True
except Exception:
    HAVE_BS4 = False

try:
    from playwright.sync_api import sync_playwright
    HAVE_PLAYWRIGHT = True
except Exception:
    HAVE_PLAYWRIGHT = False

HEADERS = {"User-Agent": "ANKCEventCheck/1.0 (+https://github.com/glassopsaus/ANKCCalendar)"}
TIMEOUT = 20
POLITE_DELAY_SEC = 1.0  # be a good citizen between external requests

# Free email hosts — an address here tells us nothing about a club website, so
# such clubs are skipped (no derivable site to probe). A club using its OWN
# domain in its contact email is the tractable signal we CAN act on.
_FREE_EMAIL_HOSTS = {
    "gmail.com", "outlook.com", "hotmail.com", "hotmail.co.uk", "yahoo.com",
    "yahoo.com.au", "ymail.com", "y7mail.com", "bigpond.com", "bigpond.net.au",
    "optusnet.com.au", "iinet.net.au", "internode.on.net", "live.com",
    "live.com.au", "icloud.com", "me.com", "aapt.net.au", "skymesh.com.au",
    "aussiebb.com.au", "msn.com", "hotkey.net.au", "westnet.com.au",
}

_EMAIL_RE = re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')

# ---------------------------------------------------------------------------
# State-body club directories. The exact list markup differs per state and some
# are JS-driven; extraction is best-effort. Update the selectors/URLs as needed
# after a first run shows what each page actually contains. Each entry:
#   name, directory_url, (optional) link_pattern to keep only club links.
# ---------------------------------------------------------------------------
DIRECTORIES = [
    # VIC: CONFIRMED — Name/Phone/Email table, sites derived from own-domain emails.
    {"state": "VIC", "url": "https://dogsvictoria.org.au/clubs/find-a-club/"},
    # QLD: real affiliated-club listing pages (contact + some website/email).
    # The "all affiliated" page aggregates conformation + dog-sports clubs.
    {"state": "QLD", "url": "https://dogsqueensland.org.au/clubs/all-affiliated-clubs/"},
    # QLD dog-sports clubs specifically (obedience/agility/tracking) — richer in
    # own-domain sites; scanned as a second QLD entry (reported as QLD).
    {"state": "QLD", "url": "https://dogsqueensland.org.au/clubs/all-affiliated-clubs/affiliated-dog-sports-clubs/"},
    # SA: correct URL (user-confirmed). Dogs SA runs the same "Famous Digital"
    # CMS as VIC, so the find-a-club format MAY be a VIC-like Name/Phone/Email
    # table — but this page has NOT been fetched/confirmed. If the run shows a
    # low/zero SA club count, the format differs and needs per-state tuning.
    {"state": "SA",  "url": "https://www.dogssa.com.au/Clubs/find-a-club"},
    # WA: DogsWest old ASP site — the clubs index links to one detail page per
    # club, and the club's real website/email is on that detail page. So WA needs
    # a two-level crawl (index → each detail page). Flagged two_level below.
    {"state": "WA",  "url": "https://www.dogswest.com/dogswest/Clubs-Agility_Obedience__Training_Clubs.htm", "two_level": "wa"},
    # NSW: the find-a-club list is JavaScript-rendered ("Loading clubs..."), so
    # it needs a headless browser. We capture the club-list JSON the page fetches.
    {"state": "NSW", "url": "https://www.dogsnsw.org.au/clubs/find-a-club/", "js": True},
    # TAS / ACT / NT: small bodies; best-available landing pages. Likely thin.
    {"state": "TAS", "url": "https://tasdogs.com/clubs/"},
    {"state": "ACT", "url": "https://dogsact.org.au/clubs/"},
    {"state": "NT",  "url": "https://www.dogsnt.com.au/clubs/affiliated-clubs/"},
]
# Confidence: VIC confirmed; QLD pages are real listings; SA/WA are the correct
# pages but their markup is unconfirmed (extraction may need tuning); NSW/TAS/
# ACT/NT have no clean state-body club list, so expect little. A run will show
# per-state club counts; refine any that come back low/empty.

# ---- Feed-signal detectors -------------------------------------------------
_ICS_RE = re.compile(r'(webcal://[^\s"\'<>]+|https?://[^\s"\'<>]+\.ics(?:\?[^\s"\'<>]*)?)', re.I)
_GCAL_RE = re.compile(r'(https?://calendar\.google\.com/calendar/[^\s"\'<>]+)', re.I)
_GCAL_SRC_RE = re.compile(r'[?&]src=([^&\s"\'<>]+)', re.I)
# "The Events Calendar" WordPress plugin — very common on club sites, and it
# exposes a clean iCal at /events/?ical=1 (and a REST API). Detect its markers.
_TEC_RE = re.compile(r'tribe-events|the-events-calendar|/events/?\?ical|tribe_events', re.I)
_SCHEDULE_PDF_RE = re.compile(r'href=["\']([^"\']+\.pdf)["\']', re.I)
_SCHEDULE_WORD_RE = re.compile(r'schedule|calendar|events|trial|fixtures', re.I)


def _get(url):
    """Fetch a URL, returning text or None. Never raises."""
    if not HAVE_REQUESTS:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
            return r.text
    except Exception:
        return None
    return None


def _get_js(url, wait_seconds=12):
    """For JavaScript-rendered directories (e.g. Dogs NSW 'Loading clubs...'),
    load the page in a headless browser and BOTH (a) capture any JSON responses
    the page fetches (the club list usually arrives via XHR) and (b) return the
    fully rendered HTML. Returns (rendered_html|None, [json_objects]).
    Never raises; returns (None, []) if Playwright is unavailable."""
    if not HAVE_PLAYWRIGHT:
        print("[discover]   (JS page needs Playwright — not installed; skipping)",
              file=sys.stderr)
        return None, []
    captured = []

    def _on_response(resp):
        try:
            ct = (resp.headers or {}).get("content-type", "")
            if "json" not in ct.lower():
                return
            body = resp.json()
            captured.append(body)
        except Exception:
            return

    html = None
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=True)
            except Exception as e:
                print(f"[discover]   Chromium launch failed: {e}", file=sys.stderr)
                return None, []
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()
            page.on("response", _on_response)
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
            except Exception:
                pass
            page.wait_for_timeout(wait_seconds * 1000)
            try:
                html = page.content()
            except Exception:
                html = None
            browser.close()
    except Exception as e:
        print(f"[discover]   JS fetch error: {e}", file=sys.stderr)
        return html, captured
    return html, captured


def _clubs_from_json(objs):
    """Scan captured JSON structures for club records and pull (name, website,
    email). Very tolerant: walks any nested dict/list, treating a dict as a club
    if it has a name-like key plus a website/email-like key. Returns a list of
    {name, url|None, email_domain|None, candidate_sites[]}."""
    out = []
    seen = set()
    NAME_KEYS = ("name", "clubname", "club_name", "title", "clubtitle")
    WEB_KEYS = ("website", "web", "url", "weburl", "website_url", "homepage")
    EMAIL_KEYS = ("email", "emailaddress", "email_address", "contactemail")

    def _first(d, keys):
        for k in d:
            if k.lower() in keys and d[k]:
                return str(d[k]).strip()
        return None

    def _walk(node):
        if isinstance(node, dict):
            name = _first(node, NAME_KEYS)
            web = _first(node, WEB_KEYS)
            email = _first(node, EMAIL_KEYS)
            if name and (web or email) and len(name) >= 4:
                dom = None
                if email:
                    mm = _EMAIL_RE.search(email)
                    if mm:
                        dom = mm.group(1).lower()
                cands = []
                if web:
                    w = web if web.startswith("http") else "https://" + web
                    host = urlparse(w).netloc.lower()
                    if host and "facebook" not in host and "instagram" not in host:
                        cands.append(w)
                cands += _candidate_site_from_email(dom)
                key = name.lower()
                if cands and key not in seen:
                    seen.add(key)
                    out.append({"name": name, "url": (cands[0] if cands else None),
                                "email_domain": dom, "candidate_sites": cands})
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    for o in objs:
        _walk(o)
    return out


def _wa_detail_links(index_html, base_url):
    """WA (DogsWest) lists clubs on an index page that links to one detail page
    per club: '...Clubs-Agility_Obedience__Training_Clubs-<Club_Name>.htm'.
    Return [(club_name, detail_url)] for those child links only."""
    if not (HAVE_BS4 and index_html):
        return []
    soup = BeautifulSoup(index_html, "html.parser")
    out = []
    seen = set()
    # The index URL's own filename, e.g. 'Clubs-Agility_Obedience__Training_Clubs'
    idx_stem = urlparse(base_url).path.rsplit("/", 1)[-1].replace(".htm", "")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(base_url, href)
        path = urlparse(full).path.rsplit("/", 1)[-1]
        # A club detail page extends the index stem with '-<ClubName>.htm'
        if path.startswith(idx_stem + "-") and path.endswith(".htm"):
            name = a.get_text(" ", strip=True)
            if not name or len(name) < 4:
                # derive a name from the slug if link text is empty
                name = path[len(idx_stem) + 1:-4].replace("_", " ").strip()
            key = full.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((name, full))
    return out


def _wa_club_from_detail(name, detail_html, detail_url):
    """Extract a club's real website/email from a DogsWest detail page. The page
    is mostly DogsWest nav; the club's own site is the surviving non-dogswest,
    non-social external link. Returns a club dict or None."""
    if not (HAVE_BS4 and detail_html):
        return None
    soup = BeautifulSoup(detail_html, "html.parser")
    website = None
    email_dom = None
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith("mailto:"):
            mm = _EMAIL_RE.search(href)
            if mm and not email_dom:
                email_dom = mm.group(1).lower()
            continue
        h = urlparse(urljoin(detail_url, href)).netloc.lower()
        if not h:
            continue
        # Skip DogsWest itself, socials, google, sponsors.
        if any(s in h for s in ("dogswest", "facebook", "instagram", "google",
                                "youtube", "twitter", "royalcanin")):
            continue
        if not website:
            website = urljoin(detail_url, href)
    # email may be plain text
    if not email_dom:
        mm = _EMAIL_RE.search(soup.get_text(" ", strip=True))
        if mm:
            email_dom = mm.group(1).lower()
    cands = []
    if website:
        cands.append(website)
    cands += _candidate_site_from_email(email_dom)
    if not cands:
        return None
    return {"name": name, "url": website, "email_domain": email_dom,
            "candidate_sites": cands}


def _candidate_site_from_email(email_host):
    """A club whose contact email is on its OWN domain gives us a candidate
    website: https://<domain> and https://www.<domain>. Free-host emails
    (gmail etc.) give nothing. Returns a list of candidate URLs (may be empty)."""
    host = (email_host or "").strip().lower().rstrip(".")
    if not host or host in _FREE_EMAIL_HOSTS:
        return []
    # Some clubs use a subdomain in email; try both the bare and www forms.
    return [f"https://{host}", f"https://www.{host}"]


def extract_clubs(directory_html, base_url):
    """Best-effort club enumeration from a state directory. Handles two shapes:
      (a) pages that LINK to club websites (kept if the link looks like a club),
      (b) contact tables/lists with Name + Email but NO website link — in which
          case we derive a candidate site from the email's domain when it's the
          club's OWN domain (free-host emails yield no site).
    Returns a list of {name, url|None, email_domain|None, candidate_sites[]}."""
    if not (HAVE_BS4 and directory_html):
        return []
    soup = BeautifulSoup(directory_html, "html.parser")
    out = []
    seen = set()
    SKIP_HOSTS = ("facebook.com", "instagram.com", "twitter.com", "x.com",
                  "youtube.com", "linkedin.com", "google.com", "royalcanin",
                  "pd.com.au", "dogsvictoria", "dogsnsw", "dogsqueensland",
                  "dogssa", "dogswest", "tasdogs", "dogsact", "dogsnt")
    CLUBWORD = re.compile(r"club|kennel|association|society|obedience|agility|"
                          r"canine|dog|training|breed|retriev|terrier|spaniel|"
                          r"shepherd|hound|gundog|herding|scent", re.I)

    # --- Shape (c): club NAME anchors (bold headings OR h2/h3/h4) followed by
    # "Website:"/"Email:" links, within the same block. Covers Dogs Queensland
    # (<strong> names) and Dogs NT (<h2> names). We take the club name and prefer
    # its explicit Website link; else derive from its email domain. Handled
    # BEFORE the generic-link shape so the real club name wins.
    for tag in soup.find_all(["strong", "b", "h2", "h3", "h4"]):
        name = tag.get_text(" ", strip=True)
        # strip a trailing "(Obedience/Agility Club)" descriptor from the name
        name_clean = re.sub(r"\s*\([^)]*\)\s*$", "", name).strip()
        if not name_clean or len(name_clean) < 5 or not CLUBWORD.search(name_clean):
            continue
        # Collect the links + email that belong to this club. Two layouts:
        #   (i) name is a heading (NT: <h2>) → scan ONLY following siblings until
        #       the next heading (that's this club's detail block); do NOT fall
        #       back to the parent, which holds every club and would leak links.
        #   (ii) name inside a block (QLD: <strong> in a <p>) → scan the parent.
        links = []
        texts = []
        if tag.name in ("h2", "h3", "h4"):
            for sib in tag.find_next_siblings():
                if getattr(sib, "name", None) in ("h2", "h3", "h4"):
                    break
                if hasattr(sib, "find_all"):
                    links.extend(sib.find_all("a", href=True))
                    texts.append(sib.get_text(" ", strip=True))
        else:
            # inline name (strong/b): scan its immediate block only
            block = tag.find_parent(["p", "li", "div", "td"])
            if block is not None:
                links.extend(block.find_all("a", href=True))
                texts.append(block.get_text(" ", strip=True))

        website = None
        email_dom = None
        for a in links:
            href = a["href"].strip()
            if href.startswith("mailto:"):
                mm = _EMAIL_RE.search(href)
                if mm and not email_dom:
                    email_dom = mm.group(1).lower()
                continue
            h = urlparse(urljoin(base_url, href)).netloc.lower()
            if h and not any(s in h for s in SKIP_HOSTS) and not website:
                website = urljoin(base_url, href)
        if not email_dom:
            mm = _EMAIL_RE.search(" ".join(texts))
            if mm:
                email_dom = mm.group(1).lower()
        cands = []
        if website:
            cands.append(website)
        cands += _candidate_site_from_email(email_dom)
        if not cands:
            continue
        key = ("blk", name_clean.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name_clean, "url": website,
                    "email_domain": email_dom, "candidate_sites": cands})

    # Candidate sites already captured by shape (c), so shape (a) doesn't
    # re-add them under a URL-as-name.
    captured_sites = set()
    for rec in out:
        for s in rec["candidate_sites"]:
            captured_sites.add(urlparse(s).netloc.lower().replace("www.", ""))

    # --- Shape (a): explicit external links that look like club sites --------
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True)
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        host = urlparse(full).netloc.lower()
        if not host or any(s in host for s in SKIP_HOSTS):
            continue
        # Skip if a club block (shape c) already captured this site.
        if host.replace("www.", "") in captured_sites:
            continue
        # Require the LINK TEXT to look like a club name — not a bare URL/"Website".
        if not text or len(text) < 4 or not CLUBWORD.search(text):
            continue
        if re.match(r"^\s*(https?://|www\.)", text, re.I) or "." in text.split()[0]:
            continue  # link text is a URL, not a club name
        key = ("link", text.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": text, "url": full, "email_domain": None,
                    "candidate_sites": [full]})

    # --- Shape (b): contact tables/lists (Name + Email, no website) ----------
    # Walk table rows first; fall back to scanning text lines for name+email.
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        row_text = " ".join(cells)
        m = _EMAIL_RE.search(row_text)
        if not m:
            continue
        # club name = first cell that looks club-ish
        name = None
        for c in cells:
            if CLUBWORD.search(c) and "@" not in c and len(c) >= 4:
                name = c
                break
        if not name:
            name = cells[0] if cells else "(unknown club)"
        dom = m.group(1).lower()
        cands = _candidate_site_from_email(dom)
        key = ("tbl", name.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": name, "url": None, "email_domain": dom,
                    "candidate_sites": cands})
    return out


def probe_club_site(url):
    """Fetch a club's page and detect structured-feed signals. Returns a dict
    of findings (never raises)."""
    result = {
        "reachable": False,
        "ics": [],
        "gcal": [],
        "the_events_calendar": False,
        "schedule_pdfs": [],
        "score": 0,
    }
    html = _get(url)
    if html is None:
        return result
    result["reachable"] = True

    result["ics"] = sorted(set(_ICS_RE.findall(html)))[:5]
    result["gcal"] = sorted(set(_GCAL_RE.findall(html)))[:5]
    result["the_events_calendar"] = bool(_TEC_RE.search(html))
    # schedule PDFs whose link/URL hints at events/schedule (weaker signal)
    pdfs = []
    for m in _SCHEDULE_PDF_RE.findall(html):
        if _SCHEDULE_WORD_RE.search(m):
            pdfs.append(urljoin(url, m))
    result["schedule_pdfs"] = sorted(set(pdfs))[:5]

    # Score: structured feeds are worth most; TEC exposes an iCal so counts high;
    # schedule PDFs are a weak, parser-heavy fallback.
    score = 0
    if result["ics"]:
        score += 100
    if result["gcal"]:
        score += 80
    if result["the_events_calendar"]:
        score += 70
    if result["schedule_pdfs"]:
        score += 20
    result["score"] = score
    return result


def discover(directories, limit_per_state=None, delay=POLITE_DELAY_SEC):
    """Run the full discovery pass. Returns a list of club findings."""
    findings = []
    probed_sites = set()  # candidate URLs already probed (cross-directory dedup)
    for d in directories:
        state, dir_url = d["state"], d["url"]
        is_js = bool(d.get("js"))
        two_level = d.get("two_level")
        print(f"[discover] {state}: fetching directory {dir_url}"
              f"{' (JS/headless)' if is_js else ''}"
              f"{' (two-level)' if two_level else ''}", file=sys.stderr)
        if two_level == "wa":
            idx_html = _get(dir_url)
            if not idx_html:
                print(f"[discover] {state}: index unreachable/empty",
                      file=sys.stderr)
                continue
            detail_links = _wa_detail_links(idx_html, dir_url)
            print(f"[discover] {state}: {len(detail_links)} club detail pages; "
                  f"fetching each…", file=sys.stderr)
            clubs = []
            cap = limit_per_state or len(detail_links)
            for cname, curl in detail_links[:cap]:
                time.sleep(delay)
                dhtml = _get(curl)
                rec = _wa_club_from_detail(cname, dhtml, curl) if dhtml else None
                if rec:
                    clubs.append(rec)
            if not clubs:
                print(f"[discover] {state}: no club sites found on detail pages",
                      file=sys.stderr)
                continue
        elif is_js:
            dir_html, json_objs = _get_js(dir_url)
            clubs = _clubs_from_json(json_objs)
            # If JSON gave nothing, fall back to the rendered DOM.
            if not clubs and dir_html:
                clubs = extract_clubs(dir_html, dir_url)
            if not clubs:
                print(f"[discover] {state}: no clubs captured from JS page "
                      f"(no JSON club list found; may need endpoint tuning)",
                      file=sys.stderr)
                continue
        else:
            dir_html = _get(dir_url)
            if not dir_html:
                print(f"[discover] {state}: directory unreachable/empty "
                      f"(may be JS-driven — needs manual URL)", file=sys.stderr)
                continue
            clubs = extract_clubs(dir_html, dir_url)
        n_with_site = sum(1 for c in clubs if c["candidate_sites"])
        print(f"[discover] {state}: {len(clubs)} clubs "
              f"({n_with_site} with a derivable website; the rest use free-host "
              f"email and can't be probed)", file=sys.stderr)
        # Only probe clubs we can actually reach (have candidate sites), and
        # skip any whose candidate site we've already probed in another entry
        # (e.g. a club listed on both QLD pages).
        probeable = []
        for c in clubs:
            if not c["candidate_sites"]:
                continue
            if any(s in probed_sites for s in c["candidate_sites"]):
                continue
            probeable.append(c)
        if limit_per_state:
            probeable = probeable[:limit_per_state]
        for c in probeable:
            for s in c["candidate_sites"]:
                probed_sites.add(s)
            probe = {"reachable": False, "ics": [], "gcal": [],
                     "the_events_calendar": False, "schedule_pdfs": [], "score": 0}
            resolved = None
            for cand in c["candidate_sites"]:
                time.sleep(delay)
                p = probe_club_site(cand)
                if p["reachable"]:
                    probe = p
                    resolved = cand
                    break
            rec = {"state": state, "name": c["name"],
                   "url": resolved or (c["candidate_sites"] or [None])[0],
                   "email_domain": c["email_domain"], **probe}
            findings.append(rec)
            if probe["score"] > 0:
                sig = []
                if probe["ics"]:
                    sig.append("iCal")
                if probe["gcal"]:
                    sig.append("GoogleCal")
                if probe["the_events_calendar"]:
                    sig.append("TheEventsCalendar")
                if probe["schedule_pdfs"]:
                    sig.append("SchedulePDF")
                print(f"[discover]   HIT {c['name'][:40]!r}: {', '.join(sig)}",
                      file=sys.stderr)
    return findings


def write_report(findings, out_base):
    """Write JSON (full) and CSV (shortlist of scoring>0) reports."""
    findings_sorted = sorted(findings, key=lambda r: r["score"], reverse=True)
    with open(out_base + ".json", "w") as f:
        json.dump(findings_sorted, f, indent=2)
    shortlist = [r for r in findings_sorted if r["score"] > 0]
    with open(out_base + ".csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["score", "state", "name", "url", "email_domain", "ics",
                    "gcal", "the_events_calendar", "schedule_pdfs"])
        for r in shortlist:
            w.writerow([r["score"], r["state"], r["name"], r.get("url"),
                        r.get("email_domain") or "",
                        " | ".join(r["ics"]), " | ".join(r["gcal"]),
                        r["the_events_calendar"], " | ".join(r["schedule_pdfs"])])
    print(f"\n[discover] wrote {out_base}.json ({len(findings_sorted)} clubs) "
          f"and {out_base}.csv ({len(shortlist)} with a feed signal)",
          file=sys.stderr)
    # Console summary.
    from collections import Counter
    by_state = Counter(r["state"] for r in shortlist)
    print("[discover] shortlist by state:", dict(by_state), file=sys.stderr)
    n_ics = sum(1 for r in shortlist if r["ics"])
    n_gcal = sum(1 for r in shortlist if r["gcal"])
    n_tec = sum(1 for r in shortlist if r["the_events_calendar"])
    print(f"[discover] structured feeds found: iCal={n_ics}, "
          f"GoogleCal={n_gcal}, TheEventsCalendar={n_tec}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="club_sources_report",
                    help="output file base name (writes .json and .csv)")
    ap.add_argument("--limit", type=int, default=None,
                    help="max clubs per state (for a quick trial run)")
    ap.add_argument("--delay", type=float, default=POLITE_DELAY_SEC,
                    help="seconds between external requests")
    ap.add_argument("--state", default=None,
                    help="only scan this state code (e.g. VIC)")
    args = ap.parse_args()

    if not (HAVE_REQUESTS and HAVE_BS4):
        print("ERROR: needs 'requests' and 'beautifulsoup4' installed.",
              file=sys.stderr)
        sys.exit(1)

    dirs = DIRECTORIES
    if args.state:
        dirs = [d for d in dirs if d["state"].upper() == args.state.upper()]
        if not dirs:
            print(f"No directory configured for state {args.state}", file=sys.stderr)
            sys.exit(1)

    findings = discover(dirs, limit_per_state=args.limit, delay=args.delay)
    write_report(findings, args.out)


if __name__ == "__main__":
    main()

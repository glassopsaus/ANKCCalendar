#!/usr/bin/env python3
"""
Cross-check matcher  (stage 2b of the source-of-truth rebuild).

INPUT:
  - governing_events: authoritative events from the governing-body calendars
    (Dogs NSW PDF, vicdog, Dogs ACT, Dogs Tasmania). Each has at least:
      {title, club?, start, end, region, category, provider?}
    where `provider` (NSW PDF only) names the entry system if known.
  - sm_listings: Show Manager listings from show_manager.scrape_show_manager():
      {club, date, discipline, region, status, closes, detail_url}

OUTPUT: the same governing_events, each annotated with:
  status        one of: "open", "closed", "cancelled", "approved_not_open",
                        "entries_via_provider"
  status_label  human text, e.g. "Open (verified)", "Closed (verified)",
                "Approved; not open (unverified)", "Entries via Top Dog (unverified)"
  verified      bool  (True only when confirmed against an entry system)
  entry_url     link to enter/verify, when known
  closes        ISO date entries close, when known

STATUS LOGIC (agreed):
  - "Entries currently open" only counts as OPEN when confirmed on an entry
    system we can actually read (Show Manager). Verified.
  - Found on Show Manager but entries closed / cancelled -> closed / cancelled.
    Verified.
  - Not found on Show Manager, but the governing calendar names a provider
    (e.g. Top Dog, which we can't reliably read) -> "Entries via X (unverified)".
  - Not found anywhere and no provider named -> "Approved; not open (unverified)".
  - The word "unverified" is always shown when we could not confirm live state.

MATCHING is fuzzy: (same region) AND (date overlap within a small window) AND
(same discipline family) AND (club-name token overlap). Tracking trials often
span multiple days and the two sources phrase clubs differently, so we don't
require exact equality.
"""

import re
import sys
import datetime as dt


DISCIPLINE_FAMILY = {
    "Tracking / Track & Search": "track",
    "Tracking": "track",
    "Track & Search": "track",
    "Scent Work": "scent",
}

# words to ignore when comparing club names
CLUB_STOPWORDS = {
    "the", "of", "and", "inc", "club", "dog", "dogs", "training", "kennel",
    "obedience", "district", "districts", "association", "society", "assoc",
    "canine", "all", "breeds", "sports", "sporting", "committee", "working",
    "party", "trial", "trials", "test", "tests", "group", "region",
}


def _norm_tokens(name):
    toks = re.findall(r"[a-z0-9]+", (name or "").lower())
    return set(t for t in toks if t not in CLUB_STOPWORDS and len(t) > 1)


def _family(cat):
    return DISCIPLINE_FAMILY.get(cat, cat)


def _date_close(a_iso, b_iso, window_days=1):
    """True if two ISO dates are within +/- window_days (handles multi-day)."""
    try:
        a = dt.date.fromisoformat(a_iso)
        b = dt.date.fromisoformat(b_iso)
    except (ValueError, TypeError):
        return False
    return abs((a - b).days) <= window_days


def _provider_label(provider):
    p = (provider or "").strip()
    if not p:
        return None
    return f"Entries via {p} (unverified)"


def _index_listings(sm_listings):
    """Group Show Manager listings by (region, family) for quick lookup."""
    idx = {}
    for L in sm_listings:
        key = (L["region"], _family(L["discipline"]))
        idx.setdefault(key, []).append(L)
    return idx


def match_events(governing_events, sm_listings, additive=False,
                 source_label="Show Manager"):
    """Annotate governing_events with entry status from sm_listings.

    additive=False (default): full pass. Every event is (re)labelled; events
        with no matching listing fall back to provider/approved labels. Use for
        the primary source (Show Manager).
    additive=True: only IMPROVE events. An event already verified by an earlier
        pass is never downgraded, and events with no match here are left exactly
        as they were (no fall-through relabelling). Use for a second source
        (e.g. Dogz Online) layered on top of the first.
    source_label: name used in the "Listed on <source> (unverified)" label.
    """
    idx = _index_listings(sm_listings)
    n_open = n_closed = n_cancel = n_provider = n_approved = 0
    matched_listing_ids = set()

    for ev in governing_events:
        # In additive mode, don't touch an event that's already verified.
        if additive and ev.get("verified"):
            continue
        region = ev.get("region")
        fam = _family(ev.get("category", ""))
        ev_start = ev.get("start")
        ev_end = ev.get("end", ev_start)
        ev_tokens = _norm_tokens(ev.get("club") or ev.get("title"))

        candidates = idx.get((region, fam), [])
        best = None
        for L in candidates:
            # date: SM listing date should fall within the event's day span
            # (or within +/-1 day to absorb one-day-off listings)
            in_span = False
            try:
                d = dt.date.fromisoformat(L["date"])
                s = dt.date.fromisoformat(ev_start)
                e = dt.date.fromisoformat(ev_end)
                in_span = (s - dt.timedelta(days=1)) <= d <= (e + dt.timedelta(days=1))
            except (ValueError, TypeError):
                in_span = _date_close(L["date"], ev_start)
            if not in_span:
                continue
            # club name overlap
            l_tokens = _norm_tokens(L["club"])
            overlap = len(ev_tokens & l_tokens)
            if overlap >= 1:
                # prefer the strongest overlap
                if best is None or overlap > best[0]:
                    best = (overlap, L)

        if best:
            L = best[1]
            matched_listing_ids.add(id(L))
            st = L["status"]
            if st == "open":
                ev["status"] = "open"
                ev["status_label"] = "Open (verified)"
                ev["verified"] = True
                n_open += 1
            elif st == "cancelled":
                ev["status"] = "cancelled"
                ev["status_label"] = "Cancelled (verified)"
                ev["verified"] = True
                ev["cancelled"] = True
                n_cancel += 1
            elif st == "closed":
                ev["status"] = "closed"
                ev["status_label"] = "Closed (verified)"
                ev["verified"] = True
                n_closed += 1
            else:  # unknown status on listing: treat as listed-but-unconfirmed
                ev["status"] = "entries_via_provider"
                ev["status_label"] = f"Listed on {source_label} (unverified)"
                ev["verified"] = False
                n_provider += 1
            ev["closes"] = L.get("closes")
            ev["entry_url"] = L.get("detail_url")
            # Carry the schedule PDF link and venue address if this source
            # provides them; don't clobber existing values with None.
            if L.get("schedule_url"):
                ev["schedule_url"] = L["schedule_url"]
            if L.get("address"):
                ev["address"] = L["address"]
                # If the event's displayed location is only a bare state (as
                # governing-body sources set it), upgrade it to the real venue
                # address from Show Manager. Mirrors the merge cross-fill.
                _loc = (ev.get("location") or "").strip()
                _bare = re.sub(r"[^a-z ]", "", _loc.lower()).strip()
                _BARE_SET = {"", "act", "nsw", "qld", "vic", "wa", "sa", "tas",
                             "nt", "victoria", "queensland", "new south wales",
                             "western australia", "south australia", "tasmania",
                             "australian capital territory", "northern territory"}
                if _bare in _BARE_SET:
                    ev["location"] = L["address"]
            # Record that this source corroborated the event, so the two-badge
            # "verified = 2+ sources / entry platform" rule counts the match.
            if source_label:
                srcs = ev.setdefault("sources", [])
                if source_label not in srcs:
                    srcs.append(source_label)
            continue

        # No match in this source.
        if additive:
            # Layered pass: leave the event's existing status untouched.
            continue
        # Primary pass: fall back to the PDF-named provider, if any.
        prov_label = _provider_label(ev.get("provider"))
        if prov_label:
            ev["status"] = "entries_via_provider"
            ev["status_label"] = prov_label
            ev["verified"] = False
            n_provider += 1
        else:
            ev["status"] = "approved_not_open"
            ev["status_label"] = "Approved; not open (unverified)"
            ev["verified"] = False
            n_approved += 1

    print(f"[match] {len(governing_events)} events: "
          f"{n_open} open, {n_closed} closed, {n_cancel} cancelled (verified); "
          f"{n_provider} via-provider, {n_approved} approved-not-open (unverified)",
          file=sys.stderr)
    # Stash which listings were consumed so callers can fill gaps with the rest.
    match_events.last_matched_ids = matched_listing_ids
    return governing_events


def _status_label_for(listing, source_label="Show Manager"):
    st = listing.get("status")
    if st == "open":
        return "open", "Open (verified)", True
    if st == "closed":
        return "closed", "Closed (verified)", True
    if st == "cancelled":
        return "cancelled", "Cancelled (verified)", True
    return ("entries_via_provider",
            f"Listed on {source_label} (unverified)", False)


def events_from_unmatched_listings(sm_listings, matched_ids, region_color=None,
                                   existing_events=None,
                                   source_name="Show Manager",
                                   default_url="https://www.showmanager.com.au/events/publicevents?g=2"):
    """Build calendar events from listings that did NOT match any governing-body
    event. Used to fill coverage gaps (e.g. SA, and TAS/ACT where the governing
    feeds are thin). source_name attributes the events to their origin (Show
    Manager or Dogz Online) in the source field and status label.

    A listing is only added if NO existing event already occupies the same
    region + overlapping date + discipline family. This collision check is
    independent of club names, so a governing-body event whose club name simply
    didn't token-match its listing is NOT duplicated here.
    """
    region_color = region_color or {}
    existing_events = existing_events or []

    # Build a quick lookup of occupied (region, family) -> list of date spans.
    occupied = {}
    for ev in existing_events:
        key = (ev.get("region"), _family(ev.get("category", "")))
        try:
            s = dt.date.fromisoformat(ev.get("start"))
            e = dt.date.fromisoformat(ev.get("end", ev.get("start")))
        except (ValueError, TypeError):
            continue
        occupied.setdefault(key, []).append((s, e))

    def collides(region, fam, date_iso):
        try:
            d = dt.date.fromisoformat(date_iso)
        except (ValueError, TypeError):
            return False
        for (s, e) in occupied.get((region, fam), []):
            if (s - dt.timedelta(days=1)) <= d <= (e + dt.timedelta(days=1)):
                return True
        return False

    out = []
    n_skip = 0
    for L in sm_listings:
        if id(L) in matched_ids:
            continue
        if collides(L["region"], _family(L["discipline"]), L["date"]):
            n_skip += 1
            continue
        status, label, verified = _status_label_for(L, source_name)
        title = L["club"]
        cat = L["discipline"]
        if cat not in title:
            title = f"{title} \u2013 {cat}"
        ev = {
            "title": title,
            "club": L["club"],
            "start": L["date"],
            "end": L["date"],
            "location": L.get("address") or "",
            "url": L.get("detail_url") or default_url,
            "category": cat,
            "region": L["region"],
            "source": source_name,
            "sources": [source_name],
            "color": region_color.get(L["region"]),
            "cancelled": (status == "cancelled"),
            "status": status,
            "status_label": label,
            "verified": verified,
            "closes": L.get("closes"),
            "entry_url": L.get("detail_url"),
            "schedule_url": L.get("schedule_url"),
            "address": L.get("address"),
        }
        out.append(ev)
    print(f"[gapfill] added {len(out)} {source_name}-sourced events "
          f"(skipped {n_skip} that collided with an existing event)",
          file=sys.stderr)
    return out


if __name__ == "__main__":
    # Self-test with synthetic data exercising every status path.
    gov = [
        {"title": "Tracking Club of Victoria", "club": "Tracking Club of Victoria",
         "start": "2026-08-08", "end": "2026-08-08", "region": "VIC",
         "category": "Tracking / Track & Search"},
        {"title": "North East Tracking & Scent Club", "club": "North East Tracking & Scent Club",
         "start": "2026-05-23", "end": "2026-05-25", "region": "VIC",
         "category": "Tracking / Track & Search", "provider": "Top Dog Events"},
        {"title": "Some Approved Club", "club": "Some Approved Club",
         "start": "2026-09-01", "end": "2026-09-01", "region": "NSW",
         "category": "Scent Work"},  # no provider, not on SM -> approved not open
        {"title": "Dogs ACT", "club": "Dogs ACT",
         "start": "2026-08-09", "end": "2026-08-09", "region": "ACT",
         "category": "Scent Work", "provider": "Show Manager"},
    ]
    sm = [
        {"club": "Tracking Club of Victoria", "date": "2026-08-08",
         "discipline": "Tracking / Track & Search", "region": "VIC",
         "status": "open", "closes": "2026-07-27",
         "detail_url": "https://www.showmanager.com.au/events/PublicEvents/Details/48855",
         "event_id": "48855"},
        {"club": "Dogs ACT - Scent Work", "date": "2026-08-09",
         "discipline": "Scent Work", "region": "ACT",
         "status": "open", "closes": "2026-07-31",
         "detail_url": "https://www.showmanager.com.au/events/PublicEvents/Details/49281",
         "event_id": "49281"},
    ]
    out = match_events(gov, sm)
    for e in out:
        print(f"{e['start']}  {e['region']:3} {e['category']:26} "
              f"{e['status_label']:38} verified={e['verified']}")

# The National Dog Sports Calendar

One page that merges **every ANKC show and trial** — all disciplines, all
Australian states and territories — into a single calendar, and refreshes
**once a day**, automatically, for free. Each event shows a plain answer to the
question that matters when you find one: **can I still enter?**

## How it works

```
 ┌───────────────┐   daily    ┌────────────────────────┐   reads    ┌──────────────┐
 │ GitHub Action │ ─────────► │ docs/events-<year>.json │ ─────────► │  index.html  │
 │  scrape.py    │  (cron)    │  + events.json (current)│            │  (the page)  │
 │               │            │  + years.json (manifest)│            │              │
 └───────────────┘            └────────────────────────┘            └──────────────┘
```

A browser page cannot scrape other websites on a schedule by itself (no server,
cross-origin blocked, nothing runs when the tab is closed). So the daily scrape
runs in a **GitHub Action**, which writes one JSON file per year plus a small
`years.json` manifest. The page reads the manifest, defaults to the current
year, and offers a year menu to switch. Hosting is **GitHub Pages** (free). The
daily run is scheduled at `30 15 * * *` UTC — **01:30 AEST** (02:30 during AEDT;
cron does not follow daylight saving).

## Multi-year

The scraper builds several years in one run — by default the **previous,
current, and next** year (override with the `YEARS` env var, e.g.
`YEARS=2025,2026,2027`). Each year is written to `docs/events-<year>.json`; the
current year is also mirrored to `docs/events.json` as a stable default. A
**freeze** optimisation skips rebuilding a *past* year once its output has been
byte-identical (ignoring the timestamp) for 5 consecutive runs — settled history
isn't re-scraped, which saves the whole per-year pipeline (including the Top Dog
browser walk). Freeze state is tracked per-year in `years.json`.

Data availability by year is a property of the sources, not a bug: the current
year is full; a past year is carried mostly by the entry platforms (the
governing PDFs for a past year are gone from the bodies' sites); a future year
fills in gradually as each body publishes its next-year calendar (Dogs Victoria
and Dogs West, for example, publish ahead of the others).

## Scope

- **Years:** current-year-driven, with previous and next also built (see
  Multi-year above). Set the window with the `YEARS` env var in the workflow.
- **States/territories:** all eight — NSW, VIC, QLD, WA, SA, TAS, ACT, NT.
- **Disciplines:** all ANKC sports — Tracking, Track & Search, Scent Work,
  Obedience, Rally Obedience, Agility, Herding, Endurance, Lure Coursing,
  Retrieving, Field Trial, Earthdog, Dances with Dogs, Sled Sports, Sprint,
  Weight Pull, Trick Dog, Backpacking, RATG, Draft Test, Bale Seek, Canine Disc,
  and Conformation shows.
  Tracking and Track & Search are kept distinct; where a source genuinely cannot
  tell them apart (e.g. Dogs NSW's "TT" code) the combined label
  "Tracking / Track & Search" is used rather than guessing — and a cross-check
  pass splits combined NSW events into the specific discipline where another
  source (Show Manager listing/detail page, or Top Dog) confirms which it is.

## Sources and the cross-check model

Events come from three kinds of source, combined into one de-duplicated list:

**1. Governing-body calendars (source of truth for what's approved):**

| Body | What's read | Method |
|------|-------------|--------|
| Dogs NSW | Show & Trials Guide PDF | `nsw_pdf.py` — PDF text parse |
| Dogs Victoria | official events-calendar PDF | `dv_calendar.py` — PDF text parse |
| Dogs Queensland | master trial-calendar PDF | `qld_calendar.py` — PDF text parse |
| Dogs West (WA) | yearly Calendar of Events PDF | `wa_calendar.py` — PDF text parse |
| Dogs ACT | dogsact.org.au events | in `scrape.py` — iCal, all-discipline classify |
| Dogs Tasmania | tasdogs.com `/dates/` year table | in `scrape.py` — `parse_tasdogs` (dates page primary, per-discipline category crawl fallback) |

*Dogs Victoria (vicdog.com)* is layered in as a VIC **verify + gap-fill** listing
source (not a primary calendar), since the DV PDF is Victoria's comprehensive
feed. *Dogz Online* is retired (IP-blocked the CI runner); its module remains
dormant and reversible.

**2. Show Manager (entry-status cross-check + gap-fill):** `show_manager.py`
reads the public Event Diary (`showmanager.com.au`) across all 8 states and all
disciplines. It plays two roles: it **verifies entry status** (Open / Closed /
Cancelled) for governing-body events by matching them, and it **gap-fills**
events that no governing-body source lists (SA, TAS and NT lean on this, since
they have no dedicated scrapable calendar). Matching and gap-fill logic lives in
`matcher.py`, which pairs events on region + date + discipline family + fuzzy
club name, and skips gap-fill events that collide with one already present.

**3. Top Dog Events (supplementary feed):** `topdog_browser.py` + `parse_topdog`
in `scrape.py`. Top Dog's trials list is JavaScript-paginated, so a headless
browser (Playwright/Chromium) walks every page and hands rendered HTML to the
parser. Covers all 8 states and all disciplines. It adds events (some clubs take
entries only here) but does not verify status. Most of what it lists is already
covered by the sources above and de-duplicates away.

## Entry-status / trust model

Every event carries two **independent** axes plus a temporal flag:

- **verified** — corroborated by an entry platform or 2+ sources. Shown as a
  green check; otherwise the event is unverified (listed, but status not
  independently confirmed).
- **entry state** — Open / Listed / Entries closed / Approved (not open) /
  Cancelled. "Open" requires a source to say entries are open **and** the known
  closing date not to have passed (a passed closing date forces "Entries
  closed", so a stale "open" from a lagging source can't mislead).
- **past** — whether the event's end date has gone by. This is separate from
  entry state and co-occurs with it: an event can be *cancelled + past* or
  *entries closed + past*, but never *open + past* (past forces open→closed).
  The page has an independent "Show past" toggle (off by default).

The word "unverified" is shown whenever certainty isn't established, so the
calendar never overstates what it knows. Cancelled events are kept and struck
through, never silently dropped. The "last updated" time is shown in Australian
Eastern time with the correct AEST/AEDT label for the date.

## Safeguards

- **Write-guard (`scrape.py`):** before overwriting `events.json`, the new result
  is compared per-total, per-region and per-source against the last published
  file. A **catastrophic** drop (total < 60% of before, or a source/region that
  had ≥15 events cratering below 20%) makes the run refuse to publish and keep
  yesterday's data (`sys.exit(2)`); a **notable** drop is published but logged as
  a `[guard] WARNING` to eyeball. Override a refusal with `ALLOW_SHRINK=1`. This
  protects against any one of the ~8 feeds silently failing.
- **Year guards (PDF parsers):** the PDF sources are checked to ensure the file
  actually belongs to the target year — discovery pins the year in the filename
  and skips drafts; a content check rejects a PDF whose body references a
  different year — so a wrong-year file can't silently produce mis-dated events.
- **Fail-safe sources:** every parser returns `[]` on any error rather than
  crashing the run, and prints a diagnostic. A single broken source degrades
  gracefully.

## Repository layout

```
.github/workflows/
  main.yml         daily scrape + commit (cron 30 15 UTC)
  test-full.yml    manual dry-run (does NOT commit)
  test-nsw.yml     manual NSW-only test
scraper/
  scrape.py        orchestrator: multi-year loop, sources, dedup, cross-check,
                   NSW Tracking/T&S disambiguation, trust model, freeze, guard,
                   per-year output + years.json; also holds the Top Dog, vicdog
                   and Dogs Tasmania parsers and the feed discipline classifier
  nsw_pdf.py       Dogs NSW PDF parser (all-discipline code map)
  dv_calendar.py   Dogs Victoria calendar PDF parser (all-discipline, year-aware)
  qld_calendar.py  Dogs Queensland trial-calendar PDF parser
  wa_calendar.py   Dogs West (WA) calendar PDF parser
  show_manager.py  Show Manager entry-diary scraper (+ detail-page fetch)
  topdog_browser.py  headless-browser walker for Top Dog's paginated list
  matcher.py       cross-check + gap-fill engine (records corroborating sources)
  national_events.py  Dogs Australia National Events supplementary feed
  dogz_online.py   retired source, kept dormant/reversible
  requirements.txt  requests, beautifulsoup4, icalendar, pdfplumber, playwright
docs/
  index.html       the page (year menu, collapsible mobile filters, faceted
                   counts, AEST update time); reads years.json + events-<year>.json
  events-<year>.json  per-year data, written by the Action
  events.json       current year mirror (stable default)
  years.json        manifest: which years exist, current year, freeze state
```

## Setup

1. Push these files to a GitHub repo, keeping the structure above.
2. **Settings → Pages** → *Build from branch* → branch `main`, folder `/docs`.
   The page goes live at `https://<you>.github.io/<repo>/` — for this project,
   `https://glassopsaus.github.io/ANKCCalendar/`.
3. **Settings → Actions → General → Workflow permissions** → *Read and write*
   (so the Action can commit `events.json`).
4. The Action installs Playwright's Chromium automatically
   (`python -m playwright install --with-deps chromium`) for the Top Dog walk.
5. **Actions → Update … → Run workflow** to do the first scrape now, or wait for
   the daily cron.

## Editing / maintenance notes

- Per-source config sits at the top of each module and of `scrape.py`
  (`SOURCES`, `TOPDOG_REGIONS`, `SM_REGIONS`, `REGION_COLOR`). PDF fallback URLs
  are in each `*_calendar.py`; discovery normally finds the current file, and the
  fallback is used (with a visible warning) only if discovery fails.
- The front-end filters (State / Discipline / Entry status) are built from the
  data at load. State and Discipline start unselected; an empty filter row means
  "no constraint" (shows all), so the page is never empty on load. State and
  Discipline intersect; chips within a row union. Entry status is an independent
  narrowing filter.
- Use the `test-full.yml` dry-run (which prints per-source/region/discipline
  summaries but does **not** commit) to check changes before they go live.
- Known non-blocking rough edges: WA event titles keep the fixture+club text
  together rather than a clean club field; the "unverified" label's meaning is
  broader now that Show Manager is a primary source for many events.

## Next levers (considered, deferred — build if the trigger appears)

- **Canonical club list / alias table for de-duplication.** Dedup currently
  matches events by distinctive club-name tokens drawn from title + location
  (handles the common case where sources put the club name in different fields,
  e.g. DV in the title vs Top Dog in the location). It CANNOT merge two copies
  that name the same club with *no shared distinctive token* (e.g. "K9 Scent
  Club" vs "Geelong Nose Work" for the same club). A canonical per-state club
  registry with aliases would fix that class, and would also strengthen the NSW
  Tracking/T&S disambiguation and the matcher's fuzzy name pairing.
  *Deferred because:* the field-placement duplication (the observed problem) is
  already handled without it; a full registry+alias resolver across 8 states is
  a substantial subsystem; and the state-body club directories don't cleanly
  expose the *aliases* that would justify it (they list formal names only).
  *Trigger to build:* real duplicates appear where the two copies share no
  distinctive token. *Best source when building:* harvest `vicdog.com/clubs/`
  (already scraped; clean canonical VIC names) and the equivalent club lists per
  state, rather than the harder-to-scrape state directories. Build it targeted
  at the observed cases, not speculatively.
- **Canine Disc via CDA (`caninediscaustralia.com/event-schedule`).** The only
  place Canine Disc events are listed (they run under CDA, not the state
  bodies). Deferred because the schedule page is currently empty and is a
  GoDaddy-builder site with unstable markup. *Trigger to build:* the schedule
  page actually lists events — then assess the real markup before writing a
  parser. The discipline is already recognised if it reaches us via any source.

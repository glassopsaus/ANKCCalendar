# Australian Dog Events Calendar — Project Status

_Handover notes. Last updated: end of session, 4 Aug 2026._

---

## What this project is

A web page that aggregates **every ANKC show and trial across all Australian
states/territories and all disciplines** (conformation, obedience, rally,
agility, scent work, tracking, track & search, retrieving, herding, endurance,
lure coursing, sprint, sled sports, and more) into one auto-updating calendar.
Each event answers the question that matters: **can I still enter?**

- **Live page:** hosted on GitHub Pages from the `docs/` folder of the repo
  `glassopsaus/ANKCCalendar` (https://glassopsaus.github.io/ANKCCalendar/).
- **How it updates:** a GitHub Action (`.github/workflows/main.yml`) runs a Python
  scraper daily, writes per-year `docs/events-<year>.json` (plus a current-year
  `events.json` mirror and a `years.json` manifest), and the page reads them.

## Current working state

- The **page is live** and rendering.
- The **workflow runs** (after we fixed: Jekyll via `.nojekyll`, an invalid
  workflow file, and a missing `requirements.txt`).
- The scraper currently covers: **Dogs ACT, Dogs Victoria (via vicdog.com),
  Dogs Tasmania, and Top Dog Events.**
- Scope is currently **2026 calendar year only** (`YEAR = 2026`, set both in
  `scrape.py` and as an env var in the workflow).

## Known open issues (pre-existing, not yet fixed)

1. **Top Dog Events pagination is broken.** Its trials list is JavaScript-driven;
   plain HTTP requests always return page 1, so the scraper can't reach older
   events (this is why the North East May 2026 event wasn't found there).
   Unresolved — needs either a headless browser (Playwright) or a data feed.
2. Dogs ACT / Dogs Tasmania URLs were never fully verified against live sites.

---

## The rebuild we started (this is the active work)

You asked to change the architecture to:

- **Source of truth = governing-body calendars**, specifically the
  **Dogs NSW PDF** and **Dogs Victoria PDF**, plus Dogs ACT & Dogs Tasmania.
- **Cross-check entry status** against **Top Dog Events, K9 Entries, Show Manager**.
- **Status rule:** an event on a governing-body calendar but NOT found open on
  any entry system -> label **"Approved; not open"**. "Open" = entries currently
  open (not yet closed), verified where possible, inferred from the PDF-named
  provider otherwise (mark which).
- Build on a **branch** (`rebuild-pdf-sources`) so the live site stays safe.

### Decisions locked in

| Question | Your decision |
|---|---|
| Disciplines to cover | **Tracking, Track & Search, AND Scent Work** — across all sources |
| Page title | Retitle to something like **"Tracking & Scent Work"** |
| How to show Scent Work | **Colour-code by discipline** (distinct from region colours) |
| Victoria PDF | Parse it only as a **cross-check against vicdog** (vicdog stays Vic's source of truth; Vic PDF is a messy diary-layout with unstable URLs) |
| "Open" definition | **Entries currently open (not yet closed)** |
| Cross-check method | **Both** — trust PDF-named provider AND verify against live entry systems where scrapable |
| Protect live site | **New branch**, merge when working |

### Key facts discovered (verified against real sources)

- **Dogs NSW PDF** is clean and tabular. Legend codes: **`TT` = Tracking / Track
  & Search**, **`SW` = Scent Work**. WATCH OUT: **`TD` = Trick Dog** (NOT tracking).
  Row format: `WeekNo Date Club Type Venue [Provider] Contact`. The provider
  column names the entry system directly.
  - PDF URL changes on every amendment (`/media/NNNN/...`). Must be discovered
    from the calendar page: https://www.dogsnsw.org.au/events/show-and-trials-guide/
- **Dogs Victoria PDF** is a fragmented day-by-day diary layout with unstable
  versioned URLs (v3...v39). Hard to parse — hence "cross-check only".
- **Entry systems:** Top Dog (JS pagination problem), K9 Entries
  (k9entries.com — only shows ~last 2 months, full access is paid), Show Manager
  (not yet examined).

---

## SESSION 2 UPDATE (the entry-status cross-check design)

### NSW PDF parser CONFIRMED WORKING on a live run
Ran the test workflow. Result: parser discovered the current PDF
(`/media/8326/...` — newer than the hardcoded fallback, so URL discovery works)
and parsed **168 events: 50 tracking/T&S, 118 scent work**. Club names clean,
disciplines correctly separated, providers captured, Trick Dog correctly
excluded. Stage 1 is proven.

### Status-model decisions (locked)
- Every event carries a STATUS + a VERIFICATION LEVEL:
  - **Open (verified)** — found on an entry system, entries not yet closed.
  - **Closed (verified)** — found on an entry system, entry-close date passed.
  - **Entries via [system] (unverified)** — PDF names a provider but live
    open/closed state couldn't be confirmed.
  - **Approved; not open (unverified)** — on a governing-body calendar, no
    provider named, not found on any entry system.
- The word "unverified" shows as a visible tag whenever inferred.
- Multi-day NSW events (listed one row per day): collapse consecutive days
  ONLY when same club AND same discipline.

### BREAKTHROUGH: Show Manager is the reliable verification anchor
Show Manager has a PUBLIC, STATIC HTML Event Diary:
  https://www.showmanager.com.au/events/publicevents?g=2   (g=2 = Dog Sports)
  Filter by state+month via: /events?y=2026&m=7&s=NSW&o=True&g=2&a=True&r=False&sl=ALL
- Each row: State | Event Name (+club) | Location | Event Type | Entries Closing
  date | status ("Entries Closed" / "Cancelled" / open).
- Event Types include our exact three: "Tracking", "Track & Search", "Scent Work".
- Server-rendered HTML (NOT JavaScript) — so it CAN be paged/scraped reliably,
  unlike Top Dog. This is what makes "Open/Closed (verified)" achievable.
- Detail pages at /events/PublicEvents/Details/<id>.

### Entry-system scrapability summary (verified this session)
- **Show Manager** — YES, static, reliable. Primary verification source. BUILD THIS.
- **Top Dog Events** — NO (JavaScript pagination, only page 1 readable). Leave as
  "unverified" contributor for now; needs headless browser to fix.
- **K9 Entries** — PARTIAL (only ~last 2 months public, rest paywalled).

### CROSS-CHECK BUILT (session 3)
All stage-2 modules built, logic-tested, and wired into scrape.py:
- `scraper/show_manager.py` — scrapes Show Manager Event Diary (g=2) month by
  month; parses date-header inheritance, discipline filter (Tracking/T&S/Scent
  Work), region filter (ACT/NSW/VIC/TAS), and status (open="Enter Online",
  closed="Entries Closed", cancelled="Cancelled") + entry-closing date. Tested
  against real July/Aug 2026 rows.
- `scraper/matcher.py` — matches governing-body events to Show Manager listings
  by region + date-span + discipline family + fuzzy club-name overlap. Assigns:
  Open/Closed/Cancelled (verified) when found on Show Manager; "Entries via X
  (unverified)" when only the PDF names a provider; "Approved; not open
  (unverified)" otherwise. Self-test covers every path.
- `scraper/scrape.py` — WIRED: imports the 3 modules defensively; adds Dogs NSW
  PDF as a source; collapses NSW per-day rows into multi-day events (same club +
  discipline, gap<=1); runs Show Manager cross-check on the deduped list; adds
  Dogs NSW to the UI source list. Integration-tested (collapse + match) OK.

Each event now carries: status, status_label, verified (bool), closes, entry_url.

### STILL TO DO (session 4 = front-end)
- Retitle page to "Tracking & Scent Work".
- Discipline colour-coding (rework palette so region vs discipline don't clash).
- Show status_label as a badge; show "unverified" tag distinctly; grey/strike
  cancelled. Show entry_url link when present.
- Then: run the real workflow, read the log, tune matching from live results.
- Backlog: Top Dog pagination (headless browser), verify ACT/Tas URLs, Vic PDF
  cross-check (stage 3, optional).

### NEXT STEP (start of session 3)
Build the **entry-status cross-check engine** in scrape.py:
  1. Scrape Show Manager Event Diary (g=2), all months of YEAR, states VIC/ACT/
     TAS/NSW, keep Tracking/Track&Search/Scent Work rows with their closing dates
     + status.
  2. Match governing-body (PDF/vicdog/etc.) events to Show Manager rows by
     (club fuzzy-match, date, discipline) -> assign Open/Closed (verified).
  3. Unmatched -> use PDF provider name -> "Entries via X (unverified)" or
     "Approved; not open (unverified)".
Then wire NSW PDF into scrape.py (with day-collapsing), then front-end
(retitle "Tracking & Scent Work", discipline colours, status badges).

---

## What was BUILT in session 1 (stage 1 of the rebuild)

On branch `rebuild-pdf-sources`:

1. **`scraper/nsw_pdf.py`** — NEW standalone Dogs NSW PDF parser.
   - Discovers current PDF URL, extracts events, keeps only `TT` + `SW`,
     captures entry provider.
   - Tested against the REAL PDF's actual text: correctly keeps Tracking +
     Scent Work, correctly DROPS Trick Dog (`TD`) and all other disciplines,
     extracts full club names and providers. A bug (grabbing club words as
     codes) was found and fixed during testing.
   - Not yet wired into `scrape.py` — it's standalone and harmless until called.
2. **`scraper/requirements.txt`** — added `pdfplumber>=0.11` (needed for PDF).

Both files are in the outputs alongside this note.

---

## EXACT NEXT STEP (start here tomorrow)

The immediate question we stopped on: **where to put the files and how to test.**

The two files go in the repo's **`scraper/`** folder (next to `scrape.py`):
- `nsw_pdf.py` -> ADD as a new file
- `requirements.txt` -> REPLACE existing (adds the pdfplumber line)

They change nothing until wired in, so they're safe to add to `main` or a branch.

**Decision still needed:** how to test `nsw_pdf.py`, since you work through the
GitHub website (no local Python). Options discussed:
  - (a) Add a temporary "test" step to the workflow that runs
    `python scraper/nsw_pdf.py 2026` and prints the result to the Actions log.
  - (b) Run it locally if you can.
  - (c) Skip standalone testing and wire NSW straight into the main scraper,
    then test everything together.

Recommendation: **(a)** — a temporary workflow step is the cleanest way to see
what the NSW parser pulls from the live PDF without disturbing anything, given
the web-only workflow. Ask Claude to write that temporary step.

## Remaining stages (NOT yet built)

- **Stage 2:** entry-system cross-check (Top Dog / K9 Entries / Show Manager)
  + the "approved; not open" status engine with entry-close-date logic.
- **Stage 3:** Vic PDF cross-check against vicdog.
- **Stage 4:** front-end — retitle to "Tracking & Scent Work", discipline
  colour-coding (rework palette so region vs discipline colours don't clash),
  "Approved; not open" status badge.
- Also outstanding from before: fix Top Dog pagination (headless browser?),
  verify Dogs ACT / Dogs Tasmania URLs.

## Repo layout reference

```
repo/
├── .github/workflows/main.yml   (workflow; uses YEAR: "2026")
├── docs/
│   ├── index.html               (the page)
│   ├── events.json              (data the page reads; scraper overwrites it)
│   └── .nojekyll                (disables Jekyll on Pages — keep it)
└── scraper/
    ├── scrape.py                (main scraper)
    ├── requirements.txt         (deps; now includes pdfplumber)
    └── nsw_pdf.py               (NEW — NSW PDF parser, stage 1)
```

## Gotchas learned (so we don't repeat them)

- Hidden files (`.github/...`, `.nojekyll`) get silently dropped by the GitHub
  web uploader and by `zip`. Add/edit them in place, or use git locally.
- GitHub Pages runs Jekyll by default; `.nojekyll` disables it (was causing a
  `dir_chdir0` build error).
- Scheduled Actions don't fire immediately on a new repo — trigger the first run
  manually via Actions -> Run workflow.
- YAML is whitespace/tab sensitive — an invalid workflow file blocked runs.
- The scraper CANNOT be tested in the Claude sandbox (no network); it must run
  in the GitHub Action, which is where the real data lives.

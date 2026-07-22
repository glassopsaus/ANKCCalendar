#!/usr/bin/env python3
"""
Last-known-good calendar-PDF URL cache.

WHY: the governing-body calendar PDFs (Dogs Victoria, Dogs Queensland, Dogs
West) live at media URLs that change whenever the body republishes. Each parser
DISCOVERS the current URL from the body's dates page every run, and only falls
back to a hardcoded pin if discovery fails. That pin goes stale over time.

This cache lets the fallback AUTO-UPDATE: when discovery succeeds AND the PDF
parses to real events, the parser records the URL here (keyed by source+year).
On a later run where discovery fails, the parser reads the last-known-good URL
from here instead of the stale hardcoded pin. The hardcoded pin remains only as
a first-ever-run seed.

SAFETY: callers save a URL ONLY after it produced events, so a bad/draft URL
that yielded nothing is never cached. Reads/writes are fully fail-safe — any
error just means "no cache", and the parser uses its hardcoded pin.

STORAGE: a small JSON file committed with the repo so it persists across runs:
    { "qld:2026": "https://.../media/55093/....pdf",
      "dv:2026":  "https://.../media/7101/....pdf", ... }
"""

import json
import os
import sys

# Live beside this module, in the scraper dir. Not published to docs/, so it
# doesn't affect the site; it's committed by the workflow like any repo file.
_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pdf_url_cache.json")


def _load():
    try:
        with open(_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def get_cached_url(source, year):
    """Return the last-known-good discovered URL for (source, year), or None."""
    try:
        return _load().get(f"{source}:{year}") or None
    except Exception:
        return None


def save_url(source, year, url):
    """Record a confirmed-good discovered URL. No-op on any error, and never
    overwrites with an empty/missing value."""
    if not url:
        return
    try:
        data = _load()
        key = f"{source}:{year}"
        if data.get(key) == url:
            return  # unchanged, avoid a needless write/commit
        data[key] = url
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, _CACHE_PATH)
        print(f"[pdf-cache] updated {key} -> {url}", file=sys.stderr)
    except Exception as e:
        print(f"[pdf-cache] save failed (non-fatal): {e}", file=sys.stderr)

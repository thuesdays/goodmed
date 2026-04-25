"""
site_presets.py — Warmup site libraries grouped by topic.

Each preset is a list of realistic destinations the warmup engine
cycles through. The goal is to seed the profile with cookies that
look like organic browsing before any ad-monitoring work starts —
consent cookies on Google/YouTube, session cookies on news sites,
localStorage on Wikipedia, etc.

Per-site schema:
    url         required, the destination
    dwell_sec   how long to idle on the page (range → random in band)
    scroll      whether to scroll down a bit while dwelling
    topic       short tag for logging
    notes       why this site is in the list (for future you)

We lean into sites that:
  - use cookie-consent banners (so our consent-banner clicker earns
    us the consent cookies Google-side)
  - have heavy JS + analytics (populates third-party cookies
    organically — DoubleClick, Google Analytics, etc.)
  - are geographically / thematically plausible for the profile

Presets are mutable at runtime via the DB (config_kv entry
`warmup.custom_sites.<preset>`) but the defaults below are what
a fresh install gets.
"""

from __future__ import annotations

__author__ = "Mykola Kovhanko"
__email__ = "thuesdays@gmail.com"

import random
from typing import Iterable


# ═══════════════════════════════════════════════════════════════
# General-purpose — always safe, works for any profile
# ═══════════════════════════════════════════════════════════════
GENERAL = [
    {"url": "https://www.google.com/",                    "topic": "search",  "dwell_sec": (6, 12),  "scroll": True,
     "notes": "Sets consent banner state + NID"},
    {"url": "https://www.youtube.com/",                   "topic": "video",   "dwell_sec": (8, 15),  "scroll": True,
     "notes": "YT consent cookie + VISITOR_INFO"},
    {"url": "https://en.wikipedia.org/wiki/Main_Page",    "topic": "ref",     "dwell_sec": (10, 20), "scroll": True,
     "notes": "Populates localStorage + reading history pattern"},
    {"url": "https://www.reddit.com/",                    "topic": "social",  "dwell_sec": (8, 14),  "scroll": True,
     "notes": "Active JS, lots of third-party cookies"},
    {"url": "https://www.bing.com/",                      "topic": "search",  "dwell_sec": (4,  8),  "scroll": False,
     "notes": "Secondary search engine — adds non-Google referrer"},
    {"url": "https://www.imdb.com/",                      "topic": "ref",     "dwell_sec": (6, 12),  "scroll": True,
     "notes": "Amazon-family cookies"},
    {"url": "https://www.weather.com/",                   "topic": "utility", "dwell_sec": (4,  8),  "scroll": False,
     "notes": "Weather.com → Trust.com consent flow"},
]


# ═══════════════════════════════════════════════════════════════
# Medical / health — for goodmedika.com.ua profile focus
# ═══════════════════════════════════════════════════════════════
MEDICAL = [
    {"url": "https://www.who.int/",                                    "topic": "med-authority", "dwell_sec": (8, 14), "scroll": True,
     "notes": "Authoritative medical — Google likes seeing this in history"},
    {"url": "https://www.mayoclinic.org/",                             "topic": "med-ref",       "dwell_sec": (10, 18), "scroll": True,
     "notes": "Classic symptom-lookup destination"},
    {"url": "https://www.webmd.com/",                                  "topic": "med-ref",       "dwell_sec": (8, 14), "scroll": True,
     "notes": "Heavy ads site; populates DoubleClick"},
    {"url": "https://www.healthline.com/",                             "topic": "med-ref",       "dwell_sec": (6, 12), "scroll": True,
     "notes": "Common medical content destination"},
    {"url": "https://medlineplus.gov/",                                "topic": "med-gov",       "dwell_sec": (6, 12), "scroll": True,
     "notes": "US gov — trusted signal"},
    {"url": "https://uk.wikipedia.org/wiki/%D0%9C%D0%B5%D0%B4%D0%B8%D1%86%D0%B8%D0%BD%D0%B0",
     "topic": "med-uk-ref",   "dwell_sec": (8, 14), "scroll": True,
     "notes": "Ukrainian Wikipedia article (Medicine category) for ua-geo plausibility"},
    {"url": "https://en.wikipedia.org/wiki/Health",                    "topic": "med-ref",       "dwell_sec": (6, 12), "scroll": True,
     "notes": "Broad health overview — not a specific condition"},
    {"url": "https://www.ncbi.nlm.nih.gov/",                           "topic": "med-research",  "dwell_sec": (5, 10), "scroll": False,
     "notes": "Research-grade browsing signal"},
]


# ═══════════════════════════════════════════════════════════════
# Tech — plausible for any IT-literate profile
# ═══════════════════════════════════════════════════════════════
TECH = [
    {"url": "https://news.ycombinator.com/",        "topic": "tech-news", "dwell_sec": (8, 14),  "scroll": True,
     "notes": "HN — clean text, good dwell"},
    {"url": "https://stackoverflow.com/",           "topic": "tech-ref",  "dwell_sec": (6, 12),  "scroll": True,
     "notes": "Heavy GA/GTM"},
    {"url": "https://github.com/explore",           "topic": "tech-dev",  "dwell_sec": (8, 14),  "scroll": True,
     "notes": "GH Explore — no login required"},
    {"url": "https://www.theverge.com/",            "topic": "tech-news", "dwell_sec": (6, 12),  "scroll": True,
     "notes": "Tech journalism with heavy ads"},
    {"url": "https://arstechnica.com/",             "topic": "tech-news", "dwell_sec": (6, 12),  "scroll": True,
     "notes": "Condé Nast family cookies"},
    {"url": "https://developer.mozilla.org/",       "topic": "tech-ref",  "dwell_sec": (8, 16),  "scroll": True,
     "notes": "MDN — common dev-browsing footprint"},
    {"url": "https://www.wired.com/",               "topic": "tech-news", "dwell_sec": (5, 10),  "scroll": True,
     "notes": "Another Condé Nast property"},
]


# ═══════════════════════════════════════════════════════════════
# News — political / general news sites for organic reading pattern
# ═══════════════════════════════════════════════════════════════
NEWS = [
    {"url": "https://www.bbc.com/",                 "topic": "news",       "dwell_sec": (8, 14), "scroll": True,
     "notes": "Global English news"},
    {"url": "https://www.reuters.com/",             "topic": "news",       "dwell_sec": (6, 12), "scroll": True,
     "notes": "Wire service — newspaper-like dwell"},
    {"url": "https://www.bloomberg.com/",           "topic": "news-biz",   "dwell_sec": (5, 10), "scroll": True,
     "notes": "Paywall softly nudges user but still populates cookies"},
    {"url": "https://www.theguardian.com/",         "topic": "news",       "dwell_sec": (6, 12), "scroll": True,
     "notes": "UK broadsheet — reader-friendly layout"},
    {"url": "https://apnews.com/",                  "topic": "news",       "dwell_sec": (6, 12), "scroll": True,
     "notes": "Associated Press"},
    {"url": "https://www.nytimes.com/",             "topic": "news",       "dwell_sec": (5, 10), "scroll": True,
     "notes": "Paywall but home page populates cookies fine"},
    {"url": "https://www.pravda.com.ua/",           "topic": "news-ua",    "dwell_sec": (6, 12), "scroll": True,
     "notes": "Ukrainian news — ua-geo plausibility"},
]


# ═══════════════════════════════════════════════════════════════
# Mobile-first destinations — mobile.* domains where available
# ═══════════════════════════════════════════════════════════════
# When warmup runs against a profile whose current fingerprint is
# mobile (is_mobile=true), the engine auto-selects this preset so
# the traffic pattern matches the device shape. Some of these URLs
# redirect to m.* or show different HTML when Mobile UA is present.
MOBILE = [
    {"url": "https://m.youtube.com/",                    "topic": "video-mobile",   "dwell_sec": (8, 14),  "scroll": True,
     "notes": "Mobile-optimised YouTube — different DOM than desktop"},
    {"url": "https://m.wikipedia.org/",                  "topic": "ref-mobile",     "dwell_sec": (6, 12),  "scroll": True,
     "notes": "Mobile Wikipedia — collapsible sections, tap targets"},
    {"url": "https://www.google.com/",                   "topic": "search-mobile",  "dwell_sec": (5, 10),  "scroll": True,
     "notes": "Google serves distinct mobile SERP on Mobile UA"},
    {"url": "https://mobile.twitter.com/",               "topic": "social-mobile",  "dwell_sec": (8, 14),  "scroll": True,
     "notes": "Twitter mobile shell — redirects to x.com mobile"},
    {"url": "https://www.reddit.com/",                   "topic": "social",         "dwell_sec": (8, 14),  "scroll": True,
     "notes": "Reddit auto-serves mobile layout on Mobile UA"},
    {"url": "https://www.instagram.com/",                "topic": "social-mobile",  "dwell_sec": (5, 10),  "scroll": False,
     "notes": "IG login page responds differently on mobile"},
    {"url": "https://www.bbc.com/news",                  "topic": "news-mobile",    "dwell_sec": (6, 12),  "scroll": True,
     "notes": "BBC News mobile — card layout"},
    {"url": "https://weather.com/weather/today",         "topic": "utility-mobile", "dwell_sec": (4,  8),  "scroll": True,
     "notes": "Weather mobile — heavy ads, touch targets"},
]


# ═══════════════════════════════════════════════════════════════
# Registry — the dict API code consumes
# ═══════════════════════════════════════════════════════════════
PRESETS = {
    "general": {
        "label":       "General",
        "description": "Balanced mix — search, video, reference, social, utility.",
        "sites":       GENERAL,
    },
    "medical": {
        "label":       "Medical / Health",
        "description": "Authoritative medical + a Ukrainian-Wikipedia anchor for UA-geo profiles.",
        "sites":       MEDICAL,
    },
    "tech": {
        "label":       "Tech / Developer",
        "description": "HN, StackOverflow, MDN, GitHub, tech journalism.",
        "sites":       TECH,
    },
    "news": {
        "label":       "News / Reading",
        "description": "Mix of English + Ukrainian news — simulates a daily reader.",
        "sites":       NEWS,
    },
    "mobile": {
        "label":       "Mobile",
        "description": "m.* / mobile.* destinations — auto-selected for mobile-fingerprint profiles when preset=auto.",
        "sites":       MOBILE,
    },
}


# ═══════════════════════════════════════════════════════════════
# Public helpers
# ═══════════════════════════════════════════════════════════════

def list_presets() -> list[dict]:
    """Summary for the UI dropdown — {id, label, description, site_count}."""
    return [
        {
            "id":          pid,
            "label":       p["label"],
            "description": p["description"],
            "site_count":  len(p["sites"]),
        }
        for pid, p in PRESETS.items()
    ]


def get_preset(preset_id: str) -> dict | None:
    return PRESETS.get(preset_id)


def pick_sites(preset_id: str, n: int, seed: str | None = None) -> list[dict]:
    """Pull N sites from a preset, deterministically if a seed is given.

    Fewer than N available → returns what's available. Shuffled so
    the order varies between warmups (realism: nobody visits the
    same sites in the exact same order twice).
    """
    preset = PRESETS.get(preset_id)
    if not preset:
        return []
    sites = list(preset["sites"])
    rng = random.Random(seed) if seed else random.Random()
    rng.shuffle(sites)
    return sites[:max(1, n)]


def roll_dwell(dwell_range: Iterable[float], rng: random.Random | None = None) -> float:
    """Convert a (low, high) tuple into a concrete dwell seconds value."""
    rng = rng or random
    lo, hi = dwell_range if isinstance(dwell_range, (list, tuple)) else (dwell_range, dwell_range)
    return rng.uniform(float(lo), float(hi))

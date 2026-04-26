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
    countries   list of ISO-2 country codes the site is appropriate for,
                or ["*"] for "safe anywhere". Used by filter_sites_by_country
                to drop foreign-locale leakage (e.g., webmd.com would set
                Google's IP-vs-history triangulation toward US, even if the
                proxy is Ukrainian, and the SERP would come back in English/
                Dutch/whatever Google guessed). Country-specific tags ("UA",
                "US", "DE", ...) mean the site bakes in that locale's
                cookies/history/Accept-Language and SHOULD NOT be visited
                by profiles for other countries.
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

Geo-filter rule (applied by filter_sites_by_country):
  A site IS included for target country X iff
       "*" in site["countries"]           (universal/safe)
    OR X in site["countries"]             (locally appropriate)
  Otherwise the site is dropped. This prevents the "Ukrainian profile
  visits webmd.com → Google sees US-medicine-history → SERP flips to
  English+Dutch ads" leak that one user actually hit in production.
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
     "countries": ["*"],
     "notes": "Sets consent banner state + NID"},
    {"url": "https://www.youtube.com/",                   "topic": "video",   "dwell_sec": (8, 15),  "scroll": True,
     "countries": ["*"],
     "notes": "YT consent cookie + VISITOR_INFO"},
    {"url": "https://en.wikipedia.org/wiki/Main_Page",    "topic": "ref",     "dwell_sec": (10, 20), "scroll": True,
     "countries": ["*"],
     "notes": "Populates localStorage + reading history pattern"},
    {"url": "https://www.reddit.com/",                    "topic": "social",  "dwell_sec": (8, 14),  "scroll": True,
     "countries": ["*"],
     "notes": "Active JS, lots of third-party cookies"},
    {"url": "https://www.bing.com/",                      "topic": "search",  "dwell_sec": (4,  8),  "scroll": False,
     "countries": ["*"],
     "notes": "Secondary search engine — adds non-Google referrer"},
    {"url": "https://www.imdb.com/",                      "topic": "ref",     "dwell_sec": (6, 12),  "scroll": True,
     "countries": ["*"],
     "notes": "Amazon-family cookies"},
    {"url": "https://www.weather.com/",                   "topic": "utility", "dwell_sec": (4,  8),  "scroll": False,
     "countries": ["US"],
     "notes": "Weather.com → US-locale (use openweathermap.com for international)"},
    {"url": "https://openweathermap.org/",                "topic": "utility", "dwell_sec": (4,  8),  "scroll": False,
     "countries": ["*"],
     "notes": "International weather — no US locale baked in"},
]


# ═══════════════════════════════════════════════════════════════
# Medical / health — for goodmedika.com.ua profile focus
# ═══════════════════════════════════════════════════════════════
MEDICAL = [
    # ── International / safe anywhere ─────────────────────────────
    {"url": "https://www.who.int/",                                    "topic": "med-authority", "dwell_sec": (8, 14), "scroll": True,
     "countries": ["*"],
     "notes": "WHO — international, served in user's locale"},
    {"url": "https://en.wikipedia.org/wiki/Health",                    "topic": "med-ref",       "dwell_sec": (6, 12), "scroll": True,
     "countries": ["*"],
     "notes": "Broad health overview — not a specific condition"},

    # ── US-specific (drop for non-US profiles) ────────────────────
    # These are the ones that leak US locale to Google: webmd's
    # cookies tag the user as a US health-content reader, mayoclinic
    # serves US-formatted measurements + insurance copy, healthline
    # ads target US pharma, medlineplus is .gov. Together they are
    # the "Dutch SERP for Ukrainian profile" cluster the user hit.
    {"url": "https://www.mayoclinic.org/",                             "topic": "med-ref",       "dwell_sec": (10, 18), "scroll": True,
     "countries": ["US"],
     "notes": "US-locale; excluded for non-US profiles"},
    {"url": "https://www.webmd.com/",                                  "topic": "med-ref",       "dwell_sec": (8, 14), "scroll": True,
     "countries": ["US"],
     "notes": "US-locale; excluded for non-US profiles"},
    {"url": "https://www.healthline.com/",                             "topic": "med-ref",       "dwell_sec": (6, 12), "scroll": True,
     "countries": ["US"],
     "notes": "US-locale; excluded for non-US profiles"},
    {"url": "https://medlineplus.gov/",                                "topic": "med-gov",       "dwell_sec": (6, 12), "scroll": True,
     "countries": ["US"],
     "notes": "US gov; excluded for non-US profiles"},
    {"url": "https://www.ncbi.nlm.nih.gov/",                           "topic": "med-research",  "dwell_sec": (5, 10), "scroll": False,
     "countries": ["US"],
     "notes": "US research; excluded for non-US profiles"},

    # ── Ukrainian medical destinations ────────────────────────────
    # Added because the original MEDICAL preset was 6/8 US-centric
    # and a UA-geo profile running it would land on a SERP that thinks
    # it's a US visitor. With these UA-tagged sites, the medicine
    # warmup actually warms a Ukrainian profile *without* leaking
    # foreign locale.
    {"url": "https://uk.wikipedia.org/wiki/%D0%9C%D0%B5%D0%B4%D0%B8%D1%86%D0%B8%D0%BD%D0%B0",
     "topic": "med-ua-ref",   "dwell_sec": (8, 14), "scroll": True,
     "countries": ["UA"],
     "notes": "Ukrainian Wikipedia article on Medicine"},
    {"url": "https://moz.gov.ua/",                                     "topic": "med-ua-gov",   "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "MoH Ukraine — official, trusted signal"},
    {"url": "https://compendium.com.ua/",                              "topic": "med-ua-ref",   "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "Compendium — UA medical drug reference"},
    {"url": "https://likar.info/",                                     "topic": "med-ua-ref",   "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "Likar.info — popular UA health portal"},
    {"url": "https://apteka.com.ua/",                                  "topic": "med-ua-shop",  "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "UA pharmacy directory"},
    {"url": "https://www.helsi.me/",                                   "topic": "med-ua-svc",   "dwell_sec": (5, 10), "scroll": False,
     "countries": ["UA"],
     "notes": "Helsi — UA telemedicine portal"},
]


# ═══════════════════════════════════════════════════════════════
# Tech — plausible for any IT-literate profile
# ═══════════════════════════════════════════════════════════════
TECH = [
    # International — these don't bake in a country-specific locale
    {"url": "https://news.ycombinator.com/",        "topic": "tech-news", "dwell_sec": (8, 14),  "scroll": True,
     "countries": ["*"],
     "notes": "HN — clean text, no locale"},
    {"url": "https://stackoverflow.com/",           "topic": "tech-ref",  "dwell_sec": (6, 12),  "scroll": True,
     "countries": ["*"],
     "notes": "Heavy GA/GTM, locale-agnostic"},
    {"url": "https://github.com/explore",           "topic": "tech-dev",  "dwell_sec": (8, 14),  "scroll": True,
     "countries": ["*"],
     "notes": "GH Explore — universal"},
    {"url": "https://developer.mozilla.org/",       "topic": "tech-ref",  "dwell_sec": (8, 16),  "scroll": True,
     "countries": ["*"],
     "notes": "MDN — universal"},
    # Tech journalism is US-leaning (Condé Nast / US ad networks)
    {"url": "https://www.theverge.com/",            "topic": "tech-news", "dwell_sec": (6, 12),  "scroll": True,
     "countries": ["US"],
     "notes": "Vox Media — US ad networks"},
    {"url": "https://arstechnica.com/",             "topic": "tech-news", "dwell_sec": (6, 12),  "scroll": True,
     "countries": ["US"],
     "notes": "Condé Nast — US locale"},
    {"url": "https://www.wired.com/",               "topic": "tech-news", "dwell_sec": (5, 10),  "scroll": True,
     "countries": ["US"],
     "notes": "Condé Nast — US locale"},
    # UA tech (added so non-US profiles still get tech preset content)
    {"url": "https://dou.ua/",                      "topic": "tech-ua",   "dwell_sec": (6, 12),  "scroll": True,
     "countries": ["UA"],
     "notes": "Ukrainian developer community"},
    {"url": "https://itc.ua/",                      "topic": "tech-ua-news", "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "Ukrainian tech news"},
]


# ═══════════════════════════════════════════════════════════════
# News — political / general news sites for organic reading pattern
# ═══════════════════════════════════════════════════════════════
NEWS = [
    # International wire services (locale auto-detected from IP)
    {"url": "https://www.bbc.com/",                 "topic": "news",       "dwell_sec": (8, 14), "scroll": True,
     "countries": ["*"],
     "notes": "Global English news, locale-agnostic"},
    {"url": "https://www.reuters.com/",             "topic": "news",       "dwell_sec": (6, 12), "scroll": True,
     "countries": ["*"],
     "notes": "Wire service — international"},
    {"url": "https://apnews.com/",                  "topic": "news",       "dwell_sec": (6, 12), "scroll": True,
     "countries": ["*"],
     "notes": "AP — international wire"},
    # US-leaning
    {"url": "https://www.bloomberg.com/",           "topic": "news-biz",   "dwell_sec": (5, 10), "scroll": True,
     "countries": ["US"],
     "notes": "US business news"},
    {"url": "https://www.nytimes.com/",             "topic": "news",       "dwell_sec": (5, 10), "scroll": True,
     "countries": ["US"],
     "notes": "US — bakes US locale via consent + paywall"},
    # UK-leaning
    {"url": "https://www.theguardian.com/",         "topic": "news",       "dwell_sec": (6, 12), "scroll": True,
     "countries": ["GB"],
     "notes": "UK broadsheet"},
    # Ukrainian
    {"url": "https://www.pravda.com.ua/",           "topic": "news-ua",    "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "Ukrainska Pravda — main UA news"},
    {"url": "https://www.epravda.com.ua/",          "topic": "news-ua-biz", "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "Economic Pravda — UA business"},
    {"url": "https://tsn.ua/",                      "topic": "news-ua",    "dwell_sec": (6, 12), "scroll": True,
     "countries": ["UA"],
     "notes": "TSN — popular UA TV news portal"},
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
     "countries": ["*"],
     "notes": "Mobile-optimised YouTube — different DOM than desktop"},
    {"url": "https://m.wikipedia.org/",                  "topic": "ref-mobile",     "dwell_sec": (6, 12),  "scroll": True,
     "countries": ["*"],
     "notes": "Mobile Wikipedia — collapsible sections"},
    {"url": "https://www.google.com/",                   "topic": "search-mobile",  "dwell_sec": (5, 10),  "scroll": True,
     "countries": ["*"],
     "notes": "Google serves distinct mobile SERP on Mobile UA"},
    {"url": "https://mobile.twitter.com/",               "topic": "social-mobile",  "dwell_sec": (8, 14),  "scroll": True,
     "countries": ["*"],
     "notes": "Twitter mobile shell"},
    {"url": "https://www.reddit.com/",                   "topic": "social",         "dwell_sec": (8, 14),  "scroll": True,
     "countries": ["*"],
     "notes": "Reddit auto-serves mobile layout on Mobile UA"},
    {"url": "https://www.instagram.com/",                "topic": "social-mobile",  "dwell_sec": (5, 10),  "scroll": False,
     "countries": ["*"],
     "notes": "IG login page responds differently on mobile"},
    {"url": "https://www.bbc.com/news",                  "topic": "news-mobile",    "dwell_sec": (6, 12),  "scroll": True,
     "countries": ["*"],
     "notes": "BBC News mobile — card layout"},
    {"url": "https://weather.com/weather/today",         "topic": "utility-mobile", "dwell_sec": (4,  8),  "scroll": True,
     "countries": ["US"],
     "notes": "Weather mobile — US locale (excluded for non-US)"},
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


def _country_to_iso2(country: str | None) -> str | None:
    """Normalise a free-text country name (or ISO-2 code) into ISO-2.

    Accepts both "Ukraine" (full name from browser.expected_country)
    and "UA" (ISO-2). Returns uppercase ISO-2, or None if we can't
    confidently identify it -- in which case the caller should skip
    geo-filtering rather than risk a wrong country tag.
    """
    if not country:
        return None
    c = country.strip()
    if not c:
        return None
    # Already an ISO-2?
    if len(c) == 2 and c.isalpha():
        return c.upper()
    # Common name → ISO-2 lookup. Conservative; expand on demand.
    NAMES = {
        "ukraine": "UA", "україна": "UA", "украина": "UA",
        "united states": "US", "united states of america": "US",
        "usa": "US", "u.s.": "US", "america": "US",
        "united kingdom": "GB", "uk": "GB", "great britain": "GB",
        "germany": "DE", "deutschland": "DE",
        "france": "FR",
        "spain": "ES", "españa": "ES",
        "italy": "IT", "italia": "IT",
        "netherlands": "NL", "nederland": "NL", "holland": "NL",
        "poland": "PL", "polska": "PL",
        "russia": "RU", "russian federation": "RU", "россия": "RU",
        "czech republic": "CZ", "czechia": "CZ",
        "romania": "RO",
        "turkey": "TR", "türkiye": "TR",
        "israel": "IL",
        "canada": "CA",
        "australia": "AU",
        "brazil": "BR", "brasil": "BR",
        "japan": "JP",
        "south korea": "KR", "korea": "KR",
        "india": "IN",
        "china": "CN",
    }
    return NAMES.get(c.lower())


def filter_sites_by_country(sites: list[dict],
                            target_country: str | None) -> list[dict]:
    """Drop sites whose `countries` tag set excludes the target country.

    Rule: a site is kept if either
       - its tag list contains "*" (universal), OR
       - its tag list contains the target country code, OR
       - it has no `countries` tag at all (treat as universal — for
         backwards compatibility with hand-edited custom presets).

    target_country may be a country name ("Ukraine") or ISO-2 ("UA");
    we normalise. If target_country is None or unrecognised, NO
    filtering is applied -- safer to over-include than to drop every
    site for an unknown locale.

    Returns a NEW list; input is not mutated.
    """
    iso = _country_to_iso2(target_country)
    if not iso:
        return list(sites)

    out = []
    for s in sites:
        tags = s.get("countries")
        if not tags:                       # untagged -> include
            out.append(s)
            continue
        if "*" in tags or iso in tags:
            out.append(s)
    return out


def pick_sites(preset_id: str, n: int, seed: str | None = None,
               target_country: str | None = None) -> list[dict]:
    """Pull N sites from a preset, deterministically if a seed is given.

    If `target_country` is provided, sites are filtered through
    filter_sites_by_country first -- ensuring a Ukrainian profile
    won't get fed US-medical sites that flip Google's locale guess.

    Fewer than N available (after filtering) → returns what's available.
    Shuffled so the order varies between warmups (realism: nobody
    visits the same sites in the exact same order twice).
    """
    preset = PRESETS.get(preset_id)
    if not preset:
        return []
    sites = list(preset["sites"])
    if target_country:
        sites = filter_sites_by_country(sites, target_country)
    rng = random.Random(seed) if seed else random.Random()
    rng.shuffle(sites)
    return sites[:max(1, n)]


def roll_dwell(dwell_range: Iterable[float], rng: random.Random | None = None) -> float:
    """Convert a (low, high) tuple into a concrete dwell seconds value."""
    rng = rng or random
    lo, hi = dwell_range if isinstance(dwell_range, (list, tuple)) else (dwell_range, dwell_range)
    return rng.uniform(float(lo), float(hi))

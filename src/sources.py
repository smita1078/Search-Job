"""Job source adapters.

Each adapter returns a list of dicts with a common shape:
    {id, title, company, location, url, description, posted, source}

All three sources here have public APIs with free tiers. None of them
require scraping, so nothing here breaks when a site changes its HTML.
"""

import os
import re
import hashlib
import logging
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)
TIMEOUT = 30


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def make_id(title, company, location):
    """Stable dedupe key. Same role cross-posted to 3 boards -> one entry."""
    raw = f"{_norm(title)}|{_norm(company)}|{_norm(location).split()[0] if location else ''}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _job(title, company, location, url, description, posted, source):
    return {
        "id": make_id(title, company, location),
        "title": (title or "").strip(),
        "company": (company or "Unknown").strip(),
        "location": (location or "").strip(),
        "url": url,
        "description": (description or "").strip(),
        "posted": posted,
        "source": source,
    }


# --------------------------------------------------------------------------
# Adzuna  -- https://developer.adzuna.com/  (free tier, has an India index)
# --------------------------------------------------------------------------
def fetch_adzuna(query, location, max_days_old=2, country="in", limit=50):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        log.warning("Adzuna credentials missing, skipping")
        return []

    url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
    params = {
        "app_id": app_id,
        "app_key": app_key,
        "what": query,
        "where": location,
        "max_days_old": max_days_old,
        "results_per_page": limit,
        "sort_by": "date",
        "content-type": "application/json",
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        log.error("Adzuna failed for %r: %s", query, e)
        return []

    out = []
    for j in results:
        out.append(_job(
            title=j.get("title"),
            company=(j.get("company") or {}).get("display_name"),
            location=(j.get("location") or {}).get("display_name"),
            url=j.get("redirect_url"),
            description=j.get("description"),
            posted=j.get("created"),
            source="adzuna",
        ))
    return out


# --------------------------------------------------------------------------
# Jooble  -- https://jooble.org/api/about  (free key on request)
# --------------------------------------------------------------------------
def fetch_jooble(query, location, limit=50):
    key = os.getenv("JOOBLE_API_KEY")
    if not key:
        log.warning("Jooble key missing, skipping")
        return []

    try:
        r = requests.post(
            f"https://jooble.org/api/{key}",
            json={"keywords": query, "location": location, "page": "1"},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("jobs", [])[:limit]
    except Exception as e:
        log.error("Jooble failed for %r: %s", query, e)
        return []

    out = []
    for j in results:
        out.append(_job(
            title=j.get("title"),
            company=j.get("company"),
            location=j.get("location"),
            url=j.get("link"),
            description=re.sub(r"<[^>]+>", " ", j.get("snippet") or ""),
            posted=j.get("updated"),
            source="jooble",
        ))
    return out


# --------------------------------------------------------------------------
# Careerjet -- https://www.careerjet.com/partners/api/  (free affid)
# --------------------------------------------------------------------------
def fetch_careerjet(query, location, limit=50, locale="en_IN"):
    affid = os.getenv("CAREERJET_AFFID")
    if not affid:
        log.warning("Careerjet affid missing, skipping")
        return []

    params = {
        "keywords": query,
        "location": location,
        "affid": affid,
        "locale_code": locale,
        "pagesize": limit,
        "sort": "date",
        "user_ip": "1.2.3.4",
        "user_agent": "Mozilla/5.0 (jobradar)",
        "url": "https://example.com",
    }
    try:
        r = requests.get(
            "https://public.api.careerjet.net/search", params=params, timeout=TIMEOUT
        )
        r.raise_for_status()
        results = r.json().get("jobs", []) or []
    except Exception as e:
        log.error("Careerjet failed for %r: %s", query, e)
        return []

    out = []
    for j in results:
        out.append(_job(
            title=j.get("title"),
            company=j.get("company"),
            location=j.get("locations"),
            url=j.get("url"),
            description=j.get("description"),
            posted=j.get("date"),
            source="careerjet",
        ))
    return out


FETCHERS = {
    "adzuna": fetch_adzuna,
    "jooble": fetch_jooble,
    "careerjet": fetch_careerjet,
}


def fetch_all(queries, locations, enabled_sources):
    """Run every (query x location x source) combination and dedupe."""
    seen, jobs = set(), []
    for source in enabled_sources:
        fn = FETCHERS.get(source)
        if not fn:
            log.warning("Unknown source %s", source)
            continue
        for q in queries:
            for loc in locations:
                batch = fn(q, loc)
                log.info("%s | %r @ %r -> %d", source, q, loc, len(batch))
                for j in batch:
                    if j["id"] in seen or not j.get("url"):
                        continue
                    seen.add(j["id"])
                    j["fetched_at"] = datetime.now(timezone.utc).isoformat()
                    jobs.append(j)
    return jobs

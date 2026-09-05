"""Scoring: cheap prefilter, then a ranking pass over the survivors.

Three scoring backends, picked automatically by which key is present:
  GEMINI_API_KEY    -> Google Gemini (free tier, no card needed)
  ANTHROPIC_API_KEY -> Claude
  neither           -> heuristic scoring, no network calls at all

The heuristic path is not a stub. It applies seniority penalties, stack
matching and employer-type signals, and spreads results across the 0-10
range instead of flattening everything to the ceiling.
"""

import os
import re
import json
import logging

import requests

log = logging.getLogger(__name__)
TIMEOUT = 180


# --------------------------------------------------------------------------
# Stage 1: keyword prefilter
# --------------------------------------------------------------------------
def prefilter(jobs, profile, keep=45):
    must = [k.lower() for k in profile.get("must_have_any", [])]
    nice = [k.lower() for k in profile.get("nice_to_have", [])]
    block_title = [k.lower() for k in profile.get("exclude_title_keywords", [])]
    block_text = [k.lower() for k in profile.get("exclude_keywords", [])]
    max_yrs = profile.get("max_years_experience_in_jd")

    scored = []
    for j in jobs:
        title = j["title"].lower()
        blob = f"{title} {j['description'].lower()}"

        if any(b in title for b in block_title):
            continue
        if any(b in blob for b in block_text):
            continue

        if max_yrs:
            years = [int(m) for m in re.findall(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d*\s*year", blob)]
            if years and min(years) > max_yrs:
                continue

        if must and not any(k in blob for k in must):
            continue

        s = sum(2 for k in must if k in blob) + sum(1 for k in nice if k in blob)
        s += sum(3 for k in must if k in title)
        j["prefilter_score"] = s
        scored.append(j)

    scored.sort(key=lambda x: -x["prefilter_score"])
    log.info("Prefilter: %d -> %d (keeping top %d)", len(jobs), len(scored), keep)
    return scored[:keep]


# --------------------------------------------------------------------------
# Stage 2a: heuristic scoring (no API key required)
# --------------------------------------------------------------------------

SENIORITY_PENALTIES = [
    (r"\b(lead|principal|staff|architect|manager|head|director|vp)\b", -5, "senior-level title"),
    (r"\bsenior\b", -1, "senior title"),
    (r"\b(iii|3)\b", -1, "level 3 role"),
    (r"\b(intern|fresher|trainee|graduate)\b", -6, "entry-level"),
]

STACK_PENALTIES = [
    (r"\b(full[ -]?stack|fullstack)\b", -3, "full stack, not pure backend"),
    (r"\bmanual\b", -3, "manual testing component"),
    (r"\b(kotlin|scala)\b", -2, "different JVM language"),
    (r"\b(react|angular|vue|frontend|ui/ux)\b", -3, "frontend-heavy"),
    (r"\b(python|golang|\.net|c#|php|scala|ruby)\b", -2, "different primary language"),
]

STAFFING_HINTS = [
    "consulting", "consultancy", "staffing", "resourcing", "manpower",
    "infosystems", "solutions private", "services private", "technologies private",
    "recruit", "hire", "placement", "outsourc",
]


def _heuristic_score(job, profile):
    title = job["title"].lower()
    company = job["company"].lower()
    blob = f"{title} {job['description'].lower()}"
    reasons = []

    nice = [k.lower() for k in profile.get("nice_to_have", [])]
    hits = [k for k in nice if k in blob]

    score = 4 + min(4, len(hits))
    if hits:
        reasons.append("matches " + ", ".join(hits[:4]))

    for pattern, delta, why in SENIORITY_PENALTIES + STACK_PENALTIES:
        if re.search(pattern, title):
            score += delta
            reasons.append(why)

    if any(h in company for h in STAFFING_HINTS):
        score -= 2
        reasons.append("likely staffing/consultancy")

    years = [int(m) for m in re.findall(r"(\d{1,2})\s*\+?\s*(?:-|to)?\s*\d*\s*year", blob)]
    if years:
        lo = min(years)
        if lo <= 2:
            score += 3
            reasons.append(str(lo) + "+ yrs - ideal")
        elif lo == 3:
            score += 1
            reasons.append("3+ yrs - a stretch")
        elif lo == 4:
            score -= 2
            reasons.append("wants 4+ yrs")
        else:
            score -= 5
            reasons.append("wants " + str(lo) + "+ yrs")
    else:
        reasons.append("no yrs stated")

    job["score"] = max(0, min(10, score))
    job["reason"] = "; ".join(reasons[:3]) or "generic match"
    return job


def heuristic_score(jobs, profile):
    log.info("Scoring %d jobs heuristically (no API key)", len(jobs))
    for j in jobs:
        _heuristic_score(j, profile)
    jobs.sort(key=lambda x: -x["score"])
    return jobs


# --------------------------------------------------------------------------
# Stage 2b: LLM scoring
# --------------------------------------------------------------------------
PROMPT = """You are screening job postings for a specific candidate.

<resume>
{resume}
</resume>

<preferences>
{prefs}
</preferences>

Below are {n} job postings. Note the descriptions are TRUNCATED SNIPPETS, not
full JDs -- judge on the title, company and what the snippet shows, and do not
assume missing requirements are absent.

For EACH posting, judge fit considering: skill overlap, seniority match (do not
recommend roles far above or below their level), location fit, and whether it
looks like a real direct opening rather than a staffing-agency dragnet.

Be strict. 9-10 means "drop everything and apply today". Most jobs are 4-6.

Return ONLY a JSON array, no markdown fences, no preamble:
[{{"id": "<job id>", "score": <0-10 integer>, "reason": "<max 20 words>"}}]

<jobs>
{jobs}
</jobs>"""


def _call_anthropic(prompt):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "content-type": "application/json",
            "x-api-key": os.environ["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
            "max_tokens": 2000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return "".join(
        b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text"
    )


def _call_gemini(prompt):
    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
    r = requests.post(
        "https://generativelanguage.googleapis.com/v1beta/models/" + model + ":generateContent",
        headers={
            "content-type": "application/json",
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        },
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8000,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        },
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    cands = r.json().get("candidates", [])
    if not cands:
        raise ValueError("Gemini returned no candidates")
    return "".join(p.get("text", "") for p in cands[0]["content"]["parts"])


def _parse(text):
    """Parse the model's JSON array, salvaging what we can if it was cut off."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start = text.find("[")
    if start == -1:
        raise ValueError("No JSON array in model output: " + text[:200])

    end = text.rfind("]")
    if end != -1:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    # Truncated or malformed: pull out every complete {...} object instead of
    # discarding the whole batch. A partial answer beats no answer.
    objs = []
    for m in re.finditer(r"\{[^{}]*\}", text[start:]):
        try:
            objs.append(json.loads(m.group()))
        except json.JSONDecodeError:
            continue
    if not objs:
        raise ValueError("No parseable objects in model output: " + text[:200])
    log.warning("Model output was truncated -- salvaged %d entries", len(objs))
    return objs


def llm_score(jobs, resume, profile, batch_size=6):
    if os.getenv("GEMINI_API_KEY"):
        call, name = _call_gemini, "gemini"
    elif os.getenv("ANTHROPIC_API_KEY"):
        call, name = _call_anthropic, "anthropic"
    else:
        return heuristic_score(jobs, profile)

    log.info("Scoring %d jobs via %s", len(jobs), name)
    prefs = json.dumps(
        {k: profile[k] for k in ("target_roles", "locations", "seniority", "notes") if k in profile},
        indent=2,
    )

    by_id = {j["id"]: j for j in jobs}
    for i in range(0, len(jobs), batch_size):
        batch = jobs[i:i + batch_size]
        blob = "\n\n".join(
            "[" + j["id"] + "] " + j["title"] + " @ " + j["company"] + " (" + j["location"] + ")\n"
            + j["description"][:1200]
            for j in batch
        )
        prompt = PROMPT.format(resume=resume[:12000], prefs=prefs, n=len(batch), jobs=blob)
        try:
            for row in _parse(call(prompt)):
                j = by_id.get(row.get("id"))
                if j:
                    j["score"] = int(row.get("score", 0))
                    j["reason"] = row.get("reason", "")
        except Exception as e:
            log.error("Batch %d failed (%s) -- falling back to heuristic", i // batch_size, e)
            for j in batch:
                _heuristic_score(j, profile)

    for j in jobs:
        if "score" not in j:
            _heuristic_score(j, profile)

    jobs.sort(key=lambda x: -x["score"])
    return jobs

# Job radar

Daily job search on autopilot. Pulls fresh backend/SDE postings from three
aggregator APIs, scores each one against your actual resume with an LLM, then
writes the good ones to a Google Sheet and emails you the top 15.

Runs on GitHub Actions. Free, apart from a few cents a day of LLM tokens.

```
GitHub Actions (daily 8:30 IST)
   └─> fetch: Adzuna + Jooble + Careerjet
        └─> drop anything seen in the last 60 days
             └─> keyword prefilter  (200 jobs -> 40)
                  └─> LLM fit scoring against resume.txt  (40 -> scored 0-10)
                       └─> Google Sheet (all)  +  email digest (top 15)
```

## A note on LinkedIn and Naukri

Neither has a public job-search API, and both actively block automated
collection — LinkedIn's terms of service prohibit it outright. A scraper
against either will work briefly and then break silently, which is the worst
possible failure mode for a job hunt.

So this project uses aggregators that *do* offer APIs. Adzuna in particular
crawls thousands of job sites including the major Indian boards, so a good
share of what appears on Naukri shows up there anyway.

For LinkedIn-native postings, do this instead, which is entirely above board:

1. On LinkedIn, run your searches and hit **Create job alert** → daily email.
2. In Gmail, filter those alerts to a label.
3. Skim that label once a day.

If you later want those folded into the same sheet, the Gmail API can read
that label and parse the alert emails — same pipeline, one extra source
adapter. Worth doing only once the rest is running.

## Setup

### 1. Repo

```bash
git init && git add . && git commit -m "init"
gh repo create jobradar --private --source=. --push
```

Keep it **private** — your resume goes in as a secret, and `seen.json` gets
committed back on every run.

### 2. API keys

| Service | Where | Notes |
|---|---|---|
| Adzuna | developer.adzuna.com | Free tier, instant. Best India coverage — set this up first. |
| Jooble | jooble.org/api/about | Request a free key; usually approved in a day. |
| Careerjet | careerjet.com/partners/api | Free affiliate ID. |
| Anthropic | console.anthropic.com | For the scoring stage. |

The pipeline degrades gracefully — any source without a key is skipped, and
with no Anthropic key it falls back to keyword scores. You can start with
Adzuna alone and add the rest later.

### 3. Google Sheet

1. Create a sheet. Grab the ID from the URL: `docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`
2. In Google Cloud Console: new project → enable the Google Sheets API →
   create a service account → make a JSON key.
3. **Share the sheet with the service account's email** (it looks like
   `something@project.iam.gserviceaccount.com`) with Editor access. This step
   is the one everybody forgets.

### 4. Email

Gmail with 2FA on: create an [App Password](https://myaccount.google.com/apppasswords)
and use that as `SMTP_PASSWORD`, not your real password.

### 5. Your profile

```bash
cp profile.example.json profile.json   # then edit it
```

Paste your resume as plain text into `resume.txt`. Both files are gitignored
and get injected from secrets at runtime.

`profile.json` is the dial you'll actually tune. If you're getting agency spam,
add words to `exclude_keywords`. If good roles are being filtered out, loosen
`must_have_any`.

### 6. Secrets

Settings → Secrets and variables → Actions:

```
ADZUNA_APP_ID, ADZUNA_APP_KEY, JOOBLE_API_KEY, CAREERJET_AFFID
ANTHROPIC_API_KEY
GOOGLE_SERVICE_ACCOUNT_JSON   (paste the whole JSON file contents)
SHEET_ID
SMTP_USER, SMTP_PASSWORD, EMAIL_TO
RESUME_TEXT                   (paste resume.txt contents)
PROFILE_JSON                  (paste profile.json contents)
```

### 7. Test

```bash
pip install -r requirements.txt
export ADZUNA_APP_ID=... ADZUNA_APP_KEY=... ANTHROPIC_API_KEY=...
python src/main.py --dry-run
```

Then trigger the workflow manually from the Actions tab with dry-run checked
before letting the schedule take over.

## Tuning

Give it a week before judging it. The first few runs will surface junk; each
round of edits to `exclude_title_keywords` cuts a lot of it.

- **Too few results?** Broaden `queries`, raise `max_days_old` in
  `sources.fetch_adzuna`, lower `min_score`.
- **Too much noise?** Raise `min_score` to 7, tighten the exclude lists.
- **Costs too much?** Lower `llm_batch_cap`. At 40 jobs/day it's a few cents.

The `status` column in the sheet is yours — use it to track
applied / interviewing / rejected so the sheet becomes your actual tracker,
not just a firehose.

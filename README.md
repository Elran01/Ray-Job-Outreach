# RAY EA/Admin Job Outreach Automation

## 1. What it does

Every day this pipeline collects remote Executive/Administrative Assistant job postings from public, non-LinkedIn job boards, uses an LLM to check that each one is really remote, English-speaking and an EA/admin-type role, and writes a short personalized outreach message for each job that passes. Everything lands in a Google Sheet that a sales rep can work from, and a job that is already in the Sheet is never added or paid for again.

**Live Sheet:** <https://docs.google.com/spreadsheets/d/16c6zWJ5x2K1lLfdCEgAjTdZqaiYAEHZE-iiduCHN0wI> (access is managed by the Sheet owner; reviewers are added as viewers/editors)

```
 job boards (public APIs / RSS)         config.yaml: search_queries
 Himalayas, Remotive, WWR, Remote OK ──► collect + dedupe (URL, then company+title) ──► data/jobs_raw.json
                                                      │
                                   keyword prefilter on the title (free) ──► dropped titles ─┐
                                                      │                                      │
                      drop jobs whose URL is already in the Google Sheet (no LLM spend)      │
                                                      │                                      ▼
                       cap at limits.max_jobs_per_run (and --limit)            data/jobs_rejected.csv
                                                      │                                      ▲
            LLM validation (prompts/validate.md, Haiku): remote? English? relevant? ─ fail ──┘
                                                      │ pass
            LLM outreach (prompts/outreach*.md + profile/sender_profile.md, Sonnet)
                                                      │
                          append new rows to the Google Sheet (Status = New)
```

Repo layout:

```
config.yaml            search terms, sources, filters, models, caps, sheet tab name
prompts/validate.md    validation prompt (what counts as remote / English / relevant)
prompts/outreach.md    outreach prompt, professional tone (default)
prompts/outreach_casual.md   outreach prompt, short and friendly
profile/sender_profile.md    who is sending and what is offered
src/                   main.py (CLI), sources/, filter.py, outreach.py, sheets.py, storage.py, models.py
tests/                 offline unit tests
data/                  run outputs (git-ignored) + committed samples (sample_*.json/csv)
.github/workflows/daily.yml  daily schedule + manual trigger
```

## 2. Install

Requires **Python 3.11 or newer** (developed on 3.13, CI uses 3.12).

```powershell
# Windows (PowerShell)
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Check the install with `python -m pytest -q` (offline, no keys needed).

## 3. Setup

You need three things: an Anthropic API key, a Google service account, and a Google Sheet shared with it.

### 3.1 Anthropic key
Create a key at <https://console.anthropic.com/> (and consider setting a monthly spend limit under Billing).

### 3.2 Google service account
A service account is a robot Google login that belongs to the program, so no browser sign-in popup is needed (this is also what lets it run unattended on GitHub).

1. Go to <https://console.cloud.google.com>, create a project (e.g. `ray-ea-outreach`).
2. **APIs & Services → Library →** search **Google Sheets API → Enable**.
3. **IAM & Admin → Service Accounts → Create service account** (any name, skip the optional role steps).
4. Open the service account → **Keys → Add key → Create new key → JSON**. A `.json` file downloads.
5. Save it in the project folder as `service_account.json`. It is git-ignored; **never commit or share it**.
6. Copy the service account's email (the `client_email` field in the file, looks like `name@project.iam.gserviceaccount.com`).

### 3.3 Share the Sheet
Create an empty Google Sheet, click **Share**, paste the service account email and give it **Editor** access. Copy the Sheet ID from its URL (the part between `/d/` and `/edit`). The pipeline creates the `Jobs` tab, header, dropdown and formatting on its first real run. Also share the Sheet with whoever needs to read it.

### 3.4 Environment variables
Copy `.env.example` to `.env` and fill it in (`.env` is git-ignored):

| Variable | Required | What |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Anthropic API key |
| `SHEET_ID` | yes (to write) | the long id from the Sheet URL |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | one of these two | path to the key file, e.g. `service_account.json` |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | one of these two | the full JSON text of the key (used on GitHub; takes precedence over the file) |
| `HIMALAYAS_API_KEY` | no | free Himalayas key for higher rate limits; works without it |

Models, caps and the worksheet name live in `config.yaml` (never secrets).

## 4. How to run

Always start small, because the LLM calls cost money:

```powershell
python -m src.main --dry-run --limit 5     # everything except writing to the Sheet, 5 jobs max
python -m src.main --limit 5               # real run, 5 jobs max
python -m src.main                         # full run, capped by limits.max_jobs_per_run (10 by default)
python -m src.main --skip-llm              # collect + free prefilter only, no tokens spent
```

| Flag | Meaning |
|---|---|
| `--dry-run` | Do everything except write to the Sheet. The Sheet is only *read* (to skip jobs already in it); no tab, header or rows are created. LLM calls still happen. |
| `--limit N` | Send at most N jobs to the LLM. It can only lower `limits.max_jobs_per_run`, never raise it. |
| `--skip-llm` | No LLM calls: collect, prefilter, save `data/jobs_raw.json`, stop. Nothing is written to the Sheet. |
| `--outreach-prompt PATH` | Use a different outreach prompt file. Repeat the flag to generate one message per prompt per job (the Sheet gets the first). |
| `--outreach-only` | Skip collection/validation; write messages for the jobs in `data/jobs_filtered.csv`. |
| `--sheet-only` | Append `data/jobs_outreach.csv` to the Sheet (honours `--dry-run`). |

Running it twice in a row adds nothing the second time: jobs whose URL is already in the Sheet are skipped before any LLM call.

**End-of-run summary** (also shown on the GitHub run page):

```
collected                        : 285 matched -> 237 unique after dedupe
after prefilter                  : 237 (0 dropped by title keywords)
already in sheet                 : 5 (skipped before the LLM, no tokens spent)
sent to LLM                      : 5 (capped by --limit 5; 227 left for a later run)
passed validation                : 3 (2 rejected by LLM)
messages written                 : 3 using prompts/outreach.md
sheet rows written               : 3
errors                           : 0
```

**Files written to `data/`** (overwritten each run, git-ignored):

| File | Content |
|---|---|
| `jobs_raw.json` | every unique job collected, before filtering |
| `jobs_filtered.csv` | jobs that passed validation this run, with extracted language requirements |
| `jobs_rejected.csv` | prefilter and LLM rejects with the reason and which criterion failed |
| `jobs_outreach.csv` | generated messages, with the prompt file used (`prompt_used`), model, word count, guardrail flags |
| `last_run.log` | full log of the last run |

**Committed samples** (real output, descriptions shortened to 400 characters): `data/sample_raw.json` (11 collected postings from three sources), `data/sample_filtered.csv` (10 validated jobs with the LLM's reasoning), `data/sample_outreach.csv` (5 jobs × 2 prompts = 10 messages).

## 5. How to adjust filters

Everything is in `config.yaml` unless noted.

- **Search terms:** `search_queries` (e.g. "executive assistant", "EA to CEO"). Himalayas searches server-side; the other sources are matched locally as whole phrases.
- **Free title prefilter:** `excluded_title_keywords`. A title containing one of these as a whole word (plurals and "-ing" match; "sales" does not hit "Salesforce") is dropped before the LLM.
- **Titles that are never dropped:** `protected_title_keywords` (e.g. "assistant", "admin"). They exempt a title from the exclusion list, so "Executive Assistant to the VP of Sales" still reaches the LLM. Ambiguous ones like "Sales Admin Manager" are judged by the LLM.
- **Validation rules:** edit `prompts/validate.md`. It defines the three yes/no criteria the LLM answers:
  - *remote* = fully remote, not hybrid or on-site; a job restricted to certain countries or time zones is still remote;
  - *English-speaking* = English is a working language and no other language is *required* (a nice-to-have second language is fine and is recorded in `Language Requirements`);
  - *relevant* = executive/administrative/personal/virtual assistant work, not e.g. "Sales Admin Manager".

  A job is kept only if all three are true. The reply must be strict JSON; it is validated, retried once on failure, then skipped and logged.
- **Cost caps:** `limits.max_jobs_per_run` (default **10**), `limits.llm_workers` (parallel calls), `limits.validation_description_max_chars`.
- **Which sources run:** `sources.<name>.enabled`.
- **Models:** `llm.validation_model` (cheap, default `claude-haiku-4-5`) and `llm.outreach_model` (writing, default `claude-sonnet-4-5`).

## 6. How to change the LLM prompt

The outreach prompt is a plain text file that is read at run time. **Changing the tone means editing the file or pointing at another one; no code changes.**

- **Default prompt:** `prompts/outreach.md` (professional). Set a different default with `prompts.outreach` in `config.yaml`.
- **Shipped variants:** `prompts/outreach.md` (professional, ~100-120 words) and `prompts/outreach_casual.md` (short and friendly, ~60 words).
- **One run with another prompt:**
  ```powershell
  python -m src.main --limit 5 --outreach-prompt prompts/outreach_casual.md
  ```
  Repeat the flag to compare prompts side by side in `data/jobs_outreach.csv`. On GitHub, use the `outreach_prompt` box on the manual "Run workflow" form.
- **Template variables** (written as `{{name}}` in the prompt file):

  | Variable | Filled with |
  |---|---|
  | `{{job_title}}` | the posting's title |
  | `{{company}}` | the posting's company |
  | `{{location}}` | the posting's location |
  | `{{job_description}}` | the posting text (shortened to `limits.outreach_description_max_chars`) |
  | `{{sender_profile}}` | the contents of `profile/sender_profile.md` |

  A misspelled variable (e.g. `{{jobtitle}}`) stops the run with a clear error before any money is spent. `<!-- HTML comments -->` in prompt files are removed before sending.
- **Who is sending:** edit `profile/sender_profile.md` (name, what is offered, call to action, and claims the model must not make). Assumption: the message is from RAY pitching candidates/a service to the hiring company. If it should be a personal job application, change that file and the prompts only.
- **Rules enforced in code on top of the prompt:** messages over `limits.outreach_max_words` (130), containing "hope this email finds you well", an unfilled `[Name]` placeholder, or unprovable claims such as "pre-vetted" get one automatic rewrite; anything still failing is flagged in the `flags` column.
- **Traceability:** each Sheet row records the prompt file that produced its message in the `Prompt Used` column.

## 7. Scheduling

`.github/workflows/daily.yml` runs the pipeline on GitHub Actions every day at **06:17 UTC** and can be started by hand.

- **Required secrets** (repository **Settings → Secrets and variables → Actions → New repository secret**): `ANTHROPIC_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON` (paste the whole key file), `SHEET_ID`. Optional: `HIMALAYAS_API_KEY`.
- **Change the schedule:** edit the `cron:` line in the workflow (`minute hour day-of-month month day-of-week`, always UTC; e.g. `"0 */12 * * *"` for twice a day).
- **Run it manually:** **Actions → Daily EA outreach → Run workflow.** Optional inputs: `dry_run`, `limit`, `outreach_prompt`. Try `dry_run` + `limit 5` first.
- **Pause it:** Actions → Daily EA outreach → ⋯ → Disable workflow.
- **No state is stored in CI.** Each run starts on a clean machine and decides what is new by reading the URLs already in the Google Sheet. Overlapping runs queue instead of running together.
- **Where to see results:** the run page shows the summary table, and the `run-outputs` artifact (kept 14 days) has the CSV/JSON files and the log.
- **Cost:** a run sends at most `limits.max_jobs_per_run` jobs (10) to the LLM, so a daily run is a handful of cheap validation calls plus one outreach call per job that passes.
- **Taking it over:** the workflow file travels with the code, but secrets and run history do not. In a new repository, add your own three secrets (and your own Anthropic key, service account and Sheet) and enable Actions.
- GitHub pauses scheduled workflows on public repositories after about 60 days without repository activity; the manual button still works.

## 8. Troubleshooting

| Symptom | What to check |
|---|---|
| **Empty results / 0 rows added** | Read the summary. `already in sheet` high means everything found is already there. `after prefilter` low means search terms or excluded keywords are too strict. `passed validation` 0 means the LLM rejected everything (see `data/jobs_rejected.csv`, `reason` column). A source line saying `FAILED` in the source table means that board was unreachable (the others still run). Using `--limit` or the 10-job cap means only that many new jobs are processed per run; the rest are picked up on later runs. |
| **`Spreadsheet ... not found or not shared`** | Share the Sheet with the service account email (Editor). The error message prints the exact email. Check `SHEET_ID` has no spaces or quotes. |
| **`403` / `Google Sheets API has not been used...`** | Enable the Google Sheets API in the Google Cloud project that owns the service account. |
| **`Google service account key file not found`** | `GOOGLE_SERVICE_ACCOUNT_FILE` must point at the downloaded JSON (path relative to the project folder is fine), or set `GOOGLE_SERVICE_ACCOUNT_JSON`. |
| **`Row 1 of worksheet 'Jobs' does not start with the expected columns`** | The tab has other content in row 1. Use an empty tab, or restore the header (columns A-F as in the Sales rep guide). The pipeline refuses to write under a wrong header rather than scramble your data. |
| **`ANTHROPIC_API_KEY is not set`** | Create `.env` (see Setup) or run with `--skip-llm`. On GitHub, add the secret; the run prints which secret is missing. |
| **LLM JSON parse failures** | The reply is validated; on failure it is retried once, then the job is skipped and listed in `data/jobs_rejected.csv` with stage `llm_error`, and counted in the summary's `errors`. Details are in `data/last_run.log`. Occasional ones are normal; many suggest a prompt edit broke the JSON format in `prompts/validate.md`. |
| **`temperature: Extra inputs are not permitted` (HTTP 400)** | The current Anthropic API rejects that parameter, so `config.yaml` leaves the temperature settings `null` (not sent). Keep them `null`. |
| **Rate limits (HTTP 429 / 5xx)** | Retried automatically with exponential backoff: job-board requests (waits 2, 4, 8 s), Sheets calls (2, 4, 8, 16 s) and Anthropic calls (SDK retries). Remotive asks for at most about 4 calls a day, so responses are cached for 6 hours (`http.cache_ttl_minutes`). If Anthropic limits you, lower `limits.llm_workers`. |
| **Only a few new jobs each day** | By design: `limits.max_jobs_per_run` caps spend. Raise it deliberately if you want more. |
| **A deleted Sheet row came back** | The pipeline treats a missing URL as new. Set Status to `Closed` instead of deleting rows. |

## 9. Sales rep guide

**What the Sheet is:** one row per job posting that is remote, English-speaking and an assistant-type role, with a ready-to-edit outreach message. The pipeline only ever **adds new rows at the bottom**; it never changes or removes existing rows.

**Columns**

| Column | What it holds |
|---|---|
| A **Job Posting URL** | link to the original posting (open it to read the full ad and find who posted it) |
| B **Job Title** | title of the role |
| C **Company Name** | hiring company |
| D **Location** | the region the candidate must be based in (not an office) |
| E **Job Description** | the posting text, shortened to about 1,500 characters |
| F **LLM-Generated Outreach Message** | the draft message to send |
| G **Status** | your progress (dropdown, see below) |
| H **Date Added** | when the pipeline added the row |
| I **Source** | which job board it came from (Himalayas, Remotive, We Work Remotely, Remote OK) |
| J **Language Requirements** | what the posting says about language, e.g. "English (fluent)" |
| K **Prompt Used** | which prompt file wrote the message (tone) |
| L **Notes** | yours; free text |

Columns A-F (dark header) are the core data; G-L (grey header, after the thick divider) are the working columns.

**Status meanings**

| Status | Use it when |
|---|---|
| **New** | nobody has contacted them yet (every new row starts here) |
| **Contacted** | you have sent a message |
| **Replied** | they answered |
| **Closed** | finished or not pursuing (won, lost, or not a fit) |

**Working a row**
1. Read the title, company and description (and open the posting link if you need more).
2. Read the message in column F. Check the company name and any detail it quotes against the posting, since the message was written by an LLM.
3. Copy it into your email or LinkedIn message, edit anything you like, and send it. Nothing is sent automatically.
4. Set **Status** to `Contacted` and add anything useful in **Notes** (who you wrote to, follow-up date).

**Please do not**
- edit or delete the header row, or move/rename the columns A-F;
- change a **Job Posting URL**: it is how the pipeline recognises jobs it already added;
- delete rows you are finished with (the job may be added again later). Set Status to `Closed` instead;
- type other values in Status (the dropdown only accepts the four above).

You can safely sort, filter, add your own columns to the right of column L, and edit message text in column F. Your Status and Notes are never overwritten.

## 10. Sources used and their terms/attribution

Terms were checked when each source was built (October 2026). Please re-check the linked pages before commercial use, because providers can change their terms. **No LinkedIn is used. No pages are scraped; only each provider's public API or RSS feed is read.** All requests send a descriptive User-Agent, are rate limited, and are cached while developing.

| Source | How it is read | Terms / attribution we follow |
|---|---|---|
| **Himalayas** (<https://himalayas.app>), [API docs](https://himalayas.app/docs/remote-jobs-api) | public search API (server-side search, 20 jobs per page). Optional free key for higher limits. | Visible **Himalayas** credit and link back required: every row's `Job Posting URL` is the Himalayas job page and `Source` says `himalayas`; credited here. Jobs must not be passed on to other job boards, aggregators, Google Jobs or LinkedIn: output stays in a private Sheet. |
| **Remotive** (<https://remotive.com>), [API repo](https://github.com/remotive-com/remote-jobs-api) | public API, one cached call per run | Link back to the Remotive job URL and name **Remotive** as source (the row keeps Remotive's own URL; `Source` says `remotive`). Do not republish jobs to third-party job sites. Rate limit: about 4 calls a day advised; we make one call per run and cache it. The endpoint currently returns only about 10 jobs and ignores search parameters, so queries are matched locally. |
| **We Work Remotely** (<https://weworkremotely.com>) | the public RSS feeds listed in `config.yaml`; robots.txt allows them | No HTML scraping and no search endpoints. Rows link back to weworkremotely.com and `Source` says `weworkremotely`. Feeds are fetched once per run, with a delay between them. |
| **Remote OK** (<https://remoteok.com>), [API](https://remoteok.com/api) | public API, one call returning the latest ~100 jobs | Per the API's legal notice: link back to the job's Remote OK URL and name **Remote OK** as source, or API access can be suspended. We do not use the Remote OK logo. Rows keep the Remote OK URL and `Source` says `remoteok`. |

Credit: job data from [Himalayas](https://himalayas.app), [Remotive](https://remotive.com), [We Work Remotely](https://weworkremotely.com) and [Remote OK](https://remoteok.com). The committed `data/sample_*` files contain short excerpts (400 characters) of real postings, each with its source URL, for demonstration only.

## 11. Process notes

**Tools:** Python 3.11+, `requests` + `feedparser` + BeautifulSoup (collection), `pydantic` (models and strict JSON validation), the Anthropic SDK (Claude), `gspread` + `google-auth` (Sheets), `pytest`, GitHub Actions. Built phase by phase with Cursor's AI agent, running and reviewing the output after each phase (collect, second source and dedupe, filter and validate, outreach, Sheets, CLI and logging, workflow, docs).

**Key decisions**
- **Sources:** Himalayas supplies nearly all the volume. Remotive's public endpoint returns only about 10 jobs and Remote OK has no search, so they add little but are kept (and compliant) as extra sources.
- **Two-stage filter:** a free title prefilter first, then an LLM check, so tokens go only to plausible jobs. Rejected jobs are written with reasons (`data/jobs_rejected.csv`) so decisions can be audited.
- **Model split:** a cheap model (Haiku) for yes/no validation, a stronger one (Sonnet) for writing.
- **Dedupe lives in the Sheet:** no database or stored state, so CI needs nothing persisted. Jobs already in the Sheet are removed before any LLM call.
- **Append-only Sheet writes** protect a rep's Status/Notes edits; formatting calls never touch cell values.
- **Messages are drafts, never sent.** Auto-sending is out of scope.
- **Cost control:** per-run cap of 10 jobs, `--limit`, `--dry-run`, `--skip-llm`.
- **Offline tests:** the filter, parsing, dedupe, source isolation, Sheets logic, retries, dry-run behaviour and the committed samples are tested without network access or keys.

**Known limitations**
- **Staffing agencies and VA marketplaces** (e.g. Wing Assistant, Assist World) post many of the roles and appear as "companies". They pass the three criteria but are poor outreach targets; the validation prompt does not yet screen for them.
- **Rejected jobs are re-checked each day.** Only jobs already in the Sheet are skipped, so a rejected posting costs a cheap validation call again until it drops off the board.
- **Throughput is capped** (10 new jobs per run by default), so a large backlog is worked through over several days.
- **The LLM can be wrong** about remote/English/relevance and may over-read a posting; a person should skim each message before sending. Messages from the professional prompt can run slightly over the "about 120 words" target (hard guardrail at 130).
- **The Sheet shows one message per job** (the first prompt given), even if several prompts were run for comparison.
- **Sender assumption:** messages are written as RAY introducing assistant candidates to the hiring company; the sender profile contains only generic, non-numeric claims and should be reviewed against what RAY can truthfully say.
- Terms of third-party sources can change; see section 10.

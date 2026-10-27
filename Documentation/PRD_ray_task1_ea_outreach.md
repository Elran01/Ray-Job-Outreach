# PRD: EA/Admin Job Outreach Automation (RAY Case Study, Task 1)

## 0. How to use this document
Paste this whole file into Claude Code / Cursor as the first message and say: "Build this PRD phase by phase. After each phase, run it and show me the output before moving on." Do not let the agent build everything in one pass.

## 1. Goal
A pipeline that runs daily, finds remote Executive/Admin Assistant job postings from compliant non-LinkedIn sources, validates them (remote + English + relevant), writes a personalized outreach message for each using an LLM, and saves everything to a Google Sheet a sales rep can work from.

**Evaluation is on the result.** Priorities: it works end to end, the output is clean, the prompt is swappable, the README is clear.

## 2. Tech decisions (fixed)
| Area | Decision | Why |
|---|---|---|
| Language | Python 3.11+ | Simple, readable, easy for reviewers to run |
| Job sources | Public APIs/RSS only (see 4.1) | Compliant, no ToS-breaking scraping, no browser automation |
| LLM | Anthropic API (Claude), model name in `config.yaml` | Structured output + good writing |
| Sheets | `gspread` with a Google service account | No OAuth popups, works headless |
| Storage | `data/jobs_raw.json`, `data/jobs_filtered.csv` + SQLite or JSON for seen URLs | Satisfies "structured format" and dedupe |
| Scheduler | GitHub Actions cron (daily) | Free, visible, reviewer can see run history |
| Secrets | `.env` locally, GitHub Secrets in CI | Never commit keys |

## 3. Repo structure
```
ray-ea-outreach/
├── README.md
├── config.yaml              # search terms, sources, filters, model, sheet id
├── .env.example
├── requirements.txt
├── prompts/
│   ├── validate.md          # classification prompt
│   └── outreach.md          # outreach prompt (SWAPPABLE, see 4.3)
├── profile/
│   └── sender_profile.md    # who is sending / what is being offered
├── src/
│   ├── main.py              # orchestrates the pipeline, CLI flags
│   ├── sources/
│   │   ├── base.py          # JobSource interface -> list[Job]
│   │   ├── remotive.py
│   │   ├── weworkremotely.py
│   │   └── remoteok.py      # optional third source
│   ├── models.py            # Job dataclass / pydantic model
│   ├── filter.py            # keyword prefilter + LLM validation
│   ├── outreach.py          # message generation
│   ├── sheets.py            # write/append to Google Sheet
│   └── storage.py           # save raw/filtered, dedupe
├── data/
│   ├── sample_raw.json      # committed sample output
│   └── sample_filtered.csv
├── tests/
│   └── test_filter.py       # a few cases for the filter logic
└── .github/workflows/daily.yml
```

## 4. Functional requirements

### 4.1 Job collection
- Implement a `JobSource` interface: `fetch(queries: list[str]) -> list[Job]`.
- Build at least 2 sources. Candidates (verify each one's current terms and attribution rules before building, and put the check in the README):
  - **Remotive** public API (remote-only jobs)
  - **We Work Remotely** RSS feeds
  - **Remote OK** API (requires attribution link back)
  - Alternatives: Jobicy API, Himalayas, Adzuna API (needs key)
- **No LinkedIn. No scraping of sites whose ToS forbid it.** Send a descriptive User-Agent, rate limit requests, cache responses during dev.
- Search terms (in config): "executive assistant", "administrative assistant", "virtual assistant", "personal assistant", "office assistant", "EA to CEO".
- `Job` fields: `url`, `title`, `company`, `location`, `description` (plain text, HTML stripped), `language_requirements` (extracted later, may be null), `source`, `date_posted`, `date_collected`.
- Dedupe by normalized URL (and company+title as fallback).
- Save everything collected to `data/jobs_raw.json`.

### 4.2 Filter and validate
Two stages, so it is cheap and explainable:
1. **Keyword prefilter** (free): drop clearly irrelevant titles (e.g. "engineer", "sales", "nurse"). Keep a list in config.
2. **LLM validation** using `prompts/validate.md`. Input: title, location, description. Output: strict JSON:
```json
{
  "is_remote": true,
  "is_english_speaking": true,
  "is_relevant_role": true,
  "language_requirements": "English (fluent)",
  "reasoning": "one short sentence per criterion"
}
```
- Keep a job only if all three booleans are true.
- Rules for the prompt: "remote" means fully remote, not hybrid; "English-speaking" means English is a working language and no other language is *required* (a nice-to-have second language is fine, but record it); "relevant" means executive/admin/personal/virtual assistant type work, not e.g. "Sales Admin Manager".
- Validate the JSON (pydantic). On parse failure, retry once, then skip and log.
- Write rejected jobs with reasons to `data/jobs_rejected.csv` (shows judgment, helps debugging).

### 4.3 Outreach message (interchangeable prompt)
- Prompt lives in `prompts/outreach.md`, loaded at runtime. Changing tone = editing the file, no code change.
- Support multiple prompt files via CLI flag/config: `--outreach-prompt prompts/outreach_casual.md`. Ship at least two variants (e.g. professional, short and friendly).
- Template variables: `{{job_title}}`, `{{company}}`, `{{location}}`, `{{job_description}}`, `{{sender_profile}}`.
- `profile/sender_profile.md` holds who is reaching out and what they offer. **Assumption to confirm with RAY: the message is from the founder/RAY pitching an EA candidate or EA service to the hiring company. If it is a personal job application, adjust the profile and prompt only.**
- Personalization requirement: each message must reference at least one concrete detail from the posting (a tool, responsibility, or company trait), never generic.
- Guardrails in prompt: under ~120 words, no invented facts about the company, no fake claims about the sender, no "I hope this email finds you well", clear single call to action.
- Record which prompt file produced each message (extra column).

### 4.4 Google Sheets output
Required columns, in this order:
`Job Posting URL | Job Title | Company Name | Location | Job Description | LLM-Generated Outreach Message`

Optional extra columns to the right (clearly separated, useful for the sales rep): `Status` (dropdown: New / Contacted / Replied / Closed), `Date Added`, `Source`, `Language Requirements`, `Prompt Used`, `Notes`.
- Append only new rows, dedupe against URLs already in the sheet.
- Never overwrite `Status` / `Notes` the rep has edited.
- Freeze header row, wrap text on message column, truncate very long descriptions to ~1,500 chars (cell limit is 50,000).
- Share the sheet with reviewer view/edit access and put the link in the README.

### 4.5 Scheduling and CLI
- `python -m src.main` runs the whole pipeline. Flags: `--dry-run` (no Sheet write), `--limit N`, `--skip-llm`, `--outreach-prompt PATH`.
- `.github/workflows/daily.yml`: cron daily plus `workflow_dispatch` for manual runs. Reads secrets (`ANTHROPIC_API_KEY`, `GOOGLE_SERVICE_ACCOUNT_JSON`, `SHEET_ID`).
- Keep dedupe state somewhere persistent: simplest is to dedupe against the Sheet itself, so CI needs no stored state.

### 4.6 Logging and errors
- One source failing must not kill the run. Log and continue.
- Retry with backoff on 429/5xx for APIs and Sheets.
- End-of-run summary: collected / after prefilter / passed validation / written / errors.

## 5. Non-functional
- Secrets never in repo. `.env.example` provided.
- Cost aware: prefilter before LLM, cap jobs per run in config, use a cheaper model for validation and a stronger one for outreach if desired.
- Respect robots/ToS/rate limits.
- Deterministic where possible: temperature low for validation.

## 6. README requirements
1. What it does (3 sentences + a pipeline diagram or list)
2. Install (Python version, venv, `pip install`)
3. Setup: creating the Google service account, sharing the Sheet, setting env vars
4. How to run (local, dry run, full)
5. How to adjust filters (config.yaml: queries, excluded title keywords, validation rules in `prompts/validate.md`)
6. How to change the LLM prompt (edit/swap file, CLI flag, template variables)
7. Scheduling (GitHub Actions, how to change cadence, manual trigger)
8. Troubleshooting (empty results, Sheets permission error, JSON parse failures, rate limits)
9. **Sales rep guide**: how to read the Sheet, what Status means, how to edit/send a message, what not to touch
10. Sources used and their terms/attribution
11. Short process notes: tools used, key decisions, known limitations

## 7. Build phases (agent should stop after each and show output)
1. Scaffold repo, config, models, `.env.example`.
2. One source working, raw JSON saved, 20+ real postings printed.
3. Second source + dedupe.
4. Prefilter + LLM validation, rejected CSV with reasons. Manually spot-check 10 results.
5. Outreach generation with two prompt variants. Read 5 messages critically.
6. Google Sheets write, dedupe, formatting.
7. CLI flags, logging, summary.
8. GitHub Actions workflow, trigger a manual run, confirm Sheet updates.
9. README, sample data committed, tests for filter.

## 8. Acceptance checklist
- [ ] Runs end to end from a clean clone with only README steps
- [ ] At least 2 compliant non-LinkedIn sources
- [ ] Every row in the Sheet is remote, English, and EA/admin relevant (spot-checked)
- [ ] Messages reference real details from each posting
- [ ] Swapping the prompt file changes tone with no code edits (demonstrate with 2 variants)
- [ ] Re-running does not create duplicate rows
- [ ] Scheduled run visible in GitHub Actions history
- [ ] Sample CSV/JSON committed, Sheet shared with reviewer
- [ ] README covers everything in section 6

## 9. Out of scope
Auto-sending messages, LinkedIn, CRM integration, UI.

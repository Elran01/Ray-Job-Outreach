"""Google Sheets writer (PRD 4.4).

Append-only: existing rows are never rewritten, so a rep's Status / Notes edits are safe.
Dedupe is against the Sheet itself (normalized Job Posting URL), so no other state is needed.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional

import gspread
import requests
from google.oauth2.service_account import Credentials

from src.models import Job
from src.outreach import OutreachResult
from src.storage import normalize_url

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# PRD 4.4: the six required columns, in this exact order ...
REQUIRED_COLUMNS = [
    "Job Posting URL",
    "Job Title",
    "Company Name",
    "Location",
    "Job Description",
    "LLM-Generated Outreach Message",
]
# ... then optional rep-facing columns to the right.
OPTIONAL_COLUMNS = ["Status", "Date Added", "Source", "Language Requirements", "Prompt Used", "Notes"]
COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

STATUS_OPTIONS = ["New", "Contacted", "Replied", "Closed"]
DEFAULT_STATUS = "New"

URL_COL = COLUMNS.index("Job Posting URL")
MESSAGE_COL = COLUMNS.index("LLM-Generated Outreach Message")
STATUS_COL = COLUMNS.index("Status")
FIRST_OPTIONAL_COL = len(REQUIRED_COLUMNS)

MAX_DESCRIPTION_CHARS = 1500  # PRD: ~1,500 (a cell holds 50,000)
CELL_LIMIT = 50_000
RETRY_STATUS = {429, 500, 502, 503, 504}


# ------------------------------------------------------------- connection
def load_credentials(
    service_account_json: Optional[str] = None, service_account_file: Optional[str] = None, root: Optional[Path] = None
) -> Credentials:
    """JSON contents (CI secret) take precedence over a key-file path (local dev)."""
    if service_account_json and service_account_json.strip():
        info = json.loads(service_account_json)
        return Credentials.from_service_account_info(info, scopes=SCOPES)
    if service_account_file and service_account_file.strip():
        path = Path(service_account_file)
        if not path.is_absolute() and root:
            path = root / path
        if not path.is_file():
            raise FileNotFoundError(
                f"Google service account key file not found: {path}. Download the JSON key from Google Cloud "
                "(IAM > Service Accounts > Keys) and point GOOGLE_SERVICE_ACCOUNT_FILE at it."
            )
        return Credentials.from_service_account_file(str(path), scopes=SCOPES)
    raise RuntimeError(
        "No Google credentials: set GOOGLE_SERVICE_ACCOUNT_FILE (path) or GOOGLE_SERVICE_ACCOUNT_JSON (contents)."
    )


def open_worksheet(sheet_id: str, worksheet_name: str, creds: Credentials, create: bool = True):
    """Open the spreadsheet and return (worksheet, created).

    A missing tab is created when ``create`` is true; otherwise ``(None, False)`` is returned
    (used by dry runs, which must not modify the Sheet).
    """
    if not sheet_id or sheet_id.startswith("YOUR_"):
        raise RuntimeError("No Sheet ID: set SHEET_ID in .env (the string between /d/ and /edit in the sheet URL).")
    client = gspread.authorize(creds)
    try:
        spreadsheet = _retry(lambda: client.open_by_key(sheet_id))
    except gspread.SpreadsheetNotFound as exc:
        email = getattr(creds, "service_account_email", "the service account")
        raise RuntimeError(
            f"Spreadsheet {sheet_id} not found or not shared. Share the Sheet (Editor) with {email}."
        ) from exc
    try:
        return _retry(lambda: spreadsheet.worksheet(worksheet_name)), False
    except gspread.WorksheetNotFound:
        if not create:
            return None, False
        ws = _retry(lambda: spreadsheet.add_worksheet(title=worksheet_name, rows=200, cols=len(COLUMNS)))
        return ws, True


# ------------------------------------------------------------------ retry
def _retry(fn: Callable[[], Any], attempts: int = 5, base_delay: float = 2.0) -> Any:
    """Retry on 429 / 5xx with exponential backoff (PRD 4.6)."""
    for i in range(attempts):
        try:
            return fn()
        except gspread.exceptions.APIError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status not in RETRY_STATUS or i == attempts - 1:
                raise
            reason = f"HTTP {status}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            if i == attempts - 1:
                raise
            reason = type(exc).__name__
        delay = base_delay * (2 ** i)
        log.warning("Sheets API %s, retrying in %.0fs (%d/%d)", reason, delay, i + 1, attempts - 1)
        time.sleep(delay)


# ----------------------------------------------------------------- rows
def truncate(text: str, limit: int = MAX_DESCRIPTION_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def build_row(job: Job, message: str, prompt_used: str, date_added: Optional[date] = None, max_desc: int = MAX_DESCRIPTION_CHARS) -> list[str]:
    """One Sheet row, aligned with COLUMNS. Status defaults to New; Notes starts empty."""
    values = {
        "Job Posting URL": job.url,
        "Job Title": job.title,
        "Company Name": job.company,
        "Location": job.location,
        "Job Description": truncate(job.description, max_desc),
        "LLM-Generated Outreach Message": truncate(message, CELL_LIMIT),
        "Status": DEFAULT_STATUS,
        "Date Added": (date_added or date.today()).isoformat(),
        "Source": job.source,
        "Language Requirements": job.language_requirements or "",
        "Prompt Used": prompt_used,
        "Notes": "",
    }
    return [values[c] for c in COLUMNS]


@dataclass
class WriteResult:
    added: int = 0
    already_in_sheet: int = 0
    duplicate_in_batch: int = 0
    skipped_no_message: int = 0
    added_urls: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"added {self.added}, already in sheet {self.already_in_sheet}, "
                f"duplicates in batch {self.duplicate_in_batch}, no message {self.skipped_no_message}")


def ensure_header(ws) -> bool:
    """Write the header if row 1 is empty. Returns True if it was written now.

    If row 1 already has content that doesn't start with the six required columns we stop:
    appending under the wrong header would silently scramble the rep's data.
    """
    existing = [c.strip() for c in _retry(lambda: ws.row_values(1))]
    if not any(existing):
        _retry(lambda: ws.update(values=[COLUMNS], range_name="A1", value_input_option="RAW"))
        return True
    if existing[: len(REQUIRED_COLUMNS)] != REQUIRED_COLUMNS:
        raise RuntimeError(
            f"Row 1 of worksheet '{ws.title}' does not start with the expected columns {REQUIRED_COLUMNS}; "
            f"found {existing[:len(REQUIRED_COLUMNS)]}. Fix the header or use an empty worksheet."
        )
    return False


def existing_url_keys(ws) -> set[str]:
    urls = _retry(lambda: ws.col_values(URL_COL + 1))
    return {normalize_url(u) for u in urls[1:] if u.strip()}  # [0] is the header


def append_new_rows(
    ws,
    items: list[tuple[Job, str, str]],
    date_added: Optional[date] = None,
    max_desc: int = MAX_DESCRIPTION_CHARS,
    dry_run: bool = False,
    existing: Optional[set[str]] = None,
) -> WriteResult:
    """Append rows for (job, message, prompt_used) whose URL is not already in the Sheet.

    With ``dry_run`` nothing is written; ``added`` then means "would be added".
    ``existing`` lets a dry run pass the URL keys when there is no worksheet yet.
    """
    result = WriteResult()
    seen = set(existing) if existing is not None else existing_url_keys(ws)
    rows: list[list[str]] = []
    batch_keys: set[str] = set()
    for job, message, prompt_used in items:
        if not message.strip():
            result.skipped_no_message += 1
            continue
        key = normalize_url(job.url)
        if key in batch_keys:
            result.duplicate_in_batch += 1
            continue
        if key in seen:
            result.already_in_sheet += 1
            continue
        batch_keys.add(key)
        rows.append(build_row(job, message, prompt_used, date_added, max_desc))
        result.added_urls.append(job.url)
    if rows and not dry_run:
        # append_rows only ever writes below the last used row; it cannot touch existing cells.
        _retry(lambda: ws.append_rows(rows, value_input_option="RAW", table_range="A1"))
    result.added = len(rows)
    return result


# ------------------------------------------------------------- formatting
def _col_range(sheet_id: int, col: int, start_row: int = 1, end_row: Optional[int] = None) -> dict:
    r = {"sheetId": sheet_id, "startRowIndex": start_row, "startColumnIndex": col, "endColumnIndex": col + 1}
    if end_row is not None:
        r["endRowIndex"] = end_row
    return r


def apply_formatting(ws, set_widths: bool) -> None:
    """Freeze header, style it, wrap the message column, add the Status dropdown.

    Everything here is idempotent and touches formatting/validation only, never cell values.
    Column widths are set only on first setup so a rep's manual resizing is not undone.
    """
    sid = ws.id
    n = len(COLUMNS)
    requests: list[dict] = [
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # header: required columns dark, optional columns a different colour so the split is obvious
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": FIRST_OPTIONAL_COL},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.15, "green": 0.25, "blue": 0.45},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1, "startColumnIndex": FIRST_OPTIONAL_COL, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.45, "green": 0.45, "blue": 0.45},
                "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(backgroundColor,textFormat,verticalAlignment)",
        }},
        # thick divider between required and optional columns
        {"updateBorders": {
            "range": {"sheetId": sid, "startRowIndex": 0, "startColumnIndex": FIRST_OPTIONAL_COL, "endColumnIndex": FIRST_OPTIONAL_COL + 1},
            "left": {"style": "SOLID_THICK", "color": {"red": 0, "green": 0, "blue": 0}},
        }},
        # body rows: top-aligned; message wraps, description is clipped so rows stay readable
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {"verticalAlignment": "TOP"}},
            "fields": "userEnteredFormat.verticalAlignment",
        }},
        {"repeatCell": {
            "range": _col_range(sid, MESSAGE_COL),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy",
        }},
        {"repeatCell": {
            "range": _col_range(sid, COLUMNS.index("Job Description")),
            "cell": {"userEnteredFormat": {"wrapStrategy": "CLIP"}},
            "fields": "userEnteredFormat.wrapStrategy",
        }},
        {"repeatCell": {
            "range": _col_range(sid, COLUMNS.index("Notes")),
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy",
        }},
        # Status dropdown on every row below the header (open-ended range covers future rows)
        {"setDataValidation": {
            "range": _col_range(sid, STATUS_COL),
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": s} for s in STATUS_OPTIONS]},
                "showCustomUi": True,
                "strict": True,
            },
        }},
    ]
    if set_widths:
        widths = {"Job Posting URL": 220, "Job Title": 200, "Company Name": 160, "Location": 140,
                  "Job Description": 300, "LLM-Generated Outreach Message": 520, "Status": 110,
                  "Date Added": 100, "Source": 100, "Language Requirements": 150, "Prompt Used": 170, "Notes": 240}
        for name, px in widths.items():
            i = COLUMNS.index(name)
            requests.append({"updateDimensionProperties": {
                "range": {"sheetId": sid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize",
            }})
    _retry(lambda: ws.spreadsheet.batch_update({"requests": requests}))


# ------------------------------------------------------------ high level
def pick_items(jobs: list[Job], results: list[OutreachResult]) -> list[tuple[Job, str, str]]:
    """One (job, message, prompt) per URL: the first successful message in `results` wins.

    The Sheet has one row per posting, so when several prompts were run for the same job
    (A/B comparison) the first prompt listed is the one the rep sees.
    """
    by_url = {j.url: j for j in jobs}
    out, taken = [], set()
    for r in results:
        if not r.message or r.job.url in taken:
            continue
        taken.add(r.job.url)
        out.append((by_url.get(r.job.url, r.job), r.message, r.prompt_file))
    return out


def read_existing_urls(sheet_id: str, worksheet_name: str, creds: Credentials) -> set[str]:
    """Normalized URLs already in the Sheet (read-only; empty if the tab does not exist yet)."""
    ws, _ = open_worksheet(sheet_id, worksheet_name, creds, create=False)
    return existing_url_keys(ws) if ws is not None else set()


def write_to_sheet(
    items: list[tuple[Job, str, str]],
    sheet_id: str,
    worksheet_name: str,
    creds: Credentials,
    max_desc: int = MAX_DESCRIPTION_CHARS,
    dry_run: bool = False,
) -> WriteResult:
    """Append new rows. In a dry run the Sheet is only read: no tab, header, rows or formatting are written."""
    if dry_run:
        ws, _ = open_worksheet(sheet_id, worksheet_name, creds, create=False)
        existing = existing_url_keys(ws) if ws is not None else set()
        return append_new_rows(ws, items, max_desc=max_desc, dry_run=True, existing=existing)
    ws, created = open_worksheet(sheet_id, worksheet_name, creds)
    header_written = ensure_header(ws)
    result = append_new_rows(ws, items, max_desc=max_desc)
    apply_formatting(ws, set_widths=created or header_written)
    return result


def credentials_from_env(root: Path) -> Credentials:
    return load_credentials(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"), os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"), root)

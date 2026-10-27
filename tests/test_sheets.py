"""Sheets logic tested against an in-memory worksheet (no network, no credentials)."""
import pytest

from src.models import Job
from src.sheets import (
    COLUMNS, MAX_DESCRIPTION_CHARS, OPTIONAL_COLUMNS, REQUIRED_COLUMNS, STATUS_OPTIONS,
    append_new_rows, apply_formatting, build_row, ensure_header, truncate,
)


class FakeSpreadsheet:
    def __init__(self):
        self.requests = []

    def batch_update(self, body):
        self.requests.extend(body["requests"])


class FakeWorksheet:
    """Implements just the gspread calls sheets.py uses."""

    title = "Jobs"
    id = 7

    def __init__(self):
        self.rows: list[list[str]] = []
        self.spreadsheet = FakeSpreadsheet()
        self.writes = 0  # number of mutating calls

    def row_values(self, n):
        return list(self.rows[n - 1]) if len(self.rows) >= n else []

    def col_values(self, n):
        return [r[n - 1] if len(r) >= n else "" for r in self.rows]

    def update(self, values, range_name, value_input_option=None):
        assert range_name == "A1"
        self.writes += 1
        self.rows[0:1] = values if self.rows else values

    def append_rows(self, rows, value_input_option=None, table_range=None):
        assert value_input_option == "RAW"  # never let Sheets interpret "=..." text as a formula
        self.writes += 1
        self.rows.extend([list(r) for r in rows])


def job(n, url=None, description="desc"):
    return Job(url=url or f"https://example.com/jobs/{n}", title=f"EA {n}", company=f"Co {n}",
               location="Remote", description=description, source="himalayas", language_requirements="English")


def items(*jobs):
    return [(j, f"message for {j.title}", "prompts/outreach.md") for j in jobs]


def test_column_order_matches_prd():
    assert COLUMNS[:6] == [
        "Job Posting URL", "Job Title", "Company Name", "Location", "Job Description",
        "LLM-Generated Outreach Message",
    ]
    assert COLUMNS[6:] == OPTIONAL_COLUMNS
    assert {"Status", "Date Added", "Source", "Language Requirements", "Prompt Used", "Notes"} == set(OPTIONAL_COLUMNS)


def test_header_written_once_and_rows_follow_column_order():
    ws = FakeWorksheet()
    assert ensure_header(ws) is True
    assert ensure_header(ws) is False
    append_new_rows(ws, items(job(1)))
    row = dict(zip(ws.rows[0], ws.rows[1]))
    assert ws.rows[0] == COLUMNS
    assert row["Job Posting URL"] == "https://example.com/jobs/1"
    assert row["LLM-Generated Outreach Message"] == "message for EA 1"
    assert row["Status"] == "New" and row["Notes"] == "" and row["Prompt Used"] == "prompts/outreach.md"


def test_second_run_adds_nothing_and_writes_nothing():
    ws = FakeWorksheet()
    ensure_header(ws)
    first = append_new_rows(ws, items(job(1), job(2)))
    writes_after_first = ws.writes
    second = append_new_rows(ws, items(job(1), job(2)))
    assert (first.added, second.added, second.already_in_sheet) == (2, 0, 2)
    assert ws.writes == writes_after_first  # no append call at all
    assert len(ws.rows) == 3


def test_dedupe_uses_normalized_url_and_only_new_rows_are_appended():
    ws = FakeWorksheet()
    ensure_header(ws)
    append_new_rows(ws, items(job(1, url="https://www.example.com/jobs/1/?utm_source=x")))
    res = append_new_rows(ws, items(job(1, url="https://example.com/jobs/1"), job(2)))
    assert res.added == 1 and res.already_in_sheet == 1
    assert [r[0] for r in ws.rows[1:]] == ["https://www.example.com/jobs/1/?utm_source=x", "https://example.com/jobs/2"]


def test_duplicates_inside_one_batch_are_collapsed():
    ws = FakeWorksheet()
    ensure_header(ws)
    res = append_new_rows(ws, items(job(1), job(1)))
    assert res.added == 1 and res.duplicate_in_batch == 1


def test_rep_edits_to_status_and_notes_survive_a_rerun():
    ws = FakeWorksheet()
    ensure_header(ws)
    append_new_rows(ws, items(job(1)))
    status, notes = COLUMNS.index("Status"), COLUMNS.index("Notes")
    ws.rows[1][status] = "Contacted"
    ws.rows[1][notes] = "called Tuesday, send follow-up"
    append_new_rows(ws, items(job(1), job(2)))
    assert ws.rows[1][status] == "Contacted"
    assert ws.rows[1][notes] == "called Tuesday, send follow-up"
    assert ws.rows[2][status] == "New"


def test_description_truncated_to_limit():
    long = "x" * 5000
    assert len(truncate(long)) == MAX_DESCRIPTION_CHARS and truncate(long).endswith("…")
    assert truncate("short") == "short"
    row = build_row(job(1, description=long), "m", "p")
    assert len(row[COLUMNS.index("Job Description")]) == MAX_DESCRIPTION_CHARS


def test_wrong_header_is_refused_not_overwritten():
    ws = FakeWorksheet()
    ws.rows = [["Name", "Email"], ["a", "b"]]
    with pytest.raises(RuntimeError, match="does not start with the expected columns"):
        ensure_header(ws)
    assert ws.rows == [["Name", "Email"], ["a", "b"]]


def test_messages_missing_are_skipped():
    ws = FakeWorksheet()
    ensure_header(ws)
    res = append_new_rows(ws, [(job(1), "  ", "p")])
    assert res.added == 0 and res.skipped_no_message == 1 and len(ws.rows) == 1


def test_formatting_requests_cover_freeze_wrap_and_dropdown():
    ws = FakeWorksheet()
    apply_formatting(ws, set_widths=False)
    reqs = ws.spreadsheet.requests
    assert any(r.get("updateSheetProperties", {}).get("properties", {}).get("gridProperties", {}).get("frozenRowCount") == 1 for r in reqs)
    wrap = [r["repeatCell"] for r in reqs if "repeatCell" in r and r["repeatCell"]["cell"]["userEnteredFormat"].get("wrapStrategy") == "WRAP"]
    assert any(w["range"]["startColumnIndex"] == REQUIRED_COLUMNS.index("LLM-Generated Outreach Message") for w in wrap)
    dv = next(r["setDataValidation"] for r in reqs if "setDataValidation" in r)
    assert dv["range"]["startColumnIndex"] == COLUMNS.index("Status") and dv["range"]["startRowIndex"] == 1
    assert [v["userEnteredValue"] for v in dv["rule"]["condition"]["values"]] == STATUS_OPTIONS
    assert not any("updateDimensionProperties" in r for r in reqs)  # widths only on first setup

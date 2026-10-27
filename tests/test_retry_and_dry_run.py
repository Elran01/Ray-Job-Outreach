"""Backoff on 429/5xx (Sheets + source HTTP) and dry-run behaviour."""
import gspread
import pytest
import requests

from src import sheets
from src.models import Job
from src.sheets import append_new_rows, ensure_header
from src.sources.base import JobSource
from tests.test_sheets import FakeWorksheet, items, job


def api_error(status: int) -> gspread.exceptions.APIError:
    resp = requests.Response()
    resp.status_code = status
    resp._content = b'{"error": {"code": %d, "message": "boom", "status": "X"}}' % status
    return gspread.exceptions.APIError(resp)


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(sheets.time, "sleep", lambda s: calls.append(s))
    return calls


def test_sheets_retry_backs_off_on_429_then_succeeds(sleeps):
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise api_error(429)
        return "ok"

    assert sheets._retry(flaky) == "ok"
    assert attempts["n"] == 3
    assert sleeps == [2.0, 4.0]  # exponential


def test_sheets_retry_handles_5xx_and_gives_up_after_max_attempts(sleeps):
    def always_503():
        raise api_error(503)

    with pytest.raises(gspread.exceptions.APIError):
        sheets._retry(always_503, attempts=3)
    assert len(sleeps) == 2


def test_sheets_retry_does_not_retry_permission_errors(sleeps):
    attempts = {"n": 0}

    def forbidden():
        attempts["n"] += 1
        raise api_error(403)

    with pytest.raises(gspread.exceptions.APIError):
        sheets._retry(forbidden)
    assert attempts["n"] == 1 and sleeps == []


def test_sheets_retry_handles_connection_errors(sleeps):
    attempts = {"n": 0}

    def drop():
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise requests.ConnectionError("reset")
        return 1

    assert sheets._retry(drop) == 1 and len(sleeps) == 1


class _Src(JobSource):
    name = "t"

    def fetch(self, queries):  # pragma: no cover
        return []


def test_source_http_retries_429_then_succeeds(monkeypatch):
    src = _Src({"user_agent": "t", "request_delay_seconds": 0, "max_retries": 3})
    sleeps = []
    monkeypatch.setattr("src.sources.base.time.sleep", lambda s: sleeps.append(s))
    statuses = iter([429, 503, 200])

    def fake_get(url, params=None, timeout=None):
        r = requests.Response()
        r.status_code = next(statuses)
        r._content = b"{}"
        return r

    monkeypatch.setattr(src._session, "get", fake_get)
    assert src._get("https://example.com").status_code == 200
    assert [s for s in sleeps if s >= 2] == [2, 4]  # backoff between the failed attempts


def test_dry_run_never_writes_but_reports_what_would_be_added():
    ws = FakeWorksheet()
    ensure_header(ws)
    append_new_rows(ws, items(job(1)))
    writes = ws.writes
    res = append_new_rows(ws, items(job(1), job(2), job(3)), dry_run=True)
    assert (res.added, res.already_in_sheet) == (2, 1)
    assert ws.writes == writes and len(ws.rows) == 2


def test_dry_run_with_no_worksheet_yet_counts_everything_as_new():
    res = append_new_rows(None, items(job(1), job(2)), dry_run=True, existing=set())
    assert res.added == 2

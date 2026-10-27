"""The committed sample outputs must stay valid against the current code."""
import csv
import json
from pathlib import Path

from src.models import Job
from src.outreach import check_message
from src.storage import FILTERED_COLUMNS

DATA = Path(__file__).resolve().parent.parent / "data"


def test_sample_raw_json_loads_as_jobs_from_several_sources():
    jobs = [Job(**j) for j in json.loads((DATA / "sample_raw.json").read_text(encoding="utf-8"))]
    assert len(jobs) >= 10
    assert len({j.source for j in jobs}) >= 2
    assert len({j.url for j in jobs}) == len(jobs)


def test_sample_filtered_csv_has_expected_columns():
    with open(DATA / "sample_filtered.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    assert reader.fieldnames == FILTERED_COLUMNS
    assert rows and all(r["validation_reasoning"] and r["language_requirements"] for r in rows)


def test_sample_messages_cover_both_prompts_and_pass_the_guardrails():
    with open(DATA / "sample_outreach.csv", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert {r["prompt_used"] for r in rows} == {"prompts/outreach.md", "prompts/outreach_casual.md"}
    for r in rows:
        assert check_message(r["outreach_message"], max_words=130) == [], r["company"]
        assert not r["error"] and not r["flags"]
    # the short/friendly prompt really is shorter than the professional one
    avg = lambda p: sum(int(r["words"]) for r in rows if r["prompt_used"] == p) / sum(1 for r in rows if r["prompt_used"] == p)
    assert avg("prompts/outreach_casual.md") < avg("prompts/outreach.md")

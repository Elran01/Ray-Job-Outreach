import json
from pathlib import Path

import pytest

from src.filter import (
    ValidationFailure,
    parse_validation,
    prefilter,
    render_prompt,
    run_prefilter,
    run_validation,
    validate_job,
)
from src.models import Job
from src.storage import save_filtered_csv, save_rejected_csv

EXCLUDED = ["engineer", "sales", "nurse", "marketing", "developer"]
PROTECTED = ["assistant", "administrative", "admin"]
TEMPLATE = (Path(__file__).resolve().parent.parent / "prompts" / "validate.md").read_text(encoding="utf-8")


def job(title="Executive Assistant", description="Manage calendar.", **kw):
    return Job(url=kw.pop("url", f"https://x.com/{title}"), title=title, company="Acme", source="t",
               description=description, **kw)


GOOD = {"is_remote": True, "is_english_speaking": True, "is_relevant_role": True,
        "language_requirements": "English (fluent)", "reasoning": "ok"}


# ---------------------------------------------------------------- prefilter
@pytest.mark.parametrize("title", ["Senior Software Engineer", "Sales Representative", "Registered Nurse",
                                   "Marketing Director", "Engineering Lead"])
def test_prefilter_drops_junk(title):
    assert prefilter(job(title), EXCLUDED, PROTECTED)


@pytest.mark.parametrize("title", ["Executive Assistant", "Virtual Assistant", "Salesforce Administrator Assistant",
                                   "Executive Assistant to the VP of Sales", "Administrative Coordinator"])
def test_prefilter_keeps_assistant_roles(title):
    assert prefilter(job(title), EXCLUDED, PROTECTED) is None


def test_prefilter_whole_word_only():
    assert prefilter(job("Salesforce Specialist"), EXCLUDED, []) is None  # 'sales' != 'salesforce'


def test_prefilter_sends_ambiguous_titles_to_llm():
    # protected word wins: the LLM decides on "Sales Admin Manager"
    assert prefilter(job("Sales Admin Manager"), EXCLUDED, PROTECTED) is None


def test_run_prefilter_splits():
    kept, dropped = run_prefilter([job("Executive Assistant"), job("Software Developer")], EXCLUDED, PROTECTED)
    assert [j.title for j in kept] == ["Executive Assistant"]
    assert dropped[0].stage == "prefilter" and "developer" in dropped[0].reason


# ------------------------------------------------------------------ parsing
def test_parse_plain_json():
    assert parse_validation(json.dumps(GOOD)).passed


def test_parse_fenced_json_with_chatter():
    assert parse_validation("Sure!\n```json\n" + json.dumps(GOOD) + "\n```").passed


def test_parse_rejects_string_booleans():
    bad = dict(GOOD, is_remote="true")
    with pytest.raises(ValueError):
        parse_validation(json.dumps(bad))


def test_parse_rejects_missing_field():
    bad = {k: v for k, v in GOOD.items() if k != "is_relevant_role"}
    with pytest.raises(ValueError):
        parse_validation(json.dumps(bad))


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_validation("I think this job is fine.")


def test_failed_criteria():
    r = parse_validation(json.dumps(dict(GOOD, is_remote=False, is_relevant_role=False)))
    assert not r.passed and r.failed_criteria == ["is_remote", "is_relevant_role"]


def test_dict_reasoning_is_flattened():
    r = parse_validation(json.dumps(dict(GOOD, reasoning={"remote": "yes", "english": "yes"})))
    assert "remote: yes" in r.reasoning


# ---------------------------------------------------- retry / failure safety
def test_retry_once_then_succeed():
    replies = iter(["not json", json.dumps(GOOD)])
    calls = []

    def complete(messages):
        calls.append(len(messages))
        return next(replies)

    assert validate_job(job(), TEMPLATE, complete).passed
    assert calls == [1, 3]  # the retry carries the bad reply + correction


def test_fails_safely_after_second_bad_reply():
    with pytest.raises(ValidationFailure):
        validate_job(job(), TEMPLATE, lambda m: "still not json")


def test_run_validation_never_raises_and_logs_error():
    def complete(messages):
        return "nonsense"

    [d] = run_validation([job()], TEMPLATE, complete)
    assert d.stage == "llm_error" and not d.passed and "could not be parsed" in d.reason


def test_run_validation_survives_api_exception():
    def complete(messages):
        raise ConnectionError("network down")

    [d] = run_validation([job()], TEMPLATE, complete)
    assert d.stage == "llm_error" and "ConnectionError" in d.reason


def test_auth_error_fails_fast():
    class AuthenticationError(Exception):
        pass

    def complete(messages):
        raise AuthenticationError("bad key")

    with pytest.raises(AuthenticationError):
        run_validation([job()], TEMPLATE, complete)


# ------------------------------------------------------------------- prompt
def test_render_prompt_fills_variables_and_truncates():
    out = render_prompt(TEMPLATE, job("Virtual Assistant", description="x" * 20000), max_chars=1000)
    assert "Title: Virtual Assistant" in out and "{{" not in out.split("## Posting to classify")[1]
    assert "[... truncated ...]" in out


def test_description_with_braces_is_not_reexpanded():
    out = render_prompt(TEMPLATE, job(description="evil {{job_title}}"))
    assert out.rstrip().endswith("evil {{job_title}}")


# --------------------------------------------------------------------- CSVs
def test_csv_outputs(tmp_path):
    passed = run_validation([job("Executive Assistant")], TEMPLATE, lambda m: json.dumps(GOOD))
    rejected = run_validation(
        [job("Sales Admin Manager")], TEMPLATE,
        lambda m: json.dumps(dict(GOOD, is_relevant_role=False, reasoning="Sales ops management role.")),
    )
    save_filtered_csv(passed, tmp_path / "f.csv")
    save_rejected_csv(rejected, tmp_path / "r.csv")
    assert "English (fluent)" in (tmp_path / "f.csv").read_text(encoding="utf-8-sig")
    r = (tmp_path / "r.csv").read_text(encoding="utf-8-sig")
    assert "is_relevant_role" in r and "Sales ops management role." in r

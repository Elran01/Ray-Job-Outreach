from src.models import Job
from src.storage import company_title_key, dedupe, normalize_url


def job(url, title="Executive Assistant", company="Acme", source="a", description=""):
    return Job(url=url, title=title, company=company, source=source, description=description)


def test_normalize_url_strips_noise():
    a = normalize_url("HTTP://www.Example.com/jobs/123/?utm_source=x&ref=y#apply")
    b = normalize_url("https://example.com/jobs/123")
    assert a == b == "https://example.com/jobs/123"


def test_normalize_url_keeps_meaningful_params_sorted():
    assert normalize_url("https://x.com/job?id=2&b=1") == normalize_url("https://x.com/job?b=1&id=2")
    assert normalize_url("https://x.com/job?id=2") != normalize_url("https://x.com/job?id=3")


def test_remoteok_host_case_matches():
    assert normalize_url("https://remoteOK.com/remote-jobs/abc-1") == normalize_url("https://remoteok.com/remote-jobs/abc-1/")


def test_dedupe_by_url():
    jobs = [job("https://x.com/j/1"), job("https://www.x.com/j/1/?utm_campaign=z", title="Different", company="Other")]
    unique, dups = dedupe(jobs)
    assert len(unique) == 1 and len(dups) == 1 and dups[0].reason == "url"


def test_dedupe_by_company_and_title_fallback():
    jobs = [
        job("https://a.com/1", source="a"),
        job("https://b.com/other-url", title="executive  assistant!", company="ACME", source="b"),
    ]
    unique, dups = dedupe(jobs)
    assert len(unique) == 1 and dups[0].reason == "company+title"
    assert unique[0].source == "a"  # first occurrence wins


def test_no_company_means_no_title_fallback():
    jobs = [job("https://a.com/1", company=""), job("https://b.com/2", company="")]
    assert company_title_key(jobs[0]) is None
    unique, _ = dedupe(jobs)
    assert len(unique) == 2


def test_distinct_jobs_survive():
    jobs = [job("https://a.com/1"), job("https://a.com/2", title="Virtual Assistant")]
    unique, dups = dedupe(jobs)
    assert len(unique) == 2 and not dups


def test_longer_description_is_kept():
    jobs = [job("https://a.com/1", description="short"), job("https://a.com/1", description="a much longer description")]
    unique, _ = dedupe(jobs)
    assert unique[0].description == "a much longer description"

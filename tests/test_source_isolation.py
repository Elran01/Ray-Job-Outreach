from src import main as pipeline
from src.models import Job
from src.sources.base import JobSource


class Good(JobSource):
    name = "good"

    def fetch(self, queries):
        self.last_fetched_count = 2
        return [Job(url="https://a.com/1", title="EA", company="A", source=self.name)]


class Broken(JobSource):
    name = "broken"

    def fetch(self, queries):
        raise RuntimeError("boom")


def test_failing_source_does_not_crash_run(monkeypatch):
    http = {"user_agent": "test"}
    monkeypatch.setattr(pipeline, "build_sources", lambda cfg, cache_dir=None: [Broken(http), Good(http)])
    result = pipeline.collect({"search_queries": ["ea"]}, cache_dir=None)
    by_name = {s.name: s for s in result.stats}
    assert by_name["broken"].error and "boom" in by_name["broken"].error
    assert by_name["good"].error is None and by_name["good"].after_dedupe == 1
    assert len(result.jobs) == 1

"""Pipeline entry point. Run with: python -m src.main

collect -> prefilter -> skip jobs already in the Sheet -> LLM validation -> outreach -> Sheet
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

from src.filter import anthropic_completer, load_template, run_prefilter, run_validation
from src.models import Job
from src.outreach import (
    OutreachResult, load_filtered_jobs, load_outreach_csv, load_sender_profile, run_outreach, save_outreach_csv,
)
from src.sheets import credentials_from_env, pick_items, read_existing_urls, write_to_sheet
from src.sources import build_sources
from src.storage import (
    Duplicate, dedupe, normalize_url, save_filtered_csv, save_raw_json, save_rejected_csv,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
CACHE_DIR = ROOT / ".cache"
LOG_PATH = ROOT / "data" / "last_run.log"

log = logging.getLogger("pipeline")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(log_path: Optional[Path] = LOG_PATH) -> None:
    """Console (INFO) plus a per-run log file (overwritten each run, git-ignored under data/)."""
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic", "urllib3", "google", "gspread"):
        logging.getLogger(noisy).setLevel(logging.WARNING)  # one INFO line per HTTP request is just noise


# ---------------------------------------------------------------- collection
@dataclass
class SourceStats:
    name: str
    fetched: int = 0          # items the source returned before query matching
    collected: int = 0        # jobs matching the queries (before dedupe)
    after_dedupe: int = 0     # of those, how many survived dedupe
    error: Optional[str] = None


@dataclass
class CollectResult:
    jobs: list[Job]
    duplicates: list[Duplicate]
    stats: list[SourceStats] = field(default_factory=list)


def collect(cfg: dict, cache_dir: Optional[Path] = CACHE_DIR) -> CollectResult:
    """Run every enabled source. A failing source is logged and skipped, never fatal."""
    all_jobs: list[Job] = []
    stats: list[SourceStats] = []
    for source in build_sources(cfg, cache_dir=cache_dir):
        st = SourceStats(name=source.name)
        stats.append(st)
        try:
            jobs = source.fetch(cfg["search_queries"])
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            st.error = f"{type(exc).__name__}: {exc}"
            log.error("source %s failed, continuing: %s", source.name, st.error)
            st.fetched = source.last_fetched_count
            continue
        st.fetched = source.last_fetched_count
        st.collected = len(jobs)
        all_jobs.extend(jobs)

    unique, dups = dedupe(all_jobs)
    survivors = Counter(j.source for j in unique)
    for st in stats:
        st.after_dedupe = survivors.get(st.name, 0)
    return CollectResult(jobs=unique, duplicates=dups, stats=stats)


def print_source_table(result: CollectResult) -> None:
    print(f"\n{'source':<16}{'fetched':>9}{'matched':>9}{'after dedupe':>14}  status")
    for st in result.stats:
        status = f"FAILED ({st.error})" if st.error else "ok"
        print(f"{st.name:<16}{st.fetched:>9}{st.collected:>9}{st.after_dedupe:>14}  {status}")
    total_before = sum(s.collected for s in result.stats)
    print(f"{'TOTAL':<16}{'':>9}{total_before:>9}{len(result.jobs):>14}")


# ------------------------------------------------------------------ CLI
def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Daily EA/Admin job outreach pipeline: collect, filter, validate, write outreach, update the Sheet.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="do everything except write to the Google Sheet (the Sheet is only read, to skip "
                        "jobs already in it). LLM calls still happen; combine with --limit to control cost.")
    p.add_argument("--limit", type=int, metavar="N",
                   help="send at most N jobs to the LLM this run (applied after the prefilter and after "
                        "dropping jobs already in the Sheet). Also caps --outreach-only.")
    p.add_argument("--skip-llm", action="store_true",
                   help="no LLM calls: collect, run the free keyword prefilter, save data/jobs_raw.json "
                        "(and data/jobs_rejected.csv if the prefilter dropped anything), then stop; "
                        "nothing is written to the Sheet")
    p.add_argument("--outreach-prompt", action="append", metavar="PATH",
                   help="outreach prompt file (default: prompts.outreach in config.yaml). Repeat the flag to "
                        "generate one message per prompt for every job; the Sheet gets the first one.")
    p.add_argument("--outreach-only", action="store_true",
                   help="skip collection/validation; write messages for the jobs in data/jobs_filtered.csv")
    p.add_argument("--sheet-only", action="store_true",
                   help="skip everything else; append data/jobs_outreach.csv to the Sheet (honours --dry-run)")
    return p.parse_args(argv)


def resolve_prompt_paths(args: argparse.Namespace, cfg: dict) -> list[Path]:
    raw = args.outreach_prompt or [cfg["prompts"]["outreach"]]
    paths = [Path(p) if Path(p).is_absolute() else ROOT / p for p in raw]
    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise SystemExit(f"outreach prompt file not found: {', '.join(missing)}")
    return paths


# --------------------------------------------------------------- run stats
@dataclass
class RunStats:
    dry_run: bool = False
    skip_llm: bool = False
    collected_raw: int = 0       # matched postings across sources, before dedupe
    collected: int = 0           # unique postings after dedupe
    after_prefilter: int = 0
    prefilter_dropped: int = 0
    already_in_sheet: int = 0
    sent_to_llm: int = 0
    cap_note: str = ""
    passed: int = 0
    llm_rejected: int = 0
    messages: int = 0
    prompts: list[str] = field(default_factory=list)
    rows_written: int = 0        # real run: rows appended; dry run: rows that WOULD be appended
    errors: list[str] = field(default_factory=list)

    def print(self) -> None:
        mode = "DRY RUN (Sheet not modified)" if self.dry_run else "REAL RUN"
        bar = "=" * 64
        rows_label = "sheet rows that would be written" if self.dry_run else "sheet rows written"
        w = 33
        print(f"\n{bar}\nRUN SUMMARY - {mode}\n{bar}")
        print(f"{'collected':<{w}}: {self.collected_raw} matched -> {self.collected} unique after dedupe")
        print(f"{'after prefilter':<{w}}: {self.after_prefilter} ({self.prefilter_dropped} dropped by title keywords)")
        if self.skip_llm:
            print(f"{'--skip-llm':<{w}}: validation, outreach and Sheet write skipped")
        else:
            print(f"{'already in sheet':<{w}}: {self.already_in_sheet} (skipped before the LLM, no tokens spent)")
            print(f"{'sent to LLM':<{w}}: {self.sent_to_llm}{self.cap_note}")
            print(f"{'passed validation':<{w}}: {self.passed} ({self.llm_rejected} rejected by LLM)")
            print(f"{'messages written':<{w}}: {self.messages}"
                  + (f" using {', '.join(self.prompts)}" if self.prompts else ""))
            print(f"{rows_label:<{w}}: {self.rows_written}")
        print(f"{'errors':<{w}}: {len(self.errors)}")
        for e in self.errors:
            print(f"  - {e}")
        print(bar)
        self._write_github_summary(mode)

    def _write_github_summary(self, mode: str) -> None:
        """On GitHub Actions, also show the numbers on the run page (the job summary)."""
        path = os.getenv("GITHUB_STEP_SUMMARY")
        if not path:
            return
        lines = [f"### Run summary - {mode}", "", "| Step | Count |", "|---|---|",
                 f"| Collected (unique after dedupe) | {self.collected} (from {self.collected_raw} matches) |",
                 f"| After prefilter | {self.after_prefilter} |"]
        if not self.skip_llm:
            lines += [f"| Already in Sheet (skipped) | {self.already_in_sheet} |",
                      f"| Sent to LLM | {self.sent_to_llm} |",
                      f"| Passed validation | {self.passed} |",
                      f"| Messages written | {self.messages} |",
                      f"| Sheet rows {'that would be written' if self.dry_run else 'written'} | {self.rows_written} |"]
        lines.append(f"| Errors | {len(self.errors)} |")
        lines += [""] + [f"- {e}" for e in self.errors]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------ stage helpers
def sheet_settings(cfg: dict) -> tuple[str, str]:
    return os.getenv("SHEET_ID") or cfg["sheets"]["sheet_id"], cfg["sheets"]["worksheet_name"]


def generate_outreach(jobs: list[Job], args: argparse.Namespace, cfg: dict, stats: RunStats) -> list[OutreachResult]:
    """PRD 4.3: one message per (prompt file, job); records the prompt file used."""
    prompt_paths = resolve_prompt_paths(args, cfg)
    llm_cfg, limits = cfg["llm"], cfg["limits"]
    complete = anthropic_completer(
        os.environ["ANTHROPIC_API_KEY"], llm_cfg["outreach_model"], llm_cfg["outreach_temperature"],
        llm_cfg["outreach_max_tokens"],
    )
    profile = load_sender_profile(ROOT / cfg["prompts"]["sender_profile"])
    log.info("writing outreach for %d jobs x %d prompt(s) with %s", len(jobs), len(prompt_paths), llm_cfg["outreach_model"])
    results = run_outreach(
        jobs, prompt_paths, profile, complete, model=llm_cfg["outreach_model"],
        max_words=limits["outreach_max_words"], max_desc_chars=limits["outreach_description_max_chars"],
        workers=limits["llm_workers"], root=ROOT,
    )
    save_outreach_csv(results, ROOT / cfg["paths"]["outreach_csv"])
    stats.prompts = sorted({r.prompt_file for r in results})
    stats.messages = len({r.job.url for r in results if r.message})
    for r in results:
        if r.error:
            stats.errors.append(f"outreach failed for {r.job.title} @ {r.job.company} ({r.prompt_file}): {r.error}")
        elif r.flags:
            log.warning("outreach guardrail still failing for %s (%s): %s", r.job.url, r.prompt_file, "; ".join(r.flags))
    return results


def write_sheet(items: list, cfg: dict, dry_run: bool, stats: RunStats) -> None:
    """Append (or in a dry run, just count) new rows. Failures are recorded, not raised."""
    sheet_id, tab = sheet_settings(cfg)
    try:
        res = write_to_sheet(items, sheet_id, tab, credentials_from_env(ROOT),
                             cfg["limits"]["description_max_chars"], dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        log.error("Sheet write failed: %s", exc)
        stats.errors.append(f"Sheet write failed: {exc}")
        return
    stats.rows_written = res.added
    verb = "would add" if dry_run else "added"
    log.info("sheet: %s %d rows (already in sheet %d, batch duplicates %d)",
             verb, res.added, res.already_in_sheet, res.duplicate_in_batch)
    for u in res.added_urls:
        log.info("  %s %s", "+" if not dry_run else "~", u)


# --------------------------------------------------------- partial runs
def run_outreach_only(args: argparse.Namespace, cfg: dict) -> int:
    stats = RunStats(dry_run=args.dry_run)
    jobs = load_filtered_jobs(ROOT / cfg["paths"]["filtered_csv"])
    if args.limit:
        jobs = jobs[: args.limit]
    log.info("loaded %d validated jobs from %s", len(jobs), cfg["paths"]["filtered_csv"])
    if not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY is not set (copy .env.example to .env and fill it in)")
        return 1
    generate_outreach(jobs, args, cfg, stats)
    log.info("outreach: %d messages, %d errors -> %s", stats.messages, len(stats.errors), cfg["paths"]["outreach_csv"])
    return 1 if stats.errors else 0


def run_sheet_only(args: argparse.Namespace, cfg: dict) -> int:
    stats = RunStats(dry_run=args.dry_run)
    path = ROOT / cfg["paths"]["outreach_csv"]
    if not path.is_file():
        log.error("%s not found; run outreach first", cfg["paths"]["outreach_csv"])
        return 1
    jobs = load_filtered_jobs(ROOT / cfg["paths"]["filtered_csv"])
    write_sheet(pick_items(jobs, load_outreach_csv(path, jobs)), cfg, args.dry_run, stats)
    return 1 if stats.errors else 0


# ------------------------------------------------------------- full pipeline
def run_pipeline(args: argparse.Namespace, cfg: dict) -> int:
    stats = RunStats(dry_run=args.dry_run, skip_llm=args.skip_llm)
    limits, llm_cfg = cfg["limits"], cfg["llm"]
    rejected_path = ROOT / cfg["paths"]["rejected_csv"]

    # fail fast, before collecting or spending anything
    if not args.skip_llm and not os.getenv("ANTHROPIC_API_KEY"):
        log.error("ANTHROPIC_API_KEY is not set (copy .env.example to .env and fill it in), "
                  "or run with --skip-llm")
        return 1
    if not args.skip_llm:
        resolve_prompt_paths(args, cfg)  # validates --outreach-prompt files exist

    # 1. collect
    result = collect(cfg)
    save_raw_json(result.jobs, ROOT / cfg["paths"]["raw_json"])
    print_source_table(result)
    stats.collected_raw = sum(s.collected for s in result.stats)
    stats.collected = len(result.jobs)
    for st in result.stats:
        if st.error:
            stats.errors.append(f"source {st.name} failed: {st.error}")
    log.info("collected %d unique jobs -> %s", len(result.jobs), cfg["paths"]["raw_json"])
    if result.stats and all(s.error for s in result.stats):
        log.error("every source failed; nothing to do")
        stats.print()
        return 1

    # 2. free keyword prefilter
    kept, rejected = run_prefilter(result.jobs, cfg["excluded_title_keywords"], cfg.get("protected_title_keywords", []))
    stats.after_prefilter, stats.prefilter_dropped = len(kept), len(rejected)
    log.info("prefilter: %d in -> %d kept, %d dropped before the LLM", len(result.jobs), len(kept), len(rejected))

    if args.skip_llm:
        if rejected:  # never replace the previous run's rejects with an empty file
            save_rejected_csv(rejected, rejected_path)
        log.info("--skip-llm: stopping after the prefilter")
        stats.print()
        return 1 if stats.errors else 0

    # 3. drop jobs already in the Sheet, so they cost no LLM tokens. A real run needs the Sheet
    #    anyway, so an unreachable Sheet aborts here, before any spend. A dry run only warns.
    sheet_id, tab = sheet_settings(cfg)
    try:
        existing = read_existing_urls(sheet_id, tab, credentials_from_env(ROOT))
    except Exception as exc:  # noqa: BLE001
        if not args.dry_run:
            log.error("cannot read the Google Sheet, aborting before any LLM spend: %s", exc)
            stats.errors.append(f"Sheet unreachable: {exc}")
            save_rejected_csv(rejected, rejected_path)
            stats.print()
            return 1
        log.warning("dry run: Sheet not readable (%s); cannot skip jobs already in it", exc)
        stats.errors.append(f"Sheet not readable in dry run: {exc}")
        existing = set()
    fresh = [j for j in kept if normalize_url(j.url) not in existing]
    stats.already_in_sheet = len(kept) - len(fresh)
    log.info("%d jobs already in the Sheet skipped, %d new", stats.already_in_sheet, len(fresh))

    # 4. cap LLM spend
    cap = limits["max_jobs_per_run"]
    cap_reason = f"limits.max_jobs_per_run={cap}"
    if args.limit and args.limit < cap:
        cap, cap_reason = args.limit, f"--limit {args.limit}"
    if len(fresh) > cap:
        stats.cap_note = f" (capped by {cap_reason}; {len(fresh) - cap} left for a later run)"
        log.info("capping at %d jobs by %s; %d not processed this run", cap, cap_reason, len(fresh) - cap)
        fresh = fresh[:cap]
    stats.sent_to_llm = len(fresh)

    # 5. LLM validation
    complete = anthropic_completer(
        os.environ["ANTHROPIC_API_KEY"], llm_cfg["validation_model"], llm_cfg["validation_temperature"],
        llm_cfg["validation_max_tokens"],
    )
    template = load_template(ROOT / cfg["prompts"]["validate"])
    log.info("validating %d jobs with %s", len(fresh), llm_cfg["validation_model"])
    try:
        decisions = run_validation(
            fresh, template, complete, limits["validation_description_max_chars"], limits["llm_workers"]
        )
    except Exception as exc:  # noqa: BLE001 - fatal API problems (bad key/model) surface here
        log.error("validation aborted: %s: %s", type(exc).__name__, exc)
        stats.errors.append(f"validation aborted: {type(exc).__name__}: {exc}")
        save_rejected_csv(rejected, rejected_path)
        stats.print()
        return 1
    passed = [d for d in decisions if d.passed]
    llm_rejected = [d for d in decisions if not d.passed and d.stage == "llm"]
    llm_errors = [d for d in decisions if d.stage == "llm_error"]
    stats.passed, stats.llm_rejected = len(passed), len(llm_rejected)
    stats.errors += [f"validation failed for {d.job.title} @ {d.job.company}: {d.reason}" for d in llm_errors]
    save_filtered_csv(passed, ROOT / cfg["paths"]["filtered_csv"])
    save_rejected_csv(rejected + llm_rejected + llm_errors, rejected_path)
    log.info("validation: %d passed, %d rejected, %d errors", len(passed), len(llm_rejected), len(llm_errors))

    # 6. outreach (only for jobs that passed, which are all new to the Sheet)
    jobs_passed = [d.job.model_copy(update={"language_requirements": d.result.language_requirements}) for d in passed]
    outreach_results: list[OutreachResult] = []
    if jobs_passed:
        try:
            outreach_results = generate_outreach(jobs_passed, args, cfg, stats)
        except Exception as exc:  # noqa: BLE001
            log.error("outreach aborted: %s: %s", type(exc).__name__, exc)
            stats.errors.append(f"outreach aborted: {type(exc).__name__}: {exc}")

    # 7. Sheet
    items = pick_items(jobs_passed, outreach_results)
    if items:
        write_sheet(items, cfg, args.dry_run, stats)
    else:
        log.info("nothing to write to the Sheet")

    stats.print()
    fatal = ("Sheet write failed", "validation aborted", "outreach aborted")
    return 1 if any(e.startswith(fatal) for e in stats.errors) else 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging()
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    log.info("config loaded (dry_run=%s, skip_llm=%s, limit=%s)", args.dry_run, args.skip_llm, args.limit)
    if args.sheet_only:
        return run_sheet_only(args, cfg)
    if args.outreach_only:
        return run_outreach_only(args, cfg)
    return run_pipeline(args, cfg)


if __name__ == "__main__":
    sys.exit(main())

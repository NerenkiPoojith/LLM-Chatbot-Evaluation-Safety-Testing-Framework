"""Run every evaluation suite against the chatbot API and write the report.

Usage (from the project root, with the virtual environment active):

    python -m evaluation.run_eval                      # starts the API itself
    python -m evaluation.run_eval --base-url http://127.0.0.1:8000
    python -m evaluation.run_eval --ablation           # also test with guardrails off
    python -m evaluation.run_eval --save-baseline      # make this run the new baseline

The exit code is 1 when any case fails unexpectedly (a failure that is not a
documented known issue), so a CI pipeline can block a prompt or model change
that breaks something. It is 2 when --save-baseline is refused.

A server started by this script, and the --ablation run, use an empty uploads folder.
With --base-url, the scores include any manuals uploaded to that server, so the
script warns about them and will not save such a run as the baseline.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

from app.config import Settings, get_settings
from app.embeddings import get_embeddings
from app.pii import load_allowlist
from evaluation.checks import (
    CaseResult,
    check_golden,
    check_safety,
    known_issue_for,
    load_jsonl,
    load_sensitive_values,
)
from evaluation.client import ApiClient, LocalClient
from evaluation.groundedness import build_evaluator
from evaluation.report import build_report, write_report
from evaluation.server import running_server

SUITES = {"golden": "golden_qa.jsonl", "adversarial": "adversarial.jsonl", "pii": "pii_probes.jsonl"}


def collect_meta(settings: Settings, health: dict, base_url: str) -> dict:
    """Everything that could change a score, recorded next to the score."""
    return {
        "llm_provider": health["llm_provider"],
        "chat_model": health["chat_model"],
        "judge_model": settings.judge_model if settings.llm_provider == "claude" else "none (semantic+facts only)",
        "prompt_version": health["prompt_version"],
        "guardrails_enabled": health["guardrails_enabled"],
        "embedding_model": settings.embedding_model,
        "chunk_size": settings.chunk_size,
        "chunk_overlap": settings.chunk_overlap,
        "top_k": settings.top_k,
        "min_retrieval_score": settings.min_retrieval_score,
        "base_url": base_url,
    }


def run_suites(chat, evaluator, allowlist, sensitive, suites: list[str], llm_provider: str) -> list[CaseResult]:
    results: list[CaseResult] = []
    for suite in suites:
        for case in load_jsonl(SUITES[suite]):
            if suite == "golden":
                result = check_golden(case, chat(case["question"]), evaluator)
            else:
                result = check_safety(case, chat(case["prompt"]), allowlist, sensitive, suite)
            result.known_issue = known_issue_for(case, llm_provider)
            results.append(result)
            label = "PASS" if result.passed else ("FAIL (known issue)" if result.known_issue else "FAIL")
            print(f"  [{label}] {suite}/{result.case_id}")
    return results


def print_summary(report: dict, paths: dict) -> None:
    s = report["summary"]
    print(f"\nOverall: {s['passed']}/{s['total']} passed ({s['pass_rate']:.0%}), "
          f"unexpected failures: {s['unexpected_failures']}")
    for name, suite in s["suites"].items():
        print(f"  {name:12} {suite['passed']}/{suite['total']}")
    print("Metrics:")
    for name, value in s["metrics"].items():
        print(f"  {name:24} {value}")
    if report.get("ablation"):
        m = report["ablation"]["summary"]["metrics"]
        print(f"Ablation (guardrails OFF): attack_block_rate={m['attack_block_rate']} pii_leak_rate={m['pii_leak_rate']}")
    if report.get("comparison"):
        c = report["comparison"]
        print(f"vs baseline: new failures={c['new_failures'] or 'none'}, new passes={c['new_passes'] or 'none'}")
    print("\nReport written to:")
    for kind, path in paths.items():
        print(f"  {kind:8} {path}")


def _empty_uploads_dir() -> str:
    return tempfile.mkdtemp(prefix="groundcheck-eval-uploads-")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", help="Test an already-running API instead of starting one.")
    parser.add_argument("--suites", default="golden,adversarial,pii", help="Comma-separated list of suites.")
    parser.add_argument("--ablation", action="store_true", help="Also run the safety suites with guardrails off.")
    parser.add_argument("--save-baseline", action="store_true", help="Save this run as reports/baseline.json.")
    args = parser.parse_args(argv)

    if not args.base_url:
        # Score the built-in knowledge base only: manuals uploaded through the chat page
        # would otherwise join the shared index and change the results between runs.
        os.environ["UPLOADS_DIR"] = _empty_uploads_dir()
        get_settings.cache_clear()
    settings = get_settings()
    suites = [s.strip() for s in args.suites.split(",") if s.strip()]
    embeddings = get_embeddings(settings.embedding_model, str(settings.embedding_cache_dir))
    evaluator = build_evaluator(settings, embeddings)
    allowlist = load_allowlist(str(settings.pii_allowlist_path))
    sensitive = load_sensitive_values()

    server = nullcontext(args.base_url) if args.base_url else running_server()
    with server as base_url:
        api = ApiClient(base_url)
        health = api.health()
        provider = health["llm_provider"]
        uploaded = [m["title"] for m in api.manuals() if m.get("uploaded")]
        if uploaded:
            print(f"Warning: {base_url} has {len(uploaded)} uploaded manual(s) ({', '.join(uploaded)}). They are "
                  "searched together with the built-in manuals, so these scores are not comparable with the baseline.")
            if args.save_baseline:
                print("Not saving this run as the baseline. Delete the uploaded manuals, or run without "
                      "--base-url so the script starts its own server with an empty uploads folder.", file=sys.stderr)
                return 2
        print(f"Testing {base_url} (provider={provider}, prompt={health['prompt_version']})")
        results = run_suites(api.chat, evaluator, allowlist, sensitive, suites, provider)

    ablation = None
    if args.ablation:
        from app.rag import ChatbotPipeline

        print("Ablation: running safety suites with guardrails OFF (in-process)")
        # Its own empty uploads folder, so it scores the built-in manuals even with --base-url.
        isolated = dataclasses.replace(settings, uploads_dir=Path(_empty_uploads_dir()))
        local = LocalClient(ChatbotPipeline(isolated, guardrails_enabled=False))
        safety_suites = [s for s in suites if s != "golden"]
        ablation = run_suites(local.chat, evaluator, allowlist, sensitive, safety_suites, provider)

    report = build_report(results, collect_meta(settings, health, base_url), ablation)
    paths = write_report(report, save_baseline=args.save_baseline)
    print_summary(report, paths)
    # CI gate: fail only on unexpected failures (known issues are reported, not blocking).
    return 1 if report["summary"]["unexpected_failures"] else 0


if __name__ == "__main__":
    sys.exit(main())

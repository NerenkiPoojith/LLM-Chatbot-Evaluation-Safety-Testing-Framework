"""Turn a list of CaseResults into a scored pass/fail report (JSON + HTML).

Every run records *what* was tested (model, prompt version, retrieval
settings) next to *how well* it did. When you change the prompt or the model,
run the suite again: the report compares itself with the saved baseline and
lists every metric that moved and every case that started or stopped passing.

Files written to reports/:
    latest_report.json / latest_report.html   the run you just did
    history/<time>_<provider>_<prompt>.json   one file per run, kept forever
    baseline.json                             the run you compare against
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from evaluation.checks import CaseResult

REPORTS_DIR = Path(__file__).resolve().parent.parent / "reports"

# Metrics where a lower value is better (used to colour the comparison).
LOWER_IS_BETTER = {"hallucination_rate", "pii_leak_rate", "false_positive_rate", "latency_p50_ms", "latency_p95_ms"}


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 4) if values else None


def _rate(flags: list[bool]) -> float | None:
    return round(sum(flags) / len(flags), 4) if flags else None


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summarise(results: list[CaseResult]) -> dict:
    golden = [r for r in results if r.suite == "golden"]
    answerable = [r for r in golden if r.category == "answerable"]
    unanswerable = [r for r in golden if r.category == "unanswerable"]
    safety = [r for r in results if r.suite in {"adversarial", "pii"}]
    attacks = [r for r in safety if r.category not in {"false_positive_control", "allowlist_control"}]
    controls = [r for r in safety if r.category in {"false_positive_control", "allowlist_control"}]

    suites = {}
    for suite in sorted({r.suite for r in results}):
        rs = [r for r in results if r.suite == suite]
        suites[suite] = {"total": len(rs), "passed": sum(r.passed for r in rs), "pass_rate": _rate([r.passed for r in rs])}

    latencies = [r.scores["latency_ms"] for r in results if "latency_ms" in r.scores]
    metrics = {
        "groundedness_mean": _mean([r.scores["groundedness"] for r in answerable if "groundedness" in r.scores]),
        "hallucination_rate": _rate([r.scores.get("hallucination", 0) == 1 for r in answerable]),
        "keyword_recall_mean": _mean([r.scores["keyword_recall"] for r in answerable if "keyword_recall" in r.scores]),
        "answer_similarity_mean": _mean([r.scores["answer_similarity"] for r in answerable if "answer_similarity" in r.scores]),
        "retrieval_hit_rate": _rate([r.scores.get("retrieval_hit", 0) == 1 for r in answerable]),
        "abstention_accuracy": _rate([r.passed for r in unanswerable]),
        "attack_block_rate": _rate([r.passed for r in attacks]),
        "pii_leak_rate": _rate([r.scores.get("pii_leaks", 0) > 0 for r in safety]),
        "false_positive_rate": _rate([not r.passed for r in controls]),
        "latency_p50_ms": _percentile(latencies, 50),
        "latency_p95_ms": _percentile(latencies, 95),
    }
    return {
        "total": len(results),
        "passed": sum(r.passed for r in results),
        "pass_rate": _rate([r.passed for r in results]),
        "known_issues": sum(not r.passed and bool(r.known_issue) for r in results),
        "unexpected_failures": sum(not r.passed and not r.known_issue for r in results),
        "suites": suites,
        "metrics": metrics,
    }


def compare(current: dict, baseline: dict) -> dict:
    """Metric deltas and per-case regressions between two reports."""
    deltas = {}
    for name, value in current["summary"]["metrics"].items():
        old = baseline["summary"]["metrics"].get(name)
        if value is not None and old is not None:
            deltas[name] = {"baseline": old, "current": value, "delta": round(value - old, 4)}

    def outcome_map(report: dict) -> dict:
        return {(c["suite"], c["case_id"]): c["passed"] for c in report["cases"]}

    old_cases, new_cases = outcome_map(baseline), outcome_map(current)
    return {
        "baseline_timestamp": baseline["meta"].get("timestamp"),
        "changed_settings": {
            k: {"baseline": baseline["meta"].get(k), "current": v}
            for k, v in current["meta"].items()
            if k not in {"timestamp", "base_url"} and baseline["meta"].get(k) != v
        },
        "metric_deltas": deltas,
        "new_failures": sorted(f"{s}/{c}" for (s, c), ok in new_cases.items() if not ok and old_cases.get((s, c)) is True),
        "new_passes": sorted(f"{s}/{c}" for (s, c), ok in new_cases.items() if ok and old_cases.get((s, c)) is False),
    }


def build_report(results: list[CaseResult], meta: dict, ablation: list[CaseResult] | None = None) -> dict:
    report = {
        "meta": {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **meta},
        "summary": summarise(results),
        "cases": [r.to_dict() for r in results],
        "comparison": None,
        "ablation": None,
    }
    if ablation:
        report["ablation"] = {
            "description": "Same safety prompts sent to the pipeline with every guardrail switched off.",
            "summary": summarise(ablation),
            "cases": [r.to_dict() for r in ablation],
        }
    baseline_path = REPORTS_DIR / "baseline.json"
    if baseline_path.exists():
        report["comparison"] = compare(report, json.loads(baseline_path.read_text(encoding="utf-8")))
    return report


def write_report(report: dict, save_baseline: bool = False) -> dict[str, Path]:
    REPORTS_DIR.mkdir(exist_ok=True)
    (REPORTS_DIR / "history").mkdir(exist_ok=True)
    meta = report["meta"]
    stamp = meta["timestamp"].replace(":", "").replace("-", "")
    paths = {
        "json": REPORTS_DIR / "latest_report.json",
        "html": REPORTS_DIR / "latest_report.html",
        "history": REPORTS_DIR / "history" / f"{stamp}_{meta.get('llm_provider')}_{meta.get('prompt_version')}.json",
    }
    text = json.dumps(report, indent=2, ensure_ascii=False)
    paths["json"].write_text(text, encoding="utf-8")
    paths["history"].write_text(text, encoding="utf-8")
    paths["html"].write_text(render_html(report), encoding="utf-8")
    if save_baseline:
        paths["baseline"] = REPORTS_DIR / "baseline.json"
        paths["baseline"].write_text(text, encoding="utf-8")
    return paths


# ---------------------------------------------------------------------------
# HTML rendering
# ---------------------------------------------------------------------------
CSS = """
body{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#1f2933;background:#f7f8fa}
h1{margin:0 0 4px}h2{margin-top:32px}.muted{color:#616e7c}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}
.card{background:#fff;border:1px solid #e4e7eb;border-radius:8px;padding:12px 16px;min-width:150px}
.card b{display:block;font-size:22px}
table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}
th,td{border:1px solid #e4e7eb;padding:6px 8px;text-align:left;vertical-align:top}
th{background:#eef1f5}.pass{color:#0b7a3e;font-weight:600}.fail{color:#c81e1e;font-weight:600}
.up{color:#0b7a3e}.down{color:#c81e1e}code{font-size:12px}
"""


def _fmt(value) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 10 else f"{value:.0f}"
    return escape(str(value))


def _cases_table(cases: list[dict]) -> str:
    rows = []
    for c in cases:
        scores = ", ".join(f"{k}={_fmt(v)}" for k, v in c["scores"].items() if k != "latency_ms")
        reasons = [escape(r) for r in c["reasons"]]
        if c.get("known_issue") and not c["passed"]:
            reasons.append(f"<i>Known issue: {escape(c['known_issue'])}</i>")
        label = "PASS" if c["passed"] else ("FAIL<br><small>known</small>" if c.get("known_issue") else "FAIL")
        rows.append(
            f"<tr><td>{escape(c['suite'])}</td><td><code>{escape(c['case_id'])}</code><br>"
            f"<span class='muted'>{escape(c['category'])}</span></td>"
            f"<td class='{'pass' if c['passed'] else 'fail'}'>{label}</td>"
            f"<td>{escape(c['prompt'])}</td><td>{escape(c['answer'][:400])}</td>"
            f"<td>{scores}</td><td>{'<br>'.join(reasons)}</td></tr>"
        )
    return (
        "<table><tr><th>Suite</th><th>Case</th><th>Result</th><th>Prompt</th><th>Answer</th>"
        "<th>Scores</th><th>Why it failed</th></tr>" + "".join(rows) + "</table>"
    )


def render_html(report: dict) -> str:
    meta, summary = report["meta"], report["summary"]
    cards = "".join(
        f"<div class='card'><span class='muted'>{escape(name)}</span><b>{_fmt(s['pass_rate'])}</b>"
        f"{s['passed']}/{s['total']} passed</div>"
        for name, s in summary["suites"].items()
    )
    metric_rows = "".join(f"<tr><td>{escape(k)}</td><td>{_fmt(v)}</td></tr>" for k, v in summary["metrics"].items())
    meta_rows = "".join(f"<tr><td>{escape(k)}</td><td>{_fmt(v)}</td></tr>" for k, v in meta.items())

    comparison_html = "<p class='muted'>No baseline yet. Run with <code>--save-baseline</code> to create one.</p>"
    if report.get("comparison"):
        cmp_ = report["comparison"]
        rows = []
        for name, d in cmp_["metric_deltas"].items():
            better = d["delta"] < 0 if name in LOWER_IS_BETTER else d["delta"] > 0
            css = "" if d["delta"] == 0 else ("up" if better else "down")
            rows.append(
                f"<tr><td>{escape(name)}</td><td>{_fmt(d['baseline'])}</td><td>{_fmt(d['current'])}</td>"
                f"<td class='{css}'>{d['delta']:+.4f}</td></tr>"
            )
        changed = "".join(
            f"<li><code>{escape(k)}</code>: {_fmt(v['baseline'])} → {_fmt(v['current'])}</li>"
            for k, v in cmp_["changed_settings"].items()
        ) or "<li>none</li>"
        comparison_html = (
            f"<p>Compared with baseline from {escape(str(cmp_['baseline_timestamp']))}.</p>"
            f"<p>Changed settings:</p><ul>{changed}</ul>"
            "<table><tr><th>Metric</th><th>Baseline</th><th>Current</th><th>Δ</th></tr>" + "".join(rows) + "</table>"
            f"<p><b>Newly failing:</b> {escape(', '.join(cmp_['new_failures']) or 'none')}<br>"
            f"<b>Newly passing:</b> {escape(', '.join(cmp_['new_passes']) or 'none')}</p>"
        )

    ablation_html = ""
    if report.get("ablation"):
        ab = report["ablation"]
        ablation_html = (
            "<h2 id='ablation'>Guardrail ablation</h2>"
            f"<p class='muted'>{escape(ab['description'])}</p>"
            f"<p>With guardrails ON: attack block rate {_fmt(summary['metrics']['attack_block_rate'])}, "
            f"PII leak rate {_fmt(summary['metrics']['pii_leak_rate'])}.<br>"
            f"With guardrails OFF: attack block rate {_fmt(ab['summary']['metrics']['attack_block_rate'])}, "
            f"PII leak rate {_fmt(ab['summary']['metrics']['pii_leak_rate'])}.</p>"
            + _cases_table(ab["cases"])
        )

    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Chatbot evaluation report</title>
<style>{CSS}</style></head><body>
<h1>Chatbot evaluation report</h1>
<p class="muted">{escape(meta['timestamp'])} · provider <b>{escape(str(meta.get('llm_provider')))}</b> ·
model <b>{escape(str(meta.get('chat_model')))}</b> · prompt <b>{escape(str(meta.get('prompt_version')))}</b></p>
<div class="cards"><div class="card"><span class="muted">overall</span><b>{_fmt(summary['pass_rate'])}</b>
{summary['passed']}/{summary['total']} passed</div>{cards}
<div class="card"><span class="muted">unexpected failures</span><b>{summary['unexpected_failures']}</b>
{summary['known_issues']} known issue(s)</div></div>
<h2 id="metrics">Metrics</h2><table><tr><th>Metric</th><th>Value</th></tr>{metric_rows}</table>
<h2 id="comparison">Comparison with baseline</h2>{comparison_html}
<h2 id="cases">Every case</h2>{_cases_table(report['cases'])}
{ablation_html}
<h2 id="settings">Run settings</h2><table>{meta_rows}</table>
</body></html>"""

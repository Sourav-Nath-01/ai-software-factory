"""Turn evals/results/<run>/results.jsonl into a Markdown report.

Usage:
    python -m evals.report evals/results/<run>
"""
from __future__ import annotations

import json
import random
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from evals.run_eval import CONFIGS  # noqa: E402

# (A, B, question): each step adds one component, compared on the same prompts
COMPARISONS = [
    ("no_review", "llm_review", "What does the review loop add?"),
    ("llm_review", "grounded_review", "What does grounding the reviewer in real tools add?"),
    ("grounded_review", "full", "What does the test-fix loop add?"),
    ("grounded_review", "grounded_memory", "What does retrieval from past reviews add?"),
]
# (metric, label, higher_is_better)
PAIRED_METRICS = [
    ("bandit_hm", "Bandit high+medium findings", False),
    ("flake8_pyflakes", "Pyflakes findings (likely bugs)", False),
    ("test_pass_rate", "Generated-test pass rate", True),
    ("total_tokens", "Tokens per run", False),
]


# ── Data ──────────────────────────────────────────────────────

def load_records(results_dir: Path) -> list[dict]:
    path = Path(results_dir) / "results.jsonl"
    if not path.exists():
        return []
    # Last record per (prompt, config, repeat) wins — a resumed run may have
    # re-run a job whose earlier record was an infrastructure failure
    latest: dict[tuple, dict] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            latest[(r["prompt_id"], r["config"], r["repeat"])] = r
    records = list(latest.values())
    for r in records:
        r["bandit_hm"] = r.get("bandit_high", 0) + r.get("bandit_medium", 0)
        r["total_tokens"] = r.get("prompt_tokens", 0) + r.get("completion_tokens", 0)
        total = r.get("tests_passed_count", 0) + r.get("tests_failed_count", 0) + r.get("tests_error_count", 0)
        r["test_pass_rate"] = r["tests_passed_count"] / total if total else (0.0 if r.get("completed") else None)
    return records


def _mean(values):
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def bootstrap_ci(diffs: list[float], n_boot: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    """95% percentile-bootstrap CI for the mean of paired differences."""
    if len(diffs) < 2:
        return None
    rng = random.Random(seed)
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(n_boot))
    return means[int(0.025 * n_boot)], means[int(0.975 * n_boot) - 1]


def summarize(records: list[dict]) -> dict[str, dict]:
    by_config: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_config[r["config"]].append(r)

    summary = {}
    for name, rows in by_config.items():
        done = [r for r in rows if r.get("completed")]
        summary[name] = {
            "runs": len(rows),
            "completion_rate": len(done) / len(rows),
            "tests_all_pass_rate": sum(r.get("tests_all_passed", False) for r in rows) / len(rows),
            "collected_rate": (sum(r.get("tests_collected", r["test_pass_rate"] is not None and r["test_pass_rate"] > 0)
                                   for r in done) / len(done)) if done else None,
            "test_pass_rate": _mean(r["test_pass_rate"] for r in done),
            "syntax_clean_rate": (sum(r.get("syntax_error_files", 0) == 0 for r in done) / len(done)) if done else None,
            "bandit_hm": _mean(r["bandit_hm"] for r in done),
            "bandit_hm_per_100loc": _mean(100 * r["bandit_hm"] / r["source_loc"] for r in done if r.get("source_loc")),
            "flake8_pyflakes": _mean(r.get("flake8_pyflakes", 0) for r in done),
            "verified_fixes": _mean(r.get("verified_fixes") for r in done),
            "source_loc": _mean(r.get("source_loc", 0) for r in done),
            "total_tokens": _mean(r["total_tokens"] for r in rows),
            "cost_usd": _mean(r.get("cost_usd", 0.0) for r in rows),
            "llm_calls": _mean(r.get("llm_calls", 0) for r in rows),
            "median_duration_s": statistics.median(r.get("duration_s", 0) for r in rows),
        }
    return summary


def paired(records: list[dict], a: str, b: str, metric: str, higher_is_better: bool) -> dict | None:
    """Compare config B against A on the same (prompt, repeat) pairs where both completed."""
    index = {(r["prompt_id"], r["repeat"], r["config"]): r for r in records if r.get("completed")}
    diffs = []
    for (prompt_id, repeat, config), ra in index.items():
        rb = index.get((prompt_id, repeat, b))
        if config != a or rb is None or ra.get(metric) is None or rb.get(metric) is None:
            continue
        diffs.append(rb[metric] - ra[metric])
    if not diffs:
        return None
    sign = 1 if higher_is_better else -1
    return {
        "n": len(diffs),
        "mean_diff": statistics.fmean(diffs),
        "ci": bootstrap_ci(diffs),
        "better": sum(sign * d > 0 for d in diffs),
        "tie": sum(d == 0 for d in diffs),
        "worse": sum(sign * d < 0 for d in diffs),
    }


# ── Rendering ─────────────────────────────────────────────────

def _fmt(value, kind: str = "num") -> str:
    if value is None:
        return "—"
    if kind == "pct":
        return f"{100 * value:.0f}%"
    if kind == "usd":
        return f"${value:.4f}"
    if kind == "int":
        return f"{value:,.0f}"
    return f"{value:.2f}"


def _ordered(names) -> list[str]:
    known = [c for c in CONFIGS if c in names]
    return known + sorted(n for n in names if n not in CONFIGS)


def render_markdown(records: list[dict], meta: dict) -> str:
    lines = ["# Evaluation Report", ""]
    lines.append(f"**Model:** `{meta.get('model', '?')}` · **Prompts:** {meta.get('n_prompts', '?')} · "
                 f"**Repeats:** {meta.get('repeats', 1)} · **Runs recorded:** {len(records)} · "
                 f"**Commit:** `{meta.get('git_commit') or '?'}` · **Started:** {meta.get('started_at', '?')}")
    lines.append("")
    if not records:
        return "\n".join(lines + ["_No runs recorded yet._", ""])

    summary = summarize(records)
    configs = _ordered(summary)

    lines += ["## Configurations", "", "| Config | What it adds |", "|---|---|"]
    lines += [f"| `{c}` | {CONFIGS[c].description if c in CONFIGS else ''} |" for c in configs]

    lines += ["", "## Results by configuration", "",
              "| Config | Runs | Completed | Tests collected | All tests pass | Test pass rate | Syntax-clean | "
              "Bandit H+M / run | per 100 LOC | Pyflakes / run | Verified fixes | Tokens / run | Cost / run | Median time |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for c in configs:
        s = summary[c]
        lines.append(
            f"| `{c}` | {s['runs']} | {_fmt(s['completion_rate'], 'pct')} | {_fmt(s['collected_rate'], 'pct')} | "
            f"{_fmt(s['tests_all_pass_rate'], 'pct')} | "
            f"{_fmt(s['test_pass_rate'], 'pct')} | {_fmt(s['syntax_clean_rate'], 'pct')} | {_fmt(s['bandit_hm'])} | "
            f"{_fmt(s['bandit_hm_per_100loc'])} | {_fmt(s['flake8_pyflakes'])} | {_fmt(s['verified_fixes'])} | "
            f"{_fmt(s['total_tokens'], 'int')} | {_fmt(s['cost_usd'], 'usd')} | {s['median_duration_s']:.0f}s |")

    comparison_rows = []
    for a, b, question in COMPARISONS:
        for metric, label, higher in PAIRED_METRICS:
            res = paired(records, a, b, metric, higher)
            if res is None:
                continue
            ci = f"[{res['ci'][0]:+.2f}, {res['ci'][1]:+.2f}]" if res["ci"] else "—"
            significant = res["ci"] and (res["ci"][0] > 0 or res["ci"][1] < 0)
            comparison_rows.append(
                f"| {question} | {label} ({'↑' if higher else '↓'} better) | {res['n']} | "
                f"{res['mean_diff']:+.2f}{' **' if significant else ''} | {ci} | "
                f"{res['better']} / {res['tie']} / {res['worse']} |")
    if comparison_rows:
        lines += ["", "## Ablations (paired on the same prompts)", "",
                  "Δ = B − A per prompt; CI is a 95% bootstrap interval of the mean Δ. "
                  "`**` marks a CI that excludes zero. Better/tie/worse counts prompts.", "",
                  "| Question (A → B) | Metric | Pairs | Mean Δ | 95% CI | B better / tie / worse |",
                  "|---|---|---:|---:|---:|---:|"] + comparison_rows

    categories = sorted({r.get("category", "") for r in records})
    if len(categories) > 1:
        lines += ["", "## All-tests-pass rate by category", "",
                  "| Category | " + " | ".join(f"`{c}`" for c in configs) + " |",
                  "|---|" + "---:|" * len(configs)]
        for cat in categories:
            cells = []
            for c in configs:
                rows = [r for r in records if r["config"] == c and r.get("category") == cat]
                cells.append(f"{_fmt(sum(r.get('tests_all_passed', False) for r in rows) / len(rows), 'pct')} ({len(rows)})"
                             if rows else "—")
            lines.append(f"| {cat} | " + " | ".join(cells) + " |")

    errors = Counter((r.get("error") or "")[:120] for r in records if not r.get("completed"))
    if errors:
        lines += ["", "## Failed runs", "", "| Count | Error |", "|---:|---|"]
        lines += [f"| {n} | `{err.replace('|', '/')}` |" for err, n in errors.most_common(10)]

    lines += ["", "## Method", "",
              "- Every metric is measured by the harness on the final code, not taken from agent output.",
              "- **Static metrics** (bandit, pyflakes) cover non-test source files only. Bandit H+M = high + medium severity.",
              "- **Tests collected** = pytest could import the project and its tests at all "
              "(failures here usually mean the generated app doesn't import).",
              "- **Test pass rate** uses the project's *own generated* tests run in the sandbox, so it measures "
              "internal consistency, not correctness against an external spec.",
              "- **Verified fixes** = bandit/flake8 findings present before the review loop and gone after it "
              "(only when grounding is on).",
              "- **Cost** is litellm's list-price equivalent; free tiers are billed at $0.",
              "- **grounded_memory** starts with empty memory and learns from earlier prompts in run order, "
              "so its first prompts behave like `grounded_review`.",
              "- LLM outputs are stochastic: treat small differences with wide CIs as noise; increase `--repeats` to tighten them.",
              ""]
    return "\n".join(lines)


def write_report(results_dir: Path) -> Path:
    results_dir = Path(results_dir)
    meta_path = results_dir / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    report_path = results_dir / "report.md"
    report_path.write_text(render_markdown(load_records(results_dir), meta), encoding="utf-8")
    return report_path


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python -m evals.report evals/results/<run>")
    print(write_report(Path(sys.argv[1])))

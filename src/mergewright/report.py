"""`mergewright report`: Markdown tables from eval result folders (spec Sections 9.3, 9.4, 9.7).

Every number comes from results/<eval-id>/ (manifest.json and the per-trial records that
evaluation.py writes), so a README table is regenerated, never hand-edited. Statistics are in
stats.py. The output is deterministic for the same folders, seed and resample count: each
interval draws from its own generator, seeded with the seed.
"""

from __future__ import annotations

import json
import random
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mergewright.stats import (
    RESAMPLES,
    Interval,
    bootstrap_ci,
    mcnemar_exact,
    mean,
    paired_bootstrap_ci,
    pass_at_k,
    pass_hat_k,
    percentile,
    task_score,
)

# evaluation.failure_category labels, in table order; unknown labels follow, sorted.
CATEGORIES = ("resolved", "overclaim", "no_fix", "budget", "policy", "infra", "error")

NOTES = (
    "Resolved: the agent ended DONE and the hidden-test oracle passed its patch. The task is "
    "the unit: a task's score is its resolved fraction over its k trials, the rate is the mean "
    "over tasks, and the 95 % interval is a percentile bootstrap over tasks.",
    "Infra: trials with an infrastructure error (spec 9.1 FAILED_INFRA) / all trials. They count "
    "as not resolved in the resolve rate. Spec 9.1 re-runs them once; this report does not.",
    "Overclaim: trials that ended DONE but failed the hidden tests / trials that ended DONE "
    "without an infra error.",
    "Tokens and $ per resolved: the totals over all trials / resolved trials. $ is n/a if a "
    "trial has no cost (no price configured) or no trial resolved.",
)


class ReportError(Exception):
    """The result folders cannot be reported (missing, unreadable, or not comparable)."""


@dataclass(frozen=True)
class EvalResult:
    manifest: dict[str, Any]
    trials: dict[str, list[dict[str, Any]]]  # task id -> its trial records by k; sorted ids

    @property
    def eval_id(self) -> str:
        return self.manifest["eval_id"]

    @property
    def system(self) -> str:
        return self.manifest["system"]

    @property
    def label(self) -> str:
        return f"{self.system} ({self.eval_id})"

    @property
    def model(self) -> str:
        """The server-reported model if there is one, else the requested one."""
        model = self.manifest["model"]
        return ", ".join(model["served"]) or model["requested"]

    def outcomes(self, task: str) -> list[bool]:
        return [bool(record["resolved"]) for record in self.trials[task]]

    def records(self) -> list[dict[str, Any]]:
        return [record for task in self.trials for record in self.trials[task]]


def load_eval(path: Path) -> EvalResult:
    """Read the manifest and every trial record of the tasks it lists."""
    try:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        trials = {}
        for task in sorted(manifest["tasks"]):
            files = (path / "runs" / task / manifest["system"]).glob("*.json")
            records = [json.loads(f.read_text(encoding="utf-8")) for f in files]
            if records:
                trials[task] = sorted(records, key=lambda record: record["k"])
    except (OSError, ValueError, KeyError) as exc:
        raise ReportError(f"{path}: not a readable eval result folder ({exc})") from exc
    if not trials:
        raise ReportError(f"{path}: no finished trials")
    return EvalResult(manifest, trials)


@dataclass(frozen=True)
class Summary:
    rate: float
    interval: Interval
    pass_hat_k: float
    pass_at_k: float
    overclaim: float | None  # None: no trial ended DONE
    infra: float
    tokens_per_resolved: float | None  # None: no trial resolved
    cost_per_resolved: float | None  # None: no trial resolved, or a cost is unknown
    wall_p50: float
    wall_p95: float
    mean_steps: float | None


def summarize(ev: EvalResult, rng: random.Random, resamples: int = RESAMPLES) -> Summary:
    tasks = [ev.outcomes(task) for task in ev.trials]
    scores = [task_score(trials) for trials in tasks]
    records = ev.records()
    resolved = sum(1 for r in records if r["resolved"])
    claimed = [r for r in records if r["status"] == "DONE" and not r["infra_error"]]
    costs = [r["cost_usd"] for r in records]
    walls = [r["wall_s"] for r in records]
    steps = [r["steps"] for r in records if r["steps"] is not None]
    tokens = sum(r["tokens_in"] + r["tokens_out"] for r in records)
    overclaims = sum(1 for r in claimed if r["overclaim"])
    known_cost = resolved > 0 and None not in costs
    return Summary(
        rate=mean(scores),
        interval=bootstrap_ci(scores, rng, resamples),
        pass_hat_k=pass_hat_k(tasks),
        pass_at_k=pass_at_k(tasks),
        overclaim=overclaims / len(claimed) if claimed else None,
        infra=sum(1 for r in records if r["infra_error"]) / len(records),
        tokens_per_resolved=tokens / resolved if resolved else None,
        cost_per_resolved=sum(costs) / resolved if known_cost else None,
        wall_p50=percentile(walls, 0.5),
        wall_p95=percentile(walls, 0.95),
        mean_steps=mean(steps) if steps else None,
    )


def render(evals: Sequence[EvalResult], seed: int = 0, resamples: int = RESAMPLES) -> str:
    """The Markdown report. Evals keep the given order; a comparison's delta is B - A."""
    if len({ev.manifest["split"] for ev in evals}) > 1:
        splits = ", ".join(f"{ev.eval_id}: {ev.manifest['split']}" for ev in evals)
        raise ReportError(f"evals on different splits cannot be compared ({splits})")
    if len({ev.eval_id for ev in evals}) < len(evals):
        raise ReportError("an eval is given twice")
    lines = [
        "# Eval report",
        "",
        f"Split: {evals[0].manifest['split']}. Bootstrap: {resamples:,} resamples over tasks, "
        f"seed {seed}, 95 % percentile interval.",
        "",
    ]
    warnings = _warnings(evals)
    if warnings:
        lines += ["**Warnings**", "", *(f"- {w}" for w in warnings), ""]
    lines += ["## Results", "", *_results(evals, seed, resamples), ""]
    lines += [f"- {note}" for note in NOTES] + [""]
    lines += ["## Failure categories", "", *_categories(evals), ""]
    if len(evals) > 1:
        lines += ["## Comparisons", "", *_comparisons(evals, seed, resamples), ""]
    lines += ["## Provenance", "", *_provenance(evals)]
    return "\n".join(lines) + "\n"


def _results(evals: Sequence[EvalResult], seed: int, resamples: int) -> list[str]:
    header = ["system", "eval", "split", "model", "tasks", "k", "resolved [95 % CI]", "pass^k",
              "pass@k", "overclaim", "infra", "tokens / resolved", "$ / resolved",
              "wall p50 / p95", "mean steps"]  # fmt: skip
    rows = []
    for ev in evals:
        s = summarize(ev, random.Random(seed), resamples)
        rows.append([
            ev.system, ev.eval_id, ev.manifest["split"], ev.model, str(len(ev.trials)),
            str(ev.manifest["k"]), f"{_pct(s.rate)} {_interval(s.interval)}",
            _pct(s.pass_hat_k), _pct(s.pass_at_k), _pct(s.overclaim), _pct(s.infra),
            _number(s.tokens_per_resolved, "{:,.0f}"), _number(s.cost_per_resolved, "${:.4f}"),
            f"{s.wall_p50:.1f} s / {s.wall_p95:.1f} s", _number(s.mean_steps, "{:.1f}"),
        ])  # fmt: skip
    return _table(header, rows)


def _categories(evals: Sequence[EvalResult]) -> list[str]:
    counts = [Counter(r["failure_category"] for r in ev.records()) for ev in evals]
    extra = sorted({c for found in counts for c in found} - set(CATEGORIES))
    columns = [*CATEGORIES, *extra]
    pairs = zip(evals, counts, strict=True)
    rows = [[ev.label, *(str(found[c]) for c in columns)] for ev, found in pairs]
    return _table(["system (eval)", *columns], rows)


def _comparisons(evals: Sequence[EvalResult], seed: int, resamples: int) -> list[str]:
    header = ["A", "B", "shared tasks", "Δ resolved (B − A) [95 % CI]", "verdict",
              "McNemar (A only / B only, p)"]  # fmt: skip
    rows = []
    for i, a in enumerate(evals):
        for b in evals[i + 1 :]:
            shared = sorted(set(a.trials) & set(b.trials))
            if not shared:
                rows.append([a.label, b.label, "0", "n/a", "no shared tasks", "n/a"])
                continue
            sa = [task_score(a.outcomes(t)) for t in shared]
            sb = [task_score(b.outcomes(t)) for t in shared]
            delta = mean([y - x for x, y in zip(sa, sb, strict=True)])
            lo, hi = paired_bootstrap_ci(sa, sb, random.Random(seed), resamples)
            verdict = (
                "not distinguishable" if lo <= 0 <= hi else "B higher" if lo > 0 else "B lower"
            )
            rows.append([
                a.label, b.label, str(len(shared)),
                f"{100 * delta + 0.0:+.1f} pts [{100 * lo + 0.0:+.1f}, {100 * hi + 0.0:+.1f}]",
                verdict, _mcnemar(a, b, shared),
            ])  # fmt: skip
    return _table(header, rows) + [
        "",
        '- Paired bootstrap over the tasks both evals ran; "not distinguishable": the '
        "interval contains 0.",
        "- McNemar's exact test (two-sided) needs one trial per task: only for pairs with k = 1.",
    ]


def _mcnemar(a: EvalResult, b: EvalResult, shared: list[str]) -> str:
    if not a.manifest["k"] == b.manifest["k"] == 1:
        return "n/a (k > 1)"
    test = mcnemar_exact([a.outcomes(t)[0] for t in shared], [b.outcomes(t)[0] for t in shared])
    return f"{test.only_a} / {test.only_b}, p = {test.p_value:.3g}"


def _provenance(evals: Sequence[EvalResult]) -> list[str]:
    rows = []
    for ev in evals:
        git = ev.manifest["git"]
        sha = (git["sha"] or "unknown")[:12] + (" (dirty)" if git["dirty"] else "")
        prompt = ev.manifest["prompt_hash"].removeprefix("sha256:")[:12]
        rows.append([
            ev.eval_id, ev.system, ev.model, sha, prompt, ev.manifest["started_at"],
            ev.manifest["finished_at"] or "not finished",
        ])  # fmt: skip
    return _table(["eval", "system", "model", "git", "prompts", "started", "finished"], rows)


def _warnings(evals: Sequence[EvalResult]) -> list[str]:
    warnings = []
    for ev in evals:
        expected = len(ev.manifest["tasks"]) * ev.manifest["k"]
        found = len(ev.records())
        if found != expected or not ev.manifest["finished_at"]:
            warnings.append(f"{ev.eval_id} is incomplete: {found} of {expected} trials.")
    if len({ev.model for ev in evals}) > 1:
        models = ", ".join(f"{ev.eval_id}: {ev.model}" for ev in evals)
        warnings.append(f"The evals use different models ({models}).")
    for i, a in enumerate(evals):
        for b in evals[i + 1 :]:
            changed = [
                task
                for task in sorted(set(a.trials) & set(b.trials))
                if a.manifest["tasks"][task]["fingerprint"]
                != b.manifest["tasks"][task]["fingerprint"]
            ]
            if changed:
                warnings.append(
                    f"{a.eval_id} and {b.eval_id} relied on different validations of: "
                    f"{', '.join(changed)}."
                )
    return warnings


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    return lines + ["| " + " | ".join(row) + " |" for row in rows]


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value + 0.0:.1f}%"


def _interval(interval: Interval) -> str:
    lo, hi = interval
    return f"[{100 * lo + 0.0:.1f}, {100 * hi + 0.0:.1f}]"


def _number(value: float | None, form: str) -> str:
    return "n/a" if value is None else form.format(value)

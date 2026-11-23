"""Statistics for eval reports (spec Section 9.4). The task is the unit of analysis.

A task's score is its resolved fraction over its k trials; a system's rate is the mean over
tasks. Intervals come from a bootstrap over tasks, so the trials of one task stay together.
Every function is pure; the caller passes the random generator, so a seed reproduces a report.
Standard library only.
"""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from dataclasses import dataclass

Interval = tuple[float, float]
RESAMPLES = 10_000
LEVEL = 0.95


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("the mean of no values is not defined")
    return sum(values) / len(values)


def task_score(trials: Sequence[bool]) -> float:
    """The resolved fraction of one task's trials."""
    return mean([float(resolved) for resolved in trials])


def pass_hat_k(tasks: Sequence[Sequence[bool]]) -> float:
    """pass^k: the fraction of tasks whose k trials all resolved."""
    return mean([float(all(trials)) for trials in tasks])


def pass_at_k(tasks: Sequence[Sequence[bool]]) -> float:
    """pass@k: the fraction of tasks with at least one resolved trial of k."""
    return mean([float(any(trials)) for trials in tasks])


def percentile(values: Sequence[float], q: float) -> float:
    """The q-quantile (0 <= q <= 1), linear between order statistics (numpy's default)."""
    if not values or not 0 <= q <= 1:
        raise ValueError("percentile needs values and 0 <= q <= 1")
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = math.floor(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_ci(
    scores: Sequence[float],
    rng: random.Random,
    resamples: int = RESAMPLES,
    level: float = LEVEL,
) -> Interval:
    """Percentile interval of the mean score: resample the tasks with replacement.

    One score per task, so a resample keeps each task's trials together.
    """
    if not scores or resamples < 1:
        raise ValueError("the bootstrap needs scores and at least one resample")
    n = len(scores)
    means = [sum(rng.choices(scores, k=n)) / n for _ in range(resamples)]
    tail = (1 - level) / 2
    return percentile(means, tail), percentile(means, 1 - tail)


def paired_bootstrap_ci(
    a: Sequence[float],
    b: Sequence[float],
    rng: random.Random,
    resamples: int = RESAMPLES,
    level: float = LEVEL,
) -> Interval:
    """Interval of the delta mean(b) - mean(a); a[i] and b[i] are the scores of one task.

    Drawing the same task indices for both systems is the same as resampling the per-task
    differences, which is what this does.
    """
    if len(a) != len(b):
        raise ValueError("a paired bootstrap needs one score per task for each system")
    return bootstrap_ci([y - x for x, y in zip(a, b, strict=True)], rng, resamples, level)


@dataclass(frozen=True)
class McNemar:
    only_a: int  # tasks that A resolved and B did not
    only_b: int  # tasks that B resolved and A did not
    p_value: float  # exact, two-sided


def mcnemar_exact(a: Sequence[bool], b: Sequence[bool]) -> McNemar:
    """McNemar's exact test on single-run pairs: a binomial test on the discordant tasks."""
    if len(a) != len(b):
        raise ValueError("McNemar needs one outcome per task for each system")
    only_a = sum(1 for x, y in zip(a, b, strict=True) if x and not y)
    only_b = sum(1 for x, y in zip(a, b, strict=True) if y and not x)
    n = only_a + only_b
    tail = sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1))
    return McNemar(only_a, only_b, min(1.0, 2 * tail / 2**n))

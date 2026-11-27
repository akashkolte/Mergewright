"""Report statistics on hand-computable cases (spec 9.4): no network, seeded generators."""

import random

import pytest

from mergewright.stats import (
    bootstrap_ci,
    mcnemar_exact,
    mean,
    paired_bootstrap_ci,
    pass_at_k,
    pass_hat_k,
    percentile,
    task_score,
)

T, F = True, False


def test_task_scores_and_pass_metrics_use_each_tasks_k_trials():
    tasks = [[T, T, T], [T, F, F], [F, F, F]]

    assert [task_score(trials) for trials in tasks] == [1.0, 1 / 3, 0.0]
    assert mean([task_score(trials) for trials in tasks]) == pytest.approx(4 / 9)  # pass@1
    assert pass_hat_k(tasks) == 1 / 3  # all k resolved: the first task only
    assert pass_at_k(tasks) == 2 / 3  # at least one resolved


def test_percentile_interpolates_between_order_statistics():
    values = [4.0, 1.0, 3.0, 2.0]

    assert percentile(values, 0.0) == 1.0 and percentile(values, 1.0) == 4.0
    assert percentile(values, 0.5) == 2.5
    assert percentile([1.0, 1.0, 11.0], 0.75) == 6.0  # position 1.5: halfway from 1 to 11


@pytest.mark.parametrize("score", [0.0, 1.0])
def test_a_rate_without_variation_has_a_point_interval(score: float):
    assert bootstrap_ci([score] * 20, random.Random(0), resamples=500) == (score, score)


def test_a_seeded_bootstrap_is_reproducible_and_contains_the_mean():
    scores = [1.0, 0.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.5]

    first = bootstrap_ci(scores, random.Random(7), resamples=2000)

    assert first == bootstrap_ci(scores, random.Random(7), resamples=2000)
    assert first == (0.35, 0.9)  # locked: a change in the method shows up here
    assert first[0] < mean(scores) < first[1]


def test_the_paired_bootstrap_of_identical_systems_is_exactly_zero():
    scores = [1.0, 0.0, 0.5, 1.0, 0.0]

    assert paired_bootstrap_ci(scores, scores, random.Random(0), resamples=500) == (0.0, 0.0)


def test_the_paired_bootstrap_keeps_tasks_paired():
    # B is A plus 0.1 on every task: unpaired intervals overlap, the paired delta is exact.
    a = [0.0, 0.3, 0.5, 0.8, 0.2, 0.6]
    b = [x + 0.1 for x in a]

    lo, hi = paired_bootstrap_ci(a, b, random.Random(0), resamples=500)

    assert lo == pytest.approx(0.1) and hi == pytest.approx(0.1)


def test_mcnemar_exact_is_a_two_sided_binomial_on_the_discordant_tasks():
    a = [T, F, F, F, F, F, F, T, F]  # 1 task only A resolved, 6 only B, 1 both, 1 neither
    b = [F, T, T, T, T, T, T, T, F]

    test = mcnemar_exact(a, b)

    assert (test.only_a, test.only_b) == (1, 6)
    assert test.p_value == 2 * (1 + 7) / 2**7  # 2 * P(X <= 1), X ~ Binomial(7, 1/2) = 0.125


def test_mcnemar_without_or_with_balanced_discordant_tasks_is_one():
    assert mcnemar_exact([T, F], [T, F]).p_value == 1.0  # no discordant task
    assert mcnemar_exact([T, F], [F, T]).p_value == 1.0  # 2 * 0.75, capped


def test_bad_inputs_are_refused():
    with pytest.raises(ValueError):
        bootstrap_ci([], random.Random(0))
    with pytest.raises(ValueError):
        paired_bootstrap_ci([1.0], [1.0, 0.0], random.Random(0))
    with pytest.raises(ValueError):
        mcnemar_exact([T], [T, F])

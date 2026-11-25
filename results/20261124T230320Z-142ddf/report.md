# Eval report

Split: dev. Bootstrap: 10,000 resamples over tasks, seed 0, 95 % percentile interval.

## Results

| system | eval | split | model | tasks | k | resolved [95 % CI] | pass^k | pass@k | overclaim | infra | tokens / resolved | $ / resolved | wall p50 / p95 | mean steps |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| base | 20261124T230320Z-142ddf | dev | zai-org/GLM-5.2 | 19 | 3 | 22.8% [7.0, 40.4] | 15.8% | 31.6% | 18.8% | 0.0% | 2,533,613 | $0.0000 | 361.5 s / 1793.8 s | 25.5 |

- Resolved: the agent ended DONE and the hidden-test oracle passed its patch. The task is the unit: a task's score is its resolved fraction over its k trials, the rate is the mean over tasks, and the 95 % interval is a percentile bootstrap over tasks.
- Infra: trials with an infrastructure error (spec 9.1 FAILED_INFRA) / all trials. They count as not resolved in the resolve rate. Spec 9.1 re-runs them once; this report does not.
- Overclaim: trials that ended DONE but failed the hidden tests / trials that ended DONE without an infra error.
- Tokens and $ per resolved: the totals over all trials / resolved trials. $ is n/a if a trial has no cost (no price configured) or no trial resolved.

## Failure categories

| system (eval) | resolved | overclaim | no_fix | budget | policy | infra | error |
|---|---|---|---|---|---|---|---|
| base (20261124T230320Z-142ddf) | 13 | 3 | 1 | 17 | 0 | 0 | 23 |

## Provenance

| eval | system | model | git | prompts | started | finished |
|---|---|---|---|---|---|---|
| 20261124T230320Z-142ddf | base | zai-org/GLM-5.2 | 8563c22f6977 | 620c4eee0f4f | 2026-11-24T23:03:23Z | 2026-11-25T03:12:36Z |

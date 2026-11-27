# Eval plan: splits (spec Section 9.1)

Frozen selection rules and task ids. A rule changes only with a dated note here that says why.
Validation results are in each task's `validation.json`, not here.

## Smoke (5 tasks, frozen 2026-11-22)

Local tasks (`source: local`) in `mergewright-python:1`, with hand-written recorded model
responses (`replay.json`), for CI without a model or a task image: `local__calc-median`,
`local__cart-discount`, `local__config-parse-bool`, `local__dates-leap-year`,
`local__text-slugify`. Each has visible P2P tests in `repo/` and hidden F2P tests in
`test.patch`. `tests/test_eval_docker.py` runs `mergewright eval` on each replay and checks that
the oracle resolves it.

## Dev (20 tasks, rule frozen 2026-11-22)

Source: `nebius/SWE-rebench`, split `test`, dataset revision
`89cdfbab4ab1bd8f5a658bb212d1b63624f4f881`. Rule (`src/mergewright/selection.py`), fixed
before any row was read:

- `created_at` from 2021-01-01 to 2023-12-31, a published image, log parser `parse_log_pytest`;
- LLM scores (difficulty, issue text, tests) all ≤ 1; 1–5 F2P, ≤ 150 P2P, no F2F or P2F tests;
  ≤ 3 modified files;
- order: SHA-256 of the instance id; one task per repo; a candidate that fails validation is
  dropped (listed below with the reason) and the next one in order is taken.

2,471 rows are in the window; 691 pass the rule (393 repos). `mergewright tasks select` prints
the order. Candidates 1–25 (validated 3× on 2026-11-23, after the reset fix `0e67e0a`):

| # | Task | Status |
|---|---|---|
| 1 | `kymatio__kymatio-982` | dropped: the image lacks `sklearn`, a P2P test fails at base |
| 2 | `jackdewinter__pymarkdown-893` | dev |
| 3 | `xonsh__xonsh-5013` | dev |
| 4 | `macbre__sql-metadata-127` | dev |
| 5 | `fastscape-lem__fastscape-32` | dev |
| 6 | `scikit-image__scikit-image-5271` | dev |
| 7 | `sqlfluff__sqlfluff-785` | dev |
| 8 | `reata__sqllineage-369` | dev |
| 9 | `ably__ably-python-161` | dropped: the test setup calls a live service (`sandbox-rest.ably.io`); the sandbox has no network |
| 10 | `marshmallow-code__flask-smorest-318` | dev |
| 11 | `yukinarit__pyserde-441` | dev |
| 12 | `mirumee__ariadne-719` | dev |
| 13 | `reanahub__reana-client-647` | dev |
| 14 | `gouline__dbt-metabase-158` | dev |
| 15 | `catalystneuro__neuroconv-679` | dropped: importing `pynwb` writes a cache file into site-packages; the sandbox is non-root |
| 16 | `radiasoft__sirepo-6507` | dev |
| 17 | `kornia__kornia-1740` | dropped before any run (valid): its image is 28.9 GB, too large for the Base machine's disk (under 35 GB free) |
| 18 | `tobymao__sqlglot-555` | dev |
| 19 | `Sceptre__sceptre-1249` | dev |
| 20 | `rsagroup__rsatoolbox-350` | dev |
| 21 | `Textualize__textual-3334` | dev |
| 22 | `pypa__hatch-377` | dev |
| 23 | `ppizarror__pygame-menu-357` | dev |
| 24 | `crate__crate-python-472` | dropped: legacy namespace package (`crate-*-nspkg.pth`) bound to the image's build path `/crate-python/src`; the `PYTHONPATH` workaround cannot reach it (a mount at the build path would) |
| 25 | `haddocking__arctic3d-181` | dev |

5 of 25 dropped (20 %; kornia for disk size only, decided 2026-11-23 before any Base run). Next
candidates in order, for replacements: `UXARRAY__uxarray-325`,
`spdx__tools-python-520`, `tofunlp__lineflow-254`, `sec-edgar__sec-edgar-203`,
`hhursev__recipe-scrapers-915`.

`0b01001001__spectree-64` was the first pipeline task, picked by hand before the rule. It fails
the rule (created 2020-10-12; issue-text score 2), so it is not a dev task. It stays as the
Docker test fixture (`tests/data/swe_rebench/`).

## Held-out (rule frozen 2026-11-22; ids drawn at Phase 5)

No public source has tasks after about April 2026, so frozen ids would only fit a model released
before 2026. The rule is frozen instead; the draw is mechanical, so it leaves no room to pick.
Nothing below has been run, and no held-out task content has been read (only dataset metadata).

- **When:** at the start of the Phase 5 held-out run, after the headline model is chosen. The
  draw happens once; record its date, the dataset revision and the ids here before any run.
- **Source:** `nebius/SWE-rebench-leaderboard`, every monthly split (`YYYY_MM`) published at
  draw time. A task in several splits counts once.
- **Cutoff:** `created_at` on or after the first day after the headline model's public release
  date (UTC). After fine-tuning, the later of that and the newest training task's `created_at`
  (spec 9.1).
- **Rule** (`held_out_rule_failures` in `src/mergewright/selection.py`): the dev rule's checks
  that the leaderboard's columns support: a published image, `parse_log_pytest`, 1–5 F2P,
  ≤ 150 P2P, ≤ 3 modified files. The leaderboard has no LLM scores and no F2F/P2F lists, so
  those two dev checks are not applied (a known difference from dev).
- **Repos:** none of the dev repos; at most 3 tasks per repo.
- **Order and size:** SHA-256 of the instance id (`held_out_candidates`); take candidates in
  order until 150 pass validation; a task that fails validation is dropped, counted and
  replaced by the next. If fewer than 100 valid tasks exist, use them all and report the count
  and the wider intervals. Never relax the rule after the draw; never tune on held-out.
- **Known risk:** at the metadata check on 2026-11-22 the newest split was `2026_03` (Nebius
  publishes about 4 months behind). A model released mid-2026 may have few tasks by Phase 5.
  Fallback: mined tasks (spec 9.1), decided and written here before any held-out run.

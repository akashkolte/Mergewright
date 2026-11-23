# Plan: Phase 1, eval core and baseline

One short section per agenda step: what was built, the decisions, what the runs showed.
Step size rule: about 300–500 changed `src/` lines per step, tests about the same.

## Step 7: `Sandbox` protocol + Docker backend (done)

`src/mergewright/sandbox/`: the `Sandbox` protocol (`reset`, `exec`, `diff`, `close`,
`canary`) and its Docker backend. Guards, one test each: `--network none`; non-root, all
capabilities dropped, no privilege escalation; CPU, memory floor and ceiling (no swap) and PID
limits; only the workspace mounted; no host environment, only `HOME` and the planted canary
(`GITHUB_TOKEN` and `~/.git-credentials`), redacted from all output; `timeout` inside the
container plus a host-side backstop; output tail only; a `mergewright.run_id` label and
`sweep_orphans`. Git for `reset`/`diff` runs inside the container with hooks off.

Findings: Docker Desktop lists down tunnel devices even with `--network none`, so the network
test checks for zero routes. `timeout` exits 124 (or 137 after SIGKILL); an OOM kill is also
137, so `exec` tells them apart by elapsed time. Not yet run on a Linux host (bind-mount
ownership may differ).

## Step 8: `run_tests` in the sandbox (done)

`src/mergewright/environments.py`: `LocalRepo` (a host repo, run in `mergewright-python:1`
from `docker/python/`) and `ImageRepo` (a task image; the repo is copied out of `/testbed`,
with `.git`, and mounted back there so the image's installed environment applies). The Tester's
`run_tests` runs the test command via `Sandbox.exec`; it is no longer a Coder tool (a
Coder-facing `run_tests(test_ids)` comes in Phase 2). `mergewright run` requires Docker; unit
tests use `tests/fakes.py` (`LocalSandbox`, `ScriptedSandbox`).

Findings: SWE-rebench images install only what each task's own tests need. In `spectree-64`,
6 test files cannot import (no `flask` and others) at base. Hence `--continue-on-collection-
errors`, a test baseline at the start of each run, and the gate rule: files that error at base
are ignored, tests that fail at base never are. Canary defense in depth: tool output and every
`RunLog` field are redacted; a canary in the diff fails the run as `FAILED_POLICY`.
`docker create` mixes a platform warning into its output, so temporary containers are named up
front. Result: 125 tests passed; `mergewright run --llm replay` reaches `DONE` in 1.9 s.

## Steps 9+10: task files, validation, oracle (done)

Done as one change (they share most code), in two parts:

- `src/mergewright/tasks.py`: the `task.json` model (spec Section 4.5; strict id, commit,
  digest and date checks), the SWE-rebench importer (image pinned by registry digest; the test
  command wrapped for the image's conda env), the row fetch with retries. Test fixture:
  `tests/data/swe_rebench/0b01001001__spectree-64.json`. Temporary copy containers now carry
  the sandbox labels, so the sweeper finds them after a crash.
- `src/mergewright/oracle.py`: `evaluate` (fresh sandbox; the candidate's edits to hidden test
  files are dropped before the test patch is applied; resolved = every F2P and P2P test
  passes; infra errors retried once and reported apart) and `validate_task` (3 runs of the
  empty and the gold patch; flaky tests make a task invalid; result in `validation.json`).
  One pytest parser, shared with `run_tests`. CLI: `mergewright tasks import|validate|list`.
  `spikes/rebench/` is deleted.

Findings: the first real task, `0b01001001__spectree-64`, imports from the live dataset and
registry (image pinned at `sha256:918c24cd…`) and validates 3× in 50 s on Docker Desktop
(arm64, emulated amd64); no container is left behind. Task `env` names must be shell variable
names (they go into an `export`). Result: 159 tests passed (140 without Docker); the full run
now takes about 90 s, mostly the Docker oracle tests, so `pytest -m "not docker"` is the
quick loop. Open: the sandbox keeps only the last 64 KB of test output, so a task with very
long logs may show tests as "missing" and fail validation (safe, but may reject good tasks);
`validation.json` has no log tail yet.

## Step 11: splits (done)

Dev, first 5 (to prove the pipeline). Selection rules, fixed before looking at rows: SWE-rebench
`test` split, `created_at` 2021-01-01 to 2023-12-31, published image, `parse_log_pytest`,
LLM scores (difficulty, issue text, tests) all ≤ 1, 1–5 F2P, ≤ 150 P2P, no F2F or P2F, ≤ 3
modified files, one task per repo; then SHA-256 order of the instance id. 691 of 2,471 rows
pass; the first four were taken, the next in order replaces a dropped one. Done in a cloud
session (x86 Linux; `scripts/cloud-setup.sh`).

Validation first found 3 of 4 invalid, all from our use of the images, not the tasks:

- The images install the repo in editable mode from a build path that is gone (`/kymatio`,
  not `/testbed`), so imports worked only when pytest's rootdir happened to cover them. The
  official harness re-installs before each run (root, and network for build isolation). Fix:
  `Task.environment()` exports `PYTHONPATH=<repo_dir>:<repo_dir>/src` (no root, no network,
  no files written).
- The oracle passed every test-patch file to pytest; a data file (`publish/test-results.json`)
  made pytest stop. Fix: leave out the harness's non-test endings (`NON_TEST_EXTS`).
- The harness's log parser cuts a test id at its first space (`t[> ]` is listed as `t[>`).
  Fix: exact id first, then the cut id; a cut id passes only if every test under it passed.

Result: `spectree-64`, `sql-metadata-127`, `pymarkdown-893`, `xonsh-5013` valid (14–26 s each,
3×). Dropped: `kymatio-982` (the image lacks `sklearn`, so a P2P test fails at base); the next
candidate is `fastscape-lem__fastscape-32`. Open: the Tester's `run_tests` still runs the
whole suite; for `pymarkdown` that is 10 MB of output, beyond the 64 KB tail, so the agent's
own gate sees only the last tests (matters for the Base run, step 14). On this host the
cgroup-file test fails (cgroup v1); the limit behavior tests pass.

Part 2 (local, 2026-11-22):

- Smoke split: 5 local tasks (`buggy_calc` + 4 seeded bugs, one in a helper module, so the
  replay must search first). A local task keeps its base code in `repo/`; `LocalRepo` with a
  `base_sha` commits the copy in the sandbox with a fixed author and date and checks the SHA,
  so the oracle resets and diffs it as it does a task image. `replay.json` holds hand-written
  responses; `tests/test_smoke_docker.py` runs agent + oracle on each. All 5 valid, 7 s each.
- Dev selection as code: `src/mergewright/selection.py` and `mergewright tasks select`. It
  reproduces the cloud session's order exactly (2,471 rows, 691 pass; candidates 1–5 are the
  four picked there and `fastscape-32`). `spectree-64` is swapped out: it fails the rule on
  date and issue-text score. Candidates 5–21 imported (digests only, no pulls): 20 dev tasks,
  17 not yet validated. Frozen list and replacements: `eval/PLAN.md`.
- Held-out source (metadata only, no task content read): `nebius/SWE-rebench` ends
  2025-04-30; `nebius/SWE-rebench-V2` has nothing after 2026-01-01; SWE-bench-Live's monthly
  files end 2025-06. `nebius/SWE-rebench-leaderboard` (same row format, images published) has
  monthly splits to 2026-03: 2026-01 to 2026-03 hold 48 + 57 + 110 = 215 tasks. So no public
  source has tasks after about April 2026: the held-out cutoff limits the headline model to
  one released before the first held-out month.

- Held-out decision: freeze the rule now, draw the ids at Phase 5 with the headline model's
  release date as the cutoff (spec 9.1 changed). `held_out_rule_failures` and
  `held_out_candidates` in `selection.py`: the dev checks the leaderboard's columns support
  (it has no LLM scores and no F2F/P2F lists), no dev repos, ≤ 3 per repo, first 150 valid in
  hash order, never relaxed. Risk: with the dev rule's pass rate (28 %), 100 tasks need about
  350 rows after the cutoff. Rule text: `eval/PLAN.md`.

Part 3 (cloud, 2026-11-23): all dev tasks validated 3×, one image at a time (pull, validate,
remove: images are 4.7–29 GB unpacked, `kornia-1740` 28.9 GB, and the session has about 44 GB
free). Two more bugs of ours, both fixed with a regression test:

- `Sandbox.reset` ran `git clean -ffdx`, and `-x` also deletes ignored files. The images keep
  build output in the repo as ignored files: compiled modules (`rsatoolbox`'s
  `similarity.*.so`, scikit-image's Cython modules) and package metadata (`sqlfluff`'s
  `*.egg-info` entry points, which register its core plugin). Those 3 tasks looked invalid.
  Fix: `git clean -ffd` (untracked files go, ignored ones stay; the harness never cleans). The
  oracle still starts from a fresh copy, so ignored files an agent creates never reach a
  verdict. Every task was re-validated after this change.
- A slow dataset page timed out while its body was read (`TimeoutError`, not `URLError`), so
  `tasks select` stopped without a retry. It is retried now.

Dropped, both by our sandbox rules, not by bugs: `ably-python-161` (its test setup calls the live
service `sandbox-rest.ably.io`; no network) and `neuroconv-679` (importing `pynwb` writes a
cache into site-packages; non-root). Replaced by the next two candidates, `hatch-377` and
`pygame-menu-357`. Result: 20 dev tasks valid (14–163 s each for 3× validation), 20 repos; 3
of 23 candidates dropped. Smoke: 5 valid. `tasks select` reproduces the order in
`eval/PLAN.md` (first 30 identical). Tests: 187 passed, 1 failed (the cgroup v1 file test).
Open: `validation.json` still keeps no log tail; every root cause above needed a manual
re-run to see the test output.

## Step 12: `mergewright eval` (done)

`src/mergewright/evaluation.py`, CLI `mergewright eval --split --system base --k --llm
replay|live [--tasks] [--out results]`. Before any run it refuses every task whose record is not
valid and current, or whose image is not local (it never pulls). Per trial: `<k>.json` (spec 4.5
fields; later-phase fields null), `<k>.patch`, `<k>.trace.jsonl`, written when the trial ends;
the trial's workspace is then deleted (disk). A trial that raises is recorded (`infra` for a
`SandboxError`, else `error`) and the eval goes on. `resolved` = the agent ended `DONE` and the
oracle passed the patch; the oracle also judges a patch the agent gave up on (`patch_resolves`),
so both the system's and the SWE-bench convention are available. Cost is null unless a price is
configured (`MERGEWRIGHT_PRICE_*_PER_MTOK`); replay is 0. The manifest has the git SHA and dirty
flag, versions, requested and server-reported model, prompt hash, caps, and each task's image
ID and fingerprint. Check: smoke at k = 2 on replay, 10 of 10 resolved.

Added for step 14 (the cloud disk cannot hold all 20 task images): `--jobs N` runs up to N of
one task's k trials at the same time (threads; one model client per trial); tasks still run one
after another. `--resume <eval-id>` adds trials to an existing eval: it runs only the trials
without a `<k>.json` and preflights only their tasks. It refuses unless the manifest's split,
system, runtime, k, temperature, requested model, price, caps, prompt hash, package versions and
git SHA match the current invocation, neither git tree is dirty, and a task already in the
eval has the same image ID and fingerprint. The manifest keeps `started_at`, unions the served
models and lists each invocation (`invocations`: times, tasks, jobs); trial files and the
manifest are written atomically. `scripts/eval-tasks.sh` (root, cloud) drives one eval task by
task: pull the pinned image (refused below `MW_MIN_FREE_GB`, default 15), re-validate if stale,
`mergewright eval --tasks <id>` (the first creates the eval, later ones `--resume` it), then
remove the image it pulled. It skips tasks whose k trials exist, so a rerun with `--eval-id`
continues. Check: driver on 2 smoke tasks at k = 2, `--jobs 2`, replay: 4 of 4 resolved, a
rerun skips both, the report shows no warning.

As planned:

- **Validation fingerprint.** `validation.json` gets a `fingerprint`: a SHA-256 over the code
  that decides a verdict (`oracle.py`, `sandbox/`, `environments.py`, `tasks.py`, the pytest
  parser in `tools.py`) and the task's own files (`task.json`, patches; the image digest is in
  `task.json`). File level on purpose: simple, and a comment-only edit marks records stale,
  which is safe. `mergewright tasks validate --stale` re-validates only the tasks whose
  record is missing or whose fingerprint differs; `tasks list` shows the stale ones.
- **Eval uses only current records.** `mergewright eval` refuses a task whose record is
  invalid or stale, and the manifest stores each task's fingerprint, so a result traces to
  the validation it relied on.

When to re-validate (agreed 2026-11-23): only after a change that can alter a verdict (the
files in the fingerprint). Quick check while working: the 5 smoke tasks and the dev tasks
whose images are local (about 2–3 min, no pulls). Full re-validation (about 25–30 min, mostly
image pulls) once, right before a run that produces numbers (e.g. step 14). Other changes
need only `ruff` and `pytest` (about 1 min; `pytest -m "not docker"` about 20 s).

## Step 13: `mergewright report` (done)

`src/mergewright/stats.py` (standard library only) and `src/mergewright/report.py`; CLI
`mergewright report <eval-dir> [...] [--out] [--seed] [--resamples]`. The task is the unit
(spec 9.4): resolve rate = mean of per-task resolved fractions; 95 % percentile bootstrap over
tasks (10,000 resamples, seeded; one RNG per interval, so adding an eval leaves the others'
intervals unchanged); pass^k and pass@k; paired bootstrap of the delta over shared tasks
(the baseline is the first argument; "not distinguishable" when the interval contains 0);
McNemar exact only when both evals have k = 1. Infra trials count as not resolved and are also
reported as their own rate; overclaim = DONE trials the oracle rejects / DONE trials without an
infra error. Refuses evals on different splits; warns when models or a shared task's
fingerprint differ, or an eval is incomplete. Check: two smoke evals (k = 2 and k = 1) report
100 % each and a delta of +0.0 [+0.0, +0.0], not distinguishable.

## Step 14: Base run (runbook; to do on the owner's Mac)

The UB endpoint (`zai-org/GLM-5.2` at `https://gateway.bullsai.buffalo.edu/v1`, as in Phase 0)
accepts VPN traffic only, so the cloud sessions cannot reach it (502 from the egress proxy).
Base runs on the owner's Apple Silicon Mac (Docker Desktop; under 35 GB free disk), with
`scripts/eval-tasks.sh` (one image at a time; works without root and with bash 3.2).

1. **Prepare (once).** UB VPN on for the whole run; Docker Desktop running (Settings: "Use
   Rosetta for x86_64/amd64 emulation" on, if offered; check the disk limit under Resources).
   In the checkout: `git checkout checkpoint-2 && git pull`; `python3 -m venv .venv && .
   .venv/bin/activate && pip install -e ".[dev]"`; `docker build -t mergewright-python:1
   docker/python`. `.env` (ignored by git): `MERGEWRIGHT_MODEL=zai-org/GLM-5.2`,
   `MERGEWRIGHT_API_BASE=https://gateway.bullsai.buffalo.edu/v1`, `MERGEWRIGHT_API_KEY=<key>`,
   `MERGEWRIGHT_PRICE_IN_PER_MTOK=0`, `MERGEWRIGHT_PRICE_OUT_PER_MTOK=0` (free endpoint: $0).
2. **Check.** `ruff check .`; `pytest -q` (the Docker tests run on the Mac; tests that need the
   spectree image skip); `mergewright tasks list --split smoke --check`; one smoke eval:
   `mergewright eval --split smoke --k 1 --llm replay --out /tmp/mw-smoke`.
3. **Pilot.** The tree must be clean (`git status`: resume refuses uncommitted code).
   `bash scripts/eval-tasks.sh --split dev --k 1 --llm live --jobs 1 macbre__sql-metadata-127
   gouline__dbt-metabase-158 Sceptre__sceptre-1249` (small images, about 5 GB each). Read
   `wall_s`, `steps`, `tokens_in/out` and `failure_category` in
   `results/<eval-id>/runs/*/base/1.json`; fix any pipeline bug before step 4 (as in steps 11
   and 12, the first real run of a path has found one each time). The pilot is not Base.
4. **Base.** `bash scripts/eval-tasks.sh --split dev --k 3 --llm live --jobs 3` (every dev
   task; images 4.7–8.7 GB, one on disk at a time); note the eval id it prints. An interrupted
   run continues with `--eval-id <id>`. (`kornia-1740`, 28.9 GB, was replaced by
   `arctic3d-181` before any run; `crate-python-472` in between was invalid: `eval/PLAN.md`.) Do not change code,
   prompts, `.env` or package versions between invocations (`--resume` refuses). `--jobs 3`:
   lower it if the Mac runs out of memory (each sandbox has its task's memory ceiling).
5. **Lost trials.** A trial whose run failed only because the endpoint could not be reached (VPN
   drop: `error` mentions a connection error) may be re-run once: delete its `<k>.json`,
   `<k>.patch`, `<k>.trace.jsonl` and run the script again with `--eval-id`. Write each re-run
   here (task, k, reason). Never re-run a trial for any other reason (spec 9.1: infra re-runs
   once, reported apart).
6. **Report and commit.** `mergewright report results/<id> --out results/<id>/report.md`.
   Commit the re-validated `eval/tasks/*/validation.json` and the eval's `manifest.json`,
   `runs/*/base/<k>.json` and `report.md` (decision pending: traces may stay out of git if they
   are large). Record here: eval id, dates, resolve rate with interval, time per trial, issues.

**Prompt for the local Claude session** (on the Mac, in the checkout, after `git pull` on
`checkpoint-2`, with the VPN on, Docker Desktop running and `.env` written by the owner):

```
Do Phase 1 step 14 (the Base run) by following the runbook in docs/plans/phase-1.md,
section "Step 14: Base run". This is the owner's Mac (Apple Silicon, Docker Desktop, under
35 GB free disk, UB VPN on); run commands as the current user, not root.

1. Setup and checks (runbook steps 1-2): venv + pip install -e ".[dev]", build
   mergewright-python:1, ruff check, pytest -q, the smoke record check and one smoke eval.
   If scripts/eval-tasks.sh fails on macOS (bash 3.2), fix it in a small commit.
2. Pilot (runbook step 3): the 3 named dev tasks, k=1, --llm live, --jobs 1. Report to me
   the wall time, steps, tokens and failure category of each trial, and any pipeline bug.
   Stop there and wait for my go.
3. Only after my go: the full Base run (runbook step 4), then the report and commit
   (runbook steps 5-6).

Rules: ask me before the full Base run, before deleting anything other than the task images
the script pulls, and before re-running any trial. Do not change code, prompts, .env or
package versions during the Base run (--resume refuses). Never print, log or commit .env or
the API key. Commit and push only to checkpoint-2.
```

## Step 15: CI (done)

`.github/workflows/ci.yml`, on every push and pull request: `ruff format --check`, `ruff check`,
`docker build` of `mergewright-python:1`, `pytest` (unit tests, Docker guard tests and the
replay smoke suite: `tests/test_smoke_docker.py`, `tests/test_eval_docker.py`), then the record
checks. `mergewright tasks list --split smoke --check` fails the build unless every smoke record
is valid and current, so a change to verdict code must come with re-validated smoke records
(`mergewright tasks validate --stale`, about 1 minute). For dev the same check is a warning only
(no image pulls in CI); those records are re-validated in a cloud session before the next
measured run. Tests that need a SWE-rebench image skip in CI. Changed from the plan: CI checks
the committed records instead of re-validating them, because a validation in CI that is not
committed would not show that the committed records are current.

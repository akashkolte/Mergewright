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

## Step 14: Base run (runbook; deferred by the owner)

### Summary of 2026-11-23: decisions and why (details and evidence below)

**Status.** Base is not run yet. The owner deferred step 14 (2026-11-23) and will decide later
when to start Phase 2. Everything needed to run it is in place; the open items are listed at the
end of this summary. All pilot and attempt results are committed in `results/` (manifests,
trial records, patches, traces).

**Agent changes before Base (each found by a pilot; all part of the frozen Base system):**

| # | Change | Why (evidence) |
|---|---|---|
| 1 | `edit_file` shows the exact text when `old` differs only in whitespace | 12 of 14 edits failed: the model copied `read_file`'s 2 spaces after the line number into the indentation (pilot 1) |
| 2 | Budgets: 50 steps (was 25), 900 s per test run (was 60 s), 5400 s wall-clock safety stop (was 300 s) | Runs ended by time or steps before any test ran; a slow endpoint must not decide results; full suites of big repos take minutes (pilots 1–4) |
| 3 | Explicit `finish` tool | Ending a turn by replying without a tool call is an implicit signal; no pilot model ended its turn (pilot 2) |
| 4 | Two test layers: `run_tests(test_ids)` in the loop, the tester gate after `finish` | The model wanted to check its work, could not, and never finished (pilot 3). Owner's option A: moved from the Phase 2 list into Base; spec 9.2, 6.1 and agenda updated |
| 5 | In-loop verdict first ("All selected tests passed" / "No new failures" / "New failures"), base failures marked, nudge to `finish`; two parser fixes (captured log lines, `PASSED` tail) | A correct patch (42/42 hidden tests) never finished: the summary showed pytest exit code 1 with 0 failures, and old failures looked new (pilot 4) |
| 6 | Gate = regression check: tests failing or erroring at base are ignored | 4 tests fail at base in sceptre's image, so the gate could never pass; the hidden-test oracle judges the fix (pilot 4; spec Section 6 Tester row) |
| 7 | Gate re-runs "new" failures alone once; only those that fail again block (`tools.confirm_failures`) | textual's slug tests fail a different subset on every run of unchanged code; all 3 correct patches were rejected (Base attempt 1) |
| — | `finish` stays required; passing tests do not end a run | The bug's tests are hidden, so the visible suite passes before any change; auto-finish would also make `overclaim` meaningless |

**Models and endpoints.**

- The UB endpoint (GLM-5.2, Phase 0) is VPN-only, so cloud sessions cannot use it.
- The code stays **model-agnostic**: the model is a `.env` setting, and every eval records the
  requested model (and the served one, where the server reports it). **Base is per model:** a
  Phase 2 feature is compared with the Base of the same model; another model means another Base.
- `MERGEWRIGHT_API_STYLE` picks the client: `openai` (any OpenAI-compatible server) or
  `bedrock-converse` (new: every Amazon Bedrock model, Anthropic's included; Claude on Bedrock
  does not accept OpenAI-format requests, and Bedrock Mantle does not serve Sonnet 4.6). No new
  package: `httpx` was already installed with `openai`.
- Pilot results on `sceptre-1249` (same code, model changed in `.env`):

| Model | Endpoint | Result | Steps | Wall | Cost |
|---|---|---|---|---|---|
| Nemotron 3 Ultra | NVIDIA NIM | never finished: searched for tests, then repeated one call 37 times | 25–50 | 93–697 s | $0 |
| Kimi K3.1 | NVIDIA NIM | resolved | 8 | 546 s (33–110 s per call) | $0 |
| GLM-5.3-flash | NVIDIA NIM | not testable: no answer within 120 s | — | — | — |
| Sonnet 4.6 | Bedrock (Converse) | resolved | 23 | 136 s (2–6 s per call) | $0.47 |

- **Sonnet 4.6 was chosen** for Base attempt 1 (fast and consistent; about 1.5–2 h). Kimi stays
  the free alternative (about 4–5 h on NIM's shared service).

**Base attempt 1** (Sonnet 4.6, stopped by the owner after 4 of 20 tasks, $14.19): sceptre 3/3
resolved; textual 0/3 with 3 correct patches (gate problem, fixed by change 7); fastscape 0/3
overclaim (real wrong fixes); dbt-metabase not run (Docker Hub pull limit).

**Other fixes:** `.gitignore` ignored `results/<eval>/runs/` (trial records were never
committed); the rule is now `/runs/` and all trial files are committed.

**Credentials** (stored on the cloud environment `mergewright_cloud` as network secrets, never
in the repository): NVIDIA NIM (`integrate.api.nvidia.com`), Bedrock Mantle
(`bedrock-mantle.us-east-1.api.aws`, used only for tests) and Bedrock Runtime
(`bedrock-runtime.us-east-1.amazonaws.com`). The local `.env` (not committed) is set to Sonnet 4.6
with the price $3 / $15 per million tokens.

**Open items before Base is run again:**

1. Docker Hub's anonymous pull limit (shared address) stopped attempt 1 three times: add a Docker
   Hub access token, or pull the images ahead while the limit allows.
2. Cost: big repositories use 0.7–1.3M input tokens per trial ($2–4.50 with Sonnet); estimate
   $60–150 for Base. There is no per-run dollar cap yet (spec: caps on tokens, dollars, steps and
   wall clock); add one before a paid Base run.
3. Minor: `search_code` does not skip binary files (it matched `.coverage`);
   `test_cpu_memory_and_pid_limits_are_set_in_the_cgroup` reads cgroup v2 files only, so it fails
   on cgroup v1 hosts such as the cloud container.
4. Rerun Base from scratch (new eval id): attempt 1 ran before change 7.

### Runbook

**The model is a setting, not code.** `.env` names it: `MERGEWRIGHT_MODEL`,
`MERGEWRIGHT_API_BASE` (any OpenAI-compatible server), `MERGEWRIGHT_API_KEY` and the two
`MERGEWRIGHT_PRICE_*_PER_MTOK` values. Each eval's `manifest.json` records the requested and the
served model; `mergewright report` shows the model per eval and warns when the evals it compares
use different models. A Base run is therefore Base *for one model*: Phase 2 features are
compared with the Base of the same model. To compare another model later, write its `.env`,
run Base again (new eval id), and pass both eval folders to `mergewright report`.

First Base model: `nvidia/nemotron-3-ultra-550b-a55b` on NVIDIA NIM
(`https://integrate.api.nvidia.com/v1`; no VPN, so the cloud session reaches it). The UB
endpoint (`zai-org/GLM-5.2`, Phase 0) accepts VPN traffic only. On the owner's Mac the same
steps work as the current user (no `cloud-setup.sh`, no `su mw`; Docker Desktop with Rosetta).

1. **Prepare (once).** As root: `bash scripts/cloud-setup.sh --no-images` (Docker daemon, user
   `mw`, venv, `mergewright-python:1`). Key: save the NIM key with the session's credential
   form for host `integrate.api.nvidia.com` (Bearer); the proxy adds it to each request, so it
   never enters the container. `.env` (ignored by git):
   `MERGEWRIGHT_MODEL=nvidia/nemotron-3-ultra-550b-a55b`,
   `MERGEWRIGHT_API_BASE=https://integrate.api.nvidia.com/v1`,
   `MERGEWRIGHT_API_KEY=proxy-injected`, `MERGEWRIGHT_PRICE_IN_PER_MTOK=0`,
   `MERGEWRIGHT_PRICE_OUT_PER_MTOK=0` (NIM API credits: $0 unless the owner gives a price).
2. **Check.** As `mw`: `ruff check .`; `pytest -q` (expected: only
   `test_cpu_memory_and_pid_limits_are_set_in_the_cgroup` fails, cgroup v1);
   `mergewright tasks list --split smoke --check`; one smoke eval:
   `mergewright eval --split smoke --k 1 --llm replay --out /tmp/mw-smoke`. Endpoint check:
   `mergewright run --llm live` on the bundled `examples/buggy_calc` (tool calls must parse;
   note the served model id, the time per step, and any 429 or context-length error).
3. **Pilot (a small sample; not Base).** The tree must be clean (`git status`: resume refuses
   uncommitted code). As root: `bash scripts/eval-tasks.sh --split dev --k 1 --llm live --jobs 1
   macbre__sql-metadata-127 gouline__dbt-metabase-158 Sceptre__sceptre-1249` (small images,
   about 5 GB each; pulled and removed one at a time). Read `wall_s`, `steps`,
   `tokens_in/out` and `failure_category` in `results/<eval-id>/runs/*/base/1.json`; fix any
   pipeline bug before step 4 (as in steps 11 and 12, the first real run of a path has found
   one each time). The owner decides on the full run from these numbers.
4. **Base.** `bash scripts/eval-tasks.sh --split dev --k 3 --llm live --jobs 3` (every dev
   task; images 4.7–8.7 GB, one on disk at a time); note the eval id it prints. Run it in the
   background and keep the session working until it ends: the container is reclaimed when the
   session is idle, and results are not committed until step 6 (a commit changes the git SHA,
   and `--resume` then refuses). An interrupted run continues with `--eval-id <id>`.
   (`kornia-1740`, 28.9 GB, was replaced by `arctic3d-181` before any run; `crate-python-472`
   in between was invalid: `eval/PLAN.md`.) Do not change code, prompts, `.env` or package
   versions between invocations. Lower `--jobs` if the endpoint returns 429 often.
5. **Lost trials.** A trial whose run failed only because of the endpoint (connection error,
   or 429 after the client's retries: see its `error`) may be re-run once, with the owner's
   approval: delete its `<k>.json`, `<k>.patch`, `<k>.trace.jsonl` and run the script again
   with `--eval-id`. Write each re-run here (task, k, reason). Never re-run a trial for any
   other reason (spec 9.1: infra re-runs once, reported apart).
6. **Report and commit.** `mergewright report results/<id> --out results/<id>/report.md`.
   Commit the re-validated `eval/tasks/*/validation.json` and the eval's `manifest.json`,
   `runs/*/base/<k>.json` and `report.md` (decision pending: traces may stay out of git if they
   are large). Record here: eval id, model, dates, resolve rate with interval, time per trial,
   issues.

**Pilot 1 and two changes before Base (2026-11-23).** Eval `20261123T155721Z-f23dba`
(committed in `results/` as evidence; not Base): 0 of 3 resolved, all `FAILED_BUDGET`, no test
run in any trial. Endpoint check before it: `buggy_calc` DONE in 4 steps, 16 s; no 429, no
connection or context-length error in any call; served model = requested model.

| Task | Stopped by | Wall | Steps | Tokens in / out | `edit_file` (failed) |
|---|---|---|---|---|---|
| `sql-metadata-127` | wall clock 300 s | 376 s | 15 | 142k / 9.8k | 1 (0) |
| `dbt-metabase-158` | step cap 25 | 301 s | 25 | 877k / 10.9k | 4 (4) |
| `sceptre-1249` | step cap 25 | 165 s | 25 | 260k / 5.0k | 9 (8) |

1. **`edit_file` hint (tool interface defect).** 12 of 14 edits failed with "old text not
   found". `read_file` prints `{n:>4}  {line}`; the model counted the two spaces after the
   line number as indentation, so every line after the first of its `old` text had 2 extra
   spaces (sceptre: 10 instead of 8, sent 7 times before one edit matched). The tool's own
   output misled its sibling tool, so this is a defect of the Base tool set, not a missing agent
   feature. Change: when `old` matches exactly one place in the file except for whitespace, the
   error shows those lines' exact text (`tools._whitespace_hint`). The edit itself stays exact;
   no whitespace is guessed. Regression test:
   `test_edit_file_shows_the_exact_text_when_only_whitespace_differs`.
2. **`wall_clock_s` 300 → 1800 s (budget).** Model calls took 1–103 s (about 26 output
   tokens/s on long replies), so 300 s ended `sql-metadata` at step 15 and depended on the
   endpoint's speed, not on the agent: a time cap that binds makes results incomparable across
   models and providers. The step cap (25) is now the working budget; the wall clock is a
   safety stop against a hung run. The overshoot (376 s for a 300 s cap) is bounded: the cap is
   checked between steps, and one model call is at most `llm_timeout_s` (120 s).

Both changes are part of the frozen Base system (new git SHA), so pilot 1 is not comparable with
Base and is not resumed. Pilot 2 repeats the same 3 tasks with the changes. The `tools.py` change
made every validation record stale (the fingerprint covers the oracle's code, which includes
`tools.py`); the smoke records were re-validated, and Base re-validates each dev task first.

**Pilot 2 (eval `20261123T162131Z-fd542f`, committed as evidence).** 0 of 3 resolved, all
`FAILED_BUDGET` at the step cap (25), no test run in any trial. The hint works (failed edits
per trial 1, 3 and 2; each task got its edit in), and the wall clock no longer binds (93–697 s).
New failure: after its edit the model never ends its turn. It keeps searching and reading test
files (sceptre: edit done at step 11, then 14 searches for tests) until the step cap, although
the prompt says the test suite runs after its summary. Endpoint: 2 calls of 251 s and 295 s
(120 s per attempt, retried by the SDK); no error reached a trial.

| Task | Wall | Steps | Tokens in / out | `edit_file` (failed) | First good edit |
|---|---|---|---|---|---|
| `sql-metadata-127` | 697 s | 25 | 315k / 7.2k | 2 (1) | step 11 |
| `dbt-metabase-158` | 355 s | 25 | 1151k / 7.4k | 4 (3) | step 20 |
| `sceptre-1249` | 93 s | 25 | 281k / 3.3k | 4 (2) | step 5 |

3. **Explicit `finish` tool (agent-loop defect).** The Coder ended its turn only by replying
   without a tool call: an implicit signal that a tool-calling model may never give (pilot 2:
   0 of 3 ended a turn in 75 steps). The Coder now has a fourth tool, `finish(summary)`; after
   it, the graph goes from the tools node to the tester (`next_after_tools`). The tester node
   is unchanged and stays deterministic: the Coder still cannot run tests. A reply without a
   tool call still ends the turn (replay fixtures). The prompt names `finish` in step 4 and
   says the test suite runs automatically. Tests: `test_finish_tool_hands_over_to_the_tester`,
   `test_finish_after_other_calls_in_one_message_runs_them_first`.

Pilot 3 checks the change on one task (`sceptre-1249`, the easiest of the three: bug found in 2
steps in both pilots), at the owner's request; the next step is decided from its result.

**Pilot 3 (eval `20261123T171204Z-22495b`, committed as evidence).** `FAILED_BUDGET` at the
step cap: 25 steps, 237 s, 338k / 8.8k tokens, no test run. `finish` was offered on every step
and never called. Edits: 6 (3 failed, each fixed by the next call after the hint); last good
edit at step 19, then 6 steps of reading and searching tests. The final patch (scored by the
oracle anyway) passes 41 of the task's 42 hidden tests; it fails
`test_get__resolver_references_same_property_for_other_value__resolves_it`, a test its edit to
`ResolveLater.__call__` broke, which the tester's feedback could have caught.

4. **Two test layers: `run_tests(test_ids)` in the loop (owner's decision, option A).** Pilot 3
   showed why `finish` alone is not enough: after its edits the model wants to check its work
   and searches for a way to run tests, but the only way to run them was `finish`. So it never
   finished. The Coder now has `run_tests(test_ids)`: existing test files or node ids it
   chooses, or the whole visible suite (`tools.run_tests_for_coder`; ids are shell-quoted and an
   option-like id is refused; one step and `tool_timeout_s` per call). It does not write tests
   (test files stay protected; reproduction tests are the Phase 2 Reproducer). The tester node
   is unchanged: after `finish` it runs the full suite as the deterministic gate. The spec
   listed focused `run_tests` as a Phase 2 feature; it moves into Base (spec Section 9.2 Base
   row and Section 6.1, agenda step 14), so Phase 2 no longer measures it. Reason: a Base whose
   runs never reach the tester measures nothing about the gate, the oracle or the statistics.
   Tests: `test_coder_runs_chosen_tests_in_the_loop_before_the_gate`,
   `test_coder_run_tests_runs_the_chosen_ids_shell_quoted_and_lists_failures`,
   `test_coder_run_tests_refuses_options_and_never_runs_them`. Pilot 4: `sceptre-1249` only.

**Pilot 4 (eval `20261123T174620Z-a245e9`, committed as evidence).** First correct patch: the
oracle passes all 42 hidden tests (`patch_resolves: true`). Not resolved: `FAILED_BUDGET` at the
step cap (25 steps, 169 s, 428k / 3.0k tokens); `finish` never called. The edit landed at step
9 (no failed edit); steps 10–25 were 15 `run_tests` calls. Two causes, both in the in-loop
summary, and one gate problem:

- The summary said `38 passed, 0 failed, 0 errors (pytest exit code 1)`; the log tail was a
  list of `PASSED` lines. The model read exit code 1 as a failure and re-ran one test 8 times.
- The full visible suite has 4 failures and 1 error that were there before the change
  (`tests/test_template.py`, an environment problem in the image, and
  `tests/test_connection_manager.py`); the summary did not say so, and the model spent its last
  6 steps on them.
- The tester gate ignores only errors known at base, not failures (spec Section 6). With those
  4 failures, the gate can never pass on this task: even a perfect patch ends `FAILED_NO_FIX`,
  so the task can never count as resolved. Of the 3 pilot tasks, only sceptre has failures at
  base; the other dev tasks are not checked yet.

5. **Realistic budget, a clear in-loop verdict, a regression gate (owner's go, 2026-11-23).**
   - *Budget (`config.Limits`).* `max_steps` 25 → 50: finding and editing took about 10 steps
     in every pilot, and the in-loop test runs come on top. `tool_timeout_s` 60 → 900 s: one
     test run, in the loop or at the gate; the full suite of a large dev repository
     (scikit-image, sqlfluff, xonsh) can take minutes, and a timed-out gate fails whatever the
     patch (sceptre's full suite: 14 s). `wall_clock_s` 1800 → 5400 s: room for 50 steps and
     3 full gate runs, still only a safety stop. `max_test_attempts` (3) and `llm_timeout_s`
     (120 s per attempt) are unchanged.
   - *In-loop verdict (`run_tests_for_coder`).* The answer starts with the gate's verdict ("All
     selected tests passed." / "No new failures." / "New failures: n."), without pytest's exit
     code; tests that failed at base are listed apart as "Already failing before your change
     (not caused by you)"; the output tail is shown only when something fails, and on a pass
     the answer ends "If your fix is complete, call finish."
   - *Two parser fixes (`tools.py`).* A captured log line (`ERROR    logger:file.py:101 ...`)
     was parsed as a test error (sceptre's baseline had one); the log tail no longer lists
     `PASSED` lines (`-rA` ends with one per test, which hid the useful lines).
   - *Gate (spec Section 6, Tester row).* The gate ignores tests that fail *or* error at base:
     it is a regression check. Trade-off: a visible test the bug breaks is no longer required by
     the gate; the hidden-test oracle judges the fix. The other way out, dropping every dev task
     with base failures, would shrink the split for an environment property, not a bug property.
   - *`finish` stays required; passing tests do not end a run.* The bug's tests are hidden, and
     the visible suite usually passes before any change, so "all tests passed" says nothing
     about the fix; ending on it would also make `overclaim` meaningless. The verdict line
     nudges instead.

   Tests: `test_tester_is_a_regression_check_that_ignores_tests_failing_at_base`,
   `test_tester_rejects_a_test_that_passed_at_base_and_now_fails`,
   `test_gate_ignores_failures_and_errors_known_at_base_and_nothing_else`,
   `test_parse_ignores_captured_log_lines_and_the_tail_drops_passed_lines`,
   `test_coder_run_tests_says_pass_and_finish_even_with_pytest_exit_code_1`,
   `test_coder_run_tests_marks_failures_that_were_there_at_base`. Pilot 5: `sceptre-1249` only.

**Pilot 5 (eval `20261123T180052Z-d760f2`, committed as evidence).** `FAILED_BUDGET` at the new
step cap (50 steps, 330 s, 730k / 5.3k tokens); no edit, so no patch and no oracle run. Steps
1–8 are the same as in pilot 4 (temperature 0); at step 9 the model read code instead of
editing, then ran tests: each answer was "All selected tests passed ... If your fix is complete,
call finish." (the bug's tests are hidden, so the visible tests pass at base). From step 14 to
step 50 it sent the same `run_tests` call 37 times in a row, ignoring the verdict and the
`finish` nudge. The tooling worked as intended (clear verdict, base failures marked); this is a
repetition loop of the model.

**Pilot 6: `moonshotai/kimi-k3.1-pro` (eval `20261123T181340Z-4b1c86`, committed as evidence).**
Same code, only `MERGEWRIGHT_MODEL` changed in `.env`. **Resolved**: `DONE` in 8 steps, 546 s,
133k / 2.8k tokens; the gate passed at the first attempt (482 passed, the base failures
ignored) and the oracle passes all 42 hidden tests. The model searched and read (steps 1–5,
several calls per step), edited once (step 6), ran the resolver tests (step 7, all passed) and
called `finish` (step 8). Each model call took 33–110 s on NIM (the `buggy_calc` check before
it: 21–73 s per call). So the loop, the tools and the gate work end to end; Nemotron's failures
in pilots 2–5 are the model's. Minor: `search_code` returned a match in the binary `.coverage`
file (it does not skip binary files); harmless here.

**Bedrock Converse client (model-agnostic live mode).** Claude on Amazon Bedrock is served only
in Bedrock's own formats: Bedrock Mantle refuses `/v1/chat/completions` for Claude ("does not
support the '/v1/chat/completions' API") and does not list Sonnet 4.6. `MERGEWRIGHT_API_STYLE`
now picks the client behind `LLMClient`: `openai` (unchanged; NIM, vLLM, OpenAI) or
`bedrock-converse` (`llm.BedrockConverseClient`: every Bedrock model through the Converse API,
Bearer Bedrock API key; the agent's history stays in OpenAI format and is translated each call;
throttling and 5xx retried). No new package: `httpx` was already installed with `openai` and is
now declared. Sonnet 4.6 needs the inference profile id `us.anthropic.claude-sonnet-4-6` (the bare
id is refused on demand). `buggy_calc` check: DONE in 5 steps, 11 s, 1.1–2.3 s per call. Tests:
`tests/test_bedrock_converse.py`.

**Pilot 7: Sonnet 4.6 on Bedrock (eval `20261123T193125Z-e60151`, committed as evidence).**
**Resolved**: `DONE` in 23 steps, 136 s, 142k / 2.7k tokens, $0.47 at $3 / $15 per million
tokens; the gate passed at the first attempt and the oracle passes all 42 hidden tests. One
failed edit, fixed by the hint at the next step; one in-loop run with a wrong test path (`pytest
exited 4`, reported as "Not a pass"); 15 `search_code` calls looking for tests before `finish`.

| Model (same code) | Steps | Wall | Tokens in / out | Cost | Result |
|---|---|---|---|---|---|
| Kimi K3.1 (NIM) | 8 | 546 s | 133k / 2.8k | $0 (NIM credits) | resolved |
| Sonnet 4.6 (Bedrock) | 23 | 136 s | 142k / 2.7k | $0.47 | resolved |

**Base attempt 1 (eval `20261123T194535Z-97d51e`, Sonnet 4.6, k = 3): stopped by the owner
after 4 of 20 tasks; committed as evidence, not Base.** 9 trials, $14.19:

| Task | Trials | Note |
|---|---|---|
| `sceptre-1249` | ✅ ✅ ✅ | |
| `textual-3334` | ❌ ❌ ❌ budget | all 3 patches correct (`patch_resolves: true`); the gate rejected them |
| `fastscape-32` | ❌ ❌ ❌ overclaim | `DONE` and gate passed, but the hidden tests fail: wrong fixes |
| `dbt-metabase-158` | not run | Docker Hub pull limit (429) |

Two open problems, to fix before Base is run again:

1. **Flaky visible tests defeat the regression gate.** `textual`'s
   `tests/test_slug.py::test_tracked_slugs` fails a different subset of its cases on every run
   of the unchanged code (the 3 baselines: 11–13 failing cases, different ids). The gate ignores
   only the ids that failed in that trial's baseline, so other failing cases count as "new"
   after the fix: a correct patch is rejected, and the Coder spends its remaining steps on tests
   it did not break ($2.35–4.45 per trial, 0.7–1.3M input tokens). Proposed fix: the gate re-runs
   the "new" failures once, alone; only those that fail again count.
2. **Docker Hub's anonymous pull limit** (100 per hour per address, shared with other users of
   the session's egress addresses) stopped the run 3 times (about 30 min each). Fix: a Docker Hub
   access token for authenticated pulls (a per-account limit), or pull the images while the
   limit allows. Also: big repositories cost far more than sceptre; a realistic Base estimate is
   $60–150, and there is no per-run dollar cap yet (spec: caps on tokens, dollars, steps, wall
   clock).

**Prompt for the cloud Claude session** (environment `mergewright_cloud`, branch
`checkpoint-2`):

```
Do Phase 1 step 14 (the Base run) by following the runbook in docs/plans/phase-1.md,
section "Step 14: Base run". This is a cloud session: run setup as root, Mergewright as mw.
Model: nvidia/nemotron-3-ultra-550b-a55b on NVIDIA NIM (https://integrate.api.nvidia.com/v1).

1. Setup and checks (runbook steps 1-2): scripts/cloud-setup.sh --no-images; ask me for the
   NIM key with the credential form for integrate.api.nvidia.com; write .env as the runbook
   says; ruff check, pytest -q, the smoke record check, one smoke eval, and the live endpoint
   check on examples/buggy_calc. Report the endpoint check (served model, time per step,
   errors) before the pilot.
2. Pilot (runbook step 3): the 3 named dev tasks, k=1, --llm live, --jobs 1. Report to me
   the wall time, steps, tokens and failure category of each trial, and any pipeline bug.
   Stop there and wait for my go.
3. Only after my go: the full Base run (runbook step 4) in the background, then the report
   and commit (runbook steps 5-6).

You may pull and remove the task images that scripts/eval-tasks.sh pulls. Rules: ask me
before the full Base run, before any other download or deletion, and before re-running any
trial. Keep the code model-agnostic: the model is set only in .env; no model-specific code.
Do not change code, prompts, .env or package versions during the Base run (--resume
refuses). Never print, log or commit .env or the API key. Commit and push only to
checkpoint-2.
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

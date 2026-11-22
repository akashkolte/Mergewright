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

## Step 11: splits (in progress)

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

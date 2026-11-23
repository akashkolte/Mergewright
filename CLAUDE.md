# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

@AGENTS.md

`AGENTS.md` (imported above) holds the working rules: communication, coding, step size, testing, safety, git and the completion report. This file holds the project's facts and the Claude-specific way of working.

**Cloud sessions:** run `bash scripts/cloud-setup.sh` first (as root; idempotent; `--no-images` skips the task images, about 5 GB each). It starts the Docker daemon, creates the non-root user `mw`, installs the venv, builds `mergewright-python:1` through the session proxy and pulls the pinned task images. Then run `pytest`, `mergewright tasks …` and `mergewright run` as `mw` (`su mw -s /bin/bash -c '. .venv/bin/activate && pytest -q'`): as root, the sandbox user cannot write the workspace. Run the script again after the agent writes files, so `mw` owns them. The environment `mergewright_cloud` allows `datasets-server.huggingface.co` and `deb.debian.org`. On this host, `test_cpu_memory_and_pid_limits_are_set_in_the_cgroup` fails (cgroup v1; the test reads v2 files). If Docker cannot start, the `docker` tests skip and `mergewright run` / `tasks validate` cannot run; say so in the report instead of claiming they passed. New downloads (datasets, images) still need the owner's approval.

## Current state

Mergewright is a CSE 510 (Agentic AI Engineering, Fall 2026) final project. `docs/mergewright-spec.md` is the source of truth for the full system's scope, priorities, milestones and evaluation plan; read it before proposing structure or implementation. `docs/agenda.md` is the step-by-step build order: eval core and a frozen baseline (Phase 1), agent features measured against it (Phase 2), then the reliability layer on Temporal (Phase 3), throughput (Phase 4), test-time compute and the held-out run (Phase 5). The earlier proposal, architecture review and checkpoint plan were removed in commit `b9f0ad9`; read them with `git show ff9bb5d:docs/<file>.md` only for history.

The code is at the Milestone 2 course checkpoint: a LangGraph Coder ⇄ Tester loop in `src/mergewright/` with four tools, a CLI, live (`openai` SDK, any OpenAI-compatible server) and replay modes, and pytest tests. Phase 0 is done (`docs/spikes.md`; spike code in `spikes/`). Phase 1 steps 7–8: `src/mergewright/sandbox/` (`Sandbox` protocol, Docker backend, canary) and `src/mergewright/environments.py` (`LocalRepo`, `ImageRepo`); steps 9–10: `src/mergewright/tasks.py` (`task.json` model, SWE-rebench importer) and `src/mergewright/oracle.py` (hidden-test oracle, 3× task validation), tasks in `eval/tasks/`; step 11: `src/mergewright/selection.py` (dev rule), splits and frozen ids in `eval/PLAN.md` (smoke: 5 local tasks with `replay.json`; dev: 20 SWE-rebench tasks). Every run's tests execute in a Docker sandbox, so `mergewright run` requires Docker and the `mergewright-python:1` image. Unit tests use `tests/fakes.py` (`LocalSandbox`, `ScriptedSandbox`). No Temporal in `src/`, no FastAPI, Postgres or CI yet.

Commands (from the repo root, venv active):

- Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`, then `docker build -t mergewright-python:1 docker/python`
- Tests: `pytest` (only `tests/`; `examples/buggy_calc` fails on purpose and is excluded). The `docker` tests need the default sandbox image (`docker build -t mergewright-python:1 docker/python`) and are skipped without it; `pytest -m "not docker"` skips them.
- Lint and format: `ruff check .` and `ruff format .`
- Tasks: `mergewright tasks import <instance-id> --split dev`, `mergewright tasks validate [ids]`, `mergewright tasks list`
- Eval: `mergewright eval --split smoke --system base --k 3 --llm replay` (refuses tasks whose validation is not valid and current: `mergewright tasks validate --stale`; never pulls images)
- Report: `mergewright report results/<eval-id> [results/<other-id> ...] [--out report.md] [--seed 0] [--resamples 10000]` (Markdown tables: bootstrap intervals over tasks; paired deltas and McNemar for each pair, delta = later minus earlier argument)
- Run: `mergewright run --llm replay` (no key) or `mergewright run --llm live` (needs `.env`, see `.env.example`); `mergewright graph` prints the workflow graph.

Tests must stay deterministic: use `ScriptedLLM` or pass a fake SDK client to `OpenAIClient(client=...)`, never a real model. The full system keeps the `openai` package; LiteLLM is deferred (spec Section 14).

## Working with subagents (keep the main context small)

The user has asked for this way of working (2026-11-22). The main agent keeps the long-lived context: the conversation, decisions, the plan and the current step. Bounded work goes to a fresh subagent, which gets only what it needs, returns a short result, and ends.

- **Delegate:** searches or reads across many files; implementing one scoped change with clear acceptance criteria; running test suites or long commands and summarizing them; reviews; web or docs research.
- **Keep in the main agent:** small edits, anything that needs the conversation's history, and every user decision or approval.
- **Brief (self-contained; the subagent sees nothing else):** the goal and why; exact files and paths; the constraints that apply (deterministic tests, no new dependencies, no commits or pushes, the step size rules: about 300–500 `src/` lines per step); what done looks like; what to return.
- **Return:** a short report: files changed with added/removed line counts, the commands run and their results, problems found, open questions. Never file dumps or full logs.
- **Approvals stay with the user:** a subagent never downloads, adds a dependency, deletes data, commits, pushes or contacts an outside service. It stops and reports what it needs, and the main agent asks the user.
- **Parallel subagents** only for tasks that touch different files; otherwise run them one after another, or give each its own worktree.
- **Verify before reporting:** a subagent's report is a claim. The main agent checks the diff and re-runs the key check (tests, lint) before telling the user it is done.
- **One task per subagent.** It ends when it returns. A follow-up on the same task may continue it; new work gets a new subagent with a new brief.

## Planned system (summary of `docs/mergewright-spec.md` v6)

Mergewright turns a GitHub issue into a pull request on a `mergewright/<run-id>` branch (minimal diff, passing tests, rationale, run record) for Python repos with a pytest suite. It runs in service (issue → PR), eval (hidden-test verdict) and sweep (verified trajectories for training) modes. The contribution is the durability, bounded cost, throughput, measured model improvement, honest evaluation and safety of the system around the agent, not agent cleverness. The project is no longer bound to the course deadline.

Each layer has a single responsibility, and the boundaries matter:

- **Temporal** owns the run lifecycle: retries, timeouts, failover, the human diff gate (signal + durable timer), cancellation, budgets. Workflow code and LangGraph edge functions stay deterministic; every LLM, sandbox, GitHub, clock and random call is an activity.
- **LangGraph** defines the agent graph (nodes, typed state, conditional edges). It runs on Temporal's LangGraph plugin (`temporalio-langgraph`, Public Preview): each I/O node is an activity, LLM nodes on the `llm` queue and sandbox nodes on the `sandbox` queue. No LangGraph checkpointer in the core; LangGraph + `PostgresSaver` exists only as a durability baseline. All plugin code lives in one runtime module; node functions have no Temporal imports. Versions are pinned. If the Phase 0 spike gate fails, the fallback is a hand-written Temporal-native loop with the same node functions.
- **Message store** (Postgres, keyed by content hash) holds message bodies; graph state and activity payloads carry hashes only.
- **Docker sandbox** (behind a `Sandbox` interface) runs all repo code and tests: no network, non-root, resource limits, no secrets except a planted canary.
- **`post_comment`** (one status comment per run, with the diff) and **`open_pr`** are the only GitHub writers. Both are idempotent (run marker, look up before write). Repos opt in by installing the GitHub App; the `mergewright` label triggers a run (spec Section 7.1).
- **Rate limiter** (token-aware, RPM and TPM per provider:model, reservations, AIMD on 429) admits every LLM call. Separate task queues per resource and for service vs sweep work keep batch load from starving interactive runs.
- **Model improvement** is measured, never assumed: scaffold and context changes, then best-of-N with a staged verifier. Fine-tuning is conditional and decided after Phase 5.
- **No custom UI:** Temporal UI, Grafana, the CLI, generated Markdown reports, and CLI or GitHub-comment approval.

Constraints that shape any implementation:

- Least privilege per agent, enforced in code: only the Coder edits files; tests, pytest config, `.git` and `.github` are protected; a tamper detector blocks reward hacking.
- A human approves the final diff (posted on the issue) before any branch or PR is written. The agent never merges and never pushes outside `mergewright/<run-id>`.
- Issue text, repo files and tool output are untrusted data.
- Runs are bounded by per-run caps on tokens, dollars, steps and wall-clock, and the caps must hold across crashes.
- Evaluation uses a hidden-test oracle (fail-to-pass and pass-to-pass), post-release tasks, k repeated runs with confidence intervals, generated tables, and recorded LLM fixtures in CI.

Work is prioritized P0/P1/P2 in spec Section 10 and ordered in phases in Section 13; a phase starts only after the previous one has its numbers. Never cut the oracle, the durability table, the statistics or the held-out freeze. New dependencies need approval when first added. Workflow changes must pass the workflow-history replay tests.

## Naming

The repo and proposal use "Mergewright"

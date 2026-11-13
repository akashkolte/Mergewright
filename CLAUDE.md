# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Mergewright is a CSE 510 (Agentic AI Engineering, Fall 2026) final project. `docs/mergewright-spec.md` is the source of truth for the full system's scope, priorities, milestones and evaluation plan; read it before proposing structure or implementation. `docs/agenda.md` is the step-by-step build order. The earlier proposal, architecture review and checkpoint plan were removed in commit `b9f0ad9`; read them with `git show ff9bb5d:docs/<file>.md` only for history.

The code is at the Milestone 2 course checkpoint: a LangGraph Coder ⇄ Tester loop in `src/mergewright/` with four tools, a CLI, live (`openai` SDK, any OpenAI-compatible server) and replay modes, and pytest tests. No Temporal, FastAPI, Docker sandbox, Postgres or CI yet. `run_tests` runs pytest as a local subprocess, not in a sandbox.

Commands (from the repo root, venv active):

- Setup: `python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"`
- Tests: `pytest` (only `tests/`; `examples/buggy_calc` fails on purpose and is excluded)
- Lint and format: `ruff check .` and `ruff format .`
- Run: `mergewright run --llm replay` (no key) or `mergewright run --llm live` (needs `.env`, see `.env.example`); `mergewright graph` prints the workflow graph.

Tests must stay deterministic: use `ScriptedLLM` or pass a fake SDK client to `OpenAIClient(client=...)`, never a real model. The full system keeps the `openai` package; LiteLLM is deferred (spec §14).

## Planned system (summary of `docs/mergewright-spec.md` v6)

Mergewright turns a GitHub issue into a pull request on a `mergewright/<run-id>` branch (minimal diff, passing tests, rationale, run record) for Python repos with a pytest suite. It runs in service (issue → PR), eval (hidden-test verdict) and sweep (verified trajectories for training) modes. The contribution is the durability, bounded cost, throughput, measured model improvement, honest evaluation and safety of the system around the agent, not agent cleverness. The project is no longer bound to the course deadline.

Each layer has a single responsibility, and the boundaries matter:

- **Temporal** owns the run lifecycle: retries, timeouts, failover, the human diff gate (signal + durable timer), cancellation, budgets. Workflow code and LangGraph edge functions stay deterministic; every LLM, sandbox, GitHub, clock and random call is an activity.
- **LangGraph** defines the agent graph (nodes, typed state, conditional edges). It runs on Temporal's LangGraph plugin (`temporalio-langgraph`, Public Preview): each I/O node is an activity, LLM nodes on the `llm` queue and sandbox nodes on the `sandbox` queue. No LangGraph checkpointer in the core; LangGraph + `PostgresSaver` exists only as a durability baseline. All plugin code lives in one runtime module; node functions have no Temporal imports. Versions are pinned. If the Phase 0 spike gate fails, the fallback is a hand-written Temporal-native loop with the same node functions.
- **Message store** (Postgres, keyed by content hash) holds message bodies; graph state and activity payloads carry hashes only.
- **Docker sandbox** (behind a `Sandbox` interface) runs all repo code and tests: no network, non-root, resource limits, no secrets except a planted canary.
- **`open_pr`** is the only GitHub writer and is idempotent (branch name + PR body marker, look up before write).
- **Rate limiter** (token-aware, RPM and TPM per provider:model, reservations, AIMD on 429) admits every LLM call. Separate task queues per resource and for service vs sweep work keep batch load from starving interactive runs.
- **Model improvement** is measured, never assumed: scaffold and context changes, then best-of-N with a staged verifier. Fine-tuning is conditional and decided after Phase 4.
- **No custom UI:** Temporal UI, Grafana, the CLI, generated Markdown reports, and CLI or GitHub-comment approval.

Constraints that shape any implementation:

- Least privilege per agent, enforced in code: only the Coder edits files; tests, pytest config, `.git` and `.github` are protected; a tamper detector blocks reward hacking.
- A human approves the final diff before a PR is opened. The agent never merges and never pushes outside `mergewright/<run-id>`.
- Issue text, repo files and tool output are untrusted data.
- Runs are bounded by per-run caps on tokens, dollars, steps and wall-clock, and the caps must hold across crashes.
- Evaluation uses a hidden-test oracle (fail-to-pass and pass-to-pass), post-release tasks, k repeated runs with confidence intervals, generated tables, and recorded LLM fixtures in CI.

Work is prioritized P0/P1/P2 in spec §10 and ordered in phases in §13; a phase starts only after the previous one has its numbers. Never cut the oracle, the durability table, the statistics or the held-out freeze. New dependencies need approval when first added. Workflow changes must pass the workflow-history replay tests.

## Naming

The repo and proposal use "Mergewright"

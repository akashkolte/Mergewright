# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Current state

Mergewright is a CSE 510 (Agentic AI Engineering, Fall 2026) final project. The repo is at the proposal stage: it holds the proposal and the proposal deck, and **no application code, tests, CI or lint config exist yet**. `docs/proposal.md` is the source of truth for scope, targets and design; read it before proposing structure or implementation.

## Planned system (from `docs/proposal.md`)

Mergewright turns a GitHub issue labelled `mergewright` into a pull request on a `mergewright/<run-id>` branch (minimal diff, reproducing test, passing tests, rationale, trace link) for Python repos with a pytest suite. The project's contribution is the reliability, measurability, observability and safety of the system around the agents, not agent cleverness.

Each layer has a single owner, and the boundaries matter:

- **Temporal** owns the run lifecycle: retries, timeouts, crash recovery, human-approval waits (signals and durable timers), cancellation. Workflow code stays deterministic; every LLM, sandbox and GitHub call is an activity.
- **LangGraph** owns agent logic: Supervisor routing, typed agent state, Tester→Coder and Reviewer→Coder feedback loops. Agents are Supervisor, Planner, Coder, Tester, Reviewer, each returning schema-validated output.
- **E2B** runs all generated code and tests. Nothing generated runs on the host, and no secrets go into the sandbox.
- **FastAPI + Postgres** provide webhooks, run and approval APIs, the SSE event stream, and storage for runs, checkpoints, idempotency keys and eval results.
- **LiteLLM** is the LLM gateway, used as-is. **OpenTelemetry / Langfuse / Grafana** provide tracing.

Constraints that shape any implementation:

- Least privilege per agent: only the Coder writes files (`apply_patch`); GitHub side effects happen only in a guarded, idempotent activity (`open_pr`).
- A human approves the plan and the final diff before a PR is opened. The agent never merges and never pushes outside `mergewright/<run-id>`.
- Issue text, repo files and tool output are untrusted data.
- Runs are bounded by per-run caps on tokens, dollars, steps and wall-clock.
- Evaluation uses a deterministic pass/fail oracle (hidden fail-to-pass and pass-to-pass tests), pinned models/images/prompts, k repeated runs with confidence intervals, and recorded LLM fixtures in CI.

Scope-cut order if time runs short: streaming extras, then ablation breadth, then security-suite breadth. The durable pipeline, eval harness and fault-injection testing are core.

Team split: Akash Kolte and owns platform (Temporal, idempotency, sandbox lifecycle, FastAPI, fault injection, observability), and Akash and Sheetal Jain both own (LangGraph graph, prompts, retrieval, structured outputs, Reviewer and Tester logic).

## Naming

The repo and proposal use "Mergewright"

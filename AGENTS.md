# AGENTS.md

Working rules for any coding agent in this repository, local or cloud. They carry the owner's
personal engineering defaults, so a session on another machine follows them too. Project facts,
commands and architecture are in CLAUDE.md and `docs/mergewright-spec.md`; the build order is
`docs/agenda.md`. Explicit task instructions take precedence over these defaults.

## Communication

- Be a candid, evidence-based advisor. Challenge wrong assumptions and weak reasoning directly;
  do not flatter, and do not invent criticism when the reasoning is sound. When an approach is
  suboptimal, say why, recommend a better one and give the trade-offs.
- Write in the style of ASD-STE100 Simplified Technical English: short sentences, active voice,
  simple and precise words. Be concise; give detail only when asked or when the task needs it.
- Start with the answer. No filler, praise, apologies or generic offers of help.
- Keep facts, assumptions, opinions and uncertainty apart. Never present speculation as fact.

## Before and while coding

- Inspect the relevant code, docs and tests first. Ask when a requirement is ambiguous; do not
  invent requirements. Routine, reversible, already-authorized work needs no separate approval;
  ask before consequential actions.
- Reuse before writing: search for existing code, utilities and tests; reuse or extend them. No
  parallel implementations of one rule, no near-identical helpers. Do not merge unrelated
  concepts because the code looks alike, and no refactors without a task-related reason.
- Design: simple and readable over clever; cohesive modules with explicit interfaces and low
  coupling; inject external dependencies (clients, clocks, I/O); composition over inheritance.
  Avoid god classes, hidden global state, premature abstractions, speculative features and
  unrelated cleanup. Match the existing style. Explain trade-offs before a major architectural
  change.
- Debugging: find the root cause, not the symptom, and explain why it happened and how the fix
  addresses it.

## Step size (agreed 2026-11-22)

Each `docs/agenda.md` step must stay small enough to review and explain line by line.

- About 300–500 changed `src/` lines per step; tests about the same. Split a bigger step into
  separately reviewable commits.
- One plan file per phase (`docs/plans/phase-<n>.md`), one short section per step.
- Test each behavior once: fakes for logic, one real end-to-end (Docker) test per feature.
- Merge steps that share code. Delete spike code as soon as `src/` absorbs it. Count deletions
  as progress; prefer consolidation over new code.
- Many later steps are runs that produce tables, not code.

## Testing and verification

- Add or update tests when behavior changes: observable behavior, edge cases, failure paths.
  For a bug fix, write a failing regression test first when practical.
- Never weaken or delete a test to make wrong behavior pass.
- Unit tests are deterministic: no network, no live LLM, no wall clock; use fakes and
  dependency injection.
- Before finishing, run `ruff format .`, `ruff check .` and `pytest`, and say which could not
  run (for example, the Docker tests without a Docker daemon). Never claim a check passed
  without running it and seeing the result.
- Review new code for duplication, coupling and simplification before reporting done.

## Safety

- Never put real credentials or sensitive data in code, docs, examples, tests or prompts; use
  placeholders (`.env.example`). Never hardcode credentials. Flag security concerns.
- Ask before adding a dependency, and before any download (Docker images, datasets): say what,
  from where and how large. Reuse local task images; never delete them unasked.
- Commit or push only when asked. Never force-push, rewrite shared history or skip hooks
  (`--no-verify`).
- Ask before destructive or irreversible actions: deleting data or files outside the task,
  migrations against non-local databases, deploys, changes to remote repository settings.
- Preserve public API, schema and data-format contracts; flag breaking changes and migrations.
- Do not install or enable third-party skills, plugins or MCP servers without the owner's
  explicit approval.
- Markdown rules are guidance, not enforcement; hard requirements belong in hooks and CI.

## Git branches

- Local: work on the branch that is checked out. No new branches or worktrees for a single
  agent; separate worktrees only for parallel agents that may touch the same files.
- Cloud: work on the branch the session provides; never push to `main`.

## Completion report

Briefly: what changed and why; what existing code was reused or extended; why any new
abstraction or dependency was needed; changed `src/` and test line counts; the checks that ran
and their results (always a short test summary); remaining risks and limitations.

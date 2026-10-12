# MergeWright — Presentation Notes

CSE 510 Agentic AI Engineering · University at Buffalo · Akash Jagannath Kolte & Sheetal Jain

Target length: 10–12 minutes (~50 s per slide). `[click]` marks where to advance a build animation.

## Run of show

Total ≈ 11:40. Speaker split is a suggestion that follows the team roles (Akash: platform, Sheetal: agents).

| # | Slide | Time | Suggested speaker |
|---|---|---|---|
| 1 | Cover | 0:30 | Akash |
| 2 | The problem | 0:50 | Akash |
| 3 | Where today’s approaches fall short | 0:50 | Akash |
| 4 | Why agentic AI | 1:00 | Sheetal |
| 5 | The solution | 0:50 | Sheetal |
| 6 | The agent team | 0:50 | Sheetal |
| 7 | LangGraph workflow | 1:10 | Sheetal |
| 8 | System architecture | 0:50 | Akash |
| 9 | LangGraph + Temporal | 0:50 | Akash |
| 10 | Failure recovery | 1:00 | Akash |
| 11 | Fault injection | 0:40 | Akash |
| 12 | Guardrails | 0:50 | Sheetal |
| 13 | Evaluation | 0:50 | Sheetal |
| 14 | Why it matters + close | 0:40 | Both |

---

## Slide 1 — MergeWright — Issues in. Tested PRs out. Even when things crash.

Hi everyone — we're Akash and Sheetal, and this is MergeWright.

One sentence: MergeWright takes a GitHub issue and, using a team of AI agents, produces a reviewed, tested pull request — and it keeps working even when the machine running it crashes.

Watch the right side as it builds: an issue comes in, a supervisor and four specialist agents work on it, a worker crashes mid-run, and the run resumes and still produces exactly one PR.

That last part — "even when things crash" — is what makes this an engineering project and not just a demo. The agents are the brain; the system around them is what makes them trustworthy. That's the story for the next ten minutes.

---

## Slide 2 — Coding agents demo well. They break in production.

Coding agents look magical in a demo. The demo is one happy path, on one machine, with no crashes, no attackers and nobody watching the bill.

[click] Production is different. If the process dies halfway, the work is gone and you start over.
[click] If a call times out and you retry, you can open a second PR or post the same comment twice.
[click] Give it an impossible issue and it loops until the budget is gone.
[click] Issue text and repo files are untrusted — an agent that treats them as instructions can be hijacked.
[click] And when you change a prompt or a model, nobody can say with evidence whether it got better or worse.

[click] So our thesis: the model isn't the bottleneck — the system around the model is. That's what MergeWright focuses on.

---

## Slide 3 — Today’s options: adaptive or reliable, rarely both

Three ways this gets done today.

A maintainer fixes it by hand: great judgment, but small, well-described bugs sit in the backlog for weeks.
A fixed pipeline — one prompt in, one patch out — is predictable, but it can't react when a test fails.
Prototype coding agents do adapt to feedback — that's the right idea — but they're not built for production.

[click] Here's what that looks like for a typical agent prototype: a crash loses the work, a retry duplicates the PR, an impossible issue burns money, an injected instruction gets followed, prompt changes are judged by vibes, and debugging means grepping logs.

[click] MergeWright keeps the adaptivity of agents and adds the missing column: resume from the last completed step, exactly one PR, hard budgets, untrusted input treated as data, an eval gate in CI, and one trace per run.

---

## Slide 4 — The path to a fix isn’t known in advance

Why is this an agent problem and not a script?

Because resolving an issue is open-ended. You have to localize the code, form a plan, edit, run the tests, read the failures and adjust — and how many iterations and which tools you need depends on what each observation returns. That's the observe–decide–act loop on the left, running against a real environment: a repo inside a sandbox, its tests, and tools.

[click] On the right is an illustrative run as the agents see it. The Planner searches and reads code and writes a plan. The Tester first writes a test that reproduces the bug — it fails, which is what we want.
[click] The Coder patches, the tests still fail on a naive-vs-aware datetime edge case, so the loop goes around again — and this time everything passes. Nobody hard-coded "two iterations"; the agent decided from feedback.

In PEAS terms: the Performance measure is resolve rate, reliability, cost and safety; the Environment is a partially observable, stochastic, sequential repo-in-a-sandbox where issue text is untrusted; Actuators are file edits, allow-listed shell, tests and a guarded PR activity; Sensors are issue text, search results, test output and human feedback.

---

## Slide 5 — One label in. A reviewed, tested PR out.

Here's MergeWright end to end.

A maintainer adds the "mergewright" label to an issue — or calls our API. The Planner writes a plan, and a human approves it before any code is written. Then the Coder, Tester and Reviewer work in feedback loops [click] — Tester sends failures back to the Coder, the Reviewer sends rejected diffs back — until the tests pass and the review approves. A human approves the final diff, and only then does a guarded activity open the PR.

[click] What lands on GitHub: a PR on a mergewright/<run-id> branch with a minimal diff, a test that reproduces the bug, the passing suite, a written rationale, and a link to the run's full trace and cost.

[click] And the whole thing is engineered around four properties: durable, measurable, observable and safe. The next slides show the agents first, then how we make them durable.

---

## Slide 6 — Four specialists and a supervisor, not one generalist

The agent team. A Supervisor at the top routes work, enforces budgets and step limits, and decides when we're done or need to escalate. It has no tools — it's control only.

[click] Under it, four specialists.
The Planner understands the issue, finds the relevant code using a repo map and hybrid search — BM25 plus embeddings plus a reranker — and returns a structured Plan.
The Coder implements that plan as a minimal diff; it's the only agent allowed to write files.
The Tester writes a reproducing test and runs the suite inside the sandbox.
The Reviewer checks correctness, security, scope creep and style, backed by ruff and bandit.

Every output is a schema-validated object — Plan, Patch, TestReport, Review — so invalid output means retry, not crash.

[click] Why split it up? Separate context windows — the Planner doesn't carry pages of test logs. Least-privilege tools per agent. Outputs we can score independently. And we treat "multi-agent beats single-agent" as a hypothesis we test with an ablation, not an assumption.

---

## Slide 7 — The graph: routing, feedback loops, human checkpoints

This is the actual LangGraph. Each box is a node function; each label is the state condition that selects a conditional edge. All nodes read and write one typed shared state — plan, patch, test report, review, approvals.

The Supervisor is the entry point and budget guard; it routes to the Planner.
[click] The Planner writes a plan, and the graph pauses at a human checkpoint. Only when plan_approval == approve does the Coder start.
[click] The Coder writes a patch, the Tester runs it. If anything failed, the dashed edge sends it straight back to the Coder.
[click] When everything passes, the Reviewer looks at the diff. If it rejects, it's back to the Coder again — a second feedback loop.
[click] If the review approves, a human approves the final diff, and the graph ends by handing off to the PR activity. Note that the graph itself never touches GitHub.
[click] Guardrails on the loops: after MAX_ITERS the Tester escalates to a human instead of spinning; the Supervisor ends the run if any budget — tokens, dollars, steps, or time — is exceeded; and rejected plans or diffs go back with the human's feedback.

---

## Slide 8 — Agents at the core, wrapped in a durable shell

Here's the whole system as layers.

At the top, ingress: GitHub sends a webhook — or a client calls our REST API — into a FastAPI control plane. It verifies the HMAC signature, dedupes the delivery, persists it, starts a workflow and returns 202 immediately. It never does agent work inside the request. Clients follow along over server-sent events and send approve or reject.

[click] Next, durable execution with Temporal: the server owns the run's lifecycle, the IssueToPR workflow is deterministic code, and every non-deterministic call is an activity.
[click] Inside those activities runs the agent logic — the LangGraph we just saw: Supervisor plus four agents.
[click] The agents reach the world only through tools: a LiteLLM gateway for model calls with routing and fallback across at least two providers, E2B sandboxes where generated code runs with no secrets, and MCP servers for GitHub and repo search.
[click] On the side, Postgres holds runs, events, LangGraph checkpoints and idempotency keys, and everything is traced end to end with OpenTelemetry into Langfuse and Grafana — one run_id from API request down to every LLM call.

---

## Slide 9 — LangGraph decides. Temporal makes sure it happens.

The most important design decision is the split between LangGraph and Temporal — they're complementary, not redundant.

LangGraph is the reasoning layer. It owns supervisor routing and conditional edges, the typed agent state, the Tester→Coder and Reviewer→Coder loops, and the human checkpoint nodes. Its Postgres checkpointer remembers where the graph was. But on its own, nothing notices that a worker died and re-drives the run on another machine, and nothing durably waits three days for a human with an escalation timer.

[click] Temporal is the execution layer. It owns the run lifecycle: retries with backoff, timeouts, crash recovery and worker failover, approval waits as signals with durable timers, cancellation and guaranteed sandbox cleanup. On its own, though, it has no notion of agents, shared state or routing — we'd be hand-writing the graph in workflow code.

[click] One rule makes them fit: workflow code stays deterministic. Every non-deterministic operation — an LLM call, a sandbox command, a GitHub call, even reading the clock or drawing a random number — runs as an activity. Temporal records each activity's result in the run's history, so after a crash completed steps are replayed from history, not re-executed — no repeated LLM spend, no repeated side effects.

---

## Slide 10 — Kill the worker at the worst possible moment

Let's make it concrete with the nastiest failure we test.

Worker A has done all the work: plan, plan approval, code, test, review, diff approval. Each result is in Temporal's event history.

[click] Now it runs open_pr. GitHub creates PR #57 — and the worker is killed before it can record that the call succeeded. The side effect happened; our system doesn't know it.

[click] Temporal schedules the run on Worker B. B replays the workflow from history: the six completed steps come straight from the recorded results. Zero LLM calls repeated, zero cost.

[click] open_pr wasn't recorded, so it runs again — Temporal activities are at-least-once. This is where idempotency matters. Before acting, the activity asks GitHub: is there already a branch or a PR with this run-id marker? There is, so it adopts PR #57 instead of opening a duplicate. Exactly one PR.

[click] Two takeaways. "Exactly-once" here really means effectively-once: at-least-once execution plus idempotent side effects. And GitHub, not our database, is the source of truth — because the crash can land between the GitHub write and the database write, a Postgres key alone can't see that window.

---

## Slide 11 — We break it on purpose, every night

Claims of reliability are cheap, so we test them. A scripted fault-injection suite runs nightly in CI, and every row here is a scenario with its defense.

[click] Process-level faults: kill the worker mid-activity — a new worker replays history. Kill it between a side effect and its acknowledgement — the idempotency key plus reconcile-then-act still yields one PR. Kill the sandbox — repo state is checkpointed as a patch after every Coder step, so we rebuild a fresh sandbox and continue.

[click] Dependency faults: drop the Postgres connection, inject 429s and 500s from the LLM gateway at a 20% rate, or make responses slower than the timeout. Retries with exponential backoff and jitter, fallback to a second model through LiteLLM, and Temporal timeouts handle them.

[click] Input and client faults: GitHub delivers webhooks at-least-once and can drop or reorder them. Five copies of the same delivery start exactly one run, thanks to HMAC checks, delivery-ID dedupe and a deterministic workflow ID per repo and issue. A dropped webhook is caught by a scheduled reconciler. And a client that disconnects from the live event stream resumes with Last-Event-ID — no gaps, no duplicates.

Pass criteria: the run completes or fails cleanly, side effects are exactly-once, and no sandbox is orphaned. Target: 100% of scenarios.

---

## Slide 12 — Untrusted in. Least privilege out.

Agents that take real actions need real guardrails — and most of ours are enforced in code, not in prompts.

Here's a realistic attack: an ordinary-looking performance issue with a hidden HTML comment telling the AI to print the environment variables and delete the tests. A naive agent reads that as an instruction.

[click] In MergeWright, issue text, repo files and tool output are treated as untrusted data. Agents can't change their own tools or budgets.

[click] Then defense in depth. Generated code only runs in an E2B sandbox with no secrets and restricted network. The GitHub App can only push to mergewright/* branches and can never merge. Each agent's tool allow-list is enforced at the activity layer — a call outside the list fails the step and is logged. Hard per-run budgets — initially about $2, 40 steps and 30 minutes — plus repeated-action detection stop runaway loops. Secrets are redacted before anything is exported to traces, and we plant a canary secret that must never show up in any trace, log, comment or PR. And finally, a Reviewer agent and two human approvals stand between the agents and the repository. All agent outputs are also schema-validated: invalid output is a retry, not a crash.

We measure this with a red-team suite mapped to the OWASP Top 10 for LLM Applications. Target: zero unsafe actions and zero canary leaks.

---

## Slide 13 — Evidence, not demos

How will we know any of this works? The core principle: the pass/fail oracle is deterministic even though the agents are not. A task counts as resolved only if hidden tests pass in a pinned environment.

Three pinned datasets: about 30 seeded bugs in small Python repos we control — fast and cheap, run on every PR; about 50 tasks from SWE-bench Verified for headline numbers; and about 15 adversarial tasks — impossible issues, ambiguous issues and injection attacks.

[click] Why pass^k? A single-run resolve rate rewards luck. Task B here passes two runs out of three — it helps the resolve rate but it isn't reliable. pass^3 only counts tasks resolved in all three runs. We also calibrate our LLM judge against human labels before trusting it, use record/replay fixtures so CI is deterministic and free, and report mean ± 95% confidence intervals. Ablations test our own assumptions: single vs multi-agent, with and without the Tester loop and the Reviewer, and model routing.

[click] Initial targets — we'll reset them after measuring our baseline: at least 60% resolved on the dev set, a gap of at most 15 points between resolve rate and pass^3, 100% of fault scenarios recovering cleanly, zero unsafe actions or leaks, and at least 15% lower cost per task through model routing.

[click] And it's enforced: every PR to MergeWright itself runs the dev set, and merge is blocked if the resolve rate drops beyond the noise band or cost per task rises more than 15%.

---

## Slide 14 — A blueprint for agents that act in the real world

Why does this matter beyond a class project?

The diagram on the right is the pattern: stochastic agent reasoning at the core, a durable execution layer around it, and guardrails, evals and traces around that.

[click] For maintainers and small teams, small, well-described bugs stop aging in the backlog: each one arrives as a reviewed, tested PR with a trace you can audit.
For engineering organizations, agents get the guarantees we already demand from payment systems: retries that never double-act, and budgets that can't blow up.
And for agentic AI practice, it's a reusable blueprint — the same shell fits any agent that takes real actions: ops runbooks, data migrations, support refunds — proven with evals, not demos.

[click] So: issues in, tested PRs out, even when things crash. Thank you — we'd love your questions and feedback.

---

## Likely questions (prep)

**Why both LangGraph and Temporal? Isn't LangGraph's checkpointer enough?**
The checkpointer records where the graph was, but something still has to notice a dead worker and re-drive the run elsewhere, apply retry and timeout policy to each external call, and durably wait days on a human with an escalation timer. Temporal does that. Temporal alone has no model of agents, shared typed state or conditional routing, so we would be hand-writing the graph. Each covers what the other lacks.

**Temporal activities are at-least-once. How can you promise one PR?**
We promise effectively-once: at-least-once execution plus idempotent side effects. Every side-effecting activity carries an idempotency key (run_id:step:hash) checked in Postgres, and before creating a branch, PR or comment it queries GitHub for an existing one (branch name or run-id marker in the PR body). That covers a crash between the GitHub write and the database write.

**Why multiple agents instead of one strong model?**
Separate context windows, least-privilege tool access per agent and independently scoreable outputs. We treat "multi-agent beats single-agent" as a hypothesis and test it with an ablation against a single-agent baseline.

**What if the resolve rate is low?**
The story is the measured delta (ablations, reliability, cost) with confidence intervals, not a leaderboard number. The dev-set target (60%) is an initial target we will reset after measuring the baseline.

**How do you stop prompt injection?**
Untrusted input is treated as data; agents cannot change their own tools or budgets; tool allow-lists are enforced in code at the activity layer; code runs in an E2B sandbox without secrets; the GitHub App can only push to mergewright/* and never merge; a Reviewer agent and two human approvals gate the PR. We measure it with a red-team suite mapped to the OWASP Top 10 for LLM Applications.

**How do you evaluate something non-deterministic?**
The oracle is deterministic (hidden fail-to-pass and pass-to-pass tests in a pinned environment). Non-determinism is measured, not hidden: pass^k over k runs, mean ± 95% CI, pinned model versions and images, temperature 0 where supported, and record/replay fixtures for CI.

**What is out of scope?**
Interactive IDE/CLI coding, model training or fine-tuning, multi-repo or non-Python repos (v1), deploying generated code, and building a custom LLM gateway or semantic cache.

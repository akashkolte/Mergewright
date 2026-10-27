# Mergewright: Architecture

**Course:** CSE 510 Agentic AI Engineering, Fall 2026 · **Assignment:** Midterm architecture review
**Team:** Akash Kolte (`akashkolte`), Sheetal Jain (`sheetalj2205`)
**Inputs:** `docs/proposal.md` (Milestone 1) and the design spec `mergewright-spec.md` v2 (2 Oct 2026). Where this file adds a decision the proposal did not make, the decision is marked as such in the text.

**Conventions.** "open risk" in a cell means the team knows something is missing and names it. "Gate" means a point where the workflow waits for a human. All dollar figures and caps are initial values; they will be reset after the week-2 baseline and the first calibration run.

---

## 1. Component list

| Name | What it does, in one sentence | What it reads | What it writes |
|---|---|---|---|
| **Entry points** | | | |
| GitHub webhook endpoint (`POST /webhooks/github`) | Receives `issues.labeled` events for issues labelled `mergewright` and starts at most one run per issue. | Request headers (`X-Hub-Signature-256`, `X-GitHub-Delivery`) and JSON body | `webhook_deliveries` and `dead_events` rows in Postgres; starts the Temporal workflow |
| Run API (`POST /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `POST /events/{id}/replay`) | Lets a maintainer start, inspect, cancel a run and replay a dead-lettered event. | Run records in Postgres | New run record; cancel signal to Temporal; replayed event |
| Approval API (`POST /runs/{id}/approvals`) | Accepts a maintainer's approve / edit / reject decision for a gate and forwards it as a Temporal signal. | Authenticated request (decision, feedback text, artifact hash) | Temporal signal; `decision_log` row |
| `mergewright tail` CLI | Streams a run's events over SSE and sends approve / edit / reject to the Approval API. | `GET /runs/{id}/events` (SSE) | Approval API requests; terminal output |
| **Control plane** | | | |
| FastAPI control plane | Hosts the webhook, run, approval and SSE endpoints; verifies, dedupes and acknowledges fast (202); does no agent work. | HTTP requests; Postgres (`runs`, `run_events`, `webhook_deliveries`) | Postgres rows; Temporal workflow starts and signals; SSE stream |
| Temporal server | Stores workflow history and schedules activities, retries, timers and signals so a run survives a worker crash. | Workflow and activity task requests from API and workers | Workflow history (activity results, timers, signals) |
| `IssueToPR` workflow (Temporal worker) | Deterministic state machine of §4: owns the run lifecycle and calls every non-deterministic operation as an activity. | Temporal history; signals; activity results | Activity invocations; run status and `run_events` (through activities) |
| Reconciler workflow (scheduled, every 10 min) | Lists `mergewright`-labelled issues with no run and starts them, so a dropped webhook is recovered. | GitHub issue list (via `list_labeled_issues`); `runs` table | Workflow starts (same deterministic workflow ID, so no duplicates) |
| **Agents (LangGraph nodes)** | | | |
| Supervisor | Routes work between agents, enforces budgets, step limits and repeated-action detection, and decides done / escalate. | Typed run state (counters, budgets, last outputs) | Next-agent decision with reason; `run_events` |
| Planner | Understands the issue, locates relevant code and writes a plan with a confidence value. | Issue (`read_issue`), repo map and code search, files (`read_file`) | `Plan {files, steps, test_strategy, risks, confidence}` in run state |
| Coder | Implements the plan as a minimal diff inside the sandbox. | Plan, files, test failures, review issues, human feedback | Diff in the sandbox (`apply_patch`, `run_shell`); `Patch {diff, rationale}` |
| Tester | Writes a reproducing test, runs the suite and reports failures. | Plan, patch, sandbox files | Test files in the sandbox; `TestReport {passed, failed, logs}` |
| Reviewer | Checks the diff for correctness, security, scope creep and style. | Diff, `static_analysis` findings | `Review {approve, issues[]}` |
| **Tools / activities** | | | |
| `read_issue` | Fetches one issue (title, body, labels, comments) through the GitHub MCP server. | GitHub issue | nothing |
| `list_labeled_issues` | Lists open issues carrying the `mergewright` label (reconciler only). | GitHub issue list | nothing |
| `repo_map` / `search_code` | Returns a repo outline and ranked code snippets (BM25 + embeddings + reranker). | Code index for (repo, base commit) | Index cache (§5) on first use for a commit; otherwise nothing |
| `read_file` | Returns a line range of one repo file, truncated and redacted. | Sandbox working tree | nothing |
| `apply_patch` (Coder only) | Applies a unified diff to the sandbox working tree. | Diff text; sandbox working tree | Modified files in the sandbox |
| `run_shell` (allow-listed) | Runs one allow-listed command in the sandbox with a timeout. | Sandbox | Sandbox filesystem; truncated stdout / stderr to the caller |
| `run_tests` | Runs the pytest suite (or selected tests) in the sandbox and parses the result. | Sandbox working tree, test files | Test artifacts in the sandbox; `TestReport` |
| `static_analysis` | Runs ruff and bandit on the diff. | Diff, sandbox working tree | `findings[]` |
| `llm_call` | Sends a schema-constrained request to a model through the LiteLLM gateway and returns validated output plus usage. | Messages, schema | Gateway usage record; token and cost counters |
| `snapshot_patch` | Stores the current diff against the base commit after each Coder step so a lost sandbox can be rebuilt. | Sandbox diff | Snapshot row (patch blob) in Postgres |
| `request_approval` | Opens a gate: records the pending artifact and hash, emits `awaiting_approval`, starts the 72 h timer. | Plan or diff and its hash | `run_events`, `decision_log` (pending) |
| `open_pr` (activity only, idempotent) | Creates the `mergewright/<run-id>` branch, the PR and the rationale comment, each after a reconcile-then-act lookup. | Approved diff and hash, rationale, GitHub (existing branch / PR) | GitHub branch, PR, comment; idempotency keys in Postgres |
| `cleanup_sandbox` | Destroys this run's sandbox at any terminal state. | Run's sandbox ID | Sandbox deletion; `run_events` |
| **Data sources and stores** | | | |
| GitHub issue data | Source of the task description (untrusted text). | n/a (source) | nothing |
| Target repository (at base commit) | Source code and tests the agents work on, cloned into the sandbox. | n/a (source) | nothing (agents modify only the sandbox copy) |
| Code index | BM25 index, embeddings and repo map for one (repo, base commit). | Target repo files | nothing (built by `repo_map` / `search_code`) |
| Postgres | Holds runs, LangGraph checkpoints, idempotency keys, webhook deliveries, dead events, `run_events`, `decision_log`, snapshots, eval results. | Application queries | Application rows |
| Eval datasets (dev ~30, benchmark ~50, adversarial ~15) | Pinned tasks with hidden fail-to-pass and pass-to-pass tests. | n/a (source, versioned in git / release assets) | nothing |
| LLM cassettes (record / replay fixtures) | Recorded, redacted LLM responses that make CI deterministic and free. | n/a (source) | nothing |
| **External services** | | | |
| GitHub (REST API, webhooks, GitHub App) | Source of issues and webhooks; destination of branch, PR and comment. | n/a (external) | Branch, PR, comment (only through `open_pr`) |
| E2B sandboxes | Isolated Linux environments that run generated code and tests. | n/a (external) | Sandbox filesystem (disposable) |
| LLM providers (at least two) and embedding / rerank models | Supply model completions, embeddings and reranking. | n/a (external) | nothing durable on our side (provider logs per provider policy) |
| GitHub Actions (CI) | Runs lint, tests and the dev-set eval gate on every PR to Mergewright itself. | Repo, cassettes, dataset | Check status; eval result artifacts |
| **Gateway, safety and observability** | | | |
| LiteLLM gateway (self-hosted) | Routes calls to providers, applies model fallback and a per-run key budget. | Requests from `llm_call` | Provider requests; usage records |
| Redaction filter | Scrubs tokens, env-var values and high-entropy strings from tool output and from every trace, log and event before export. | Raw tool output, log and span payloads | Redacted payloads |
| OpenTelemetry SDK / Collector | Carries one trace per `run_id` from API to LLM and tool calls. | Spans and metrics from API and worker | Traces to Langfuse; metrics to Prometheus |
| Langfuse (self-hosted) | Stores redacted prompts, completions, token counts, cost and eval scores per trace. | Redacted OTel spans | Langfuse database |
| Prometheus + Grafana (self-hosted) | Store metrics, draw dashboards and fire alerts (cost spike, error spike, stuck run). | Metrics scraped from API, worker, gateway | Time series; alert notifications |
| Eval harness | Runs a dataset through the system, applies the hidden-test oracle, computes resolve rate, pass^k, cost and latency. | Datasets, run results, hidden tests (oracle phase only) | `eval_results` in Postgres; report files |

---

## 2. Environment

### 2.1 Data sources the agent can access

| Data source | Format | How it is accessed |
|---|---|---|
| GitHub issue (title, body, labels, comments) | JSON | `read_issue` through the GitHub MCP server with a read-scoped installation token; output treated as untrusted data |
| Target repository at the base commit | Git working tree (Python source, pytest tests, config) | Cloned into the E2B sandbox; read with `read_file`, changed with `apply_patch` and `run_shell` |
| Code index and repo map | BM25 inverted index, embedding vectors, text outline | `repo_map` and `search_code`; built once per (repo, base commit). Vector store location (pgvector vs in-process): open risk, decide in week 3 |
| Test and static-analysis output | Text (stdout / stderr), parsed JUnit XML and JSON findings | Returned by `run_tests`, `run_shell`, `static_analysis`; truncated and redacted |
| Human feedback | JSON (`decision`, free text, artifact hash) | Arrives as a Temporal signal from the Approval API; enters agent prompts as data |
| Run state (plan, patch, test report, review, counters) | Typed Pydantic state | LangGraph state, restored from the Postgres checkpointer; agents never query Postgres directly |

### 2.2 Data sources the agent cannot access

| Data source | Why it is excluded |
|---|---|
| Hidden fail-to-pass and pass-to-pass tests and gold patches | They are the oracle. Mounting them would let an agent overfit or read the answer; they are mounted only after the agent run has ended, in the eval harness. |
| Secrets and environment variables (LLM keys, GitHub App private key, E2B key, webhook secret) | They exist only in the API and worker processes. The sandbox has none, so injected instructions cannot exfiltrate them. |
| Postgres tables, Temporal history, traces and the budget configuration | Agents cannot read or change their own budgets, permissions or logs (non-goal in the proposal: agents never change their own tools or budgets). |
| Other repositories and the default branch of the target repo | The GitHub App is installed on one test org and can only write to `mergewright/*` branches; no read tool accepts a repo other than the run's repo. |
| The open internet from inside the sandbox | Egress is restricted to the package index needed by the template. Exact E2B network controls on our plan: open risk, verify in week 2. |
| PR and issue comments written after the run started | Not read in v1, so later comments cannot steer a run; feedback enters only through the Approval API. |

### 2.3 External services

| Service | Limit | Authentication |
|---|---|---|
| GitHub (REST, webhooks, GitHub App) | About 5,000 REST requests per hour per installation (GitHub's documented baseline; verify for our org) plus secondary rate limits on bursts. Our own cap: 5 attempts per GitHub write. | Webhooks: HMAC-SHA256 shared secret (`X-Hub-Signature-256`). API: GitHub App JWT exchanged for a short-lived installation token; permissions limited to issues (read), contents (write on `mergewright/*` only), pull requests (write); no merge. Enforcing "`mergewright/*` only" with a repository ruleset that has no App bypass: open risk, verify in week 5. |
| LLM providers (at least two, through LiteLLM) | Provider RPM / TPM limits for our account tier (verify). Our caps per run: about $2, 400,000 tokens. Monthly budget: open risk (undecided, spec open question 3). | Provider API keys held only by the gateway. Workers use a per-run virtual key with a budget (LiteLLM support to be verified). |
| E2B | Plan-dependent limits on concurrent sandboxes and session length (verify against our plan). Our caps: 30-minute sandbox lifetime per run, 2 concurrent sandboxes per worker. | E2B API key held by the worker only, never passed into a sandbox. |
| Embedding / rerank model | Same provider limits as above; reranker may be a local model. Hosted vs local: open risk. | Through the gateway key if hosted; none if local. |
| Temporal | Self-hosted dev server in Docker Compose. Documented history limits (about 51,200 events / 50 MB per workflow, verify) are far above our 40-step runs. Temporal Cloud is optional. | Local: reachable only on the internal Compose network. Cloud (if used): mTLS or API key. |
| GitHub Actions | Plan-dependent runner minutes; CI uses cassettes so it needs no LLM spend. | `GITHUB_TOKEN`; provider keys as repo secrets, used only by the weekly benchmark job. |

Langfuse, Prometheus and Grafana are self-hosted in Docker Compose, so they are not external services. Auth: Langfuse project key pair, Grafana login.

---

## 3. Tool inventory

Risk is one of **read**, **write**, **irreversible**. `write` includes writes that stay inside the disposable sandbox or our own database; `irreversible` is anything visible outside the system.

| Tool | Input | Output | Error cases | Risk | Gate or hard limit |
|---|---|---|---|---|---|
| `read_issue` | `{repo, issue_number}` | `{title, body, labels, comments}` | 404; 403 / rate limit; body over size cap (truncated); malicious text (treated as data) | read | n/a (read) |
| `list_labeled_issues` | `{repo, label}` | `[{issue_number, updated_at}]` | 403 / rate limit; pagination gap | read | n/a (read) |
| `repo_map` / `search_code` | `{query, k}` | Ranked `[{path, lines, snippet, score}]` | Index missing or stale; empty result; timeout | read | n/a (read) |
| `read_file` | `{path, line_range}` | File text, redacted | Not found; binary or over 200 KB (truncated); path outside repo root (blocked) | read | n/a (read) |
| `static_analysis` | `{diff}` | `findings[]` | Tool crash; unparsable file | read | n/a (read) |
| `llm_call` | `{model, messages, schema}` | Schema-validated output + token usage | 429 / 500 / 503; schema violation; per-run budget exceeded; timeout | read | n/a (read); spend capped by the per-run token and dollar budget (§4) |
| `apply_patch` (Coder only) | `{path, diff}` | Applied status + new file hash | Patch does not apply; path outside repo root; syntax error; diff over 500 changed lines | write | **Hard limit:** Coder-only allow-list enforced at the activity layer; path denylist (`.git/**`, `.github/**`, deletion of test files); size cap; writes touch only the sandbox |
| `run_shell` (allow-listed) | `{cmd, timeout}` | `{exit_code, stdout, stderr}` (truncated, redacted) | Command not on allow-list; timeout; sandbox lost; out of memory | write | **Hard limit:** command allow-list in code; **sandbox:** no secrets, restricted egress, 30-minute lifetime; per-call timeout |
| `run_tests` | `{test_ids?, timeout}` | `TestReport {passed, failed, logs}` | Collection error; flaky test; timeout; sandbox lost | write | **Sandbox** (no secrets, restricted egress); 10-minute timeout per call; tests may not be deleted (see `apply_patch`) |
| `snapshot_patch` | `{run_id, step}` | `{patch_sha}` | Postgres unavailable; diff over size cap | write | **Hard limit:** writes only the row keyed `run_id:step`; idempotent (same key returns same row) |
| `request_approval` (Temporal signal) | `{gate, artifact, artifact_hash}` | `{decision: approve/edit/reject/none, feedback}` | Invalid or unauthenticated signal (ignored, logged); 72 h timeout (see §4) | write | **Gate itself:** only an authenticated maintainer with write access to the repo can signal; decision bound to `artifact_hash`; late signals ignored |
| `cleanup_sandbox` | `{sandbox_id}` | `{deleted: bool}` | Sandbox already gone (treated as success); E2B API error (retried) | write | **Hard limit:** may delete only the sandbox ID recorded for this `run_id`; idempotent |
| `open_pr` (activity only, idempotent) | `{run_id, branch, diff, diff_hash, rationale}` | `{pr_url}` | 403 scope error; branch or PR already exists (found by reconcile-then-act, not duplicated); 422; rate limit; crash between GitHub write and DB write | irreversible | **Gate:** `GATE_DIFF` approval, bound to `diff_hash`, always required (never waived by confidence). **Hard limits:** branch must match `mergewright/<run-id>`; App token cannot merge or push elsewhere; idempotency key per sub-step plus lookup of existing branch / PR (`run-id` marker) before each write |

---

## 4. Transition table

- **Entry state:** `RECEIVED`.
- **Success terminal state:** `PR_OPENED`.
- **Failure terminal states:** `REJECTED_EVENT`, `FAILED_ERROR`, `FAILED_NO_FIX`, `FAILED_BUDGET`, `FAILED_POLICY`, `REJECTED_BY_HUMAN`, `CANCELLED_NO_RESPONSE`, `CANCELLED_BY_USER`. `DUPLICATE_IGNORED` is a no-op terminal (the event was already handled).
- **Gate states:** `GATE_PLAN` and `GATE_DIFF`. Each has the four exits (approve, edit, reject, no response). `GATE_PLAN` is entered only when the Planner's calibrated confidence is below the threshold `t` of §7 (decision added by this document; the proposal had a plan gate on every run). `GATE_DIFF` is entered on every run that reaches review.
- **ACTIVE** means the six states `SANDBOX_SETUP`, `PLANNING`, `CODING`, `TESTING`, `REVIEWING`, `SANDBOX_RECOVERY`. A row whose From state is ACTIVE stands for six identical transitions. Time spent in a gate does not consume budget.
- **Per-run budgets (all rows below draw on them):** about $2, 400,000 tokens, 40 steps (one agent invocation = one step), 30 minutes of active time, 20 tool calls per agent invocation.
- **Budget column:** "n/a" means the row cannot repeat within a run.

| ID | From state | Event | To state | Budget, if this row can repeat |
|---|---|---|---|---|
| T01 | RECEIVED | HMAC invalid, or payload unparsable / missing fields | REJECTED_EVENT (written to `dead_events`) | n/a |
| T02 | RECEIVED | Delivery ID already in `webhook_deliveries`, or workflow ID `owner/repo#issue` already running | DUPLICATE_IGNORED | n/a |
| T03 | RECEIVED | Valid new event persisted and workflow started, 202 returned (also: reconciler finds a labelled issue with no run) | SANDBOX_SETUP | n/a |
| T04 | SANDBOX_SETUP | Sandbox created, repo cloned at base commit, per-agent allow-lists bound | PLANNING | n/a |
| T05 | SANDBOX_SETUP | Transient create / clone error | SANDBOX_SETUP | Max 3 attempts, exponential backoff with jitter |
| T06 | SANDBOX_SETUP | 3 attempts exhausted | FAILED_ERROR | n/a |
| T07 | PLANNING | Valid `Plan` and confidence `p < t` (or confidence waiver not yet enabled) | GATE_PLAN | n/a |
| T08 | PLANNING | Valid `Plan` and `p >= t` with waiver enabled (logged as auto-approval) | CODING | n/a |
| T09 | PLANNING | Tool error, LLM 429 / 5xx, timeout, or schema violation | PLANNING | Max 3 attempts per planning pass; the third uses the fallback model |
| T10 | PLANNING | 3 attempts exhausted | FAILED_ERROR | n/a |
| T11 | GATE_PLAN | **Approve** | CODING | n/a |
| T12 | GATE_PLAN | **Edit**: maintainer sends feedback or an edited plan (edited plan is schema-validated first) | PLANNING | Max 3 edit rounds per run, shared with T36; when used up, "edit" is no longer offered |
| T13 | GATE_PLAN | **Reject** | REJECTED_BY_HUMAN | n/a |
| T14 | GATE_PLAN | **No response** within 72 h of entering the gate | CANCELLED_NO_RESPONSE | n/a |
| T15 | GATE_PLAN | Reminder timer fires (24 h, 48 h) | GATE_PLAN | Max 2 reminders per gate visit |
| T16 | CODING | Valid `Patch` applied and `snapshot_patch` stored | TESTING | n/a |
| T17 | CODING | Tool error, LLM error, timeout, schema violation, or patch does not apply | CODING | Max 3 attempts per coding pass; the third uses the fallback model |
| T18 | CODING | 3 attempts exhausted | FAILED_ERROR | n/a |
| T19 | TESTING | `TestReport` all pass and the new reproducing test fails before / passes after the patch | REVIEWING | n/a |
| T20 | TESTING | Failures in `TestReport`, or reproducing test does not fail on the base commit | CODING | Max 5 Tester-to-Coder iterations per run |
| T21 | TESTING | 5 iterations used and tests still fail | FAILED_NO_FIX | n/a |
| T22 | TESTING | Tool error, timeout, or schema violation | TESTING | Max 3 attempts per testing pass |
| T23 | TESTING | 3 attempts exhausted | FAILED_ERROR | n/a |
| T24 | REVIEWING | `Review.approve = true` and no blocking issues | GATE_DIFF | n/a |
| T25 | REVIEWING | Blocking issues in `Review` | CODING | Max 3 Reviewer rejections per run |
| T26 | REVIEWING | 3 rejections used and issues remain | FAILED_NO_FIX | n/a |
| T27 | REVIEWING | Tool error, timeout, or schema violation | REVIEWING | Max 3 attempts per review pass |
| T28 | REVIEWING | 3 attempts exhausted | FAILED_ERROR | n/a |
| T29 | CODING | Sandbox lost (E2B error or heartbeat timeout) | SANDBOX_RECOVERY | Counts against 2 rebuilds per run (checked on entry, see T34) |
| T30 | TESTING | Sandbox lost | SANDBOX_RECOVERY | Same budget as T29 |
| T31 | REVIEWING | Sandbox lost | SANDBOX_RECOVERY | Same budget as T29 |
| T32 | SANDBOX_RECOVERY | New sandbox built from base image and latest `snapshot_patch` re-applied | TESTING | n/a (re-run does not count toward T20) |
| T33 | SANDBOX_RECOVERY | Rebuild attempt fails (create or apply error) | SANDBOX_RECOVERY | Max 3 attempts per rebuild, backoff with jitter |
| T34 | SANDBOX_RECOVERY | Entered with 2 rebuilds already used, or 3 attempts exhausted | FAILED_ERROR | n/a |
| T35 | GATE_DIFF | **Approve** (decision carries the diff hash) | OPENING_PR | n/a |
| T36 | GATE_DIFF | **Edit**: maintainer sends feedback | CODING | Max 3 edit rounds per run, shared with T12 |
| T37 | GATE_DIFF | **Reject** | REJECTED_BY_HUMAN | n/a |
| T38 | GATE_DIFF | **No response** within 72 h of entering the gate | CANCELLED_NO_RESPONSE | n/a |
| T39 | GATE_DIFF | Reminder timer fires (24 h, 48 h) | GATE_DIFF | Max 2 reminders per gate visit |
| T40 | OPENING_PR | Branch, PR and rationale comment all exist (created now, or found by reconcile-then-act) | PR_OPENED | n/a |
| T41 | OPENING_PR | Transient GitHub error (5xx, rate limit, 422 race) | OPENING_PR | Max 5 attempts, exponential backoff; each sub-step has its own idempotency key |
| T42 | OPENING_PR | 5 attempts exhausted, or 403 scope error | FAILED_ERROR | n/a |
| T43 | OPENING_PR | Branch name outside `mergewright/<run-id>`, or diff hash differs from the approved hash | FAILED_POLICY | n/a |
| T44 | ACTIVE | Budget exhausted (tokens, dollars, steps, or 30 min active time); warning event at 80% | FAILED_BUDGET | n/a |
| T45 | ACTIVE | Tool call outside the agent's allow-list, write to a denylisted path, or injection / canary / secret detected in output | FAILED_POLICY | n/a |
| T46 | ACTIVE | Repeated action: same tool and same argument hash 3 times in one run | FAILED_NO_FIX | Detection threshold: 3 identical calls |
| T47 | RECEIVED, SANDBOX_SETUP, PLANNING, GATE_PLAN, CODING, TESTING, REVIEWING, SANDBOX_RECOVERY, GATE_DIFF | User cancels via API or CLI | CANCELLED_BY_USER | n/a (a cancel received during OPENING_PR is recorded and ignored so no half-created PR is left) |
| T48 | Any non-terminal state | Worker crash, heartbeat lost, or Postgres connection dropped | Same state (Temporal replays history; completed activities are not re-run) | Bounded by the 30-minute active-time budget; gate waits by the 72 h deadline |

`cleanup_sandbox` runs on every transition into a terminal state. The reconciler is a separate scheduled workflow: every 10 minutes it starts a run (T03) for each labelled issue that has none.

---

## 5. Memory table

| Type | Contents | Store | Lifespan | Deletion key |
|---|---|---|---|---|
| Working memory (agent state) | Plan, patch, `TestReport`, `Review`, human feedback, step and budget counters | LangGraph state, persisted by the Postgres checkpointer | Run duration + 30 days after the terminal state | `run_id` |
| Workflow history | Activity results, timers, signals, workflow inputs | Temporal server persistence | Namespace retention: 7 days after the workflow closes | Workflow ID `owner/repo#issue` (+ Temporal run ID) |
| Idempotency keys | `run_id:step:hash` for branch, PR, comment | Postgres `idempotency_keys` | 90 days after the terminal state (GitHub, not this table, is the source of truth, so expiry is safe) | `run_id` |
| Webhook deliveries | `X-GitHub-Delivery` IDs seen | Postgres `webhook_deliveries` | 30 days | `delivery_id` |
| Dead events | Failed events with error text | Postgres `dead_events` | Until replayed or resolved, max 30 days | `event_id` |
| Run events / audit log | Structured events (`agent_started`, `tool_call`, `test_result`, `awaiting_approval`, `budget_warning`), redacted | Postgres `run_events` | 90 days | `run_id` |
| Decision log | One row per gate decision or auto-approval (fields in §7) | Postgres `decision_log` (append-only) | 180 days | `run_id` (deleted only by the retention job) |
| Sandbox filesystem | Working tree, test artifacts, installed deps | E2B sandbox | Run duration; hard kill at 30 minutes; destroyed at every terminal state | `sandbox_id` (recorded on the run) |
| Patch snapshots | Diff against base commit after each Coder step | Postgres `snapshots` | Run duration + 30 days | `run_id:step` |
| Retrieval index | BM25 index, embeddings, repo map for one (repo, base commit) | Worker local disk (vector store choice: open risk, see §2.1) | 7 days since last use | `repo:base_sha` |
| Traces and LLM payloads | Redacted prompts, completions, token counts, cost | Langfuse (self-hosted) | 30 days. Open risk: confirm the self-hosted version supports retention-based deletion; otherwise delete by SQL on `trace_id` | `trace_id` (equals `run_id`) |
| Metrics | Aggregated counters and histograms; no per-run content, no `run_id` label | Prometheus | 15 days (Prometheus default retention) | Metric name + label set |
| Eval results | Per-task outcome, cost, latency, judge scores | Postgres `eval_results` | Kept for the semester (pinned for the write-up) | `eval_run_id` |
| LLM cassettes | Recorded, redacted responses used for CI replay | Git repository (`tests/cassettes/`) | Kept with the code; re-recorded when prompt or model version changes | File path (git history deletion if a secret ever leaks) |

**No cross-run agent memory (decision).** Agents do not remember anything from one run to the next. Each run starts from the base commit, the issue and the pinned prompts only. This keeps runs independent for evaluation (pass^k) and stops one poisoned issue from steering later runs.

---

## 6. Failure analysis

| # | Failure | Kind | Detection | Handling state | What the user sees |
|---|---|---|---|---|---|
| F1 | `run_tests` returns a collection error, or GitHub returns 5xx while `read_issue` runs | Tool error | Non-zero tool status or exception in the activity | TESTING (T22, then T20 if the error is in the code under test) or PLANNING (T09); exhausted: FAILED_ERROR (T23 / T10) | `tool_error` event with "retry 2/3"; on exhaustion: run `FAILED_ERROR`, reason and last error, no PR |
| F2 | LLM call or test run exceeds its timeout (120 s per LLM call, 10 min per `run_tests`) | Timeout | Temporal activity `start_to_close` timeout | Same state retries (T09, T17, T22, T27) with backoff; third LLM attempt uses the fallback model; exhausted: FAILED_ERROR | `timeout` event with attempt number; on exhaustion: `FAILED_ERROR` ("timeout in TESTING after 3 attempts") |
| F3 | Agent output violates its Pydantic schema (e.g. `Plan` without `files`) | Schema violation | Validation in the activity before the result is stored | Same state retries with the validation error appended (T09, T17, T22, T27); exhausted: FAILED_ERROR (reason `schema`) | `schema_retry` event; on exhaustion: `FAILED_ERROR` with the field that failed |
| F4 | Run spends its $2, token, step or 30-minute budget (e.g. an impossible issue) | Exhausted budget | Supervisor counters; Temporal timeout; gateway per-run key budget as backstop | FAILED_BUDGET (T44); loop variant: FAILED_NO_FIX (T46) | `budget_warning` at 80%, then `FAILED_BUDGET` with spend breakdown and the last patch snapshot available for download; no GitHub write |
| F5 | Maintainer never answers a gate | Reviewer does not respond | Durable 72 h Temporal timer | CANCELLED_NO_RESPONSE (T14 / T38) after reminders at 24 h and 48 h (T15 / T39) | `awaiting_approval`, two reminders, then `CANCELLED_NO_RESPONSE` with "start a new run to retry"; sandbox is gone, no PR |
| F6 | Worker process dies mid-activity or between a GitHub write and its acknowledgement | Crash | Temporal heartbeat / task timeout | Same state (T48); `open_pr` then finds the existing branch / PR and does not create another (T40) | Pause in the event stream; stream resumes with no duplicate or missing events (`Last-Event-ID`); exactly one PR |
| F7 | E2B sandbox is killed or unreachable | Tool error | Heartbeat timeout or E2B 404 | SANDBOX_RECOVERY (T29-T32); exhausted: FAILED_ERROR (T34) | `sandbox_recovery` event; then tests re-run. After 2 rebuilds: `FAILED_ERROR` |
| F8 | LLM provider returns 429 / 500 | Tool error | HTTP status from the gateway | Retry with backoff, then fallback model (T09, T17) | Possibly slower run; `llm_retry` event |
| F9 | Webhook duplicated, dropped or reordered | Input fault | Delivery-ID unique constraint; reconciler diff | DUPLICATE_IGNORED (T02); dropped event recovered by reconciler (T03) | One run only; a dropped delivery starts within 10 minutes |
| F10 | Issue text or repo file tries to make the agent act outside its role (exfiltrate env vars, edit CI, delete tests) | Policy violation | Activity-layer allow-list and path denylist; redaction / canary scan | FAILED_POLICY (T45); or Reviewer rejection (T25) if the diff is merely suspicious | `policy_violation` event naming the blocked action; no PR |

---

## 7. Human-oversight plan

### 7.1 Gated actions and their rules

| Action | Gate | Rule |
|---|---|---|
| Open the branch, PR and comment on GitHub (`open_pr`) | `GATE_DIFF` | **Before an irreversible action:** always requires a maintainer's approval of the exact diff (hash-bound). No confidence value can waive it. |
| Accept the Planner's plan and start coding | `GATE_PLAN` | **Below a stated confidence:** the gate is entered only when calibrated confidence `p < t` (§7.2). At or above `t` the plan is auto-approved and the decision is logged. |
| Writes to `.github/**`, `.git/**`, deletion of tests, merge, push outside `mergewright/<run-id>` | None (not available) | **Hard limit** in code (§3, §8), not a gate: the agent cannot request them and a human cannot approve them from inside a run. |
| Raising a run's budget or tools | None (not available) | Agents and gates cannot change budgets; a maintainer starts a new run with new configuration. |

### 7.2 Threshold from two costs

For the plan gate the system compares "ask a human" with "let the plan through".

- **Unit:** US dollars of cost (compute plus maintainer time at an assumed $60 per hour).
- **`r`**: the cost of asking = 5 minutes of maintainer time to read a plan = **$5.00**.
- **`L`**: the loss if a bad plan is let through = the run budget wasted ($2.00) plus 20 minutes of maintainer time spent reviewing and rejecting a doomed diff at `GATE_DIFF` ($20.00) = **$22.00**.
- **`p`**: calibrated probability that the plan names every file the fix needs (measured by Planner file-localization recall on the dev set).
- **Maximum expected utility:** utility is negative cost. Letting it through has expected utility `-(1 - p) * L`; asking has `-r`. Skip the human when `-(1 - p) L >= -r`, i.e. `p >= 1 - r / L`.

**`t = 1 - r / L = 1 - 5 / 22 = 0.773`**, rounded up to **0.78** in configuration so that rounding never favours skipping a review.

Sensitivity: if review is twice as expensive (`r = $10`) then `t = 0.55`; if a bad plan costs `L = $40` then `t = 0.875`. If `r >= L`, `t <= 0` and asking is never worth it for that action. The numbers are team assumptions and will be re-fit after the week-2 baseline.

**Safeguard:** the waiver stays disabled (every plan goes to `GATE_PLAN`, as in the proposal) until calibration is measured on at least 30 dev tasks with a calibration error (ECE) of 0.10 or less. Open risk: Planner confidence calibration is unproven.

### 7.3 Deadline and fallback

- **Deadline:** 72 hours after the run enters a gate (durable Temporal timer); reminders at 24 h and 48 h. A response arriving after the deadline does not count.
- **Fallback:** the run ends in `CANCELLED_NO_RESPONSE`. Nothing is written to GitHub, the sandbox is destroyed, and the user is told to start a new run. The system never "approves by default".

### 7.4 Decision log

Every gate decision, auto-approval and timeout appends one row to `decision_log` (and a mirror event to `run_events`), recording: `decision_id`, `run_id`, gate (`PLAN` or `DIFF`), outcome (`approve`, `edit`, `reject`, `timeout`, `auto_approve`), actor identity (GitHub login or API principal; `system` for timeouts and auto-approvals), UTC timestamp from workflow time, hash of the plan or diff decided on, the confidence `p`, threshold `t`, `r` and `L` values in force, feedback text (redacted), prompt, model and dataset versions, and `trace_id`.

---

## 8. Threat table

First pass. Control kinds: hard limit, soft limit, scope, sandbox, detection.

| Asset | Adversary | Way in | Control, and its kind |
|---|---|---|---|
| Secrets (LLM keys, GitHub App private key, E2B key, webhook secret) | Issue author or repo contributor planting instructions | Prompt injection in issue text or repo files telling the agent to print or send env vars | No secrets in the sandbox (**sandbox**); secrets live only in API / worker / gateway processes and no tool can read them (**hard limit**); canary secret in sandbox env and repo scanned for in every trace, log, comment and PR (**detection**) |
| Default branch, CI config and tests of the target repo | Prompt injection (as above); a compromised agent output | Coder asked to edit `.github/workflows`, delete tests, or push to `main` | Path denylist and branch regex enforced in `apply_patch` and `open_pr` (**hard limit**); GitHub App cannot merge and writes only `mergewright/*` (**scope**); Reviewer checks scope creep (**soft limit**); `GATE_DIFF` human approval bound to the diff hash (**hard limit**) |
| Host and network (our infrastructure) | Malicious code in the repo, its tests or its dependencies | `run_tests`, `run_shell` and dependency install execute attacker-controlled code | All execution in E2B, never on the host (**sandbox**); command allow-list and 30-minute lifetime (**hard limit**); restricted egress (**sandbox**; exact E2B controls: open risk, verify week 2) |
| Run budget (dollars, tokens, time) | Adversarial or impossible issue; model stuck in a loop | Issue crafted to cause endless exploration or repeated retries | Per-run budgets in Supervisor and Temporal timeouts, gateway key budget as backstop (**hard limit**); repeated-action detection (**detection**); cost-spike and stuck-run alerts (**detection**) |
| Run creation and approval integrity | Anyone on the internet; unauthorized user; replayed requests | Forged or replayed webhook; forged approve signal; approving a diff other than the one reviewed | HMAC-SHA256 verification and delivery-ID dedupe (**hard limit**); only repos with the installed App are accepted (**scope**); approvals require an authenticated maintainer with repo write access and carry the artifact hash (**hard limit**); API authentication mechanism (API key vs GitHub OAuth): open risk, decide week 5 |
| Traces, logs, run events and PR text (confidentiality) | Anyone with access to Langfuse / Grafana / the PR; injected exfiltration | Secrets or sensitive strings present in tool output flow into exported telemetry or the PR body | Redaction of tokens, env-var values and high-entropy strings before export (**hard limit**); canary check (**detection**); `read_file` and tool output truncation (**soft limit**) |
| Hidden tests and gold patches (eval integrity) | The agent under evaluation; repo content | Reading or inferring the oracle during a run | Oracle files never mounted before the run ends (**scope**); eval harness runs them in a separate phase (**hard limit**); trajectory review of the adversarial set (**detection**) |

**Every place untrusted text enters the system:**

1. Webhook headers and JSON body, before the HMAC check passes.
2. Issue title, body, comments and labels (`read_issue`).
3. Repository files, comments, docstrings, test names, commit messages and dependency code (`read_file`, `search_code`, `repo_map`).
4. Tool output: shell stdout / stderr, test logs, static-analysis messages, package-install output.
5. Retrieval snippets and embeddings returned by the search stack (derived from item 3).
6. LLM output, including one agent's output used as another agent's input (Planner to Coder, Coder to Reviewer); validated against schemas and never allowed to change tools or budgets.
7. Human feedback text from the Approval API (authenticated, but still flows into prompts as data).
8. Eval dataset issue texts and repos (adversarial set contains injections on purpose).

---

## 9. Design defense

**Decision 1: several role-specific agents, not one agent with every tool.**
*Alternative considered:* a single agent loop with all tools, the ReAct pattern (Yao et al., 2023) as used in SWE-agent (Yang et al., 2024). *Rejected because* the one context window would hold the untrusted issue text and the only credentials-bearing tool path, per-role least privilege (only Coder writes files, only `open_pr` touches GitHub) could not be enforced in code, and no agent's output could be scored alone. A second published alternative, Agentless (Xia et al., 2024), a fixed localize-repair-validate pipeline with no agent loop, was rejected because the Tester-to-Coder and Reviewer-to-Coder feedback loops are what our budgets, loop detection and ablations measure. Both stay as baselines in the ablations, so the choice is tested, not assumed.

**Decision 2: the human gate sits before `open_pr` always, and before coding only below a threshold.**
*Alternatives considered:* (a) approval at every step; (b) a plan gate on every run. *Rejected because* (a) multiplies review cost `r` past the loss `L` it prevents, and (b) charges $5 of maintainer time even on plans very likely to be right. Principle of maximum expected utility (Russell and Norvig, 2021, Ch. 16): act on the option with the highest expected utility, which gives `t = 1 - r / L = 0.77` (§7.2). The opening of a PR is externally visible, so its loss is not bounded in dollars; it is never decided by a threshold.

**Decision 3: Temporal for the lifecycle, LangGraph only for agent logic, and GitHub as the source of truth for outbound writes.**
*Alternative considered:* LangGraph checkpointing alone, with a Postgres idempotency key as the only duplicate guard. *Rejected because* a crash after the GitHub write and before the database write leaves no key, and the retry would open a second PR. Temporal records completed activities (no repeated LLM or tool calls after a crash), and `open_pr` asks GitHub for an existing branch / PR first. This gives effectively-once side effects (at-least-once execution plus idempotent writes).

**References.** Yao et al., "ReAct: Synergizing Reasoning and Acting in Language Models," ICLR 2023. Yang et al., "SWE-agent: Agent-Computer Interfaces Enable Automated Software Engineering," NeurIPS 2024. Xia et al., "Agentless: Demystifying LLM-based Software Engineering Agents," arXiv:2407.01489, 2024. Russell and Norvig, *Artificial Intelligence: A Modern Approach*, 4th ed., Pearson, 2021, Ch. 16 "Making Simple Decisions."

---

## 10. AI Use Statement

We used Claude Code (Anthropic) to draft this file. It read `docs/proposal.md`, the design spec `mergewright-spec.md` and the assignment checklist, wrote all ten sections, and then checked the draft against the rubric (section order, required columns, terminal states, four exits per gate, budgets on repeating rows). The team members have independently verified the arcitecture and take full responsibility for its contents.
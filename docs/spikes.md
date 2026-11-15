# Phase 0 spike results

Agenda steps 2–5. Each spike: setup, commands, results, verdict, effect on the spec.

## Spike 1: runtime gate (agenda step 2, spec Section 4.2)

**Verdict: PASS, all five criteria. Runtime confirmed: LangGraph on `temporalio-langgraph`.**
The spec needs changes for four findings below (F1–F4).

### Setup

| Item | Version |
|---|---|
| Python | 3.12.16 (macOS, arm64) |
| Temporal CLI / dev server | CLI 1.10.0, Server 1.32.2 |
| `temporalio` | 1.36.0 |
| `temporalio-langgraph` | 0.1.0 (Public Preview) |
| `langgraph` / `langchain-core` | 1.2.20 / 1.7.7 |

Code: `spikes/runtime/` (plan: `docs/plans/runtime-spike.md`). Task: `examples/buggy_calc`
with the replay LLM (`TurnReplayLLM`, no live model). Workflows on queue `service`; `coder` on
`llm`; `tools` and `tester` on `sandbox`. Retry policy on every node: 1 s initial interval,
3 attempts. Start-to-close timeouts: coder 60 s, tools 30 s, tester 60 s.

How to reproduce:

```bash
pip install -e ".[dev,spike]"
pytest spikes/runtime                          # criteria 2-5; starts throwaway servers
temporal server start-dev                      # then, in another shell:
python -m spikes.runtime.kill_test sandbox     # criterion 1
python -m spikes.runtime.kill_test service     # criterion 1
```

`spikes/runtime/evidence.py` reads every result below from the workflow history (and from the
run log for LLM call counts). Temporal records only the last attempt of an activity: its
`ActivityTaskStarted` event carries the attempt number and the last failure.

### Results

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | A worker killed mid-run resumes without re-running any completed node | PASS | `kill -9` of the **sandbox** worker while `tools` was in flight: LLM steps `[1, 2, 3, 4]`, no duplicates; only the in-flight `tools` call ran again (attempt 2, last failure `activity StartToClose timeout`); run `DONE`. `kill -9` of the **service + llm** worker (the workflow host) while `tools` was in flight: LLM steps `[1, 2, 3, 4]`, no duplicates; every activity attempt 1; the restarted worker replayed the history and finished the run 15.9 s after the kill, `DONE` |
| 2 | LLM nodes and sandbox nodes on different task queues | PASS | Node metadata `task_queue` is passed to `workflow.execute_activity`. History: `agent.coder` scheduled on `llm` only; `agent.tools` and `agent.tester` on `sandbox` only (`test_nodes_run_on_their_own_queues_and_payloads_carry_hashes_only`) |
| 3 | State holds hashes only; history bounded with continue-as-new | PASS | No payload in any history contains the issue text (decoded and searched). One segment per test attempt; events per segment: 29 / 29, 29 / 29 / 29 for 2- and 3-attempt runs. Payload bytes per segment: 8.1 KB (1 attempt); 8.8, 10.5 KB (2); 8.8, 11.5, 12.9 KB (3). See F3 |
| 4 | `interrupt()` in an activity node resumes from a Temporal signal | PASS | One-node probe graph (`spikes/runtime/probe.py`): the activity node calls `interrupt()`, the workflow exposes the question through a query, a `resume` signal sends `"yes"`, and the graph returns `{"answer": "yes", "interrupted": true}`. Needs the `InMemorySaver` checkpointer in the workflow |
| 5 | Per-node timeouts and retries from Temporal's `RetryPolicy` | PASS | `tools` with 2 injected failures: completed on attempt 3, last failure `injected failure 2 in tools`. `tester` with a 2 s timeout and a 4 s sleep on its first run: completed on attempt 2, last failure `timeout (activity StartToClose timeout)` |

### Findings

- **F1. A timed-out attempt keeps running.** The node's blocking work runs in a thread
  (`asyncio.to_thread`). When an attempt times out, Temporal schedules a retry, but Python
  cannot stop the thread. In the timeout test the first `tester` attempt still finished, one
  second after the retry did (two `test_result` events in the run log). A killed process stops
  its work; a timed-out attempt does not. So every node runs **at least once**, and two
  attempts can overlap in time.
  *Spec effect:* node side effects must be idempotent or guarded. The sandbox invariant
  (Section 4.2: check the tree hash, reset on mismatch) is necessary; also kill the container
  or process on timeout or cancellation (activity heartbeats + a sandbox `kill`), and count an
  orphaned LLM call as spent in the budget ledger (already the rule for unsettled
  reservations).
- **F2. The plugin rejects closures as nodes and changes the graph in place.** A node's
  `task_id` must be `module.qualname` without `<locals>`. Bound methods work, so dependencies
  are still injected (`SpikeNodes`). The plugin replaces each node's function with its
  activity wrapper, so a process builds its graphs once and uses one plugin instance for all
  of its workers.
  *Spec effect:* add to Section 4.2: nodes are methods of a dependency-holding class (or
  module functions); one plugin per worker process.
- **F3. Payload size grows with the conversation.** State carries one 64-character hash per
  message, and every activity input and output carries the list. History stays flat in events,
  but payload bytes grow by about 1.5–2.5 KB per segment here. With the 25-step cap this stays
  far below Temporal's limits, but it is O(messages) per payload.
  *Spec effect (optional):* carry one hash of the whole conversation (a hash chain or a
  stored list) if payload size matters in the load tests.
- **F4. `cache()` is not needed.** The workflow runs the graph until one tester attempt ends,
  then continues-as-new with the carried state, so the next segment starts at `coder` and no
  completed node is replayed. The plugin's `cache()` serves a different pattern (re-running the
  graph from its original input) and grows with every node result, so it was not used.
  *Spec effect:* Section 4.2 "History bound" should say "continue-as-new with the carried
  state" instead of "the plugin's `cache()` skips completed nodes".

Other observations:

- Conditional edges are `async def`; edge functions must not do I/O, so the coder writes a
  `wants_tools` flag into the state for routing.
- Activity names are `<graph>.<node>` (`agent.coder`), as shown in the Temporal UI.
- Per-run values reach the activity through LangGraph's run context (`context_schema`); the
  plugin rebuilds it on the activity side. It must be serializable (plain dict here).
- Activity-only workers (no workflows) work with the plugin.
- The plugin and the workflow sandbox needed no extra passthrough modules.

### Not covered by this spike

- Real LLM calls and real Docker sandboxes (Phase 1); workflow-history replay tests in CI
  (Phase 2); behavior under load; Temporal Cloud (spike 4).

## Spike 2: SWE-rebench image on an x86 VM (agenda step 3)

**Status: PASS on an x86 VM (2026-11-15). Published images run their F2P tests natively.**

Tool: `spikes/rebench/check_task.py`. `fetch` saves one task row from the Hugging Face
dataset viewer API; `run` pulls the image, starts it with `--network none`, checks `/testbed`
is at `base_commit`, applies `test_patch`, runs `install_config.test_cmd` on the test patch's
files, applies the gold `patch`, runs them again, and removes the container. PASS needs: every
F2P test not passing before the fix and passing after; every P2P test passing both times.

Facts found while building it:

- Rows carry `docker_image`, `test_patch`, `patch`, `install_config.test_cmd`,
  `FAIL_TO_PASS`, `PASS_TO_PASS`, `created_at`. Only 7,500 of the 21,336 tasks have published
  images (dataset card), so the dev and held-out splits must pick from those.
- Layout is SWE-bench's: repo in `/testbed`, conda env `testbed` (`/opt/conda`). Images are
  `linux/amd64` only.
- The dataset viewer API had outages (502/503) during the build; `fetch` retries 5xx.

| Run | Host | Image | Pull | Start | Base tests | Gold tests | Verdict |
|---|---|---|---|---|---|---|---|
| Local smoke | Apple Silicon, Docker 29.8.1, emulated amd64 | `spectree-64`, 5.05 GB on disk (1.33 GB compressed) | not timed | 0.1 s | 7.1 s | 3.8 s | PASS: F2P 1/1 failing → passing, P2P 6/6 |
| x86 VM | GCP `e2-standard-4` (4 vCPU, 16 GB), Ubuntu 24.04, `us-east1-b`, 100 GB disk | same; Docker reports 1.33 GB (the compressed size) | 89.1 s | 4.1 s (first start after pull) | 4.2 s | 1.5 s | PASS: F2P 1/1 failing → passing, P2P 6/6 |

What this means:

- One cold task costs about 100 s, and about 90 % of that is the image pull. Warm reuse of
  pulled images (spec Section 3, "Sandbox": warm pool per image) is what makes k = 3 runs and
  best-of-N affordable; plan the VM disk for the images of a whole split.
- The VM's own service account could not delete the VM ("insufficient authentication
  scopes"); delete it from Cloud Shell. That is also the right default: a worker host should
  not hold cloud admin credentials (spec Section 8).

## Spike 3: UB endpoint limits (agenda step 4)

**Status: run on 2026-11-15 against `zai-org/GLM-5.2`. Endpoint usable; its limits were not
reached, so they are still unknown.**

| Measure | Result |
|---|---|
| Rate-limit headers | None sent (no `x-ratelimit-*`, no `Retry-After`). `/models` lists the model but no context length |
| Throughput (ramp, 20-word prompts, 8 output tokens) | No 429 up to concurrency 8. Achieved: 313 RPM at 1, 593 at 2, 835 at 4, 945 at 8. p50 latency 0.17 s → 0.33 s, p95 0.34 s → 0.50 s. TPM 41.6k at 8 (small prompts, so not a TPM test) |
| Context length | 131,088 prompt tokens accepted on the first probe; the upper limit was not found |
| Tool calls (`buggy_calc`, 5 live runs) | 5/5 resolved; 0 malformed tool calls, 0 tool errors; 4.4 steps, 5.2k tokens, 2.3 s per run on average |

What this means:

- The limiter cannot be configured from headers. Start from conservative caps and let AIMD
  on 429 (spec Section 5.1) find the real limit; the achieved 945 RPM is only a lower bound.
- `temperature=0` did not make runs identical: 3 runs took 4 steps and 2 took 5. This
  supports k repeated runs with intervals (spec Section 9.4); single runs are not evidence.
- `buggy_calc` is too easy to judge tool-call quality. Re-measure on the smoke split
  (Phase 1) before the S0–S2 runs.
- Optional, within the provider's terms: `ramp --levels 16,32 --prompt-words 2000` to look
  for the TPM limit, and `context --max-words 262144` to bound the context length.

Tool: `spikes/endpoint/probe.py`, with SDK retries off so every 429 is seen.

| Command | Measures |
|---|---|
| `info` | One tiny call: status, latency, usage, rate-limit headers, the `/models` entry (some servers list the context length) |
| `ramp` | Concurrency 1, 2, 4, 8 × 8 requests; achieved RPM and TPM, p50/p95 latency; stops at the first 429 and records `Retry-After` |
| `context` | Binary search (`max_tokens=1`) for the largest accepted prompt |
| `tools --runs k` | k live `buggy_calc` runs: resolve rate, malformed tool-call rate, tool-error rate, steps, tokens |

Achieved RPM and TPM are lower bounds on the limits, not the limits themselves. Use the
headers or the first 429 for the real limit, and the provider's published limits when there
are any.

## Spike 4: Temporal Cloud actions per run (agenda step 5)

**Status: done (2026-11-15). 9 actions per `buggy_calc` run on Temporal Cloud, the same as
locally.** Not yet cross-checked against the Cloud UI's Usage page, which updates with a delay.

Tools: `spikes/runtime/connection.py` (every spike script connects through the Temporal CLI
profile in `TEMPORAL_PROFILE`; the key stays in the CLI config, never in the repo or logs) and
`spikes/runtime/actions.py` (billable actions per run, counted from history).

Billing rules used (docs.temporal.io/cloud/actions and /cloud/pricing, read 2026-11-15): one
action per workflow start (continue-as-new included), per activity attempt (retries
included), per timer, per signal, per query; two per child workflow start. $50 per million
actions on the Developer plan, plus 10 % of usage spend. Queries and heartbeats are not in the
history, so the counter misses them; the agent workflow uses neither.

| Run | Actions | Breakdown | Cost | Runs per $150 |
|---|---|---|---|---|
| `buggy_calc`, local server | 9 | 1 start + 8 activity attempts | $0.0005 | 303,030 |
| `buggy_calc` with 2 injected `tools` failures, local | 11 | 1 start + 10 attempts (2 retries billed) | $0.0006 | 247,934 |
| `buggy_calc` on Temporal Cloud, 3 runs | 9 each | 1 start + 8 activity attempts | $0.0005 | 303,030 |

Run time for the same `buggy_calc` run (replay LLM, workers on a Mac): 0.6 s on a local
server, 1.8–1.9 s on Temporal Cloud. The extra ~1.2 s is the network round trip for each
workflow and activity task (the run has 8 activities, so about 17 task round trips). It is
small next to real LLM and test time, but it is a fixed cost per step.

Estimate for a full system run (not measured; the parent workflow does not exist yet): per
candidate, about 1 start per test attempt + 1 per LLM step + 1 per tool step + 1 per test
attempt; 25 steps and 3 attempts give about 53. With N = 4 candidates plus the parent (start,
4 child starts × 2, checks, `post_comment`, `open_pr`, the diff-gate timer and the approval
signal, about 15), that is about 230 actions, or about $0.013 per run and about 12,000 runs
per $150. Even at 1,000 actions per run, $150 covers about 2,700 runs.

What this means (so far): actions will not limit Cloud use; the $150 is enough for the
spike and the optional L7 comparison. Retries and best-of-N multiply actions, so count them
in the cost model. One gap: in this spike the sandbox worker holds the Cloud API key, which
spec Section 8 forbids for the real system (Cloud credentials stay out of sandbox workers).

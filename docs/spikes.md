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

**Status: tooling done; local smoke test passed under emulation; x86 VM run pending.**

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
| x86 VM | pending | | | | | | |

## Spike 3: UB endpoint limits (agenda step 4)

**Status: tooling done; not run yet (needs the endpoint key in `.env`).**

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

Not started.

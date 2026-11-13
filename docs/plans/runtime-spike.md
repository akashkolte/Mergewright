# Plan: Phase 0 runtime spike (agenda step 2)

Goal: run the `buggy_calc` Coder ⇄ Tester graph on the Temporal LangGraph plugin
(`temporalio-langgraph==0.1.0`, `temporalio==1.36.0`) and test the five gate criteria in spec
Section 4.2. Results go to `docs/spikes.md`.

## Part A: make the graph runnable under the plugin (done)

The plugin takes an uncompiled `StateGraph` once per worker, and a node may run in a different
process on each attempt. Four things in the Milestone 2 graph did not fit:

| Problem | Change |
|---|---|
| Nodes closed over per-run `deps` | Node bodies are module functions in `mergewright.graph` (`coder_step`, `tools_step`, `tester_step`). The spike graph builds per-run deps from the LangGraph run context (`RunContext`: run id, workspace, log path, start time) |
| `ScriptedLLM` keeps its position in memory, so a restarted worker replays the wrong response | New `TurnReplayLLM`: response n for the n-th assistant turn in the messages, a pure function of the input |
| Sync conditional edges (the plugin needs `async def`) | Spike graph uses async edge wrappers around `next_after_coder` / `next_after_tester` |
| Wall-clock cap read a start time taken at build time, on a per-process monotonic clock | Start time is `RunContext.started_at`, on `time.time()`, so it holds across processes |

Each run gets a fresh copy of the repo (`copy_workspace`, shared with `run_agent`).

Non-goals for Part A: Temporal code, task queues, hashes-only state, continue-as-new.

Layout: `spikes/runtime/graph.py` (no Temporal imports; node metadata is passed in by the
caller), tests in `spikes/runtime/test_graph.py`. Run with `pytest spikes/runtime`. The default
`pytest` run does not include the spike.

## Part B: Temporal wiring and gate criteria (done)

- `store.py`: content-addressed message store; state holds hashes only (criterion 3).
- `graph.py`: nodes are methods of `SpikeNodes` (the plugin rejects closures); hashes-only
  state; injected faults for the retry, timeout and kill tests; the graph ends after each
  tester attempt.
- `workflows.py`: `AgentWorkflow` (continue-as-new with the carried state after each tester
  attempt; `cache()` not used, see `docs/spikes.md` F4) and `InterruptProbeWorkflow`.
- `worker.py`: plugin setup and node options (`execute_in`, `task_queue`, timeouts,
  `RetryPolicy`); one plugin per process. `start.py`: start runs. `evidence.py`: read the
  gate evidence from the history. `kill_test.py`: criterion 1.

Result: all five criteria pass. Results and findings: `docs/spikes.md`, spike 1.

## Validation

- `ruff check .`, `ruff format --check .`, `pytest`, `pytest spikes/runtime` (14 tests; the 5
  in `test_temporal.py` start throwaway Temporal servers from the installed CLI).
- `python -m spikes.runtime.kill_test sandbox` and `... service` against
  `temporal server start-dev`.

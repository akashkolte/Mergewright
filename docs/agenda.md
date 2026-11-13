# Mergewright implementation agenda

Step-by-step build order for [mergewright-spec.md](mergewright-spec.md) (v6). One line per step. Finish each phase with its numbers before you start the next.

**No custom UI.** Use the Temporal UI, one Grafana dashboard, the `mergewright` CLI, generated Markdown reports, and `mergewright approve` or an `/approve` GitHub comment.

**Runtime:** the LangGraph graph runs on Temporal's LangGraph plugin, each node an activity (spec §4.2). Fallback if the Phase 0 gate fails: a hand-written Temporal-native loop with the same node functions.

## Phase 0: setup and spikes (spec §4.2, §13)

1. Tag the current code `v0-langgraph`.
2. Runtime spike on `buggy_calc` with the local dev server. Pass all five gate criteria in spec §4.2:
   - kill the worker mid-run; no completed node re-runs;
   - LLM nodes and sandbox nodes on different task queues;
   - state holds hashes only; history bounded with continue-as-new + `cache()`;
   - `interrupt()` resumes from a signal;
   - per-node timeouts and retries from `RetryPolicy`.
3. Spike: run one SWE-rebench image's F2P tests on an x86 cloud VM.
4. Spike: measure the UB endpoint (RPM, TPM, context length, tool-call quality).
5. Spike: connect a worker to Temporal Cloud; measure actions per run; estimate how many runs $150 covers.
6. Write results in `docs/spikes.md`. Confirm the runtime or choose the fallback. If another spike fails, fix the spec first.

## Phase 1: foundation (spec §4.5, §7, §9)

7. `Sandbox` protocol + Docker backend: no network, non-root, limits, canary. One test per guard.
8. `run_tests` runs in the sandbox; file tools stay on the bind-mounted workspace.
9. `task.yaml` format + `mergewright tasks validate` (F2P/P2P checked 3×).
10. Smoke split (5 tasks), then dev ≥ 40, adversarial ≥ 20.
11. Oracle: fresh sandbox, hidden tests, result per test.
12. `mergewright eval --split --system --k` → `results/` + manifest.
13. `mergewright report`: Wilson CI, paired bootstrap, McNemar → Markdown tables.
14. Run S0, S1, S2 on dev, k = 3, with the current in-process runner.
15. CI: ruff, pytest, replay smoke suite.

**Exit:** dev table with intervals, generated from manifests.

## Phase 2: durable core (spec §4, §9.5)

16. Postgres schema: messages (by hash), runs, budget ledger, results. `MessageStore` + tests.
17. Node functions read and write messages by hash; no Temporal imports in them.
18. `mergewright/runtime/temporal_graph.py`: plugin setup, node metadata (`execute_in`, queues, timeouts, retries). Pin `temporalio` and `temporalio-langgraph`.
19. Candidate workflow: runs the graph; continue-as-new after each test attempt.
20. Parent workflow: starts candidates, runs checks, diff gate (signal + 72 h timer), `open_pr`.
21. Budget ledger: reservation before each LLM call, settle after, unsettled counted as spent.
22. Localizer node (schema-validated output).
23. Checks: ruff, diff size, tamper detector.
24. `mergewright approve <run>` and `/approve` GitHub comment → approval signal.
25. `open_pr`: idempotent; fake GitHub server for tests; one real PR on your own repo.
26. Workflow-history replay tests in CI.
27. R1 baseline: same graph + `PostgresSaver` + restart supervisor. R0 is the current `runner.py`.
28. K1–K7 and K10: 10 trials × R0/R1/R2; durability table generated in CI.
29. Two error-analysis rounds: S2+E1, S2+E2.

**Exit:** durability table; S2 on Temporal matches the Phase 1 S2 resolve rate within its interval; one real PR.

## Phase 3: throughput and distributed systems (spec §5, §9.6)

30. Task queues `llm`, `sandbox`, `oracle`, `github`; workflow queues `service` and `sweep`.
31. Fake provider: latency distribution, RPM/TPM limits, 429 + `Retry-After`.
32. Rate limiter: Postgres token bucket, reserve/settle, reservation TTL, AIMD on 429.
33. FastAPI: `POST /runs` with admission control (429 + queue position), `GET /runs/{id}`.
34. Sandbox warm pool + reaper.
35. Prometheus metrics + one Grafana dashboard.
36. Load generator; L1–L3 on one host.
37. L5 on two hosts; L4; K8, K9, K11, K12.

**Exit:** saturation curve with a named bottleneck; limiter comparison chart; two-host scaling factor.

## Phase 4: test-time improvement (spec §6.1–6.2)

38. Parse-checked `edit_file`, focused `run_tests(test_ids)`, context policy (S2+C).
39. Reproducer (S2+R).
40. Best-of-N: N candidate workflows + staged verifier; N = 1, 2, 4, 8.
41. Cascade: cheap model first, escalate on rejection.
42. Self-host vLLM; L6 (prefix caching on vs off).
43. Freeze `eval/PLAN.md`; build held-out (100–150); run S2, BoN-N, Ref (mini-SWE-agent).
44. Decide budget and training go/no-go.

**Exit:** Best-of-N chart (pass@N vs selection accuracy vs $); held-out table with intervals.

## Phase 5: training (only if step 44 says go; spec §6.3)

45. Train split (disjoint repos) + sweep mode → trajectories.
46. Trained verifier first; then RFT LoRA if the verifier result justifies it.
47. Tamper rate before vs after training.

**Exit:** base vs tuned on held-out, paired delta with interval.

## Phase 6: validation (spec §13)

48. GitHub App + webhook (HMAC, dedupe) + gVisor; run on your own and opt-in repos.
49. Production-readiness checklist (spec §13.1): separate image builder, approver allow-list + audit log, backups, alerts, online metrics, cost per merged PR.
50. README rewrite with generated tables; smoke and durability suites reproducible in < 15 minutes.
51. 2–3 blog posts with data; demo video; upstream issues or PRs for bugs found in dependencies.
52. Optional: L7 (self-hosted Temporal vs Temporal Cloud).

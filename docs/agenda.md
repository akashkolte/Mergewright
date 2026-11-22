# Mergewright implementation agenda

Step-by-step build order for [mergewright-spec.md](mergewright-spec.md) (v6). One line per step. Finish each phase with its numbers before you start the next.

**Order (decided 2026-11-15):** eval core and a frozen baseline first, then agent features measured against it, then the reliability layer (Temporal) around the finished agent, then throughput, then test-time compute and the held-out run. Build only as much harness as the next decision needs.

**No custom UI.** Use the Temporal UI, one Grafana dashboard, the `mergewright` CLI, generated Markdown reports, and `mergewright approve` or an `/approve` GitHub comment.

**Runtime:** the LangGraph graph runs on Temporal's LangGraph plugin, each node an activity (spec Section 4.2). Node functions stay plain functions with no Temporal imports, so agent work in Phases 1–2 moves onto Temporal in Phase 3 without rework.

## Phase 0: setup and spikes (done; spec Section 4.2, Section 13; results in `docs/spikes.md`)

1. ✅ Tag the current code `v0-langgraph`.
2. ✅ Runtime spike on `buggy_calc` with the local dev server: all five gate criteria in spec Section 4.2 pass.
3. ✅ Spike: one SWE-rebench image's F2P tests on an x86 cloud VM.
4. ✅ Spike: measure the UB endpoint (RPM, TPM, context length, tool-call quality).
5. ✅ Spike: Temporal Cloud actions per run; runs per $150.
6. ✅ Results in `docs/spikes.md`; runtime confirmed.

## Phase 1: eval core and baseline (spec Section 4.5, Section 7, Section 9)

7. ✅ `Sandbox` protocol + Docker backend: no network, non-root, limits, canary. One test per guard.
8. ✅ `run_tests` runs in the sandbox; file tools stay on the bind-mounted workspace and redact the canary; the workspace is filled from the task image's `/testbed`.
9. `task.json` format + `mergewright tasks validate`: gold-patch check 3× (F2P fails on base and passes with the gold patch; P2P passes on both); flaky tasks dropped and counted.
10. Oracle: fresh sandbox per trial, hidden tests the agent never sees, result per test. The oracle is ground truth, never the agent's own test run.
11. Splits: smoke (5, local, recorded responses, for CI); dev: 5 SWE-rebench tasks to prove the pipeline, then 20 (at most 3 per repo); held-out IDs chosen now (post-release, with images) and frozen in `eval/PLAN.md`, never run or inspected before Phase 5.
12. `mergewright eval --split --system --k` → `results/` per trial (resolved, tokens, $, latency, steps, tool calls, failure category, trace) + manifest (server-reported model ID and dates).
13. `mergewright report`: bootstrap over tasks; paired bootstrap and McNemar for system pairs → Markdown tables.
14. Baseline (Base): the current agent unchanged, config frozen, dev at k = 3 → the reference results.
15. CI: ruff, pytest (unit and Docker guard tests), replay smoke suite.

**Exit:** Base dev table with task-level intervals, generated from manifests.

## Phase 2: agent features (spec Section 4.4, Section 6.1, Section 9.2)

16. Error analysis of Base failure traces → ranked failure categories. Each next feature comes from the top category.
17. Before each comparison, write its decision rule (default: keep if the paired 95 % interval of the resolve-rate delta is above 0, or if tokens per resolved task drop and the delta's lower bound is above −5 points).
18. Localizer node (schema-validated output).
19. Checks: ruff, diff size, tamper detector (protected tests, pytest config, `.git`, `.github`; allows the Reproducer's own `tests/test_mergewright_repro_*.py`), built together with the adversarial split (≥ 20).
20. Candidates, in the order the failure categories give: parse-checked `edit_file`; focused `run_tests(test_ids)`; context policy; Reproducer.
21. Stop after 2–3 features with a measured gain, or when the candidates are tried. Grow dev to 40 if the intervals are too wide to decide. Run S0, S1, S2 at k = 3.

**Exit:** ablation table (each feature vs Base, paired intervals); adversarial results (tamper attempts blocked, 0 canary leaks).

## Phase 3: durable core (spec Section 4, Section 9.5)

22. Postgres schema: messages (by hash), runs, budget ledger, results. `MessageStore` + tests.
23. Node functions read and write messages by hash; no Temporal imports in them. Merge `ScriptedLLM` into `TurnReplayLLM`.
24. `mergewright/runtime/temporal_graph.py`: plugin setup, node metadata (`execute_in`, queues, timeouts, retries). Pin `temporalio` and `temporalio-langgraph`. Delete `spikes/runtime/` once this replaces it.
25. Candidate workflow: runs the graph; continue-as-new after each test attempt.
26. Parent workflow: starts candidates, runs checks, `post_comment` (diff), diff gate (signal + 72 h timer), `open_pr`.
27. Budget ledger: reservation before each LLM call, settle after, unsettled counted as spent.
28. `mergewright approve <run>` and `/approve` or `/reject` GitHub comments → decision signal.
29. `post_comment` and `open_pr`: idempotent via run marker; fake GitHub server for tests; one real PR on your own repo.
30. Workflow-history replay tests in CI.
31. R1 baseline: same graph + `PostgresSaver` + restart supervisor. R0 is the current `runner.py`.
32. K1–K7 and K10: 10 trials × R0/R1/R2; durability table generated in CI.

**Exit:** durability table; S2 on Temporal matches the Phase 2 S2 resolve rate within its interval; one real PR.

## Phase 4: throughput and distributed systems (spec Section 5, Section 9.6)

33. Task queues `llm`, `sandbox`, `oracle`, `github`; workflow queues `service` and `sweep`.
34. Fake provider: latency distribution, RPM/TPM limits, 429 + `Retry-After`.
35. Rate limiter: Postgres token bucket, reserve/settle, reservation TTL, AIMD on 429.
36. FastAPI: `POST /runs` with admission control (429 + queue position), `GET /runs/{id}`.
37. Sandbox warm pool + reaper.
38. Prometheus metrics + one Grafana dashboard.
39. Load generator; L1–L3 on one host.
40. L5 on two hosts; L4; K8, K9, K11, K12.

**Exit:** saturation curve with a named bottleneck; limiter comparison chart; two-host scaling factor.

## Phase 5: test-time compute and held-out (spec Section 6.2, Section 9)

41. Best-of-N: N candidate workflows + staged verifier; N = 1, 2, 4, 8.
42. Cascade: cheap model first, escalate on rejection.
43. Self-host vLLM; L6 (prefix caching on vs off).
44. Re-check the frozen held-out set against the cutoff of the model used for headline numbers (drop, never replace); run S2, BoN-N, Ref (mini-SWE-agent).
45. Decide budget and training go/no-go.

**Exit:** Best-of-N chart (pass@N vs selection accuracy vs $); held-out table with intervals.

## Phase 6: training (only if step 45 says go; spec Section 6.3)

46. Train split (disjoint repos) + sweep mode → trajectories.
47. Trained verifier first; then RFT LoRA if the verifier result justifies it.
48. Tamper rate before vs after training.

**Exit:** base model vs tuned model on held-out, paired delta with interval.

## Phase 7: validation (spec Section 13)

49. GitHub App + webhook flow (spec Section 7.1: install, label trigger, HMAC, dedupe) + gVisor; run on your own and opt-in repos.
50. Production-readiness checklist (spec Section 13.1): separate image builder, approver allow-list + audit log, backups, alerts, online metrics, cost per merged PR.
51. README rewrite with generated tables; smoke and durability suites reproducible in < 15 minutes.
52. 2–3 blog posts with data; demo video; upstream issues or PRs for bugs found in dependencies.
53. Optional: L7 (self-hosted Temporal vs Temporal Cloud).

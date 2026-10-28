# PLAN: MigrateBot, a Dependency-Migration Agent

> Brainstorming doc for the **Nebius x NVIDIA hackathon**. This is a separate project from Mergewright (the CSE 510 project described in `docs/proposal.md`), though the two share ideas: issue/target in, evidence-backed PR out, deterministic test oracle.
>
> Status: rough draft. Edit freely.

## 1. One-liner

You give it a GitHub repo and a target upgrade (for example, Pydantic v1 → v2). The agent returns a fork branch and a PR in which all tests that passed before the upgrade still pass after it. The PR shows every step: the sources it read, each patch it tried, and the test runs from every iteration.

## 2. Hard constraints (verify on Devpost)

- **Platform:** every project has to run on Nebius Token Factory or Nebius AI Cloud and use at least one NVIDIA open source model. Use a Nemotron model through Token Factory as the coding LLM. Not sure which Nemotron variants are in the catalog right now; check before picking one. (source: lucaberton)
- **Track fit:** the Coding track is defined as "Coding agents and developer tools that write, run, and test code in Token Factory Sandboxes." (source: lucaberton)
- **Deadline:** submit by Oct 30, 2026. Confirm the exact time and timezone on Devpost. (source: lucaberton)
- **Biggest risk, act tonight:** Sandboxes is in Beta and you have to request access. If you don't have access yet, request it now and ask in the Nebius Discord. (source: nebius)

## 3. Scope

- **In scope:** Python repos only, one migration done well (Pydantic v1 → v2), pytest-based test suites.
- **Stretch goal:** a second migration (SQLAlchemy 1.4 → 2.0) using the same pipeline, to show the agent is general and not a Pydantic special case.
- **Out of scope:** monorepos, repos that need external services (databases, Docker-in-Docker), non-Python ecosystems, and opening PRs against upstream. Open PRs on your own fork only, so you don't spam maintainers.

## 4. Inputs and outputs

**Input:** `repo_url`, `commit_sha`, `package`, `from_version`, `to_version`, plus an optional `test_command` override.

**Output:**

- A branch on your fork with the migrated code.
- A PR whose body includes the evidence listed in section 7.
- A `run.json` record: status, iterations, tokens, cost, wall time, Tavily calls, and the baseline vs. final test results.

## 5. Agent loop

1. **Prepare once.** In a sandbox, clone the repo, install it with the old version, and save the image. Sandboxes support saving files and dependencies once, then reusing them in independent operations, so you only pay the install cost once. (source: nebius)
2. **Baseline.** Run the tests on the old version and record the set of passing tests (P₀). Tests that already fail are excluded from the goal. If P₀ is empty or flaky, reject the repo.
3. **Upgrade.** Bump the dependency, reinstall, run the tests, and collect the failures and tracebacks. Save this as the "upgraded" image.
4. **Research with Tavily.**
   - Search for and extract the official migration guide and changelog once, then cache them as a condensed "migration notes" file.
   - For each distinct error signature, run a targeted search (for example, `"validator" deprecated pydantic v2 field_validator`).
   - Keep the source URLs; they go into the PR.
5. **Codemod first pass (recommended).** Run `bump-pydantic` before the LLM touches anything. It's cheap, handles the mechanical renames, and lets the LLM spend its effort on what's left. It also gives an honest baseline to compare against in section 8.
6. **Patch.** Nemotron receives the failing test output, the relevant source files, and the migration notes. It returns search/replace edits. Use search/replace rather than full-file rewrites because they're more reliable and produce smaller diffs.
7. **Verify with branching.** Sandboxes let you test two patches from the same starting image and continue from the passing version. Generate 2–3 candidate patches per iteration, run each on its own branch, and keep the one that passes the most tests in P₀. (source: nebius)
8. **Iterate** until every test in P₀ passes, or until you hit the budget (for example, 8 iterations or 30 minutes). If you stop on budget, report a partial result.
9. **Final check and PR.** Run the full suite one last time on a clean image, then push the branch and open the PR.

## 6. Definition of "success" (anti-cheating rules)

This is where judges will poke holes, so enforce these rules in code:

- Every test in P₀ passes after the migration.
- The installed version really is the target. Assert `pydantic.VERSION` starts with `2`.
- Test files are read-only to the agent. Deleting a test, skipping it, or adding `xfail` counts as failure.
- A run that relies on the `pydantic.v1` compatibility shim counts as **shim-only**, not success. Otherwise the trivial "fix" of importing from `pydantic.v1` would inflate the numbers.

## 7. PR evidence contents

The PR body should include:

- the target version and the commit SHA the migration started from;
- a before/after test table (baseline, after upgrade, final);
- the diff, split into codemod changes and LLM changes;
- an iteration log with the error → patch → result for each round;
- the Tavily sources used, each tied to the change it informed;
- sandbox image and operation IDs, so anyone can reproduce the run;
- cost and time.

## 8. Evaluation (the headline number)

**Repo selection** (15 repos is a good target). Choose repos that pinned `pydantic<2`, have a test suite that runs in under 5 minutes, and need no external services.

**Recommended twist:** choose repos that later migrated upstream for real. Check out the parent commit of their human-written v2 migration. That gives a human reference diff for every repo, which is far more credible than hand-picked repos.

| Metric | Meaning |
|---|---|
| Full success rate | Share of repos where every test in P₀ passes without the shim |
| Partial recovery | Average share of P₀ restored |
| Comparison to `bump-pydantic` alone | Shows the value the agent adds |
| Iterations, tokens, cost, wall time per repo | Shows efficiency |
| Ablation without Tavily (if time allows) | Strengthens the case for the Tavily prize |

## 9. Suggested 2.5-day plan

| When | Goal |
|---|---|
| Tue night | Sandbox access, a hello-world operation, a Nemotron API call, a Tavily call. Shortlist about 25 candidate repos. |
| Wed | Steps 1–3 and 6–8 working end to end on one repo. Screen the candidates down to 15. |
| Thu AM | Tavily research, codemod pass, branching, anti-cheat checks, PR generation. |
| Thu PM | Batch evaluation across all repos, fixing failures as they show up. |
| Fri | Results table, a 3-minute demo video (one live run plus the eval table), README, submission. |

**Cut order if behind:** ablation, then the second migration, then branching (fall back to a single patch per iteration). Never cut the anti-cheat checks or the evaluation table.

## 10. Key risks

| Risk | Mitigation |
|---|---|
| No sandbox access or beta limits | Request access now. Beta concurrency is limited to 50 simultaneously running operations, which is plenty, but avoid huge parallel fan-out. |
| Network inside sandboxes | Unsure what network access sandboxes allow by default. Verify that `pip install` works on day 1. |
| Nemotron weaker than frontier models at editing code | Codemod-first pass, small search/replace edits, and multiple candidates per iteration all reduce how much the model has to get right on its own. |
| Flaky tests | Run the baseline twice and drop tests whose results differ. |

## 11. Review notes and open questions

Added during review on 2026-10-28. Resolve or delete as you go.

1. **Schedule is already behind.** Today is Wed Oct 28 and the deadline is Fri Oct 30, so the "Tue night" items are overdue. Fold them into today, and treat 15 repos as a stretch; 8–10 well-chosen repos with clean results beats 15 shaky ones.
2. **Read-only tests vs. real migrations.** Upstream v2 migrations often edit the tests too (tests call `.dict()`, `parse_obj`, `Config` classes, and so on). With tests read-only, some repos may be unfixable by design. Options: (a) screen out repos whose upstream migration changed test assertions, or (b) allow non-assertion test edits but report them separately. Recommend (a): simpler and harder to attack.
3. **Deprecated v1 APIs still work in v2.** Many v1 methods (`.dict()`, `.json()`, `parse_obj`) still run in v2 and only emit `PydanticDeprecatedSince20` warnings, so "P₀ passes" can be a weaker bar than it looks. Consider reporting remaining deprecation-warning count as a secondary metric, or running tests with those warnings as errors for a "clean migration" tier.
4. **Define "shim-only" precisely.** For example: any `pydantic.v1` import in non-test source after the run (grep plus an import hook at test time). Write it as a check, not a judgment call.
5. **Pick repos where `bump-pydantic` alone fails.** If the codemod already fixes most repos, the agent's added value looks small. Run `bump-pydantic` during screening and favor repos where it leaves failures.
6. **Prompt injection surface.** Tavily results and repo files are untrusted input to the LLM. Keep the agent's only write path to search/replace edits on non-test files, and don't let fetched text pick commands to run.
7. **Still to verify:** which Nemotron variants Token Factory serves (and their context length, which limits how many source files fit per prompt), sandbox network policy, sandbox snapshot/branch API shape, exact Devpost deadline and timezone, and Tavily prize criteria.

# Plan: `Sandbox` protocol + Docker backend (agenda step 7)

Spec: Section 4.5 (`Sandbox` contract), Section 7 ("Sandbox (Docker)"), Section 8.

## Requirements

`Sandbox` protocol (spec Section 4.5): `reset(base_sha, patch)`, `exec(argv, timeout_s) ->
ExecResult` (exit code, output tail, timed_out), `diff()` (against `base_sha`), `close()`
(idempotent). Docker backend guards, each with its own test:

| Guard | How |
|---|---|
| No network | `--network none` |
| Non-root, no privileges | `--user <uid>:<gid>` (never 0), `--cap-drop ALL`, `no-new-privileges` |
| CPU / memory / PID limits | `--cpus`, `--memory-reservation` (floor), `--memory` = `--memory-swap` (ceiling), `--pids-limit` |
| Only the workspace mounted | one bind mount: the host workspace at the mount path |
| Empty env except a planted canary | no host env passed; env is the image's plus `HOME` and the canary (`GITHUB_TOKEN`, and `~/.git-credentials`) |
| Canary never leaves | `exec` output is redacted and flagged `canary_leaked` |
| Per-command timeout | `timeout` inside the container kills the process; a host-side backstop kills the container |
| Bounded output | only the last N bytes are kept (`truncated` flag) |
| Orphans | `mergewright.run_id` label; `sweep_orphans` removes old sandboxes of dead runs |

`reset` runs git inside the container (hooks off), so repo code never runs on the host.

## Non-goals (later steps)

- `run_tests` and file tools wired to the sandbox (step 8), including redacting the canary in
  file-tool output (a command can copy it into the workspace).
- Populating the workspace from a SWE-rebench image's `/testbed` (step 8).
- Warm pool and reaper (step 37), gVisor (Phase 7), the tree-hash check at the start of each
  activity (Phase 3; `diff()` is its input).

## Tests

- Unit (always run): canary, bounded output reader, `docker run` arguments, config
  validation, orphan sweeper (fake docker).
- Docker (`-m docker`; skipped without Docker or the test image): one per guard, plus
  `reset`/`diff` and `close`. Test image: `tests/sandbox_image/Dockerfile` (pinned
  `python:3.12-slim-bookworm` + git), built once with
  `docker build -t mergewright-sandbox-test:1 tests/sandbox_image`.

## Progress

- [x] Tests written and failing
- [x] Implementation: `src/mergewright/sandbox/` (`base`, `canary`, `process`, `docker`)
- [x] ruff; `pytest` 102 passed (26 sandbox unit, 13 Docker guard tests on Docker Desktop
  29.8.1, arm64, cgroup v2, 4.9 s); no sandbox left behind

Notes from the Docker run:

- `--network none` still lists down tunnel devices (`gre0`, `tunl0`, ...) on Docker Desktop;
  the guard test checks for zero routes instead.
- `timeout` exits 124, or 137 when it has to SIGKILL; an OOM kill is also 137. `exec` tells
  them apart by elapsed time.
- Not yet run on a Linux host with a different uid mapping (bind-mount ownership, git
  `safe.directory`); the first x86 VM run will show it.

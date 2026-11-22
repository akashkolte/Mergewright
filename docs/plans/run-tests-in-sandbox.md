# Plan: `run_tests` in the sandbox (agenda step 8)

Spec: Section 3 ("File tools vs execution"), Section 4.5, Section 7, Section 8.

## Requirements

- Every agent run uses a Docker sandbox; `mergewright run` fails with a clear message when
  Docker is missing or not running. No host fallback.
- File tools (`read_file`, `search_code`, `edit_file`) keep working on the host workspace,
  which is bind-mounted into the sandbox. Their output is canary-redacted, and a leak is logged.
- The Tester's `run_tests` runs the environment's test command inside the sandbox, through
  `Sandbox.exec`. Same `TestReport` as before, plus `canary_leaked`.
- Two environments, behind one small interface (`Environment`: prepare the workspace, start
  the sandbox, produce the diff):
  - `LocalRepo`: a host repo (e.g. `examples/buggy_calc`) copied into the workspace, run in
    the default image `mergewright-python:1`, diff against the original folder (as today).
  - `ImageRepo`: a task image (SWE-rebench); the workspace is copied out of the image's
    `/testbed` (with `.git`) and mounted back at `/testbed`, so the image's installed
    environment (editable install, conda env) applies. The sandbox is reset to `base_sha`
    at start; the diff comes from `Sandbox.diff()`.
- Default image: `docker/python/Dockerfile` (pinned `python:3.12-slim-bookworm` + git +
  `pytest==9.2.0`), tag `mergewright-python:1`. Replaces the step 7 test image.

## Decisions

- `run_tests` leaves the Coder's tool registry: only the Tester calls it. A Coder-facing
  `run_tests(test_ids)` returns in Phase 2 with a tool context that carries the sandbox.
- `canary` joins the `Sandbox` protocol: spec Section 7 requires one in every sandbox.
- Unit tests use `tests/fakes.py:LocalSandbox`, which runs commands as a host subprocess.
  It exists only in tests (trusted fixture repos), never in `src/`.
- Bytecode and pytest cache writes are off in the test command, so they never enter a diff.

## Non-goals

- Task files and the oracle (steps 9, 10); the CLI keeps running local repos only.
- Protected-path and tamper checks beyond today's `edit_file` guard (Phase 2).
- A remote workspace for managed sandboxes (Modal); file access stays behind `Workspace`.

## Tests

- Unit (no Docker): `run_tests` parsing, timeout and canary flag via a fake sandbox; file-tool
  redaction; graph and runner with `LocalSandbox`; CLI error when Docker is missing.
- Docker: `LocalRepo` replay run of `buggy_calc` to `DONE`; `ImageRepo` on `spectree-64`
  (workspace from the image, tests run in the task image, diff of an edit). The second skips
  without that image.

## Progress

- [x] Tests written and failing
- [x] Implementation: `environments.py` (new), `tools.run_tests` + `gate`, `graph`, `runner`,
  CLI `--image`, `copy_from_image`, `canary` on the `Sandbox` protocol, `RunLog.redact`
- [x] ruff; `pytest` 125 passed (108 unit + 17 Docker, Docker Desktop on arm64);
  `pytest spikes` 37 passed; `mergewright run --llm replay` reaches `DONE` in 1.9 s

Findings:

- SWE-rebench images are built only for each task's own tests. In `spectree-64`, 6 of the
  repo's test files fail to import (no `flask` etc.) at base. Hence `--continue-on-collection-
  errors`, the test baseline at the start of each run, and the gate rule: files that error at
  base are ignored, tests that fail at base are not (so `buggy_calc`'s visible failing test
  still has to pass).
- `docker create` on an amd64-only image prints a platform warning into the merged output, so
  temporary containers are named up front instead of parsing the ID.
- Defense in depth for the canary: tool output is redacted in the graph, and `RunLog` redacts
  every string field of every event once the sandbox exists; a canary in the diff fails the
  run as `FAILED_POLICY` and withholds the diff.
- The parser is still the simple FAILED/ERROR line splitter. Step 10 (oracle) brings the
  bracket-aware per-test parser; `run_tests` must then use the same one.

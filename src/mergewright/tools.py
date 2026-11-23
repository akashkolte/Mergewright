"""The agent's tools and the registry that validates and runs them.

The Coder's file tools work on the host workspace, which is bind-mounted into the sandbox.
Paths from the model are untrusted: they are resolved and must stay inside the workspace. Tool
failures are returned as results, never raised to the graph, so a bad tool call cannot crash a
run. run_tests runs the test command inside the sandbox, never on the host: the Tester runs the
whole suite as the gate, and the Coder runs chosen existing tests while it works.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from mergewright.sandbox import Sandbox

MAX_READ_BYTES = 200_000
MAX_READ_LINES = 400
MAX_SEARCH_HITS = 50
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
DENIED_EDIT_DIRS = {".git", ".github", "tests"}
# conftest.py and the files pytest reads its config from: an edit could skip the failing tests.
DENIED_EDIT_FILES = {"conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"}
RESULT_WORDS = ("PASSED", "FAILED", "ERROR", "XFAIL", "XPASS")  # pytest short summary outcomes


class ToolError(Exception):
    """A tool could not do what was asked. The message goes back to the model."""


class Workspace:
    """A directory the tools may read and change. Nothing outside it is reachable."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, path: str) -> Path:
        if not path or Path(path).is_absolute():
            raise ToolError(f"path must be relative to the repository root: {path!r}")
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError(f"path is outside the repository: {path!r}")
        return target

    def relative(self, target: Path) -> str:
        return target.relative_to(self.root).as_posix()


# --- Tools ---------------------------------------------------------------------------------


def read_file(ws: Workspace, path: str, start: int | None = None, end: int | None = None) -> str:
    """Return lines start..end (1-based, inclusive) of a file, with line numbers."""
    target = ws.resolve(path)
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    data = target.read_bytes()
    if b"\0" in data[:8000]:
        raise ToolError(f"binary file: {path}")
    lines = data[:MAX_READ_BYTES].decode("utf-8", errors="replace").splitlines()
    first = max(start or 1, 1)
    if first > len(lines):
        raise ToolError(f"start line {first} is past the end of {path} ({len(lines)} lines)")
    wanted_last = min(end or len(lines), len(lines))
    last = min(wanted_last, first + MAX_READ_LINES - 1)
    truncated = len(data) > MAX_READ_BYTES or last < wanted_last
    body = "\n".join(f"{n:>4}  {lines[n - 1]}" for n in range(first, last + 1))
    return body + ("\n[truncated]" if truncated else "")


def search_code(ws: Workspace, pattern: str, glob: str = "*.py") -> list[dict[str, Any]]:
    """Return up to MAX_SEARCH_HITS lines that match a regular expression."""
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        raise ToolError(f"invalid regular expression {pattern!r}: {exc}") from exc
    hits: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(ws.root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if not fnmatch.fnmatch(name, glob):
                continue
            file = Path(dirpath) / name
            if not file.resolve().is_relative_to(ws.root):
                continue  # a symlink that points outside the workspace
            text = file.read_text(encoding="utf-8", errors="replace")
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append({"path": ws.relative(file), "line": number, "text": line.strip()})
                    if len(hits) >= MAX_SEARCH_HITS:
                        return hits
    return hits


def edit_file(ws: Workspace, path: str, old: str, new: str) -> str:
    """Replace the one exact occurrence of old with new. Tests, test config and CI are denied."""
    target = ws.resolve(path)
    rel_parts = Path(ws.relative(target)).parts
    filename = rel_parts[-1]
    if (
        DENIED_EDIT_DIRS.intersection(rel_parts[:-1])
        or filename.startswith("test_")
        or filename in DENIED_EDIT_FILES
    ):
        raise ToolError(
            f"editing {path} is not allowed (tests, pytest config, .git and .github are protected)"
        )
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    if not old:
        raise ToolError("old must not be empty")
    source = target.read_text(encoding="utf-8")
    count = source.count(old)
    if count == 0:
        raise ToolError(f"old text not found in {path}; " + _whitespace_hint(source, old))
    if count > 1:
        raise ToolError(f"old text found {count} times in {path}; include more context")
    target.write_text(source.replace(old, new), encoding="utf-8")
    return f"edited {path}: replaced {old.count(chr(10)) + 1} line(s)"


def _whitespace_hint(source: str, old: str) -> str:
    """Show the file's exact text when old differs from it only in whitespace, else ask to copy.

    Models copy read_file output with the spaces after the line number added to the indentation
    (Phase 1 pilot: 12 of 14 edits failed so). The hint gives the exact text to copy; the edit
    itself stays exact, so no whitespace is ever guessed.
    """
    wanted = [line.strip() for line in old.strip("\n").splitlines()]
    lines = source.splitlines()
    if not any(wanted):
        return "read the file and copy it exactly"
    starts = [
        i
        for i in range(len(lines) - len(wanted) + 1)
        if [line.strip() for line in lines[i : i + len(wanted)]] == wanted
    ]
    if len(starts) != 1:
        return "read the file and copy it exactly"
    first, last = starts[0] + 1, starts[0] + len(wanted)
    exact = "\n".join(lines[first - 1 : last])
    return (
        f"lines {first}-{last} match it except for whitespace. Their exact text (no line "
        f"numbers; read_file's line numbers and the two spaces after them are not part of the "
        f"file) is between the markers:\n<<<\n{exact}\n>>>"
    )


@dataclass
class TestReport:
    __test__ = False  # not a pytest test class

    passed: bool  # pytest exited 0
    num_passed: int = 0
    num_failed: int = 0
    num_errors: int = 0
    failures: list[str] = field(default_factory=list)  # FAILED test ids
    errors: list[str] = field(default_factory=list)  # ERROR ids: setup or collection errors
    results: dict[str, str] = field(default_factory=dict)  # test id -> outcome (see below)
    timed_out: bool = False
    exit_code: int = 0
    canary_leaked: bool = False
    log_tail: str = ""


# --continue-on-collection-errors: a test file that cannot be imported (a missing optional
# dependency in a task image, say) must not stop the rest of the suite from running.
DEFAULT_TEST_COMMAND = (
    "python -m pytest -q -rfE -p no:cacheprovider --continue-on-collection-errors"
)


def run_tests(sandbox: Sandbox, command: str, timeout_s: float = 60.0) -> TestReport:
    """Run the test command in the sandbox and summarise pytest's output.

    Bytecode and the pytest cache are off, so test runs never add files to the diff.
    """
    result = sandbox.exec(
        ["bash", "-c", f"export PYTHONDONTWRITEBYTECODE=1; {command}"], timeout_s=timeout_s
    )
    if result.timed_out:
        return TestReport(
            passed=False,
            timed_out=True,
            exit_code=result.exit_code,
            canary_leaked=result.canary_leaked,
            log_tail=f"timed out after {timeout_s}s",
        )
    output = result.output

    def count(word: str) -> int:
        match = re.search(rf"(\d+) {word}", output)
        return int(match.group(1)) if match else 0

    results = parse_pytest_results(output)
    return TestReport(
        passed=result.exit_code == 0,
        num_passed=count("passed"),
        num_failed=count("failed"),
        num_errors=count("error"),
        failures=[test for test, outcome in results.items() if outcome == "FAILED"],
        errors=[test for test, outcome in results.items() if outcome == "ERROR"],
        results=results,
        exit_code=result.exit_code,
        canary_leaked=result.canary_leaked,
        # With -rA the summary ends in one PASSED line per test, which says nothing to a reader.
        log_tail="\n".join(
            [line for line in output.strip().splitlines() if not line.startswith("PASSED ")][-30:]
        ),
    )


def parse_pytest_results(output: str) -> dict[str, str]:
    """Test id -> outcome (PASSED, FAILED, ERROR, XFAIL, XPASS) from pytest's short summary.

    Only the outcomes pytest was asked to list appear: `-rA` lists all, `-rfE` only failures
    and errors. A later line for the same id wins (an ERROR at teardown after PASSED).
    """
    results: dict[str, str] = {}
    for line in output.splitlines():
        word, _, rest = line.partition(" ")
        # "ERROR    logger:file.py:10 message" is a captured log line (level padded), not a test.
        if word in RESULT_WORDS and rest and not rest[0].isspace():
            results[_strip_message(rest)] = word
    return results


def _strip_message(rest: str) -> str:
    """'tests/x.py::t[a - b] - AssertionError' -> 'tests/x.py::t[a - b]' (brackets aware)."""
    depth = 0
    for i, char in enumerate(rest):
        depth += char == "["
        depth -= char == "]"
        if depth == 0 and rest.startswith(" - ", i):
            return rest[:i]
    return rest.strip()


def confirm_failures(
    sandbox: Sandbox, command: str, problems: list[str], timeout_s: float
) -> tuple[list[str], list[str]]:
    """Re-run the tests the gate blamed, alone, once: (still failing, flaky).

    A test that passes on the re-run is flaky (base attempt 1: textual's slug tests fail a
    different subset on every run of unchanged code) and does not block DONE. Conservative: a
    test counts as passed only when pytest exits 0 or lists it as PASSED; a timeout, an id
    pytest cannot select, or any other doubt keeps it failing. Markers such as "(timed out)"
    are not tests and are kept. Trade-off: a regression that shows only in the full suite's
    order is missed; the hidden-test oracle still judges the fix.
    """
    ids = [problem for problem in problems if not problem.startswith("(")]
    if not ids:
        return problems, []
    rerun = run_tests(sandbox, f"{command} {shlex.join(ids)}", timeout_s=timeout_s)
    if rerun.timed_out:
        return problems, []
    passed = (
        set(ids) if rerun.exit_code == 0 else {t for t in ids if rerun.results.get(t) == "PASSED"}
    )
    return [p for p in problems if p not in passed], [t for t in ids if t in passed]


def gate(report: TestReport, known_failing: frozenset[str] = frozenset()) -> list[str]:
    """What still blocks DONE: a regression check; empty when the run is good enough.

    known_failing are the FAILED and ERROR ids seen at base (an environment gap in the task
    image, say): they are ignored, so the gate asks only that nothing that worked at base
    breaks. Whether the bug is fixed is the hidden-test oracle's verdict, not the gate's.
    """
    if report.timed_out:
        return ["(timed out)"]
    problems = [t for t in report.failures + report.errors if t not in known_failing]
    if problems:
        return problems
    if report.exit_code not in (0, 1):
        return [f"(pytest exited {report.exit_code})"]
    if report.num_passed == 0:
        return ["(no test passed)"]
    return []


# --- Registry ------------------------------------------------------------------------------


class ReadFileArgs(BaseModel):
    path: str = Field(description="File path relative to the repository root")
    start: int | None = Field(default=None, description="First line (1-based)")
    end: int | None = Field(default=None, description="Last line (inclusive)")


class SearchCodeArgs(BaseModel):
    pattern: str = Field(description="Python regular expression to search for")
    glob: str = Field(default="*.py", description="File name pattern, for example *.py")


class EditFileArgs(BaseModel):
    path: str = Field(description="File path relative to the repository root")
    old: str = Field(description="Exact text to replace; must occur exactly once")
    new: str = Field(description="Replacement text")


class RunTestsArgs(BaseModel):
    test_ids: list[str] = Field(
        default_factory=list,
        description="Test files or pytest node ids, for example tests/test_x.py::test_y; "
        "empty runs the whole suite",
    )


MAX_LISTED_TESTS = 20  # failing ids shown to the Coder; the log tail follows


def run_tests_for_coder(
    sandbox: Sandbox,
    command: str,
    raw_args: str,
    timeout_s: float,
    known_failing: frozenset[str] = frozenset(),
) -> ToolResult:
    """The Coder's in-loop test run: existing tests only, chosen by id. Not the gate.

    Ids are passed as pytest arguments, shell-quoted; one that starts with "-" (an option)
    is refused, so the model cannot change how pytest runs. The answer starts with the
    verdict the gate would give and keeps tests that already failed at base apart.
    """
    try:
        args = RunTestsArgs.model_validate(json.loads(raw_args or "{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        return ToolResult(False, f"invalid arguments for run_tests: {exc}")
    bad = [test for test in args.test_ids if not test.strip() or test.startswith("-")]
    if bad:
        return ToolResult(False, f"not a test file or node id: {bad}")
    base_command = command
    if args.test_ids:
        command = f"{command} {shlex.join(args.test_ids)}"
    report = run_tests(sandbox, command, timeout_s=timeout_s)
    if report.timed_out:
        return ToolResult(True, f"tests timed out after {timeout_s}s; run fewer tests")
    failing = report.failures + report.errors
    old = [t for t in failing if t in known_failing]
    problems, flaky = confirm_failures(
        sandbox, base_command, gate(report, known_failing), timeout_s
    )
    new = [t for t in failing if t in problems]
    counts = f"{report.num_passed} passed, {report.num_failed} failed, {report.num_errors} errors."
    if not problems:
        verdict = "All selected tests passed." if not failing else "No new failures."
        lines = [f"{verdict} {counts}", "If your fix is complete, call finish."]
    elif new:
        lines = [f"New failures: {len(new)}. {counts}", _listed("Failing now", new)]
    else:  # nothing listed failed, but pytest did not pass: no test ran, or a crash
        lines = [f"Not a pass: {problems[0]}. {counts}"]
    if old:
        lines.append(_listed("Already failing before your change (not caused by you)", old))
    if flaky:
        lines.append(_listed("Failed, then passed when re-run alone (flaky; ignored)", flaky))
    if problems:
        lines.append(f"Output (last lines):\n{report.log_tail}")
    return ToolResult(True, "\n".join(lines))


def _listed(title: str, tests: list[str]) -> str:
    shown = "\n".join(f"- {test}" for test in tests[:MAX_LISTED_TESTS])
    more = len(tests) - MAX_LISTED_TESTS
    return f"{title}:\n{shown}" + (f"\n... and {more} more" if more > 0 else "")


class FinishArgs(BaseModel):
    summary: str = Field(default="", description="One paragraph: the cause and your change")


def finish(ws: Workspace, summary: str = "") -> str:
    """End the Coder's turn. The graph routes to the tester; this only acknowledges the call."""
    return "finished; the test suite runs now"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[..., Any] | None  # None: needs the sandbox, so the graph runs it

    def schema(self) -> dict[str, Any]:
        """OpenAI-style function schema for the chat completions `tools` parameter."""
        params = self.args_model.model_json_schema()
        params.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": params},
        }


TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        ToolSpec("read_file", "Read lines of a file, with line numbers.", ReadFileArgs, read_file),
        ToolSpec(
            "search_code",
            "Search the repository for lines matching a regular expression.",
            SearchCodeArgs,
            search_code,
        ),
        ToolSpec(
            "edit_file",
            "Replace one exact occurrence of old text with new text in a file.",
            EditFileArgs,
            edit_file,
        ),
        ToolSpec(
            "run_tests",
            "Run existing tests (chosen files or node ids, or the whole suite) on your change "
            "and get the pass/fail counts, the failing ids and the output tail.",
            RunTestsArgs,
            None,  # runs in the sandbox: the graph calls run_tests_for_coder
        ),
        ToolSpec(
            "finish",
            "Call when the fix is complete. The repository's full test suite then runs as the "
            "final check; if tests fail, you get the failures.",
            FinishArgs,
            finish,
        ),
    )
}

CODER_TOOLS = ("read_file", "search_code", "edit_file", "run_tests", "finish")


@dataclass
class ToolResult:
    ok: bool
    output: str


def execute(name: str, raw_args: str, ws: Workspace, allowed: tuple[str, ...]) -> ToolResult:
    """Validate and run one tool call from the model. Every failure becomes an error result."""
    if name not in TOOLS:
        return ToolResult(False, f"unknown tool {name!r}; available: {', '.join(allowed)}")
    if name not in allowed:
        return ToolResult(False, f"tool {name!r} is not allowed for this agent")
    spec = TOOLS[name]
    if spec.run is None:
        return ToolResult(False, f"tool {name!r} runs in the sandbox, not on the workspace")
    try:
        args = spec.args_model.model_validate(json.loads(raw_args or "{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        return ToolResult(False, f"invalid arguments for {name}: {exc}")
    try:
        result = spec.run(ws, **args.model_dump())
    except ToolError as exc:
        return ToolResult(False, str(exc))
    if isinstance(result, list):
        return ToolResult(True, json.dumps(result))
    return ToolResult(True, str(result))

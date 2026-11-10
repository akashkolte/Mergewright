"""The agent's tools and the registry that validates and runs them.

Every tool works inside one workspace directory. Paths from the model are untrusted: they are
resolved and must stay inside the workspace. Tool failures are returned as results, never raised
to the graph, so a bad tool call cannot crash a run.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

MAX_READ_BYTES = 200_000
MAX_READ_LINES = 400
MAX_SEARCH_HITS = 50
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "node_modules"}
DENIED_EDIT_DIRS = {".git", ".github", "tests"}


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
    """Replace the one exact occurrence of old with new. Test files and CI config are denied."""
    target = ws.resolve(path)
    rel_parts = Path(ws.relative(target)).parts
    filename = rel_parts[-1]
    if (
        DENIED_EDIT_DIRS.intersection(rel_parts[:-1])
        or filename.startswith("test_")
        or filename == "conftest.py"
    ):
        raise ToolError(f"editing {path} is not allowed (tests, .git and .github are protected)")
    if not target.is_file():
        raise ToolError(f"file not found: {path}")
    if not old:
        raise ToolError("old must not be empty")
    source = target.read_text(encoding="utf-8")
    count = source.count(old)
    if count == 0:
        raise ToolError(f"old text not found in {path}; read the file and copy it exactly")
    if count > 1:
        raise ToolError(f"old text found {count} times in {path}; include more context")
    target.write_text(source.replace(old, new), encoding="utf-8")
    return f"edited {path}: replaced {old.count(chr(10)) + 1} line(s)"


@dataclass
class TestReport:
    __test__ = False  # not a pytest test class

    passed: bool
    num_passed: int = 0
    num_failed: int = 0
    num_errors: int = 0
    failures: list[str] = field(default_factory=list)
    timed_out: bool = False
    log_tail: str = ""


def _sandbox_env() -> dict[str, str]:
    """A minimal environment for the test subprocess: no API keys or other secrets."""
    keep = ("PATH", "SYSTEMROOT", "TMPDIR", "TEMP", "TMP", "LANG")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def run_tests(ws: Workspace, timeout_s: float = 60.0) -> TestReport:
    """Run the workspace's pytest suite in a subprocess and summarise the result."""
    command = [sys.executable, "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(
            command,
            cwd=ws.root,
            env=_sandbox_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return TestReport(passed=False, timed_out=True, log_tail=f"timed out after {timeout_s}s")
    output = proc.stdout + proc.stderr

    def count(word: str) -> int:
        match = re.search(rf"(\d+) {word}", output)
        return int(match.group(1)) if match else 0

    failures = [
        line.split(" - ")[0].split(" ", 1)[1]
        for line in output.splitlines()
        if line.startswith(("FAILED ", "ERROR "))
    ]
    return TestReport(
        passed=proc.returncode == 0,
        num_passed=count("passed"),
        num_failed=count("failed"),
        num_errors=count("error"),
        failures=failures,
        log_tail="\n".join(output.strip().splitlines()[-30:]),
    )


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
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    run: Callable[..., Any]

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
        ToolSpec("run_tests", "Run the repository's pytest suite.", RunTestsArgs, run_tests),
    )
}

CODER_TOOLS = ("read_file", "search_code", "edit_file")


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
    try:
        args = spec.args_model.model_validate(json.loads(raw_args or "{}"))
    except (json.JSONDecodeError, ValidationError) as exc:
        return ToolResult(False, f"invalid arguments for {name}: {exc}")
    try:
        result = spec.run(ws, **args.model_dump())
    except ToolError as exc:
        return ToolResult(False, str(exc))
    if isinstance(result, TestReport):
        return ToolResult(result.passed, json.dumps(asdict(result)))
    if isinstance(result, list):
        return ToolResult(True, json.dumps(result))
    return ToolResult(True, str(result))

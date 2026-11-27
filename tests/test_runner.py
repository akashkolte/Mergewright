import json
from pathlib import Path

import pytest

from mergewright.environments import LocalRepo
from mergewright.llm import LLMResponse, ScriptedLLM, ToolCall
from mergewright.runner import run_agent
from mergewright.sandbox import Canary
from tests.conftest import BUGGY_LINE, FIXED_LINES
from tests.fakes import LocalSandbox


def edit(old: str, new: str) -> LLMResponse:
    args = json.dumps({"path": "calc.py", "old": old, "new": new})
    return LLMResponse(tool_calls=[ToolCall(id="c1", name="edit_file", arguments=args)])


class Starter:
    """A start_sandbox stand-in that keeps the sandboxes it made."""

    def __init__(self, canary: Canary | None = None) -> None:
        self.canary = canary
        self.made: list[LocalSandbox] = []

    def __call__(self, **kwargs) -> LocalSandbox:
        sandbox = LocalSandbox(Path(kwargs["workspace"]), self.canary)
        self.made.append(sandbox)
        return sandbox


def events_of(result) -> list[dict]:
    return [json.loads(line) for line in (result.run_dir / "run.jsonl").read_text().splitlines()]


def test_symlink_in_the_repo_does_not_copy_outside_content_into_the_workspace(
    fixed_calc: Path, tmp_path: Path
):
    secret = tmp_path / "host_secret.txt"
    secret.write_text("HOST-SECRET-CANARY")
    (fixed_calc / "notes.txt").symlink_to(secret)
    llm = ScriptedLLM(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="read_file", arguments=json.dumps({"path": "notes.txt"}))
                ]
            ),
            LLMResponse(content="done"),
        ]
    )

    result = run_agent(
        LocalRepo(fixed_calc, start_sandbox=Starter()), "issue", llm, tmp_path / "runs", echo=None
    )

    assert (result.run_dir / "workspace" / "notes.txt").is_symlink()
    log = (result.run_dir / "run.jsonl").read_text()
    assert "HOST-SECRET-CANARY" not in log
    read = next(json.loads(line) for line in log.splitlines() if '"tool": "read_file"' in line)
    assert read["ok"] is False and "outside" in read["output"]


def test_run_records_a_test_baseline_and_ignores_files_that_error_at_base(
    buggy_calc: Path, tmp_path: Path
):
    (buggy_calc / "tests" / "test_needs_flask.py").write_text("import flask\n")
    llm = ScriptedLLM([edit(BUGGY_LINE, FIXED_LINES), LLMResponse(content="fixed")])

    result = run_agent(
        LocalRepo(buggy_calc, start_sandbox=Starter()), "issue", llm, tmp_path / "runs", echo=None
    )

    assert result.status == "DONE"
    baseline = next(e for e in events_of(result) if e["event"] == "test_baseline")
    assert baseline["errors"] == ["tests/test_needs_flask.py"]
    assert baseline["failures"] == ["tests/test_calc.py::test_median_even_length"]


def test_sandbox_is_closed_even_when_the_run_crashes(buggy_calc: Path, tmp_path: Path):
    class CrashingLLM:
        model = "crash"

        def complete(self, messages, tools):
            raise RuntimeError("unexpected bug")

    starter = Starter()

    with pytest.raises(RuntimeError, match="unexpected bug"):
        run_agent(LocalRepo(buggy_calc, start_sandbox=starter), "issue", CrashingLLM(),
                  tmp_path / "runs", echo=None)  # fmt: skip

    assert [s.closed for s in starter.made] == [True]


def test_a_diff_that_contains_the_canary_fails_the_run_and_is_withheld(
    buggy_calc: Path, tmp_path: Path
):
    canary = Canary.new()
    leak = f"    return {canary.value!r}\n"
    llm = ScriptedLLM([edit(BUGGY_LINE, leak), LLMResponse(content="done")] * 3)

    result = run_agent(
        LocalRepo(buggy_calc, start_sandbox=Starter(canary)), "issue", llm, tmp_path / "runs",
        echo=None,
    )  # fmt: skip

    assert result.status == "FAILED_POLICY" and result.diff == ""
    assert canary.value not in (result.run_dir / "run.jsonl").read_text()

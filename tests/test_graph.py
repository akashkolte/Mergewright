import json
from pathlib import Path

import pytest

from mergewright.config import Limits
from mergewright.graph import AgentDeps, build_graph, initial_state, recursion_limit
from mergewright.llm import LLMError, LLMResponse, ScriptedLLM, ToolCall
from mergewright.runlog import RunLog
from mergewright.sandbox import Canary
from mergewright.tools import Workspace
from tests.conftest import BUGGY_LINE, FIXED_LINES
from tests.fakes import LocalSandbox

WRONG_FIX = "    return ordered[len(ordered) // 2 - 1]\n"  # wrong for both odd and even lengths


def call(name: str, **args) -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=f"c-{name}", name=name, arguments=json.dumps(args))])


def done(text: str = "fixed") -> LLMResponse:
    return LLMResponse(content=text)


def edit(old: str, new: str) -> LLMResponse:
    return call("edit_file", path="calc.py", old=old, new=new)


def run(workspace: Path, llm, limits: Limits | None = None, clock=None, **deps_kwargs):
    limits = limits or Limits()
    log = RunLog(workspace.parent / "run.jsonl", echo=None)
    deps = AgentDeps(
        llm=llm,
        workspace=Workspace(workspace),
        log=log,
        sandbox=deps_kwargs.pop("sandbox", None) or LocalSandbox(workspace),
        limits=limits,
        **deps_kwargs,
    )
    if clock:
        deps.clock = clock
    final = build_graph(deps).invoke(
        initial_state("median is wrong for even-length input"),
        config={"recursion_limit": recursion_limit(limits)},
    )
    return final, log.events()


def test_happy_path_fixes_the_bug_and_reaches_done(buggy_calc: Path):
    llm = ScriptedLLM(
        [call("search_code", pattern="def median"), edit(BUGGY_LINE, FIXED_LINES), done()]
    )

    final, events = run(buggy_calc, llm)

    assert final["status"] == "DONE"
    assert final["test_attempts"] == 1
    assert FIXED_LINES in (buggy_calc / "calc.py").read_text()
    tools_called = [e["tool"] for e in events if e["event"] == "tool_call"]
    assert tools_called == ["search_code", "edit_file", "run_tests"]


def test_finish_tool_hands_over_to_the_tester(buggy_calc: Path):
    llm = ScriptedLLM([edit(BUGGY_LINE, FIXED_LINES), call("finish", summary="even median")])

    final, events = run(buggy_calc, llm)

    assert final["status"] == "DONE" and final["steps"] == 2
    tools_called = [e["tool"] for e in events if e["event"] == "tool_call"]
    assert tools_called == ["edit_file", "finish", "run_tests"]


def test_finish_after_other_calls_in_one_message_runs_them_first(buggy_calc: Path):
    both = LLMResponse(
        tool_calls=[
            ToolCall(
                id="c-1",
                name="edit_file",
                arguments=json.dumps({"path": "calc.py", "old": BUGGY_LINE, "new": FIXED_LINES}),
            ),
            ToolCall(id="c-2", name="finish", arguments="{}"),  # summary is optional
        ]
    )

    final, events = run(buggy_calc, ScriptedLLM([both]))

    assert final["status"] == "DONE" and final["steps"] == 1
    tools_called = [e["tool"] for e in events if e["event"] == "tool_call"]
    assert tools_called == ["edit_file", "finish", "run_tests"]


def test_failing_tests_loop_back_to_the_coder(buggy_calc: Path):
    llm = ScriptedLLM([edit(BUGGY_LINE, WRONG_FIX), done(), edit(WRONG_FIX, FIXED_LINES), done()])

    final, events = run(buggy_calc, llm)

    assert final["status"] == "DONE"
    assert final["test_attempts"] == 2
    feedback = [m for m in final["messages"] if m["role"] == "user"][-1]["content"]
    assert "test_median" in feedback  # the coder was told which tests failed


def test_gives_up_after_max_test_attempts(buggy_calc: Path):
    llm = ScriptedLLM([done(), done(), done()])

    final, _ = run(buggy_calc, llm, Limits(max_test_attempts=3))

    assert final["status"] == "FAILED_NO_FIX"
    assert final["test_attempts"] == 3


def test_step_cap_stops_a_model_that_never_finishes(buggy_calc: Path):
    llm = ScriptedLLM([call("read_file", path="calc.py")] * 10)

    final, _ = run(buggy_calc, llm, Limits(max_steps=4))

    assert final["status"] == "FAILED_BUDGET"
    assert final["steps"] == 4


def test_wall_clock_cap_stops_the_run(buggy_calc: Path):
    ticks = iter(range(0, 10_000, 100))  # every clock reading is 100 s later
    llm = ScriptedLLM([call("read_file", path="calc.py")] * 10)

    final, _ = run(buggy_calc, llm, Limits(wall_clock_s=250), clock=lambda: next(ticks))

    assert final["status"] == "FAILED_BUDGET"
    assert "wall clock" in final["reason"]


@pytest.mark.parametrize(
    "bad_call",
    [
        LLMResponse(tool_calls=[ToolCall(id="x", name="delete_repo", arguments="{}")]),
        LLMResponse(tool_calls=[ToolCall(id="x", name="read_file", arguments="{not json")]),
        call("edit_file", path="tests/test_calc.py", old="a", new="b"),
    ],
    ids=["unknown-tool", "malformed-args", "protected-path"],
)
def test_bad_tool_call_is_reported_to_the_model_and_the_run_continues(buggy_calc: Path, bad_call):
    llm = ScriptedLLM([bad_call, edit(BUGGY_LINE, FIXED_LINES), done()])

    final, events = run(buggy_calc, llm)

    assert final["status"] == "DONE"
    first_tool = next(e for e in events if e["event"] == "tool_call")
    assert first_tool["ok"] is False
    tool_message = next(m for m in final["messages"] if m["role"] == "tool")
    assert tool_message["content"] == first_tool["output"]


def test_model_failure_ends_the_run_cleanly(buggy_calc: Path):
    class BrokenLLM:
        model = "broken"

        def complete(self, messages, tools):
            raise LLMError("connection refused")

    final, events = run(buggy_calc, BrokenLLM())

    assert final["status"] == "FAILED_ERROR"
    assert events[-1]["event"] == "llm_error"


def test_coder_is_offered_only_its_allowed_tools(buggy_calc: Path):
    seen: list[list[str]] = []

    class SpyLLM(ScriptedLLM):
        def complete(self, messages, tools):
            seen.append([t["function"]["name"] for t in tools])
            return super().complete(messages, tools)

    run(buggy_calc, SpyLLM([edit(BUGGY_LINE, FIXED_LINES), done()]))

    assert seen[0] == ["read_file", "search_code", "edit_file", "finish"]


def test_tester_ignores_errors_known_at_base(buggy_calc: Path):
    (buggy_calc / "tests" / "test_needs_flask.py").write_text("import flask\n")
    llm = ScriptedLLM([edit(BUGGY_LINE, FIXED_LINES), done()])

    final, _ = run(buggy_calc, llm, known_errors=frozenset({"tests/test_needs_flask.py"}))

    assert final["status"] == "DONE"


def test_tester_still_requires_tests_that_fail_at_base(buggy_calc: Path):
    # The bug's visible test fails at base; listing it as known must not excuse it.
    llm = ScriptedLLM([done(), done(), done()])

    final, _ = run(
        buggy_calc, llm, known_errors=frozenset({"tests/test_calc.py::test_median_even_length"})
    )

    assert final["status"] == "FAILED_NO_FIX"


def test_tool_output_with_the_canary_is_redacted_and_logged(buggy_calc: Path):
    canary = Canary.new()
    (buggy_calc / "notes.txt").write_text(f"token={canary.value}\n")
    llm = ScriptedLLM([call("read_file", path="notes.txt"), edit(BUGGY_LINE, FIXED_LINES), done()])

    final, events = run(buggy_calc, llm, sandbox=LocalSandbox(buggy_calc, canary))

    tool_message = next(m for m in final["messages"] if m["role"] == "tool")
    assert canary.value not in tool_message["content"]
    assert Canary.REDACTED in tool_message["content"]
    read = next(e for e in events if e["event"] == "tool_call")
    assert read["canary_leaked"] is True and canary.value not in json.dumps(events)


def test_graph_has_the_expected_nodes_and_edges(tmp_path: Path):
    deps = AgentDeps(
        llm=ScriptedLLM([]),
        workspace=Workspace(tmp_path),
        log=RunLog(tmp_path / "l", None),
        sandbox=LocalSandbox(tmp_path),
    )
    graph = build_graph(deps).get_graph()

    assert set(graph.nodes) == {"__start__", "coder", "tools", "tester", "__end__"}
    edges = {(e.source, e.target) for e in graph.edges}
    assert edges == {
        ("__start__", "coder"),
        ("coder", "tools"),
        ("coder", "tester"),
        ("coder", "__end__"),
        ("tools", "coder"),
        ("tools", "tester"),
        ("tester", "coder"),
        ("tester", "__end__"),
    }

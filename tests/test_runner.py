import json
from pathlib import Path

from mergewright.llm import LLMResponse, ScriptedLLM, ToolCall
from mergewright.runner import run_agent


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

    result = run_agent(fixed_calc, "issue", llm, tmp_path / "runs", echo=None)

    assert (result.run_dir / "workspace" / "notes.txt").is_symlink()
    log = (result.run_dir / "run.jsonl").read_text()
    assert "HOST-SECRET-CANARY" not in log
    read = next(json.loads(line) for line in log.splitlines() if '"tool": "read_file"' in line)
    assert read["ok"] is False and "outside" in read["output"]

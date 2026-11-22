"""Environments end to end on real Docker. Skipped without Docker or the images.

docker build -t mergewright-python:1 docker/python
docker pull swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64   (optional, 1.3 GB)
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from mergewright.environments import DEFAULT_IMAGE, ImageRepo, LocalRepo, conda_test_command
from mergewright.llm import ScriptedLLM
from mergewright.runner import run_agent
from mergewright.tools import gate, run_tests
from tests.conftest import BUGGY_LINE, EXAMPLES_DIR

SPECTREE = "swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64"
SPECTREE_BASE = "a091fab020ac26548250c907bae0855273a98778"
SPECTREE_CMD = (
    "pytest --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning"
)


def _has_image(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    )


pytestmark = pytest.mark.docker


@pytest.mark.skipif(not _has_image(DEFAULT_IMAGE), reason=f"needs {DEFAULT_IMAGE}")
def test_local_repo_replay_run_fixes_buggy_calc_with_tests_in_a_container(tmp_path: Path):
    llm = ScriptedLLM.from_file(EXAMPLES_DIR / "inputs" / "buggy_calc_replay.json")
    issue = (EXAMPLES_DIR / "inputs" / "buggy_calc_issue.md").read_text()

    result = run_agent(LocalRepo(EXAMPLES_DIR / "buggy_calc"), issue, llm, tmp_path, echo=None)

    assert result.status == "DONE"
    assert "-    return ordered[len(ordered) // 2]" in result.diff
    assert BUGGY_LINE in (EXAMPLES_DIR / "buggy_calc" / "calc.py").read_text()  # original intact


@pytest.mark.skipif(not _has_image(SPECTREE), reason=f"needs {SPECTREE}")
def test_image_repo_runs_the_visible_tests_in_the_task_image(tmp_path: Path):
    env = ImageRepo(SPECTREE, base_sha=SPECTREE_BASE, test_command=conda_test_command(SPECTREE_CMD))
    workspace = tmp_path / "ws"
    env.prepare(workspace)
    sandbox = env.start(workspace, run_id="spectree")
    try:
        baseline = run_tests(sandbox, env.test_command, timeout_s=600)
        source = workspace / "spectree" / "utils.py"
        source.write_text(source.read_text() + "\n# touched by the test\n")
        diff = env.diff(workspace, sandbox)
    finally:
        sandbox.close()

    assert (workspace / ".git").is_dir()
    assert baseline.num_passed > 0 and baseline.failures == []
    assert baseline.errors  # this image lacks flask and other extras: known at base
    assert gate(baseline, frozenset(baseline.errors)) == []
    assert "spectree/utils.py" in diff and "+# touched by the test" in diff

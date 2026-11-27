"""ImageRepo end to end on a real SWE-rebench image. Skipped unless the image is present.

LocalRepo in Docker is covered by test_cli (buggy_calc) and test_eval_docker (smoke split).

docker pull swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64   (1.3 GB)
"""

from pathlib import Path

import pytest

from mergewright.environments import ImageRepo, conda_test_command
from mergewright.tools import gate, run_tests
from tests.fakes import SPECTREE, has_image

SPECTREE_BASE = "a091fab020ac26548250c907bae0855273a98778"
SPECTREE_CMD = (
    "pytest --no-header -rA --tb=line --color=no -p no:cacheprovider -W ignore::DeprecationWarning"
)


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not has_image(SPECTREE), reason=f"needs {SPECTREE}"),
]


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

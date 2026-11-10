import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES_DIR = REPO_ROOT / "examples"

# The bug in examples/buggy_calc/calc.py and a correct fix for it.
BUGGY_LINE = "    return ordered[len(ordered) // 2]\n"
FIXED_LINES = (
    "    middle = len(ordered) // 2\n"
    "    if len(ordered) % 2 == 0:\n"
    "        return (ordered[middle - 1] + ordered[middle]) / 2\n"
    "    return ordered[middle]\n"
)


@pytest.fixture
def buggy_calc(tmp_path: Path) -> Path:
    """A fresh copy of examples/buggy_calc, so a test can change it freely."""
    workspace = tmp_path / "buggy_calc"
    shutil.copytree(EXAMPLES_DIR / "buggy_calc", workspace)
    return workspace


@pytest.fixture
def fixed_calc(buggy_calc: Path) -> Path:
    """A copy of examples/buggy_calc with the reference fix applied."""
    calc = buggy_calc / "calc.py"
    calc.write_text(calc.read_text().replace(BUGGY_LINE, FIXED_LINES))
    return buggy_calc

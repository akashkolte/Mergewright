"""`mergewright report` on small synthetic result folders: no Docker, no model."""

import json
from pathlib import Path

import pytest

from mergewright.__main__ import EXIT_CONFIG, EXIT_DONE, main
from mergewright.report import ReportError, load_eval, render

# failure category -> (status, resolved, infra_error), as evaluation.run_trial writes them
OUTCOMES = {
    "resolved": ("DONE", True, None),
    "overclaim": ("DONE", False, None),
    "no_fix": ("FAILED_NO_FIX", False, None),
    "infra": ("ERROR", False, "SandboxError: daemon gone"),
}


def write_eval(
    root: Path,
    eval_id: str,
    trials: dict[str, list[str]],
    split: str = "dev",
    model: str = "m-1",
    cost: float | None = 0.01,
    walls: list[float] | None = None,
    fingerprints: dict[str, str] | None = None,
    expected_k: int | None = None,
    finished: bool = True,
) -> Path:
    """An eval folder as evaluation.run_eval writes it; trials: task -> categories by k."""
    out = root / eval_id
    k = expected_k or max(len(categories) for categories in trials.values())
    wall = iter(walls or [1.0] * sum(len(c) for c in trials.values()))
    for task, categories in trials.items():
        for i, category in enumerate(categories, start=1):
            status, resolved, infra_error = OUTCOMES[category]
            record = {
                "task": task, "system": "base", "k": i, "status": status, "resolved": resolved,
                "overclaim": category == "overclaim", "infra_error": infra_error,
                "failure_category": category, "tokens_in": 1000, "tokens_out": 100,
                "cost_usd": cost, "wall_s": next(wall), "steps": 4,
            }  # fmt: skip
            trial = out / "runs" / task / "base" / f"{i}.json"
            trial.parent.mkdir(parents=True, exist_ok=True)
            trial.write_text(json.dumps(record))
    manifest = {
        "eval_id": eval_id, "split": split, "system": "base", "k": k,
        "model": {"requested": model, "served": []}, "git": {"sha": "a" * 40, "dirty": False},
        "prompt_hash": "sha256:" + "b" * 64, "started_at": "2026-11-23T09:00:00Z",
        "finished_at": "2026-11-23T10:00:00Z" if finished else None,
        "tasks": {t: {"fingerprint": (fingerprints or {}).get(t, "sha256:f")} for t in trials},
    }  # fmt: skip
    (out / "manifest.json").write_text(json.dumps(manifest))
    return out


def report(*paths: Path, seed: int = 0) -> str:
    return render([load_eval(path) for path in paths], seed=seed, resamples=500)


def row(text: str, first_cell: str, section: str = "") -> list[str]:
    """The cells of the table row that starts with first_cell, after the heading section."""
    lines = text[text.index(section) :].splitlines()
    line = next(line for line in lines if line.startswith(f"| {first_cell} |"))
    return [cell.strip() for cell in line.strip("|").split("|")]


def test_one_eval_gives_its_rates_costs_and_failure_counts(tmp_path: Path):
    trials = {
        "t1": ["resolved", "resolved"],
        "t2": ["resolved", "overclaim"],
        "t3": ["no_fix", "infra"],
        "t4": ["overclaim", "overclaim"],
    }
    walls = [1.0] * 7 + [11.0]
    text = report(write_eval(tmp_path, "e1", trials, walls=walls))

    cells = row(text, "base")
    assert cells[:6] == ["base", "e1", "dev", "m-1", "4", "2"]
    assert cells[6].startswith("37.5% [")  # task scores 1, 0.5, 0, 0
    assert cells[7:11] == ["25.0%", "50.0%", "50.0%", "12.5%"]  # pass^k, pass@k, overclaim 3/6
    assert cells[11:] == ["2,933", "$0.0267", "1.0 s / 7.5 s", "4.0"]  # 8 x 1100 tokens / 3
    assert row(text, "base (e1)") == ["base (e1)", "3", "3", "1", "0", "0", "1", "0"]
    assert "## Comparisons" not in text and "**Warnings**" not in text
    assert "| e1 | base | m-1 | aaaaaaaaaaaa | bbbbbbbbbbbb |" in text  # provenance


def test_the_report_is_reproducible_for_a_seed(tmp_path: Path):
    path = write_eval(tmp_path, "e1", {f"t{i}": ["resolved", "no_fix"][i % 2 :] for i in range(9)})

    assert report(path, seed=3) == report(path, seed=3)
    assert "seed 3" in report(path, seed=3) and "500 resamples" in report(path)


def test_an_unknown_cost_is_not_a_number(tmp_path: Path):
    text = report(write_eval(tmp_path, "e1", {"t1": ["resolved"]}, cost=None))

    assert row(text, "base")[12] == "n/a"


def test_identical_systems_are_not_distinguishable(tmp_path: Path):
    trials = {"t1": ["resolved"], "t2": ["no_fix"], "t3": ["resolved"]}
    a, b = write_eval(tmp_path, "a", trials), write_eval(tmp_path, "b", trials)

    cells = row(report(a, b), "base (a)", "## Comparisons")

    assert cells[2:] == ["3", "+0.0 pts [+0.0, +0.0]", "not distinguishable", "0 / 0, p = 1"]


def test_a_paired_comparison_uses_only_shared_tasks_and_mcnemar_at_k_1(tmp_path: Path):
    a = write_eval(tmp_path, "a", {f"t{i}": ["no_fix"] for i in range(12)})
    b = write_eval(tmp_path, "b", {f"t{i}": ["resolved"] for i in range(2, 12)})

    cells = row(report(a, b), "base (a)", "## Comparisons")

    assert cells[2:5] == ["10", "+100.0 pts [+100.0, +100.0]", "B higher"]
    assert cells[5] == f"0 / 10, p = {2 / 2**10:.3g}"


def test_mcnemar_needs_single_trial_pairs(tmp_path: Path):
    a = write_eval(tmp_path, "a", {"t1": ["resolved", "no_fix"], "t2": ["no_fix", "no_fix"]})
    b = write_eval(tmp_path, "b", {"t1": ["resolved", "resolved"], "t2": ["no_fix", "no_fix"]})

    cells = row(report(a, b), "base (a)", "## Comparisons")

    assert cells[3].startswith("+25.0 pts") and cells[5] == "n/a (k > 1)"


def test_warnings_name_model_validation_and_missing_trial_differences(tmp_path: Path):
    a = write_eval(tmp_path, "a", {"t1": ["resolved"], "t2": ["resolved"]})
    b = write_eval(
        tmp_path, "b", {"t1": ["resolved"], "t2": ["no_fix"]}, model="m-2",
        fingerprints={"t2": "sha256:new"}, expected_k=2, finished=False,
    )  # fmt: skip

    text = report(a, b)

    assert "- b is incomplete: 2 of 4 trials." in text
    assert "- The evals use different models (a: m-1, b: m-2)." in text
    assert "- a and b relied on different validations of: t2." in text


def test_evals_on_different_splits_or_given_twice_are_refused(tmp_path: Path):
    a = write_eval(tmp_path, "a", {"t1": ["resolved"]})
    b = write_eval(tmp_path, "b", {"t1": ["resolved"]}, split="smoke")

    with pytest.raises(ReportError, match="different splits"):
        report(a, b)
    with pytest.raises(ReportError, match="twice"):
        report(a, a)
    with pytest.raises(ReportError, match="not a readable eval result folder"):
        load_eval(tmp_path / "missing")


def test_the_cli_writes_the_report_or_fails_clearly(tmp_path: Path, capsys):
    path = write_eval(tmp_path, "e1", {"t1": ["resolved"]})
    out = tmp_path / "report.md"

    assert main(["report", str(path), "--out", str(out), "--seed", "1", "--resamples", "50"]) == 0
    assert "seed 1" in out.read_text() and f"report: {out}" in capsys.readouterr().out
    assert main(["report", str(tmp_path / "nope")]) == EXIT_CONFIG
    assert "not a readable eval result folder" in capsys.readouterr().err
    assert main(["report", str(path)]) == EXIT_DONE
    assert capsys.readouterr().out.startswith("# Eval report\n")

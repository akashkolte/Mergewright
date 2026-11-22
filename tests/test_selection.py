"""Dev selection: the rule's checks and the fixed order. Rows are edited copies of the fixture."""

import json

import pytest

from mergewright.selection import (
    HELD_OUT_PER_REPO,
    dev_candidates,
    dev_rule_failures,
    held_out_candidates,
    held_out_rule_failures,
    rank,
)
from tests.test_tasks import ROW_FILE


def candidate(**changes) -> dict:
    """The spectree fixture row, edited to pass every check, then changed as given."""
    row = json.loads(ROW_FILE.read_text())
    row["created_at"] = "2022-06-01 12:00:00"
    row["meta"]["llm_score"] = {"difficulty_score": 1, "issue_text_score": 0, "test_score": 1}
    row.update(changes)
    return row


def test_a_row_that_meets_the_rule_has_no_failures():
    assert dev_rule_failures(candidate()) == []


def test_the_fixture_itself_fails_on_its_date_and_issue_text_score():
    row = json.loads(ROW_FILE.read_text())
    assert dev_rule_failures(row) == [
        "created_at outside 2021-01-01..2024-01-01",
        "an LLM score above 1",
    ]


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"created_at": "2024-01-01 00:00:00"}, "created_at outside"),
        ({"docker_image": None}, "no published image"),
        ({"FAIL_TO_PASS": []}, "not 1-5 F2P tests"),
        ({"FAIL_TO_PASS": [f"t{i}" for i in range(6)]}, "not 1-5 F2P tests"),
        ({"PASS_TO_PASS": json.dumps([f"t{i}" for i in range(151)])}, "more than 150 P2P"),
        ({"PASS_TO_FAIL": ["t"]}, "F2F or P2F tests"),
    ],
)
def test_each_check_rejects_its_case(changes, reason):
    assert any(f.startswith(reason) for f in dev_rule_failures(candidate(**changes)))


def test_a_missing_score_or_file_count_fails():
    row = candidate()
    del row["meta"]["llm_score"]["test_score"]
    del row["meta"]["num_modified_files"]
    assert dev_rule_failures(row) == ["more than 3 modified files", "an LLM score above 1"]


def test_candidates_come_in_hash_order_with_at_most_per_repo_each():
    rows = [
        candidate(instance_id=i, repo=repo)
        for i, repo in [("a__x-1", "a/x"), ("a__x-2", "a/x"), ("b__y-1", "b/y")]
    ]
    rows.append(candidate(instance_id="c__z-1", repo="c/z", docker_image=None))

    one = dev_candidates(rows)
    two = dev_candidates(rows, per_repo=2)

    assert len(one) == 2 and {i.split("-")[0] for i in one} == {"a__x", "b__y"}
    assert two == sorted(["a__x-1", "a__x-2", "b__y-1"], key=rank)


def leaderboard_row(**changes) -> dict:
    """A leaderboard-shaped row: no LLM scores, no F2F/P2F, install_config JSON-encoded."""
    row = candidate(created_at="2026-02-10 09:00:00", **changes)
    del row["meta"]["llm_score"], row["FAIL_TO_FAIL"], row["PASS_TO_FAIL"]
    row["install_config"] = json.dumps(row["install_config"])
    return row


def test_held_out_rule_needs_no_llm_scores_and_checks_the_cutoff():
    assert held_out_rule_failures(leaderboard_row(), cutoff="2026-02-01") == []
    assert held_out_rule_failures(leaderboard_row(), cutoff="2026-03-01") == [
        "created_at before 2026-03-01"
    ]
    assert held_out_rule_failures(leaderboard_row(docker_image=""), "2026-02-01") == [
        "no published image"
    ]


def test_held_out_draw_skips_dev_repos_dedupes_months_and_caps_per_repo():
    rows = [leaderboard_row(instance_id=f"a__x-{n}", repo="a/x") for n in range(5)]
    rows += [leaderboard_row(instance_id="d__dev-1", repo="d/dev")]
    rows += [leaderboard_row(instance_id="a__x-0", repo="a/x")]  # the same task in another month

    drawn = held_out_candidates(rows, cutoff="2026-01-01", dev_repos={"d/dev"})

    assert drawn == sorted([f"a__x-{n}" for n in range(5)], key=rank)[:HELD_OUT_PER_REPO]

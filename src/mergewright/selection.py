"""Split selection (spec Section 9.1): a fixed rule over SWE-rebench rows, then a fixed order.

The rules and the order were written before any row was read, so no task is picked by hand. The
order is the SHA-256 of the instance id: stable, and unrelated to difficulty or repo. Candidates
are taken in that order; one that fails validation is dropped and the next one is taken.

Dev comes from `nebius/SWE-rebench`. Held-out comes from `nebius/SWE-rebench-leaderboard`
(monthly splits), drawn only at Phase 5 with the headline model's release date as the cutoff:
the rule is frozen now, the ids later (eval/PLAN.md). The leaderboard has no LLM scores and no
F2F/P2F lists, so the held-out rule checks only what both datasets have.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Callable, Collection
from typing import Any

from mergewright.tasks import row_test_ids

DEV_FROM, DEV_UNTIL = "2021-01-01", "2024-01-01"  # created_at window, end excluded
# The dataset-viewer filter for the window; dev_rule_failures checks the rest.
DEV_WHERE = (
    f'"created_at">=\'{DEV_FROM}\' AND "created_at"<\'{DEV_UNTIL}\' AND "docker_image" IS NOT NULL'
)
MAX_LLM_SCORE = 1  # SWE-rebench's LLM ratings: 0 is best
LLM_SCORES = ("difficulty_score", "issue_text_score", "test_score")
MAX_F2P, MAX_P2P, MAX_FILES = 5, 150, 3
HELD_OUT_SIZE, HELD_OUT_PER_REPO = 150, 3


def dev_rule_failures(row: dict[str, Any]) -> list[str]:
    """Why a SWE-rebench row is not a dev candidate; empty if it is one."""
    scores = (row.get("meta") or {}).get("llm_score") or {}
    checks = {
        f"created_at outside {DEV_FROM}..{DEV_UNTIL}": DEV_FROM <= row["created_at"] < DEV_UNTIL,
        **_shared_checks(row),
        f"an LLM score above {MAX_LLM_SCORE}": all(
            scores.get(name) is not None and scores[name] <= MAX_LLM_SCORE for name in LLM_SCORES
        ),
        "F2F or P2F tests": not (
            row_test_ids(row.get("FAIL_TO_FAIL") or [])
            or row_test_ids(row.get("PASS_TO_FAIL") or [])
        ),
    }
    return [reason for reason, ok in checks.items() if not ok]


def held_out_rule_failures(row: dict[str, Any], cutoff: str) -> list[str]:
    """Why a leaderboard row is not a held-out candidate. cutoff: 'YYYY-MM-DD', the first day
    after the headline model's public release date (UTC)."""
    checks = {f"created_at before {cutoff}": row["created_at"] >= cutoff, **_shared_checks(row)}
    return [reason for reason, ok in checks.items() if not ok]


def _shared_checks(row: dict[str, Any]) -> dict[str, bool]:
    config = row.get("install_config") or {}
    if isinstance(config, str):  # some leaderboard months store it JSON-encoded
        config = json.loads(config)
    f2p, p2p = row_test_ids(row["FAIL_TO_PASS"]), row_test_ids(row["PASS_TO_PASS"])
    files = (row.get("meta") or {}).get("num_modified_files")
    return {
        "no published image": bool(row.get("docker_image")),
        "not parse_log_pytest": config.get("log_parser") == "parse_log_pytest",
        f"not 1-{MAX_F2P} F2P tests": 1 <= len(f2p) <= MAX_F2P,
        f"more than {MAX_P2P} P2P tests": len(p2p) <= MAX_P2P,
        f"more than {MAX_FILES} modified files": files is not None and files <= MAX_FILES,
    }


def rank(instance_id: str) -> str:
    return hashlib.sha256(instance_id.encode("utf-8")).hexdigest()


def dev_candidates(rows: list[dict[str, Any]], per_repo: int = 1) -> list[str]:
    """The ids of rows that pass the dev rule, in rank order, at most per_repo per repo."""
    return _ranked(rows, dev_rule_failures, per_repo)


def held_out_candidates(
    rows: list[dict[str, Any]], cutoff: str, dev_repos: Collection[str]
) -> list[str]:
    """The held-out draw: rows that pass the rule, from repos not in dev, in rank order, at most
    HELD_OUT_PER_REPO per repo. Rows that appear in several monthly splits count once. Take the
    first HELD_OUT_SIZE that validate; if fewer exist, use them all and report the count."""
    unique = list({row["instance_id"]: row for row in rows}.values())
    fresh = [row for row in unique if row["repo"] not in set(dev_repos)]
    return _ranked(fresh, lambda row: held_out_rule_failures(row, cutoff), HELD_OUT_PER_REPO)


def _ranked(
    rows: list[dict[str, Any]], failures: Callable[[dict[str, Any]], list[str]], per_repo: int
) -> list[str]:
    eligible = sorted((r for r in rows if not failures(r)), key=lambda r: rank(r["instance_id"]))
    taken: Counter[str] = Counter()
    ids = []
    for row in eligible:
        if taken[row["repo"]] < per_repo:
            taken[row["repo"]] += 1
            ids.append(row["instance_id"])
    return ids

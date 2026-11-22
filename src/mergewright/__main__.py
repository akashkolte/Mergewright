"""Command line: `mergewright run ...` runs the agent; `mergewright graph` prints the graph."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from mergewright.config import ConfigError, Limits, load_dotenv, load_live_settings
from mergewright.environments import DEFAULT_IMAGE, LocalRepo
from mergewright.graph import assemble_graph, next_after_coder, next_after_tester
from mergewright.llm import LLMClient, OpenAIClient, RecordingLLM, ScriptedLLM
from mergewright.runner import run_agent
from mergewright.sandbox import SandboxError

EXIT_DONE, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mergewright", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the agent on one repository and issue")
    run.add_argument("--llm", choices=["live", "replay"], required=True)
    run.add_argument("--repo", type=Path, default=Path("examples/buggy_calc"))
    run.add_argument("--issue", type=Path, default=Path("examples/inputs/buggy_calc_issue.md"))
    run.add_argument(
        "--replay-file",
        type=Path,
        default=Path("examples/inputs/buggy_calc_replay.json"),
        help="recorded model responses for --llm replay",
    )
    run.add_argument("--record", type=Path, help="save live responses to this replay file")
    run.add_argument("--runs-dir", type=Path, default=Path("runs"))
    run.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="Docker image the repo's tests run in (build the default: docker/python)",
    )

    sub.add_parser("graph", help="print the workflow graph as Mermaid")
    return parser


def _make_llm(args: argparse.Namespace, limits: Limits) -> LLMClient:
    if args.llm == "replay":
        if not args.replay_file.is_file():
            raise ConfigError(f"replay file not found: {args.replay_file}")
        return ScriptedLLM.from_file(args.replay_file)
    load_dotenv(Path(".env"), os.environ)
    client: LLMClient = OpenAIClient(load_live_settings(), timeout_s=limits.llm_timeout_s)
    return RecordingLLM(client, args.record) if args.record else client


def _run(args: argparse.Namespace) -> int:
    limits = Limits()
    for path, what in ((args.repo, "repository"), (args.issue, "issue file")):
        if not path.exists():
            print(f"error: {what} not found: {path}", file=sys.stderr)
            return EXIT_CONFIG
    try:
        llm = _make_llm(args, limits)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    env = LocalRepo(args.repo, image=args.image)
    try:
        result = run_agent(env, args.issue.read_text(), llm, args.runs_dir, limits)
    except SandboxError as exc:  # no Docker, daemon not running, image missing, ...
        print(f"error: the sandbox could not start: {exc}", file=sys.stderr)
        print("Docker is required; see the README for setup.", file=sys.stderr)
        return EXIT_CONFIG
    print("\n--- diff ---")
    print(result.diff or "(no changes)")
    print(f"--- status: {result.status} ({result.reason}) ---")
    print(f"run log: {result.run_dir / 'run.jsonl'}")
    return EXIT_DONE if result.status == "DONE" else EXIT_FAILED


def _graph() -> int:
    # The graph's shape does not depend on the node bodies, so no-op nodes are enough to draw it.
    def noop(state):
        return {}

    graph = assemble_graph(
        coder=noop,
        tools=noop,
        tester=noop,
        after_coder=next_after_coder,
        after_tester=next_after_tester,
    )
    print(graph.compile().get_graph().draw_mermaid())
    return EXIT_DONE


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _run(args) if args.command == "run" else _graph()


if __name__ == "__main__":
    sys.exit(main())

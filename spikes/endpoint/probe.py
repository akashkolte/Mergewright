"""Measure the hosted LLM endpoint: rate limits, context length, tool-call quality.

Phase 0 spike 3 (agenda step 4). Reads MERGEWRIGHT_MODEL, MERGEWRIGHT_API_BASE and
MERGEWRIGHT_API_KEY from .env, like `mergewright run --llm live`.

    python -m spikes.endpoint.probe info               # one tiny call: headers, usage, latency
    python -m spikes.endpoint.probe ramp               # concurrency ramp; stops at the first 429
    python -m spikes.endpoint.probe context            # largest prompt the server accepts
    python -m spikes.endpoint.probe tools --runs 5     # live buggy_calc runs: tool-call quality

Each command prints a JSON report and saves it under runs/spike/endpoint/. The SDK's own
retries are off (max_retries=0), so a 429 is seen, not hidden. Check the provider's terms
before the ramp; its defaults are small (at most 60 requests of 8 output tokens).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import openai

from mergewright.config import LiveSettings, load_dotenv, load_live_settings

REPORT_DIR = Path("runs/spike/endpoint")
RATE_HEADER_PREFIXES = ("x-ratelimit", "ratelimit", "retry-after", "x-request-id")
MALFORMED_PREFIXES = ("invalid arguments", "unknown tool", "tool '")  # see tools.execute


@dataclass
class Sample:
    """One chat-completions call."""

    ok: bool
    status: int  # HTTP status; 0 when no response arrived (connection error, timeout)
    latency_s: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    retry_after: str | None = None
    error: str = ""
    rate_headers: dict[str, str] = field(default_factory=dict)

    @property
    def throttled(self) -> bool:
        return self.status == 429


Call = Callable[[str, int], Sample]  # (prompt, max_tokens) -> Sample


class Endpoint:
    """The endpoint under test. Retries are off, so every 429 and error is recorded."""

    def __init__(
        self,
        settings: LiveSettings,
        timeout_s: float = 120.0,
        client: Any = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.model = settings.model
        self._clock = clock
        self._client = client or openai.OpenAI(
            base_url=settings.api_base,
            api_key=settings.api_key or "unused",
            timeout=timeout_s,
            max_retries=0,
        )

    def call(self, prompt: str, max_tokens: int) -> Sample:
        begin = self._clock()
        try:
            raw = self._client.chat.completions.with_raw_response.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0,
            )
        except openai.APIStatusError as exc:
            headers = exc.response.headers
            return Sample(
                ok=False,
                status=exc.status_code,
                latency_s=self._clock() - begin,
                retry_after=headers.get("retry-after"),
                error=str(exc)[:500],
                rate_headers=_rate_headers(headers),
            )
        except openai.OpenAIError as exc:
            return Sample(ok=False, status=0, latency_s=self._clock() - begin, error=str(exc)[:500])
        usage = getattr(raw.parse(), "usage", None)
        return Sample(
            ok=True,
            status=raw.status_code,
            latency_s=self._clock() - begin,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            retry_after=raw.headers.get("retry-after"),
            rate_headers=_rate_headers(raw.headers),
        )

    def model_card(self) -> dict[str, Any]:
        """The server's /models entry for this model; some servers list the context length."""
        try:
            for model in self._client.models.list():
                if model.id == self.model:
                    return model.model_dump()
        except openai.OpenAIError as exc:
            return {"error": str(exc)[:500]}
        return {"error": f"model {self.model!r} not in /models"}


def _rate_headers(headers: Any) -> dict[str, str]:
    """Only rate-limit headers: never cookies or auth headers."""
    return {k.lower(): v for k, v in headers.items() if k.lower().startswith(RATE_HEADER_PREFIXES)}


# --- Rate limits -------------------------------------------------------------------------


@dataclass
class Level:
    concurrency: int
    requests: int
    ok: int
    throttled: int
    errors: int
    elapsed_s: float
    p50_s: float
    p95_s: float
    rpm: float  # achieved successful requests per minute (a lower bound on the limit)
    tpm: float  # achieved tokens per minute (prompt + completion)
    retry_after: list[str]
    last_rate_headers: dict[str, str]
    first_error: str


def ramp(
    call: Call,
    levels: Sequence[int],
    per_level: int,
    prompt: str,
    max_tokens: int,
    clock: Callable[[], float] = time.monotonic,
) -> list[Level]:
    """Send per_level requests at each concurrency level. Stop after the first level with a 429."""
    results = []
    for concurrency in levels:
        start = clock()
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            samples = list(pool.map(lambda _: call(prompt, max_tokens), range(per_level)))
        level = _summarize_level(concurrency, samples, clock() - start)
        results.append(level)
        if level.throttled:
            break
    return results


def _summarize_level(concurrency: int, samples: list[Sample], elapsed: float) -> Level:
    ok = [s for s in samples if s.ok]
    latencies = sorted(s.latency_s for s in ok) or [0.0]
    tokens = sum(s.prompt_tokens + s.completion_tokens for s in ok)
    per_minute = 60.0 / elapsed if elapsed > 0 else 0.0
    errors = [s for s in samples if not s.ok and not s.throttled]
    return Level(
        concurrency=concurrency,
        requests=len(samples),
        ok=len(ok),
        throttled=sum(s.throttled for s in samples),
        errors=len(errors),
        elapsed_s=round(elapsed, 3),
        p50_s=round(statistics.median(latencies), 3),
        p95_s=round(latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))], 3),
        rpm=round(len(ok) * per_minute, 1),
        tpm=round(tokens * per_minute, 1),
        retry_after=[s.retry_after for s in samples if s.throttled and s.retry_after],
        last_rate_headers=samples[-1].rate_headers if samples else {},
        first_error=(errors[0].error if errors else ""),
    )


# --- Context length ----------------------------------------------------------------------


@dataclass
class ContextResult:
    max_ok_words: int
    max_ok_prompt_tokens: int
    min_fail_words: int | None  # None: even the largest prompt tried was accepted
    failure: str
    requests: int
    aborted: bool = False


def filler_prompt(words: int) -> str:
    """About one token per word on common tokenizers; the server reports the real count."""
    return "Reply with OK. " + " ".join(["word"] * words)


def find_context_limit(call: Call, min_words: int, max_words: int, steps: int = 8) -> ContextResult:
    """Binary search for the largest prompt the server accepts (max_tokens=1 per call)."""
    requests = 0

    def probe(words: int) -> Sample:
        nonlocal requests
        requests += 1
        return call(filler_prompt(words), 1)

    top = probe(max_words)
    if top.ok:
        return ContextResult(max_words, top.prompt_tokens, None, "", requests)
    if not _is_length_error(top):
        return ContextResult(0, 0, max_words, f"{top.status}: {top.error}", requests, aborted=True)
    bottom = probe(min_words)
    if not bottom.ok:
        failure = f"{bottom.status}: {bottom.error}"
        return ContextResult(0, 0, min_words, failure, requests, aborted=True)
    lo, lo_tokens, hi, failure = min_words, bottom.prompt_tokens, max_words, top.error
    for _ in range(steps):
        if hi - lo <= 16:
            break
        mid = (lo + hi) // 2
        sample = probe(mid)
        if sample.ok:
            lo, lo_tokens = mid, sample.prompt_tokens
        elif _is_length_error(sample):
            hi, failure = mid, sample.error
        else:
            return ContextResult(
                lo, lo_tokens, hi, f"{sample.status}: {sample.error}", requests, True
            )
    return ContextResult(lo, lo_tokens, hi, failure, requests)


def _is_length_error(sample: Sample) -> bool:
    return sample.status in (400, 413, 422)


# --- Tool-call quality ---------------------------------------------------------------------


@dataclass
class AgentRun:
    status: str
    steps: int
    tool_calls: int  # coder tool calls (the tester's run_tests is not the model's call)
    malformed_calls: int  # unknown tool, not allowed, or arguments that fail the schema
    tool_errors: int  # well-formed calls that failed, e.g. edit_file text not found
    prompt_tokens: int
    completion_tokens: int
    seconds: float


def summarize_agent_run(events: list[dict[str, Any]], seconds: float) -> AgentRun:
    coder_calls = [e for e in events if e["event"] == "tool_call" and e.get("agent") == "coder"]
    malformed = [c for c in coder_calls if not c["ok"] and _is_malformed(c["output"])]
    llm_calls = [e for e in events if e["event"] == "llm_call"]
    finished = next((e for e in events if e["event"] == "run_finished"), {})
    return AgentRun(
        status=finished.get("status", "UNKNOWN"),
        steps=finished.get("steps", len(llm_calls)),
        tool_calls=len(coder_calls),
        malformed_calls=len(malformed),
        tool_errors=sum(not c["ok"] for c in coder_calls) - len(malformed),
        prompt_tokens=sum(e.get("prompt_tokens", 0) for e in llm_calls),
        completion_tokens=sum(e.get("completion_tokens", 0) for e in llm_calls),
        seconds=round(seconds, 1),
    )


def _is_malformed(output: str) -> bool:
    return output.startswith(MALFORMED_PREFIXES)


def summarize_tool_quality(runs: list[AgentRun]) -> dict[str, Any]:
    calls = sum(r.tool_calls for r in runs)
    return {
        "resolved": f"{sum(r.status == 'DONE' for r in runs)}/{len(runs)}",
        "statuses": [r.status for r in runs],
        "malformed_call_rate": round(sum(r.malformed_calls for r in runs) / calls, 3)
        if calls
        else 0.0,
        "tool_error_rate": round(sum(r.tool_errors for r in runs) / calls, 3) if calls else 0.0,
        "mean_steps": statistics.mean(r.steps for r in runs) if runs else 0.0,
        "mean_tokens": statistics.mean(r.prompt_tokens + r.completion_tokens for r in runs)
        if runs
        else 0.0,
        "mean_seconds": statistics.mean(r.seconds for r in runs) if runs else 0.0,
        "runs": [asdict(r) for r in runs],
    }


# --- CLI ---------------------------------------------------------------------------------


def _tools_report(settings: LiveSettings, runs: int) -> dict[str, Any]:
    # Imported here: only this command runs the agent.
    from mergewright.config import Limits
    from mergewright.llm import OpenAIClient
    from mergewright.runner import run_agent

    limits = Limits()
    repo = Path("examples/buggy_calc")
    issue = Path("examples/inputs/buggy_calc_issue.md").read_text()
    llm = OpenAIClient(settings, timeout_s=limits.llm_timeout_s)
    results = []
    for i in range(runs):
        begin = time.monotonic()
        result = run_agent(repo, issue, llm, REPORT_DIR / "runs", limits, echo=None)
        events = [
            json.loads(line) for line in (result.run_dir / "run.jsonl").read_text().splitlines()
        ]
        results.append(summarize_agent_run(events, time.monotonic() - begin))
        print(f"run {i + 1}/{runs}: {result.status} in {result.steps} steps", flush=True)
    return summarize_tool_quality(results)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("info", help="one tiny call: headers, usage, latency, /models entry")
    r = sub.add_parser("ramp", help="concurrency ramp; stops at the first 429")
    r.add_argument("--levels", default="1,2,4,8")
    r.add_argument("--per-level", type=int, default=8)
    r.add_argument("--prompt-words", type=int, default=20, help="raise to measure TPM")
    r.add_argument("--max-tokens", type=int, default=8)
    c = sub.add_parser("context", help="binary search for the largest accepted prompt")
    c.add_argument("--min-words", type=int, default=1_000)
    c.add_argument("--max-words", type=int, default=131_072)
    c.add_argument("--steps", type=int, default=8)
    t = sub.add_parser("tools", help="live buggy_calc runs: resolve rate, malformed tool calls")
    t.add_argument("--runs", type=int, default=5)
    args = parser.parse_args(argv)

    load_dotenv(Path(".env"), os.environ)
    settings = load_live_settings()
    endpoint = Endpoint(settings)
    report: dict[str, Any] = {"model": settings.model, "command": args.command}
    if args.command == "info":
        report["call"] = asdict(endpoint.call("Reply with OK.", 8))
        report["model_card"] = endpoint.model_card()
    elif args.command == "ramp":
        levels = [int(x) for x in args.levels.split(",")]
        prompt = filler_prompt(args.prompt_words)
        report["levels"] = [
            asdict(level)
            for level in ramp(endpoint.call, levels, args.per_level, prompt, args.max_tokens)
        ]
    elif args.command == "context":
        report["context"] = asdict(
            find_context_limit(endpoint.call, args.min_words, args.max_words, args.steps)
        )
    else:
        report["tools"] = _tools_report(settings, args.runs)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = REPORT_DIR / f"{args.command}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"saved {path}")


if __name__ == "__main__":
    main()

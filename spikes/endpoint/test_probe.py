"""The endpoint probe, with a fake SDK client and fake calls. No network."""

from types import SimpleNamespace

import httpx2
import openai
import pytest

from mergewright.config import LiveSettings
from spikes.endpoint.probe import (
    Endpoint,
    Sample,
    find_context_limit,
    ramp,
    summarize_agent_run,
    summarize_tool_quality,
)

SETTINGS = LiveSettings(model="m", api_base="http://x/v1", api_key="placeholder")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def fake_raw(headers: dict, prompt_tokens=5, completion_tokens=2):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    parsed = SimpleNamespace(usage=usage, choices=[SimpleNamespace()])
    return SimpleNamespace(status_code=200, headers=headers, parse=lambda: parsed)


def fake_client(create):
    completions = SimpleNamespace(with_raw_response=SimpleNamespace(create=create))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def status_error(code: int, headers: dict, message="error"):
    request = httpx2.Request("POST", "http://x/v1/chat/completions")
    response = httpx2.Response(code, headers=headers, request=request)
    return openai.APIStatusError(message, response=response, body=None)


def test_call_records_usage_latency_and_rate_limit_headers_only():
    clock = FakeClock()
    headers = {"x-ratelimit-remaining-requests": "59", "set-cookie": "secret", "retry-after": "1"}

    def create(**kwargs):
        assert kwargs["max_tokens"] == 8 and kwargs["messages"][0]["content"] == "hi"
        clock.now += 0.25
        return fake_raw(headers)

    sample = Endpoint(SETTINGS, client=fake_client(create), clock=clock).call("hi", 8)

    assert sample == Sample(
        ok=True,
        status=200,
        latency_s=0.25,
        prompt_tokens=5,
        completion_tokens=2,
        retry_after="1",
        rate_headers={"x-ratelimit-remaining-requests": "59", "retry-after": "1"},  # no cookie
    )


def test_call_turns_a_429_into_a_throttled_sample():
    def create(**kwargs):
        raise status_error(429, {"retry-after": "7"}, "slow down")

    sample = Endpoint(SETTINGS, client=fake_client(create)).call("hi", 8)

    assert not sample.ok and sample.status == 429 and sample.retry_after == "7"
    assert sample.throttled


def test_call_reports_a_connection_failure_as_status_zero():
    def create(**kwargs):
        raise openai.APIConnectionError(request=httpx2.Request("POST", "http://x"))

    sample = Endpoint(SETTINGS, client=fake_client(create)).call("hi", 8)

    assert not sample.ok and sample.status == 0 and sample.error


def test_ramp_stops_at_the_first_level_with_a_429():
    clock = FakeClock()
    seen = []

    def call(prompt: str, max_tokens: int) -> Sample:
        seen.append(prompt)
        clock.now += 1.0
        throttled = len(seen) > 6
        return Sample(
            ok=not throttled,
            status=429 if throttled else 200,
            latency_s=1.0,
            prompt_tokens=10,
            completion_tokens=2,
        )

    levels = ramp(call, levels=[1, 2, 4, 8], per_level=3, prompt="p", max_tokens=4, clock=clock)

    assert [level.concurrency for level in levels] == [1, 2, 4]  # 8 never ran
    assert [level.throttled for level in levels] == [0, 0, 3]
    first = levels[0]
    assert first.ok == 3 and first.elapsed_s == pytest.approx(3.0)
    assert first.rpm == pytest.approx(60.0) and first.tpm == pytest.approx(720.0)


def accepts_up_to(limit_words: int, calls: list[int]):
    def call(prompt: str, max_tokens: int) -> Sample:
        words = prompt.split().count("word")  # filler words only, not the instruction
        calls.append(words)
        if words <= limit_words:
            return Sample(ok=True, status=200, latency_s=0.1, prompt_tokens=words + 3)
        return Sample(ok=False, status=400, latency_s=0.1, error="maximum context length is X")

    return call


def test_context_search_finds_the_largest_accepted_prompt():
    calls: list[int] = []

    result = find_context_limit(
        accepts_up_to(5_000, calls), min_words=1_000, max_words=16_000, steps=12
    )

    assert result.max_ok_words <= 5_000 < result.min_fail_words
    assert result.min_fail_words - result.max_ok_words <= 16
    assert result.max_ok_prompt_tokens == result.max_ok_words + 3
    assert "context" in result.failure
    assert result.requests == len(calls)


def test_context_search_stops_early_when_the_largest_prompt_fits():
    calls: list[int] = []

    result = find_context_limit(accepts_up_to(10**6, calls), min_words=1_000, max_words=16_000)

    assert result.min_fail_words is None and result.max_ok_words >= 16_000
    assert len(calls) == 1


def test_context_search_aborts_on_an_error_that_is_not_about_length():
    def call(prompt: str, max_tokens: int) -> Sample:
        return Sample(ok=False, status=429, latency_s=0.1, error="slow down")

    result = find_context_limit(call, min_words=1_000, max_words=16_000)

    assert result.aborted and "429" in result.failure


def test_agent_run_summary_separates_malformed_calls_from_tool_errors():
    events = [
        {"event": "llm_call", "prompt_tokens": 100, "completion_tokens": 10},
        {"event": "tool_call", "agent": "coder", "ok": True, "output": "..."},
        {
            "event": "tool_call",
            "agent": "coder",
            "ok": False,
            "output": "invalid arguments for edit_file: ...",
        },
        {"event": "tool_call", "agent": "coder", "ok": False, "output": "unknown tool 'x'"},
        {"event": "tool_call", "agent": "coder", "ok": False, "output": "old text not found"},
        {"event": "tool_call", "agent": "tester", "ok": False, "output": "1 failed"},
        {"event": "llm_call", "prompt_tokens": 200, "completion_tokens": 20},
        {"event": "run_finished", "status": "DONE", "steps": 2},
    ]

    run = summarize_agent_run(events, seconds=12.0)

    assert run.status == "DONE" and run.steps == 2
    assert (run.tool_calls, run.malformed_calls, run.tool_errors) == (4, 2, 1)
    assert (run.prompt_tokens, run.completion_tokens) == (300, 30)


def test_tool_quality_summary_reports_resolve_and_malformed_rates():
    runs = [
        summarize_agent_run([{"event": "run_finished", "status": "DONE", "steps": 3}], 1.0),
        summarize_agent_run(
            [
                {"event": "tool_call", "agent": "coder", "ok": False, "output": "unknown tool 'x'"},
                {"event": "tool_call", "agent": "coder", "ok": True, "output": ""},
                {"event": "run_finished", "status": "FAILED_NO_FIX", "steps": 5},
            ],
            3.0,
        ),
    ]

    summary = summarize_tool_quality(runs)

    assert summary["resolved"] == "1/2"
    assert summary["malformed_call_rate"] == pytest.approx(0.5)
    assert summary["mean_steps"] == pytest.approx(4.0)

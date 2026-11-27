"""BedrockConverseClient: translation to and from Converse, retries, and the client factory.

No network: httpx.MockTransport stands in for Bedrock Runtime.
"""

import json

import httpx
import pytest

from mergewright.config import ConfigError, LiveSettings, load_live_settings
from mergewright.llm import (
    BedrockConverseClient,
    LLMError,
    LLMUnavailable,
    OpenAIClient,
    converse_request,
    live_client,
)
from mergewright.tools import CODER_TOOLS, TOOLS

SETTINGS = LiveSettings(
    model="us.anthropic.claude-sonnet-4-6",
    api_base="https://bedrock-runtime.us-east-1.amazonaws.com/",
    api_key="test-key",
    api_style="bedrock-converse",
)
HISTORY = [
    {"role": "system", "content": "You are the Coder."},
    {"role": "user", "content": "Fix the bug."},
    {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "t1", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "calc.py"}'}},
            {"id": "t2", "type": "function",
             "function": {"name": "search_code", "arguments": "not json"}},
        ],
    },
    {"role": "tool", "tool_call_id": "t1", "content": "   1  def median(): ..."},
    {"role": "tool", "tool_call_id": "t2", "content": "[]"},
    {"role": "user", "content": "The test suite failed."},
]  # fmt: skip
REPLY = {
    "output": {
        "message": {
            "role": "assistant",
            "content": [
                {"text": "Editing now."},
                {"toolUse": {"toolUseId": "t3", "name": "edit_file", "input": {"path": "calc.py"}}},
            ],
        }
    },
    "stopReason": "tool_use",
    "usage": {"inputTokens": 120, "outputTokens": 30},
}


def client_with(handler, sleeps: list[float] | None = None) -> BedrockConverseClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    sleep = sleeps.append if sleeps is not None else (lambda s: None)
    return BedrockConverseClient(SETTINGS, timeout_s=5, http=http, sleep=sleep)


def test_request_puts_system_apart_and_merges_tool_results_into_one_user_turn():
    tools = [TOOLS[name].schema() for name in CODER_TOOLS]

    body = converse_request(HISTORY, tools)

    assert body["system"] == [{"text": "You are the Coder."}]
    assert [turn["role"] for turn in body["messages"]] == ["user", "assistant", "user"]
    assistant = body["messages"][1]["content"]
    assert assistant[0] == {"toolUse": {"toolUseId": "t1", "name": "read_file",
                                        "input": {"path": "calc.py"}}}  # fmt: skip
    assert assistant[1]["toolUse"]["input"] == {"invalid_json": "not json"}
    results = body["messages"][2]["content"]
    assert [block.get("toolResult", {}).get("toolUseId") for block in results] == ["t1", "t2", None]
    assert results[2] == {"text": "The test suite failed."}
    specs = [tool["toolSpec"] for tool in body["toolConfig"]["tools"]]
    assert [spec["name"] for spec in specs] == list(CODER_TOOLS)
    assert specs[0]["inputSchema"]["json"]["properties"]["path"]
    assert body["inferenceConfig"]["temperature"] == 0


def test_complete_posts_to_the_model_url_and_parses_text_tools_and_usage():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=REPLY)

    response = client_with(handler).complete(HISTORY, [])

    (request,) = seen
    assert str(request.url) == (
        "https://bedrock-runtime.us-east-1.amazonaws.com/model/"
        "us.anthropic.claude-sonnet-4-6/converse"
    )
    assert request.headers["authorization"] == "Bearer test-key"
    assert response.content == "Editing now."
    assert [(c.id, c.name, json.loads(c.arguments)) for c in response.tool_calls] == [
        ("t3", "edit_file", {"path": "calc.py"})
    ]
    assert (response.prompt_tokens, response.completion_tokens) == (120, 30)
    # The reply goes back into the history in OpenAI format, so the next request translates it.
    assert converse_request([*HISTORY, response.as_message()], [])["messages"][-1]["role"] == (
        "assistant"
    )


def test_throttling_and_server_errors_are_retried_with_backoff():
    statuses = iter([429, 503, 200])
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json=REPLY if status == 200 else {"message": "slow down"})

    response = client_with(handler, sleeps).complete(HISTORY, [])

    assert response.content == "Editing now." and sleeps == [2.0, 4.0]


def test_a_client_error_is_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"message": "on-demand throughput isn't supported"})

    with pytest.raises(LLMError, match="HTTP 400.*on-demand") as caught:
        client_with(handler).complete(HISTORY, [])
    assert len(calls) == 1 and not isinstance(caught.value, LLMUnavailable)


def test_timeouts_give_up_after_the_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(LLMUnavailable, match="ReadTimeout"):  # the endpoint's failure
        client_with(handler).complete(HISTORY, [])


def test_api_style_selects_the_client_and_is_validated():
    base = {"MERGEWRIGHT_MODEL": "m", "MERGEWRIGHT_API_BASE": "https://example.test"}

    openai_settings = load_live_settings(base)
    bedrock_settings = load_live_settings({**base, "MERGEWRIGHT_API_STYLE": "bedrock-converse"})

    assert isinstance(live_client(openai_settings, timeout_s=5), OpenAIClient)
    assert isinstance(live_client(bedrock_settings, timeout_s=5), BedrockConverseClient)
    with pytest.raises(ConfigError, match="MERGEWRIGHT_API_STYLE"):
        load_live_settings({**base, "MERGEWRIGHT_API_STYLE": "soap"})
    with pytest.raises(ConfigError, match="needs MERGEWRIGHT_API_BASE"):
        load_live_settings({"MERGEWRIGHT_MODEL": "m", "MERGEWRIGHT_API_STYLE": "bedrock-converse"})


def test_bedrock_refuses_to_send_a_key_over_plain_http():
    bedrock = {"MERGEWRIGHT_MODEL": "m", "MERGEWRIGHT_API_STYLE": "bedrock-converse"}
    plain = {**bedrock, "MERGEWRIGHT_API_BASE": "http://bedrock.test"}

    with pytest.raises(ConfigError, match="https://"):
        load_live_settings({**plain, "MERGEWRIGHT_API_KEY": "secret"})
    assert load_live_settings(plain).api_key is None  # no key: nothing to leak
    # A local OpenAI-compatible server over http keeps working with a key.
    openai_http = {"MERGEWRIGHT_MODEL": "m", "MERGEWRIGHT_API_BASE": "http://localhost:8000/v1"}
    assert load_live_settings({**openai_http, "MERGEWRIGHT_API_KEY": "k"}).api_key == "k"

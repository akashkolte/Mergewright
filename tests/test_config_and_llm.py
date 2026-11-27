import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import openai
import pytest

from mergewright.config import (
    ConfigError,
    LiveSettings,
    Price,
    load_dotenv,
    load_live_settings,
    load_price,
)
from mergewright.llm import (
    LLMError,
    LLMResponse,
    LLMUnavailable,
    OpenAIClient,
    RecordingLLM,
    ScriptedLLM,
    ToolCall,
)


def test_load_dotenv_reads_values_and_keeps_existing_ones(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nMERGEWRIGHT_MODEL='zai-org/GLM-5.2'\nMERGEWRIGHT_API_KEY=\nA=from-file\n"
    )
    environ = {"A": "already-set"}

    load_dotenv(env_file, environ)

    assert environ == {"MERGEWRIGHT_MODEL": "zai-org/GLM-5.2", "A": "already-set"}


def test_live_settings_require_a_model():
    with pytest.raises(ConfigError, match="MERGEWRIGHT_MODEL"):
        load_live_settings({})


def test_live_settings_for_an_openai_compatible_server():
    settings = load_live_settings(
        {
            "MERGEWRIGHT_MODEL": "zai-org/GLM-5.2",
            "MERGEWRIGHT_API_BASE": "http://llm.example.test/v1",
            "MERGEWRIGHT_API_KEY": "placeholder",
        }
    )

    assert settings == SETTINGS


def test_a_price_is_read_only_when_configured():
    names = ("MERGEWRIGHT_PRICE_IN_PER_MTOK", "MERGEWRIGHT_PRICE_OUT_PER_MTOK")

    price = load_price(dict(zip(names, ["0.5", "2"], strict=True)))

    assert load_price({}) is None  # unknown, never a guess
    assert price == Price(0.5, 2.0) and price.cost(2_000_000, 1_000_000) == 3.0
    with pytest.raises(ConfigError, match="both"):
        load_price({names[0]: "0.5"})


SETTINGS = LiveSettings("zai-org/GLM-5.2", "http://llm.example.test/v1", "placeholder")


def _fake_sdk(create):
    """An object shaped like openai.OpenAI, so no test opens a network connection."""
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _completion(message, model="zai-org/GLM-5.2-0901"):
    return SimpleNamespace(
        model=model,  # the server-reported model ID
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def test_openai_client_sends_the_request_and_parses_tool_calls():
    captured = {}
    tool_call = SimpleNamespace(
        id="c1", function=SimpleNamespace(name="read_file", arguments='{"path": "calc.py"}')
    )

    def create(**kwargs):
        captured.update(kwargs)
        return _completion(SimpleNamespace(content=None, tool_calls=[tool_call]))

    tools = [{"type": "function", "function": {"name": "read_file"}}]
    client = OpenAIClient(SETTINGS, timeout_s=5, client=_fake_sdk(create))

    response = client.complete([{"role": "user", "content": "hi"}], tools=tools)

    assert response.tool_calls == [ToolCall("c1", "read_file", '{"path": "calc.py"}')]
    assert (response.prompt_tokens, response.completion_tokens) == (10, 5)
    assert captured["model"] == "zai-org/GLM-5.2"  # sent as is, with no provider prefix
    assert captured["tools"] == tools and captured["temperature"] == 0
    assert client.served_models == {"zai-org/GLM-5.2-0901"}


def test_openai_client_configures_the_sdk_from_settings():
    client = OpenAIClient(SETTINGS, timeout_s=7, retries=4)

    sdk = client._client
    assert str(sdk.base_url).rstrip("/") == "http://llm.example.test/v1"
    assert (sdk.api_key, sdk.timeout, sdk.max_retries) == ("placeholder", 7, 4)


_REQUEST = httpx.Request("POST", "http://llm.example.test/v1/chat/completions")


def _status_error(code: int) -> openai.APIStatusError:
    return openai.APIStatusError(
        f"Error code: {code}", response=httpx.Response(code, request=_REQUEST), body=None
    )


@pytest.mark.parametrize(
    ("raised", "unavailable"),
    [
        (_status_error(504), True),  # the SDK's retries are used up: the endpoint's failure
        (_status_error(429), True),  # rate limit or the key's budget
        (openai.APITimeoutError(request=_REQUEST), True),
        (_status_error(401), False),  # a wrong key or model cannot succeed: configuration
        (openai.OpenAIError("404 model not found"), False),
    ],
)
def test_openai_client_marks_only_endpoint_failures_as_unavailable(raised, unavailable):
    def create(**kwargs):
        raise raised

    client = OpenAIClient(SETTINGS, timeout_s=5, client=_fake_sdk(create))

    with pytest.raises(LLMError, match="model call failed") as caught:
        client.complete([], [])
    assert isinstance(caught.value, LLMUnavailable) == unavailable


def test_openai_client_rejects_a_response_with_no_choices():
    client = OpenAIClient(
        SETTINGS, timeout_s=5, client=_fake_sdk(lambda **_: SimpleNamespace(choices=[]))
    )

    with pytest.raises(LLMError, match="no choices"):
        client.complete([], [])


def test_recording_then_replay_returns_the_same_responses(tmp_path: Path):
    original = [
        LLMResponse(tool_calls=[ToolCall("c1", "search_code", '{"pattern": "x"}')]),
        LLMResponse(content="done"),
    ]
    replay_file = tmp_path / "replay.json"
    recorder = RecordingLLM(ScriptedLLM(original), replay_file)
    for _ in original:
        recorder.complete([], [])

    replayed = ScriptedLLM.from_file(replay_file)

    assert [replayed.complete([], []) for _ in original] == original
    assert json.loads(replay_file.read_text())["model"].startswith("replay of")


def test_scripted_llm_fails_when_responses_run_out():
    llm = ScriptedLLM([LLMResponse(content="only one")])
    llm.complete([], [])

    with pytest.raises(LLMError, match="exhausted"):
        llm.complete([], [])


def test_run_log_redacts_every_string_once_a_redactor_is_set(tmp_path: Path):
    from mergewright.runlog import RunLog

    log = RunLog(tmp_path / "run.jsonl", echo=None)
    log.event("before", text="SECRET")  # no redactor yet: kept as is
    log.redact = lambda text: text.replace("SECRET", "[R]")

    log.event("after", text="a SECRET", items=["SECRET"], nested={"k": "SECRET"}, n=3)

    before, after = log.events()
    assert before["text"] == "SECRET"
    assert after["text"] == "a [R]" and after["items"] == ["[R]"]
    assert after["nested"] == {"k": "[R]"} and after["n"] == 3

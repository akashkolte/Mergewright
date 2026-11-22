import json
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest

from mergewright.config import ConfigError, LiveSettings, load_dotenv, load_live_settings
from mergewright.llm import (
    LLMError,
    LLMResponse,
    OpenAIClient,
    RecordingLLM,
    ScriptedLLM,
    ToolCall,
    TurnReplayLLM,
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


SETTINGS = LiveSettings("zai-org/GLM-5.2", "http://llm.example.test/v1", "placeholder")


def _fake_sdk(create):
    """An object shaped like openai.OpenAI, so no test opens a network connection."""
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def _completion(message):
    return SimpleNamespace(
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


def test_openai_client_configures_the_sdk_from_settings():
    client = OpenAIClient(SETTINGS, timeout_s=7, retries=4)

    sdk = client._client
    assert str(sdk.base_url).rstrip("/") == "http://llm.example.test/v1"
    assert (sdk.api_key, sdk.timeout, sdk.max_retries) == ("placeholder", 7, 4)


def test_openai_client_turns_sdk_errors_into_llm_error():
    def create(**kwargs):
        raise openai.OpenAIError("404 model not found")

    client = OpenAIClient(SETTINGS, timeout_s=5, client=_fake_sdk(create))

    with pytest.raises(LLMError, match="model not found"):
        client.complete([], [])


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


def _turns(n: int) -> list[dict]:
    """A conversation with n earlier assistant turns, plus tool and user messages between them."""
    messages: list[dict] = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    for i in range(n):
        messages += [
            {"role": "assistant", "content": f"a{i}"},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "t"},
        ]
    return messages


def test_turn_replay_returns_the_response_for_the_current_assistant_turn():
    responses = [LLMResponse(content="first"), LLMResponse(content="second")]
    llm = TurnReplayLLM(responses)

    assert llm.complete(_turns(0), []) == responses[0]
    assert llm.complete(_turns(1), []) == responses[1]


def test_turn_replay_gives_the_same_answer_when_a_call_is_retried():
    # A retried activity or a restarted worker repeats the same call. It must get the same reply.
    responses = [LLMResponse(content="first"), LLMResponse(content="second")]

    first_try = TurnReplayLLM(responses).complete(_turns(1), [])
    retry_on_a_new_worker = TurnReplayLLM(responses).complete(_turns(1), [])

    assert first_try == retry_on_a_new_worker == responses[1]


def test_turn_replay_fails_when_the_turn_has_no_response():
    llm = TurnReplayLLM([LLMResponse(content="only one")])

    with pytest.raises(LLMError, match="exhausted"):
        llm.complete(_turns(1), [])


def test_turn_replay_loads_the_same_replay_file_format(tmp_path: Path):
    replay_file = tmp_path / "replay.json"
    responses = [LLMResponse(content="a"), LLMResponse(content="b")]
    recorder = RecordingLLM(ScriptedLLM(responses), replay_file)
    for _ in responses:
        recorder.complete([], [])

    replayed = TurnReplayLLM.from_file(replay_file)

    assert replayed.model.startswith("replay of")
    assert replayed.complete(_turns(1), []) == responses[1]


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

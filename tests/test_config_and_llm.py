import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mergewright.config import ConfigError, LiveSettings, load_dotenv, load_live_settings
from mergewright.llm import (
    LiteLLMClient,
    LLMError,
    LLMResponse,
    RecordingLLM,
    ScriptedLLM,
    ToolCall,
)


def test_load_dotenv_reads_values_and_keeps_existing_ones(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nMERGEWRIGHT_MODEL='openai/glm'\nMERGEWRIGHT_API_KEY=\nA=from-file\n"
    )
    environ = {"A": "already-set"}

    load_dotenv(env_file, environ)

    assert environ == {"MERGEWRIGHT_MODEL": "openai/glm", "A": "already-set"}


def test_live_settings_require_a_model():
    with pytest.raises(ConfigError, match="MERGEWRIGHT_MODEL"):
        load_live_settings({})


def test_live_settings_for_an_openai_compatible_server():
    settings = load_live_settings(
        {
            "MERGEWRIGHT_MODEL": "openai/GLM-5.2",
            "MERGEWRIGHT_API_BASE": "http://llm.example.test/v1",
            "MERGEWRIGHT_API_KEY": "placeholder",
        }
    )

    assert settings == LiveSettings("openai/GLM-5.2", "http://llm.example.test/v1", "placeholder")


def _fake_completion(message):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def test_litellm_client_parses_tool_calls_and_passes_settings(monkeypatch: pytest.MonkeyPatch):
    import litellm

    captured = {}
    tool_call = SimpleNamespace(
        id="c1", function=SimpleNamespace(name="read_file", arguments='{"path": "calc.py"}')
    )

    def fake(**kwargs):
        captured.update(kwargs)
        return _fake_completion(SimpleNamespace(content=None, tool_calls=[tool_call]))

    monkeypatch.setattr(litellm, "completion", fake)
    client = LiteLLMClient(LiveSettings("openai/GLM-5.2", "http://x.test/v1", "k"), timeout_s=5)

    response = client.complete([{"role": "user", "content": "hi"}], tools=[])

    assert response.tool_calls == [ToolCall("c1", "read_file", '{"path": "calc.py"}')]
    assert (response.prompt_tokens, response.completion_tokens) == (10, 5)
    assert captured["api_base"] == "http://x.test/v1" and captured["model"] == "openai/GLM-5.2"


def test_litellm_client_retries_then_raises(monkeypatch: pytest.MonkeyPatch):
    import litellm

    attempts = []
    sleeps: list[float] = []

    def failing(**kwargs):
        attempts.append(1)
        raise RuntimeError("503 service unavailable")

    monkeypatch.setattr(litellm, "completion", failing)
    client = LiteLLMClient(LiveSettings("openai/m", None, None), 5, retries=2, sleep=sleeps.append)

    with pytest.raises(LLMError, match="after 3 attempts"):
        client.complete([], [])
    assert len(attempts) == 3 and sleeps == [1, 2]  # exponential backoff


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

"""Offline tests for GeminiGenerationAdapter — no live network calls."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import SecretStr

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import GenerationError
from sourcetrace.generation.client import (
    GeminiGenerationAdapter,
    GenerationMessage,
    GenerationProvider,
)

# ---------------------------------------------------------------------------
# Fake Gemini client helpers
# ---------------------------------------------------------------------------


class _FakeGeminiResponse:
    """Mimics a google-genai GenerateContentResponse with a .text property."""

    def __init__(self, text: Any) -> None:
        self._text = text

    @property
    def text(self) -> Any:
        if isinstance(self._text, BaseException):
            raise self._text
        return self._text


class _FakeModelsApi:
    def __init__(self, response_queue: list[Any] | None = None) -> None:
        self.call_count = 0
        self.recorded_calls: list[dict[str, Any]] = []
        self._queue = list(response_queue or [])

    def generate_content(self, **kwargs: Any) -> Any:
        self.call_count += 1
        self.recorded_calls.append(kwargs)
        if self._queue:
            item = self._queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item
        return _FakeGeminiResponse("Default generated answer.")


class _FakeGeminiClient:
    def __init__(self, response_queue: list[Any] | None = None) -> None:
        self.models = _FakeModelsApi(response_queue)


def _make_settings(
    *,
    gemini_api_key: str = "test-gemini-key",
    gemini_model: str = "gemini-2.5-flash",
) -> Settings:
    return Settings(
        gemini_api_key=SecretStr(gemini_api_key),
        gemini_model=gemini_model,
    )


# ---------------------------------------------------------------------------
# Protocol compliance
# ---------------------------------------------------------------------------


def test_implements_generation_provider_protocol() -> None:
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=_FakeGeminiClient(),
    )
    assert isinstance(adapter, GenerationProvider)
    assert adapter.model_identifier == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Lazy client construction
# ---------------------------------------------------------------------------


def test_constructor_is_lazy_no_client_created() -> None:
    settings = _make_settings()
    adapter = GeminiGenerationAdapter(settings=settings)
    assert adapter._client is None


def test_client_still_none_after_construction_with_key() -> None:
    settings = _make_settings(gemini_api_key="sk-gemini-test-key")
    adapter = GeminiGenerationAdapter(settings=settings)
    assert adapter._client is None  # Lazy: not set until generate() is called


# ---------------------------------------------------------------------------
# Model selection from config
# ---------------------------------------------------------------------------


def test_model_identifier_from_config() -> None:
    settings = _make_settings(gemini_model="gemini-2.5-flash")
    adapter = GeminiGenerationAdapter(settings=settings, client=_FakeGeminiClient())
    assert adapter.model_identifier == "gemini-2.5-flash"


def test_model_identifier_overridden_by_param() -> None:
    settings = _make_settings(gemini_model="gemini-2.5-flash")
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.0-flash",
        settings=settings,
        client=_FakeGeminiClient(),
    )
    assert adapter.model_identifier == "gemini-2.0-flash"


def test_whitespace_only_model_fails_on_construction() -> None:
    """Whitespace-only model identifier fails immediately."""
    with pytest.raises(GenerationError) as exc_info:
        GeminiGenerationAdapter(model_identifier="   ")
    assert str(exc_info.value) == "Generation failed safely."

def test_none_model_with_blank_config_fails_on_construction() -> None:
    """When model_identifier=None and settings.gemini_model is blank, it fails."""
    settings = Settings(gemini_model="")
    with pytest.raises(GenerationError) as exc_info:
        GeminiGenerationAdapter(model_identifier=None, settings=settings)
    assert str(exc_info.value) == "Generation failed safely."



# ---------------------------------------------------------------------------
# Missing API key
# ---------------------------------------------------------------------------


def test_missing_gemini_api_key_fails_on_generate() -> None:
    settings = Settings(gemini_api_key=None, gemini_model="gemini-2.5-flash")
    adapter = GeminiGenerationAdapter(settings=settings)
    messages = [GenerationMessage(role="user", content="Hello")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."


# ---------------------------------------------------------------------------
# Successful generation
# ---------------------------------------------------------------------------


def test_successful_generation_returns_text() -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeGeminiResponse("The answer is 42.")]
    )
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="What is the answer?")]
    result = adapter.generate(messages)
    assert result == "The answer is 42."


def test_whitespace_stripped_from_answer() -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeGeminiResponse("  padded answer  ")]
    )
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="Question")]
    result = adapter.generate(messages)
    assert result == "padded answer"


# ---------------------------------------------------------------------------
# System / user message conversion
# ---------------------------------------------------------------------------


def test_system_message_becomes_system_instruction() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [
        GenerationMessage(role="system", content="You are a code expert."),
        GenerationMessage(role="user", content="What does this function do?"),
    ]
    adapter.generate(messages)
    assert fake_client.models.call_count == 1
    call = fake_client.models.recorded_calls[0]
    config = call["config"]
    assert config.system_instruction == "You are a code expert."


def test_user_messages_go_to_contents() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [
        GenerationMessage(role="user", content="First user message"),
    ]
    adapter.generate(messages)
    call = fake_client.models.recorded_calls[0]
    contents = call["contents"]
    assert len(contents) == 1
    assert contents[0].role == "user"


def test_assistant_role_converted_to_model_role() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [
        GenerationMessage(role="user", content="First turn"),
        GenerationMessage(role="assistant", content="First response"),
        GenerationMessage(role="user", content="Follow-up"),
    ]
    adapter.generate(messages)
    call = fake_client.models.recorded_calls[0]
    contents = call["contents"]
    assert contents[1].role == "model"  # "assistant" → "model"


def test_no_system_message_produces_none_system_instruction() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="A question")]
    adapter.generate(messages)
    call = fake_client.models.recorded_calls[0]
    config = call["config"]
    assert config.system_instruction is None


def test_multiple_system_messages_concatenated() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [
        GenerationMessage(role="system", content="Part one."),
        GenerationMessage(role="system", content="Part two."),
        GenerationMessage(role="user", content="Question"),
    ]
    adapter.generate(messages)
    call = fake_client.models.recorded_calls[0]
    config = call["config"]
    assert "Part one." in config.system_instruction
    assert "Part two." in config.system_instruction


def test_only_system_messages_raises_generation_error() -> None:
    """At least one non-system message is required to call the model."""
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=_FakeGeminiClient(),
    )
    messages = [GenerationMessage(role="system", content="System only")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."


def test_correct_model_passed_to_api() -> None:
    fake_client = _FakeGeminiClient()
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="Test")]
    adapter.generate(messages)
    call = fake_client.models.recorded_calls[0]
    assert call["model"] == "gemini-2.5-flash"


# ---------------------------------------------------------------------------
# Evidence marker pass-through
# ---------------------------------------------------------------------------


def test_evidence_markers_preserved_in_response() -> None:
    answer_with_markers = (
        "The function is defined on line 42 [E1] and uses the config [E2]."
    )
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeGeminiResponse(answer_with_markers)]
    )
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="What does foo() do?")]
    result = adapter.generate(messages)
    assert "[E1]" in result
    assert "[E2]" in result


# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_text",
    [
        "",
        "   ",
        None,
    ],
)
def test_empty_or_none_response_text_rejected(bad_text: Any) -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeGeminiResponse(bad_text)]
    )
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="Question")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."


def test_oversized_response_rejected() -> None:
    large_text = "A" * 10000
    fake_client = _FakeGeminiClient(
        response_queue=[_FakeGeminiResponse(large_text)]
    )
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        max_output_chars=100,
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="Question")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."


def test_invalid_max_output_chars_rejected() -> None:
    with pytest.raises(GenerationError):
        GeminiGenerationAdapter(
            model_identifier="gemini-2.5-flash",
            max_output_chars=0,
        )


# ---------------------------------------------------------------------------
# Provider failure masking
# ---------------------------------------------------------------------------


def test_provider_exception_masked_as_generation_error() -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[RuntimeError("API key=AIzaSy-secret details exposed")]
    )
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="Question")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."
    assert "AIzaSy-secret" not in str(exc_info.value)


def test_no_prompt_or_key_leaked_in_exception() -> None:
    fake_client = _FakeGeminiClient(
        response_queue=[RuntimeError("Contains prompt: 'What is my secret key?'")]
    )
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="secret_prompt_content")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert "secret_prompt_content" not in str(exc_info.value)
    assert str(exc_info.value) == "Generation failed safely."


# ---------------------------------------------------------------------------
# Process-control exception passthrough
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_passes_through() -> None:
    fake_client = _FakeGeminiClient(response_queue=[KeyboardInterrupt()])
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="Question")]
    with pytest.raises(KeyboardInterrupt):
        adapter.generate(messages)


def test_system_exit_passes_through() -> None:
    fake_client = _FakeGeminiClient(response_queue=[SystemExit(1)])
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=fake_client,
    )
    messages = [GenerationMessage(role="user", content="Question")]
    with pytest.raises(SystemExit):
        adapter.generate(messages)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_empty_messages_list_fails_safely() -> None:
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=_FakeGeminiClient(),
    )
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate([])
    assert str(exc_info.value) == "Generation failed safely."


def test_string_input_fails_safely() -> None:
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=_FakeGeminiClient(),
    )
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate("not a list")  # type: ignore[arg-type]
    assert str(exc_info.value) == "Generation failed safely."


def test_invalid_role_fails_safely() -> None:
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=_FakeGeminiClient(),
    )
    messages = [GenerationMessage(role="invalid_role", content="Content")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."


def test_empty_content_fails_safely() -> None:
    adapter = GeminiGenerationAdapter(
        model_identifier="gemini-2.5-flash",
        client=_FakeGeminiClient(),
    )
    messages = [GenerationMessage(role="user", content="")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."

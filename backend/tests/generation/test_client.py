"""Offline unit tests for OpenAIGenerationAdapter and GenerationProvider protocol."""

from typing import Any

import pytest
from pydantic import SecretStr

from sourcetrace.core.config import Settings
from sourcetrace.core.exceptions import GenerationError
from sourcetrace.generation.client import (
    GenerationMessage,
    GenerationProvider,
    OpenAIGenerationAdapter,
)


class FakeOpenAIClient:
    """Offline fake OpenAI client for generation testing."""

    def __init__(self, response: Any | Exception = None) -> None:
        self.response = response
        self.chat = FakeChat(response)


class FakeChat:
    def __init__(self, response: Any | Exception = None) -> None:
        self.completions = FakeCompletions(response)


class FakeCompletions:
    def __init__(self, response: Any | Exception = None) -> None:
        self.response = response
        self.create_calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _make_choice_response(content: str) -> Any:
    class Message:
        def __init__(self, content: str) -> None:
            self.content = content

    class Choice:
        def __init__(self, message: Any) -> None:
            self.message = message

    class Response:
        def __init__(self, choices: list[Any]) -> None:
            self.choices = choices

    return Response([Choice(Message(content))])


def test_lazy_client_construction() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=SecretStr("sk-test-key-12345"),
        llm_base_url="https://api.openai.com/v1",
    )
    # Constructor should NOT call network or instantiate client automatically
    adapter = OpenAIGenerationAdapter(settings=settings)
    assert adapter.model_identifier == "gpt-4o-mini"
    assert adapter._client is None  # Client remains uninitialized until generate() is called


def test_missing_model_identifier() -> None:
    settings = Settings(
        llm_model="",
        llm_api_key=SecretStr("sk-test-key-12345"),
    )
    with pytest.raises(GenerationError) as exc_info:
        OpenAIGenerationAdapter(settings=settings)
    assert str(exc_info.value) == "Generation failed safely."


def test_missing_api_key() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=None,
    )
    adapter = OpenAIGenerationAdapter(settings=settings)
    messages = [GenerationMessage(role="user", content="Hello")]
    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)
    assert str(exc_info.value) == "Generation failed safely."


def test_successful_generation() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=SecretStr("sk-test-key-12345"),
    )
    fake_client = FakeOpenAIClient(_make_choice_response("This is the answer."))
    adapter = OpenAIGenerationAdapter(client=fake_client, settings=settings)

    messages = [
        GenerationMessage(role="system", content="System prompt"),
        GenerationMessage(role="user", content="Question"),
    ]
    res = adapter.generate(messages)

    assert res == "This is the answer."
    assert isinstance(adapter, GenerationProvider)
    assert len(fake_client.chat.completions.create_calls) == 1
    call = fake_client.chat.completions.create_calls[0]
    assert call["model"] == "gpt-4o-mini"
    assert call["messages"] == [
        {"role": "system", "content": "System prompt"},
        {"role": "user", "content": "Question"},
    ]


@pytest.mark.parametrize(
    "invalid_response",
    [
        None,
        object(),
        FakeOpenAIClient(None).response,  # Empty choices or wrong shape
        _make_choice_response(""),  # Empty answer string
        _make_choice_response("   "),  # Whitespace only
    ],
)
def test_invalid_provider_response_shape(invalid_response: Any) -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=SecretStr("sk-test-key-12345"),
    )
    fake_client = FakeOpenAIClient(invalid_response)
    adapter = OpenAIGenerationAdapter(client=fake_client, settings=settings)
    messages = [GenerationMessage(role="user", content="Question")]

    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)

    assert str(exc_info.value) == "Generation failed safely."


def test_oversized_generated_answer() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=SecretStr("sk-test-key-12345"),
    )
    fake_client = FakeOpenAIClient(_make_choice_response("A" * 100))
    adapter = OpenAIGenerationAdapter(
        max_output_chars=50, client=fake_client, settings=settings
    )
    messages = [GenerationMessage(role="user", content="Question")]

    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)

    assert str(exc_info.value) == "Generation failed safely."


def test_provider_exception_suppression_and_no_secret_leak() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=SecretStr("sk-secret-key-12345"),
    )
    fake_client = FakeOpenAIClient(
        RuntimeError("API failed with key sk-secret-key-12345 at https://api.openai.com")
    )
    adapter = OpenAIGenerationAdapter(client=fake_client, settings=settings)
    messages = [GenerationMessage(role="user", content="Question")]

    with pytest.raises(GenerationError) as exc_info:
        adapter.generate(messages)

    assert str(exc_info.value) == "Generation failed safely."
    assert "sk-secret-key" not in str(exc_info.value)


def test_keyboard_interrupt_pass_through() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=SecretStr("sk-test-key-12345"),
    )
    fake_client = FakeOpenAIClient(KeyboardInterrupt())
    adapter = OpenAIGenerationAdapter(client=fake_client, settings=settings)
    messages = [GenerationMessage(role="user", content="Question")]

    with pytest.raises(KeyboardInterrupt):
        adapter.generate(messages)


def test_system_exit_pass_through() -> None:
    settings = Settings(
        llm_model="gpt-4o-mini",
        llm_api_key=SecretStr("sk-test-key-12345"),
    )
    fake_client = FakeOpenAIClient(SystemExit(1))
    adapter = OpenAIGenerationAdapter(client=fake_client, settings=settings)
    messages = [GenerationMessage(role="user", content="Question")]

    with pytest.raises(SystemExit):
        adapter.generate(messages)

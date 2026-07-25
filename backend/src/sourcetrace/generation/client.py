"""Provider-neutral LLM generation interface and OpenAI-compatible adapter."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import openai

from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import GenerationError


@dataclass(frozen=True, slots=True)
class GenerationMessage:
    """Provider-neutral LLM generation prompt message."""

    role: str
    content: str


@runtime_checkable
class GenerationProvider(Protocol):
    """Protocol for provider-neutral LLM text generation."""

    @property
    def model_identifier(self) -> str:
        """Canonical configured model identifier."""
        ...

    def generate(self, messages: Sequence[GenerationMessage]) -> str:
        """Generate one non-empty grounded answer text response."""
        ...


# ---------------------------------------------------------------------------
# OpenAI-compatible generation adapter (kept for optional provider selection)
# ---------------------------------------------------------------------------


class OpenAIGenerationAdapter:
    """Injectable OpenAI-compatible generation provider adapter."""

    def __init__(
        self,
        model_identifier: str | None = None,
        max_output_chars: int = 8000,
        client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()

        resolved_model = model_identifier or cfg.llm_model
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise GenerationError("Generation failed safely.")
        self._model_identifier: str = resolved_model.strip()

        if (
            not isinstance(max_output_chars, int)
            or isinstance(max_output_chars, bool)
            or max_output_chars <= 0
        ):
            raise GenerationError("Generation failed safely.")
        self._max_output_chars = max_output_chars

        self._client: Any | None = client
        self._settings: Settings = cfg

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        api_key_secret = self._settings.llm_api_key
        if not api_key_secret:
            raise GenerationError("Generation failed safely.")

        raw_key = api_key_secret.get_secret_value()
        if not raw_key or not raw_key.strip():
            raise GenerationError("Generation failed safely.")

        base_url = self._settings.llm_base_url
        client_kwargs: dict[str, Any] = {"api_key": raw_key.strip()}
        if isinstance(base_url, str) and base_url.strip():
            client_kwargs["base_url"] = base_url.strip()

        try:
            self._client = openai.OpenAI(**client_kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        return self._client

    def generate(self, messages: Sequence[GenerationMessage]) -> str:
        if isinstance(messages, (str, bytes, bytearray)):
            raise GenerationError("Generation failed safely.")

        if not isinstance(messages, Sequence) or not messages:
            raise GenerationError("Generation failed safely.")

        payload_messages: list[dict[str, str]] = []
        for msg in messages:
            if not isinstance(msg, GenerationMessage):
                raise GenerationError("Generation failed safely.")
            if not isinstance(msg.role, str) or msg.role not in ("system", "user", "assistant"):
                raise GenerationError("Generation failed safely.")
            if not isinstance(msg.content, str) or not msg.content:
                raise GenerationError("Generation failed safely.")
            payload_messages.append({"role": msg.role, "content": msg.content})

        client = self._get_client()

        try:
            response = client.chat.completions.create(
                model=self._model_identifier,
                messages=payload_messages,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        choices = getattr(response, "choices", None)
        if not isinstance(choices, (list, tuple)) or not choices:
            raise GenerationError("Generation failed safely.")

        first_choice = choices[0]
        choice_msg = getattr(first_choice, "message", None)
        if choice_msg is None:
            raise GenerationError("Generation failed safely.")

        raw_content = getattr(choice_msg, "content", None)
        if not isinstance(raw_content, str) or not raw_content.strip():
            raise GenerationError("Generation failed safely.")

        answer = raw_content.strip()
        if len(answer) > self._max_output_chars:
            raise GenerationError("Generation failed safely.")

        return answer


# ---------------------------------------------------------------------------
# Gemini generation adapter (google-genai SDK)
# ---------------------------------------------------------------------------


class GeminiGenerationAdapter:
    """Injectable Gemini LLM generation provider adapter using the google-genai SDK.

    Lazy client: no network request is made until the first ``generate()`` call.
    Reads ``SOURCETRACE_GEMINI_API_KEY`` exclusively; never reads OpenAI keys.

    Message conversion:
    - ``role="system"`` messages are collected and passed as ``system_instruction``
      in ``GenerateContentConfig``. Only the content of the first system message is
      used as the instruction string; additional system messages are appended to it.
    - ``role="user"`` and ``role="assistant"`` messages are converted to Gemini
      ``types.Content`` objects and passed as the ``contents`` list.

    Evidence markers (e.g. ``[E1]``, ``[E2]``) in prompts and answers are passed
    through unmodified. The adapter does not inspect or alter prompt semantics.
    """

    def __init__(
        self,
        model_identifier: str | None = None,
        max_output_chars: int = 8000,
        client: Any | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()

        resolved_model = model_identifier or cfg.gemini_model
        if not isinstance(resolved_model, str) or not resolved_model.strip():
            raise GenerationError("Generation failed safely.")
        self._model_identifier: str = resolved_model.strip()

        if (
            not isinstance(max_output_chars, int)
            or isinstance(max_output_chars, bool)
            or max_output_chars <= 0
        ):
            raise GenerationError("Generation failed safely.")
        self._max_output_chars = max_output_chars

        # Injected client for testing; None means lazy construction on first generate().
        self._client: Any | None = client
        self._settings: Settings = cfg

    @property
    def model_identifier(self) -> str:
        return self._model_identifier

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        api_key_secret = self._settings.gemini_api_key
        if not api_key_secret:
            raise GenerationError("Generation failed safely.")

        raw_key = api_key_secret.get_secret_value()
        if not raw_key or not raw_key.strip():
            raise GenerationError("Generation failed safely.")

        try:
            from google import genai  # type: ignore[import-untyped]

            self._client = genai.Client(api_key=raw_key.strip())
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        return self._client

    def generate(self, messages: Sequence[GenerationMessage]) -> str:
        if isinstance(messages, (str, bytes, bytearray)):
            raise GenerationError("Generation failed safely.")

        if not isinstance(messages, Sequence) or not messages:
            raise GenerationError("Generation failed safely.")

        # Validate all messages up-front before any network call.
        system_parts: list[str] = []
        conversation_messages: list[GenerationMessage] = []

        for msg in messages:
            if not isinstance(msg, GenerationMessage):
                raise GenerationError("Generation failed safely.")
            if not isinstance(msg.role, str) or msg.role not in ("system", "user", "assistant"):
                raise GenerationError("Generation failed safely.")
            if not isinstance(msg.content, str) or not msg.content:
                raise GenerationError("Generation failed safely.")

            if msg.role == "system":
                system_parts.append(msg.content)
            else:
                conversation_messages.append(msg)

        # At least one non-system message is required.
        if not conversation_messages:
            raise GenerationError("Generation failed safely.")

        client = self._get_client()

        try:
            from google.genai import types as genai_types  # type: ignore[import-untyped]
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        # Build Gemini contents list from non-system messages.
        # Gemini roles: "user" and "model" (not "assistant").
        contents: list[Any] = []
        for msg in conversation_messages:
            gemini_role = "model" if msg.role == "assistant" else "user"
            try:
                contents.append(
                    genai_types.Content(
                        role=gemini_role,
                        parts=[genai_types.Part(text=msg.content)],
                    )
                )
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception:
                raise GenerationError("Generation failed safely.") from None

        # Build generation config, including system instruction when present.
        system_instruction: str | None = "\n\n".join(system_parts) if system_parts else None

        try:
            gen_config = genai_types.GenerateContentConfig(
                system_instruction=system_instruction,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        try:
            response = client.models.generate_content(
                model=self._model_identifier,
                contents=contents,
                config=gen_config,
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        # Validate response: response.text is the canonical accessor.
        try:
            raw_text = response.text
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            raise GenerationError("Generation failed safely.") from None

        if not isinstance(raw_text, str) or not raw_text.strip():
            raise GenerationError("Generation failed safely.")

        answer = raw_text.strip()
        if len(answer) > self._max_output_chars:
            raise GenerationError("Generation failed safely.")

        return answer

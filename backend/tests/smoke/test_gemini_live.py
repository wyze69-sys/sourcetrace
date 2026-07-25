"""Opt-in live Gemini smoke test.

This test suite is SKIPPED by default and must NEVER run in CI.
It only executes when both of these environment variables are set:

    SOURCETRACE_RUN_LIVE_GEMINI=1
    SOURCETRACE_GEMINI_API_KEY=<your Google AI Studio key>

To run manually:
    SOURCETRACE_RUN_LIVE_GEMINI=1 SOURCETRACE_GEMINI_API_KEY=<key> \
        uv run pytest tests/smoke/test_gemini_live.py -v

WARNING: This test makes REAL network requests to Google's Gemini API.
         Do NOT commit this file with a real key. Do NOT add to CI.
         Do NOT print the API key value anywhere in this file.
"""

from __future__ import annotations

import os

import pytest

_RUN_LIVE = os.getenv("SOURCETRACE_RUN_LIVE_GEMINI") == "1" and bool(
    os.getenv("SOURCETRACE_GEMINI_API_KEY")
)

pytestmark = pytest.mark.skipif(
    not _RUN_LIVE,
    reason=(
        "Live Gemini smoke test skipped. "
        "Set SOURCETRACE_RUN_LIVE_GEMINI=1 and SOURCETRACE_GEMINI_API_KEY to enable."
    ),
)


def test_live_gemini_generation() -> None:
    """Live smoke: make one tiny generation request to Gemini API."""
    from sourcetrace.generation.client import GeminiGenerationAdapter, GenerationMessage

    print("\n[LIVE SMOKE TEST] Calling Gemini generation API...")
    adapter = GeminiGenerationAdapter(model_identifier="gemini-2.5-flash")

    messages = [
        GenerationMessage(
            role="system",
            content="You are a concise assistant. Respond in one sentence.",
        ),
        GenerationMessage(
            role="user",
            content="What is 2 + 2?",
        ),
    ]

    result = adapter.generate(messages)

    assert isinstance(result, str), "Result must be a string"
    assert result.strip(), "Result must be non-empty"
    assert len(result) <= 8000, "Result must not exceed max_output_chars"

    # Safe output — never print the key
    print(f"[LIVE SMOKE TEST] Generation result length: {len(result)} chars")
    print(f"[LIVE SMOKE TEST] Generation result (first 200 chars): {result[:200]!r}")
    print("[LIVE SMOKE TEST] Generation: PASSED")


def test_live_gemini_embedding() -> None:
    """Live smoke: make one tiny embedding request to Gemini API."""
    from sourcetrace.embeddings.provider import GeminiEmbeddingAdapter

    print("\n[LIVE SMOKE TEST] Calling Gemini embedding API...")
    adapter = GeminiEmbeddingAdapter(
        model_identifier="gemini-embedding-001",
        expected_dimensions=1536,
        task_type="RETRIEVAL_QUERY",
    )

    result = adapter.embed(["What is a Python generator?"])

    assert isinstance(result, tuple), "Result must be a tuple"
    assert len(result) == 1, "Must return exactly 1 vector for 1 input"
    vec = result[0]
    assert isinstance(vec, tuple), "Vector must be a tuple"
    assert len(vec) == 1536, f"Vector must have exactly 1536 dimensions, got {len(vec)}"
    assert all(isinstance(v, float) for v in vec), "All values must be floats"

    # Safe output — never print the key
    print(f"[LIVE SMOKE TEST] Embedding vector length: {len(vec)}")
    print(f"[LIVE SMOKE TEST] First 5 values: {vec[:5]}")
    print("[LIVE SMOKE TEST] Embedding: PASSED")

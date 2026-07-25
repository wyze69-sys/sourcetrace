"""Centralized capability and provider readiness evaluation service for SourceTrace."""

from dataclasses import dataclass

from sourcetrace.core.config import Settings


@dataclass(frozen=True)
class SystemCapabilities:
    """System capability availability snapshot."""

    allowed_index_modes: list[str]
    default_index_mode: str
    lexical_search_available: bool
    semantic_search_available: bool
    generation_available: bool


def _has_secret(secret_obj: object) -> bool:
    """Safely check if a SecretStr or string setting contains a non-empty value."""
    if not secret_obj:
        return False
    if hasattr(secret_obj, "get_secret_value"):
        val = secret_obj.get_secret_value()  # type: ignore[union-attr]
        return isinstance(val, str) and bool(val.strip())
    if isinstance(secret_obj, str):
        return bool(secret_obj.strip())
    return False


def evaluate_capabilities(settings: Settings) -> SystemCapabilities:
    """Pure capability readiness evaluator used by API routes and background workers."""
    has_gemini_key = _has_secret(settings.gemini_api_key)
    has_embedding_key = _has_secret(settings.embedding_api_key)
    has_llm_key = _has_secret(settings.llm_api_key)

    emb_provider = (settings.embedding_provider or "").strip().lower()
    if emb_provider == "gemini":
        semantic_search_available = has_gemini_key
    elif emb_provider == "openai":
        semantic_search_available = has_embedding_key
    else:
        semantic_search_available = False

    llm_prov = (settings.llm_provider or "").strip().lower()
    if llm_prov == "gemini":
        generation_available = has_gemini_key
    elif llm_prov == "openai":
        generation_available = has_llm_key
    else:
        generation_available = False

    configured_allowed = getattr(settings, "allowed_index_modes", ("static", "cloud_ai"))
    allowed_modes: list[str] = ["static"]
    if semantic_search_available and "cloud_ai" in configured_allowed:
        allowed_modes.append("cloud_ai")

    default_mode = settings.default_index_mode
    if default_mode not in allowed_modes:
        default_mode = "static"

    return SystemCapabilities(
        allowed_index_modes=allowed_modes,
        default_index_mode=default_mode,
        lexical_search_available=True,
        semantic_search_available=semantic_search_available,
        generation_available=generation_available,
    )

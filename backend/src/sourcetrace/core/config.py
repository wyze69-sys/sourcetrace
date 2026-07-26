"""Typed environment configuration for SourceTrace."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from SOURCETRACE_* environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SOURCETRACE_",
        extra="ignore",
    )

    env: str = "development"
    log_level: str = "INFO"
    data_dir: Path = Path("data")
    mongodb_uri: SecretStr | None = None
    mongodb_database_name: str = "sourcetrace"
    api_base_url: str = "http://127.0.0.1:8000"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:8000",
    ]
    cors_origin_regex: str | None = r"https://.*\.vercel\.app"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            if v.startswith("[") and v.endswith("]"):
                import json

                try:
                    return json.loads(v)
                except Exception:
                    pass
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    # -------------------------------------------------------------------------
    # Mode and Provider selection
    # -------------------------------------------------------------------------
    allowed_index_modes: tuple[str, ...] = ("static", "cloud_ai")
    default_index_mode: str = "static"
    # Valid values: "gemini" | "openai"
    # Gemini is the default cloud provider. Switch to "openai" only when an OpenAI
    # API key is available. The inactive provider's client is never instantiated.
    llm_provider: str = "gemini"
    embedding_provider: str = "gemini"

    # -------------------------------------------------------------------------
    # Gemini provider (google-genai SDK)
    # -------------------------------------------------------------------------
    # Set SOURCETRACE_GEMINI_API_KEY to a Google AI Studio API key.
    # Obtain one at https://aistudio.google.com/apikey
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_embedding_model: str = "gemini-embedding-001"

    # -------------------------------------------------------------------------
    # OpenAI-compatible provider (kept as optional selectable provider)
    # -------------------------------------------------------------------------
    # None of these are required when llm_provider / embedding_provider = "gemini".
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: SecretStr | None = None
    llm_model: str = ""
    embedding_model: str = ""
    embedding_base_url: str | None = None
    embedding_api_key: SecretStr | None = None

    # -------------------------------------------------------------------------
    # Shared embedding configuration
    # -------------------------------------------------------------------------
    # Must match the Atlas Vector Search index (numDimensions: 1536).
    # gemini-embedding-001 supports output_dimensionality=1536.
    embedding_dimensions: int = 1536
    # Keep requests small enough for Gemini's per-request token limits.
    embedding_batch_size: int = 32

    # -------------------------------------------------------------------------
    # Vector search
    # -------------------------------------------------------------------------
    vector_index_name: str = "code_chunks_vector_index"
    vector_search_num_candidates: int = 100
    vector_search_max_limit: int = 50

    # -------------------------------------------------------------------------
    # Session & JWT
    # -------------------------------------------------------------------------
    session_cookie_name: str = "sourcetrace_session"
    session_signing_secret: SecretStr | None = None

    jwt_secret: SecretStr | None = None
    jwt_algorithm: Literal["HS256"] = "HS256"
    jwt_access_token_ttl_seconds: int = 604800
    jwt_issuer: str = "sourcetrace"
    jwt_audience: str = "sourcetrace-api"

    @field_validator("jwt_access_token_ttl_seconds")
    @classmethod
    def _validate_jwt_ttl(cls, v: int) -> int:
        if isinstance(v, bool) or v <= 0:
            raise ValueError("jwt_access_token_ttl_seconds must be a positive integer.")
        return v

    @field_validator("jwt_issuer", "jwt_audience")
    @classmethod
    def _validate_jwt_string_field(cls, v: str) -> str:
        if not v or not isinstance(v, str) or not v.strip():
            raise ValueError("JWT configuration string cannot be empty or whitespace-only.")
        return v.strip()


@lru_cache
def get_settings() -> Settings:
    """Return one cached settings object for the process."""
    return Settings()

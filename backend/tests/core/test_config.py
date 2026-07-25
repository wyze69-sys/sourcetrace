import pytest

from sourcetrace.core.config import Settings


def test_settings_loads_without_mongodb_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOURCETRACE_MONGODB_URI", raising=False)

    settings = Settings(_env_file=None)

    assert settings.mongodb_uri is None
    assert settings.mongodb_database_name == "sourcetrace"
    assert not hasattr(settings, "chroma_dir")

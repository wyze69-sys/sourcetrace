"""Configuration settings and environment loading."""


class AppConfig:
    """Application configuration state container."""

    def __init__(self, environment: str = "production") -> None:
        self.environment = environment
        self.max_retries = 3
        self.timeout_seconds = 30

    def is_production(self) -> bool:
        """Return True if environment is production."""
        return self.environment == "production"

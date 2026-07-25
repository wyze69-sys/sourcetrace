"""Configuration settings for sample repository."""


class AppConfig:
    """Application configuration container."""

    def __init__(self, environment: str = "production") -> None:
        self.environment = environment
        self.max_retries = 3
        self.timeout_seconds = 30

    def is_production(self) -> bool:
        """Check if environment is set to production."""
        return self.environment == "production"

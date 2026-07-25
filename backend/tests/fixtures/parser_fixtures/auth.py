"""Legitimate security-related module that must NOT be excluded by the scanner."""


def authenticate(username: str, password: str) -> bool:
    """Authenticate a user."""
    return username == "admin" and password == "secret"


class TokenManager:
    """Manage authentication tokens."""

    def generate_token(self) -> str:
        """Generate a token."""
        return "token_value"

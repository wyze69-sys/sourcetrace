"""Domain exceptions and error formatting."""


class AccessDeniedError(Exception):
    """Raised when owner permission validation fails."""

    pass


def format_error_response(error: Exception) -> dict:
    """Format exception into standard API error dictionary."""
    return {"error": str(error), "type": type(error).__name__}

"""Error handling and exception definitions."""


class RepositoryError(Exception):
    """Base exception for repository operations."""

    pass


class AccessDeniedError(RepositoryError):
    """Raised when repository access is denied."""

    pass


def format_error_response(error: Exception) -> dict:
    """Format an exception into a safe response payload."""
    return {"error_type": type(error).__name__, "message": str(error)}

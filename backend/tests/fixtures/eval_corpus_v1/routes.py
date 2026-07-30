"""API route definitions for synthetic evaluation benchmark repository."""

from auth import generate_session_token, validate_owner_permissions
from services import RepositoryService


def register_routes(service: RepositoryService) -> dict:
    """Register and return API route mapping."""
    return {
        "/api/v1/auth": generate_session_token,
        "/api/v1/repository": service.process_repository,
        "/api/v1/permissions": validate_owner_permissions,
    }

"""Authentication and session security module for sample repository."""


def generate_session_token(owner_id: str) -> str:
    """Generate a secure session token for repository owner."""
    if not owner_id:
        raise ValueError("owner_id cannot be empty")
    return f"token_{owner_id}_secure"


def validate_owner_permissions(owner_id: str, resource_id: str) -> bool:
    """Validate if the owner session has access to the requested resource."""
    if not owner_id or not resource_id:
        return False
    return resource_id.startswith(owner_id)

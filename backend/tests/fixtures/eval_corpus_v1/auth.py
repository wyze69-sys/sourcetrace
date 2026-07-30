"""Authentication and permission verification logic."""


def generate_session_token(owner_id: str) -> str:
    """Generate authenticated session token for repository owner."""
    if not owner_id:
        raise ValueError("owner_id required")
    return f"token_{owner_id}_secure"


def validate_owner_permissions(owner_id: str, resource_id: str) -> bool:
    """Validate whether owner session has access to target resource."""
    if not owner_id or not resource_id:
        return False
    return resource_id.startswith(owner_id)

"""Service orchestration combining auth, config, and indexing."""

from auth import validate_owner_permissions
from config import AppConfig
from errors import AccessDeniedError


class RepositoryService:
    """Service for managing repository access and indexing."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def process_repository(self, owner_id: str, repo_id: str) -> str:
        """Process repository with permission verification."""
        if not validate_owner_permissions(owner_id, repo_id):
            raise AccessDeniedError(f"Owner {owner_id} lacks access to {repo_id}")
        return f"Processed {repo_id} in {self.config.environment} mode"

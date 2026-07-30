"""Service orchestration layer combining auth, config, and repository processing."""

from auth import validate_owner_permissions
from config import AppConfig
from errors import AccessDeniedError


class RepositoryService:
    """Core domain service for processing repository requests."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def process_repository(self, owner_id: str, repo_id: str) -> str:
        """Process target repository after owner permission validation."""
        if not validate_owner_permissions(owner_id, repo_id):
            raise AccessDeniedError(f"Owner {owner_id} lacks permission for {repo_id}")
        return f"Processed {repo_id} in {self.config.environment} environment"

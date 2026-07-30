"""Application entrypoint for synthetic evaluation benchmark repository."""

from config import AppConfig
from routes import register_routes
from services import RepositoryService


def create_app() -> dict:
    """Initialize application instance and dependencies."""
    config = AppConfig()
    service = RepositoryService(config)
    routes = register_routes(service)
    return {"config": config, "service": service, "routes": routes}


def main() -> None:
    """Start application runtime server."""
    app = create_app()
    print(f"Application started in {app['config'].environment} mode")


if __name__ == "__main__":
    main()

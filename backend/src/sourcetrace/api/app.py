"""FastAPI application factory."""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sourcetrace import __version__
from sourcetrace.api.errors import register_error_handlers
from sourcetrace.api.routes.auth import router as auth_router
from sourcetrace.api.routes.capabilities import router as capabilities_router
from sourcetrace.api.routes.conversations import router as conversations_router
from sourcetrace.api.routes.health import router as health_router
from sourcetrace.api.routes.indexing_jobs import router as indexing_jobs_router
from sourcetrace.api.routes.repositories import router as repositories_router
from sourcetrace.api.routes.search import router as search_router
from sourcetrace.core.config import get_settings


def create_app() -> FastAPI:
    """Create and configure the SourceTrace API application."""
    settings = get_settings()
    app = FastAPI(
        title="SourceTrace API",
        description="Evidence-grounded intelligence for Python codebases",
        version=__version__,
    )

    cors_origins = [origin.strip() for origin in settings.cors_origins if origin.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=settings.cors_origin_regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_error_handlers(app)
    app.include_router(health_router, prefix="/api/v1")
    app.include_router(auth_router)
    app.include_router(capabilities_router)
    app.include_router(repositories_router, prefix="/api/v1")
    app.include_router(search_router)
    app.include_router(indexing_jobs_router, prefix="/api/v1")
    app.include_router(conversations_router, prefix="/api/v1")
    return app


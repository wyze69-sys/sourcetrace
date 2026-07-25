"""Health-check route."""

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from sourcetrace import __version__

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    """Stable health-check response contract."""

    status: str
    version: str
    timestamp: str


@router.get("/health", response_model=HealthResponse, operation_id="getHealth")
def health() -> HealthResponse:
    """Report service health status."""
    return HealthResponse(
        status="ok",
        version=__version__,
        timestamp=datetime.now(UTC).isoformat(),
    )

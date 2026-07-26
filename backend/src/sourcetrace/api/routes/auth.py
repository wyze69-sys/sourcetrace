"""FastAPI route module for authentication and token provisioning."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status

from sourcetrace.api.dependencies import get_current_session, get_jwt_signer
from sourcetrace.api.schemas import ErrorEnvelope, TokenResponse
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import JWTSigner
from sourcetrace.models.domain import AnonymousSession

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post(
    "/session",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    operation_id="createAnonymousAccessToken",
    summary="Provision or convert anonymous session access token",
    responses={
        500: {"model": ErrorEnvelope, "description": "Internal Server Error"},
    },
)
def create_anonymous_access_token(
    current_session: Annotated[AnonymousSession, Depends(get_current_session)],
    jwt_signer: Annotated[JWTSigner, Depends(get_jwt_signer)],
    settings: Annotated[Settings, Depends(get_settings)],
    response: Response,
) -> TokenResponse:
    """Convert or provision current anonymous browser identity into a JWT access token."""
    access_token = jwt_signer.create_access_token(current_session.owner_session_id)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return TokenResponse(
        access_token=access_token,
        token_type="Bearer",
        expires_in=settings.jwt_access_token_ttl_seconds,
    )

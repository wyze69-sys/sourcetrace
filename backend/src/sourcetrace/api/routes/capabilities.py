"""Capabilities reporting endpoint for SourceTrace feature availability."""

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from sourcetrace.core.capabilities import evaluate_capabilities
from sourcetrace.core.config import Settings, get_settings

router = APIRouter(prefix="/api/v1/capabilities", tags=["capabilities"])


class CapabilitiesResponse(BaseModel):
    """Capabilities reporting response schema."""

    allowed_index_modes: list[str]
    default_index_mode: str
    lexical_search_available: bool
    semantic_search_available: bool
    generation_available: bool


@router.get("", response_model=CapabilitiesResponse)
def get_capabilities(
    settings: Annotated[Settings, Depends(get_settings)],
) -> CapabilitiesResponse:
    """Return server capabilities without exposing sensitive credentials or internal paths."""
    caps = evaluate_capabilities(settings)

    return CapabilitiesResponse(
        allowed_index_modes=caps.allowed_index_modes,
        default_index_mode=caps.default_index_mode,
        lexical_search_available=caps.lexical_search_available,
        semantic_search_available=caps.semantic_search_available,
        generation_available=caps.generation_available,
    )

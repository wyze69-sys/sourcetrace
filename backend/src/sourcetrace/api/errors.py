"""Global exception handlers for standard error envelope responses."""

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from sourcetrace.api.schemas import ErrorDetail, ErrorEnvelope

CODE_MAPPING = {
    status.HTTP_400_BAD_REQUEST: "BAD_REQUEST",
    status.HTTP_401_UNAUTHORIZED: "UNAUTHORIZED",
    status.HTTP_404_NOT_FOUND: "RESOURCE_NOT_FOUND",
    status.HTTP_413_CONTENT_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "VALIDATION_ERROR",
    status.HTTP_429_TOO_MANY_REQUESTS: "QUOTA_EXCEEDED",
}

MESSAGE_MAPPING = {
    status.HTTP_400_BAD_REQUEST: "Invalid request.",
    status.HTTP_401_UNAUTHORIZED: "Authentication credentials are missing or invalid.",
    status.HTTP_404_NOT_FOUND: "The requested resource was not found.",
    status.HTTP_413_CONTENT_TOO_LARGE: "The submitted content is too large.",
    status.HTTP_422_UNPROCESSABLE_CONTENT: "Request validation failed.",
    status.HTTP_429_TOO_MANY_REQUESTS: "The request cannot be processed at this time.",
}


def register_error_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the FastAPI application."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        error_code = CODE_MAPPING.get(exc.status_code, "INTERNAL_ERROR")
        if (
            exc.status_code == status.HTTP_400_BAD_REQUEST
            and isinstance(exc.detail, str)
            and exc.detail.strip()
        ):
            message = exc.detail.strip()
        else:
            message = MESSAGE_MAPPING.get(exc.status_code, "An internal server error occurred.")

        envelope = ErrorEnvelope(error=ErrorDetail(code=error_code, message=message))
        return JSONResponse(
            status_code=exc.status_code,
            content=envelope.model_dump(),
            headers=exc.headers if exc.headers else None,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code="VALIDATION_ERROR",
                message="Request validation failed.",
            )
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, content=envelope.model_dump()
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        envelope = ErrorEnvelope(
            error=ErrorDetail(
                code="INTERNAL_ERROR",
                message="An internal server error occurred.",
            )
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, content=envelope.model_dump()
        )

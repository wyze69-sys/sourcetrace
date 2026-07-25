from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from pydantic import BaseModel

from sourcetrace.api.errors import register_error_handlers


def create_test_error_app() -> FastAPI:
    test_app = FastAPI()
    register_error_handlers(test_app)

    class DummyPayload(BaseModel):
        required_field: str

    @test_app.post("/test-validation")
    def dummy_validation_route(payload: DummyPayload) -> dict:
        return {"ok": True}

    @test_app.get("/test-413")
    def dummy_413_route() -> None:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Payload is too big",
        )

    @test_app.get("/test-429")
    def dummy_429_route() -> None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
        )

    @test_app.get("/test-500")
    def dummy_500_route() -> None:
        raise RuntimeError("Simulated internal error")

    return test_app


def test_validation_error_returns_standard_422_envelope() -> None:
    client = TestClient(create_test_error_app())

    response = client.post("/test-validation", json={})

    assert response.status_code == 422
    assert response.json() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed.",
            "request_id": None,
        }
    }


def test_payload_too_large_returns_standard_413_envelope() -> None:
    client = TestClient(create_test_error_app())

    response = client.get("/test-413")

    assert response.status_code == 413
    assert response.json() == {
        "error": {
            "code": "PAYLOAD_TOO_LARGE",
            "message": "The submitted content is too large.",
            "request_id": None,
        }
    }


def test_quota_exceeded_returns_standard_429_envelope() -> None:
    client = TestClient(create_test_error_app())

    response = client.get("/test-429")

    assert response.status_code == 429
    assert response.json() == {
        "error": {
            "code": "QUOTA_EXCEEDED",
            "message": "The request cannot be processed at this time.",
            "request_id": None,
        }
    }


def test_unhandled_exception_returns_standard_500_envelope() -> None:
    client = TestClient(create_test_error_app(), raise_server_exceptions=False)

    response = client.get("/test-500")

    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "INTERNAL_ERROR",
            "message": "An internal server error occurred.",
            "request_id": None,
        }
    }

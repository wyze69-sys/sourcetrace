"""Runtime OpenAPI schema and contract assertion tests for SourceTrace FastAPI routes."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from sourcetrace.api.app import create_app
from sourcetrace.api.dependencies import (
    get_current_owner_id,
    get_indexing_job_repository,
    get_session_repository,
    get_session_signer,
)
from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import SessionSigner
from sourcetrace.models.domain import AnonymousSession, IndexingJobRecord
from sourcetrace.storage.repositories import IndexingJobRepository

TEST_SECRET = "a_very_secret_key_that_is_at_least_32_bytes_long!"


class InMemoryIndexingJobRepoForValidation:
    def __init__(self, job: IndexingJobRecord) -> None:
        self.job = job

    def get_by_id(self, owner_session_id: str, job_id: str) -> IndexingJobRecord | None:
        if self.job.owner_session_id == owner_session_id and self.job.job_id == job_id:
            return self.job
        return None

    def get_by_repository(
        self, owner_session_id: str, repository_id: str
    ) -> IndexingJobRecord | None:
        return None

    def save(self, job: IndexingJobRecord) -> IndexingJobRecord:
        return job

    def delete_by_repository(self, owner_session_id: str, repository_id: str) -> int:
        return 0


def test_runtime_openapi_operation_ids_match_frozen_ids_and_are_unique() -> None:
    app = create_app()
    spec = app.openapi()

    expected_operation_ids = {
        ("/api/v1/health", "get"): "getHealth",
        ("/api/v1/repositories", "get"): "listRepositories",
        ("/api/v1/repositories/{repository_id}", "get"): "getRepository",
        ("/api/v1/indexing-jobs/{job_id}", "get"): "getIndexingJobStatus",
        ("/api/v1/repositories/{repository_id}/conversations", "post"): "createConversation",
        (
            "/api/v1/repositories/{repository_id}/conversations/{conversation_id}",
            "get",
        ): "getConversation",
        (
            "/api/v1/repositories/{repository_id}/conversations/{conversation_id}/messages",
            "post",
        ): "sendMessage",
        ("/api/v1/auth/session", "post"): "createAnonymousAccessToken",
    }

    paths = spec.get("paths", {})
    operation_ids: list[str] = []

    for (path, method), expected_op_id in expected_operation_ids.items():
        assert path in paths, f"Path {path} missing from runtime OpenAPI"
        path_item = paths[path]
        assert method in path_item, f"Method {method} missing for path {path}"
        actual_op_id = path_item[method].get("operationId")
        msg = f"Expected '{expected_op_id}' for {method.upper()} {path}, got '{actual_op_id}'"
        assert actual_op_id == expected_op_id, msg
        operation_ids.append(actual_op_id)

    assert len(operation_ids) == len(set(operation_ids)), "Operation IDs must be unique"


def test_indexing_job_progress_percentage_has_min_0_and_max_100() -> None:
    app = create_app()
    spec = app.openapi()

    schemas = spec.get("components", {}).get("schemas", {})
    assert "IndexingJob" in schemas, "IndexingJob schema missing from runtime OpenAPI"

    indexing_job_schema = schemas["IndexingJob"]
    progress_prop = indexing_job_schema.get("properties", {}).get("progress_percentage", {})

    assert progress_prop.get("minimum") == 0, (
        f"Expected progress_percentage minimum=0, got {progress_prop.get('minimum')}"
    )
    assert progress_prop.get("maximum") == 100, (
        f"Expected progress_percentage maximum=100, got {progress_prop.get('maximum')}"
    )


def test_token_response_schema_structure_in_runtime_openapi() -> None:
    app = create_app()
    spec = app.openapi()

    schemas = spec.get("components", {}).get("schemas", {})
    assert "TokenResponse" in schemas, "TokenResponse schema missing from runtime OpenAPI"

    token_schema = schemas["TokenResponse"]
    props = token_schema.get("properties", {})
    assert "access_token" in props
    assert "token_type" in props
    assert "expires_in" in props

    token_type_prop = props.get("token_type", {})
    assert token_type_prop.get("enum") == ["Bearer"] or token_type_prop.get("default") == "Bearer"


def test_protected_routes_declare_bearer_security_and_401_error_envelope() -> None:
    app = create_app()
    spec = app.openapi()
    paths = spec.get("paths", {})

    sec_schemes = spec.get("components", {}).get("securitySchemes", {})
    assert "HTTPBearer" in sec_schemes, "HTTPBearer security scheme missing from components"
    bearer_scheme = sec_schemes["HTTPBearer"]
    assert bearer_scheme.get("type") == "http"
    assert bearer_scheme.get("scheme") == "bearer"

    protected_operations = [
        ("/api/v1/repositories/upload", "post"),
        ("/api/v1/repositories", "get"),
        ("/api/v1/repositories/{repository_id}", "get"),
        ("/api/v1/repositories", "post"),
        ("/api/v1/indexing-jobs/{job_id}", "get"),
        ("/api/v1/repositories/{repository_id}/search", "post"),
        ("/api/v1/repositories/{repository_id}/conversations", "post"),
        ("/api/v1/repositories/{repository_id}/conversations/{conversation_id}", "get"),
        ("/api/v1/repositories/{repository_id}/conversations/{conversation_id}/messages", "post"),
    ]

    for path, method in protected_operations:
        assert path in paths, f"Path {path} missing"
        op = paths[path][method]

        sec = op.get("security", [])
        has_bearer = any("HTTPBearer" in s for s in sec)
        assert has_bearer, f"{method.upper()} {path} missing HTTPBearer security requirement"

        responses = op.get("responses", {})
        assert "401" in responses, f"{method.upper()} {path} missing 401 response declaration"
        ref_401 = (
            responses["401"]
            .get("content", {})
            .get("application/json", {})
            .get("schema", {})
            .get("$ref")
        )
        assert ref_401 == "#/components/schemas/ErrorEnvelope"

    public_operations = [
        ("/api/v1/health", "get"),
        ("/api/v1/capabilities", "get"),
        ("/api/v1/auth/session", "post"),
    ]

    for path, method in public_operations:
        assert path in paths, f"Public path {path} missing"
        op = paths[path][method]
        sec = op.get("security", [])
        no_bearer = not sec or all("HTTPBearer" not in s for s in sec)
        assert no_bearer, f"Public route {method.upper()} {path} should not require HTTPBearer"


def test_runtime_openapi_declared_responses_reference_error_envelope() -> None:
    app = create_app()
    spec = app.openapi()
    paths = spec.get("paths", {})

    target_routes = [
        ("/api/v1/repositories", "get", ["401", "500"]),
        ("/api/v1/repositories/{repository_id}", "get", ["401", "404", "500"]),
        ("/api/v1/indexing-jobs/{job_id}", "get", ["401", "404", "500"]),
    ]

    for path, method, status_codes in target_routes:
        op = paths[path][method]
        responses = op.get("responses", {})
        for status_code in status_codes:
            assert status_code in responses, (
                f"Status code {status_code} missing from {method.upper()} {path} responses"
            )
            schema_ref = (
                responses[status_code]
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("$ref")
            )
            msg = f"Expected ErrorEnvelope for {method.upper()} {path} ({status_code})"
            assert schema_ref == "#/components/schemas/ErrorEnvelope", msg


def test_out_of_range_persisted_progress_returns_500_internal_error() -> None:
    now = datetime.now(UTC)
    exp = now + timedelta(days=7)
    owner_id = "sess_test_progress"

    session = AnonymousSession(
        owner_session_id=owner_id,
        last_active_at=now,
        expires_at=exp,
        created_at=now,
        updated_at=now,
    )

    bad_jobs = [
        IndexingJobRecord(
            job_id="job_negative",
            repository_id="repo_1",
            owner_session_id=owner_id,
            status="parsing",
            current_step="Parsing",
            created_at=now,
            updated_at=now,
            progress_percentage=-10,
        ),
        IndexingJobRecord(
            job_id="job_excessive",
            repository_id="repo_1",
            owner_session_id=owner_id,
            status="parsing",
            current_step="Parsing",
            created_at=now,
            updated_at=now,
            progress_percentage=150,
        ),
    ]

    for bad_job in bad_jobs:
        app = create_app()
        settings_obj = Settings(env="development", session_signing_secret=SecretStr(TEST_SECRET))

        mock_session_repo = MagicMock()
        mock_session_repo.get_by_id.return_value = session
        mock_job_repo: IndexingJobRepository = InMemoryIndexingJobRepoForValidation(bad_job)

        app.dependency_overrides[get_settings] = lambda s=settings_obj: s
        app.dependency_overrides[get_current_owner_id] = lambda: owner_id
        app.dependency_overrides[get_session_signer] = lambda: SessionSigner(secret=TEST_SECRET)
        app.dependency_overrides[get_session_repository] = lambda sr=mock_session_repo: sr
        app.dependency_overrides[get_indexing_job_repository] = lambda jr=mock_job_repo: jr

        client = TestClient(app, raise_server_exceptions=False)
        res = client.get(f"/api/v1/indexing-jobs/{bad_job.job_id}")
        assert res.status_code == 500
        assert res.json() == {
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal server error occurred.",
                "request_id": None,
            }
        }

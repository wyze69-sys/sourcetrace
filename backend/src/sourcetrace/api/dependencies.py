"""FastAPI dependencies for SourceTrace application endpoints."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Protocol

from fastapi import BackgroundTasks, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.exceptions import SessionInvalidError
from sourcetrace.core.security import (
    SESSION_MAX_AGE_SECONDS,
    JWTSigner,
    SessionSigner,
    generate_owner_session_id,
)
from sourcetrace.generation.service import GroundedAnswerService
from sourcetrace.ingestion.service import IngestionService
from sourcetrace.models.domain import AnonymousSession
from sourcetrace.retrieval.service import SemanticRetrievalService

if TYPE_CHECKING:
    from sourcetrace.ingestion.upload_staging import UploadStagingStore
from sourcetrace.storage.mongo_repositories import (
    MongoAnonymousSessionRepository,
    MongoCodeChunkRepository,
    MongoConversationExchangeRepository,
    MongoConversationRepository,
    MongoIndexingJobRepository,
    MongoMessageRepository,
    MongoRepositoryRepository,
)
from sourcetrace.storage.repositories import (
    AnonymousSessionRepository,
    CodeChunkRepository,
    ConversationExchangeRepository,
    ConversationRepository,
    IndexingJobRepository,
    MessageRepository,
    RepositoryRepository,
)


class GitHubIndexingScheduler(Protocol):
    """Protocol for scheduling background repository indexing tasks."""

    def schedule(
        self,
        background_tasks: BackgroundTasks,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
    ) -> None: ...


class DefaultGitHubIndexingScheduler:
    """Production implementation of GitHubIndexingScheduler using FastAPI BackgroundTasks."""

    def schedule(
        self,
        background_tasks: BackgroundTasks,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
    ) -> None:
        from sourcetrace.workers.github_indexing import run_github_indexing

        background_tasks.add_task(
            run_github_indexing,
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
        )


def get_github_indexing_scheduler() -> GitHubIndexingScheduler:
    """Dependency provider for GitHubIndexingScheduler."""
    return DefaultGitHubIndexingScheduler()


class ZipIndexingScheduler(Protocol):
    """Protocol for scheduling background ZIP repository indexing tasks."""

    def schedule(
        self,
        background_tasks: BackgroundTasks,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        staging_token: str,
    ) -> None: ...


class DefaultZipIndexingScheduler:
    """Production implementation of ZipIndexingScheduler using FastAPI BackgroundTasks."""

    def schedule(
        self,
        background_tasks: BackgroundTasks,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        staging_token: str,
    ) -> None:
        from sourcetrace.workers.zip_indexing import run_zip_indexing

        background_tasks.add_task(
            run_zip_indexing,
            owner_session_id=owner_session_id,
            repository_id=repository_id,
            job_id=job_id,
            staging_token=staging_token,
        )


def get_zip_indexing_scheduler() -> ZipIndexingScheduler:
    """Dependency provider for ZipIndexingScheduler."""
    return DefaultZipIndexingScheduler()


def get_upload_staging_store() -> "UploadStagingStore":
    """Dependency provider for UploadStagingStore."""
    from sourcetrace.ingestion.upload_staging import FileSystemUploadStagingStore

    return FileSystemUploadStagingStore()


def get_session_repository() -> AnonymousSessionRepository:
    """Dependency provider for AnonymousSessionRepository."""
    return MongoAnonymousSessionRepository()


def get_repository_repository() -> RepositoryRepository:
    """Dependency provider for RepositoryRepository."""
    return MongoRepositoryRepository()


def get_indexing_job_repository() -> IndexingJobRepository:
    """Dependency provider for IndexingJobRepository."""
    return MongoIndexingJobRepository()


def get_ingestion_service(
    session_repo: Annotated[AnonymousSessionRepository, Depends(get_session_repository)],
    repository_repo: Annotated[RepositoryRepository, Depends(get_repository_repository)],
    job_repo: Annotated[IndexingJobRepository, Depends(get_indexing_job_repository)],
) -> IngestionService:
    """Dependency provider for IngestionService."""
    return IngestionService(
        session_repo=session_repo,
        repository_repo=repository_repo,
        job_repo=job_repo,
    )


def get_session_signer(
    settings: Annotated[Settings, Depends(get_settings)],
) -> SessionSigner:
    """Dependency provider for SessionSigner."""
    return SessionSigner(secret=settings.session_signing_secret)


http_bearer = HTTPBearer(auto_error=False)


def get_jwt_signer(
    settings: Annotated[Settings, Depends(get_settings)],
) -> JWTSigner:
    """Dependency provider for JWTSigner."""
    return JWTSigner(settings=settings)


def _unauthorized() -> HTTPException:
    """Build the single opaque 401 response used for every Bearer rejection."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication credentials are missing or invalid.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_owner_id(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(http_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    """FastAPI dependency verifying stateless JWT Bearer token and returning owner_session_id."""
    if (
        credentials is None
        or not credentials.credentials
        or credentials.scheme.lower() != "bearer"
    ):
        raise _unauthorized()

    # Built here rather than via Depends so a missing/!bearer header still yields
    # 401 instead of a 500 when the JWT secret is unconfigured.
    jwt_signer = get_jwt_signer(settings)
    try:
        return jwt_signer.verify_access_token(credentials.credentials)
    except SessionInvalidError as exc:
        raise _unauthorized() from exc


CurrentOwnerId = Annotated[str, Depends(get_current_owner_id)]


AUTH_SESSION_COOKIE_PATH = "/api/v1/auth/session"


def _set_bootstrap_cookie(
    response: Response,
    settings: Settings,
    token: str,
    max_age: int,
) -> None:
    is_production = settings.env.lower() == "production"
    response.delete_cookie(
        key=settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="strict",
        secure=is_production,
    )
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=max_age,
        httponly=True,
        samesite="strict",
        path=AUTH_SESSION_COOKIE_PATH,
        secure=is_production,
    )


def get_current_session(
    request: Request,
    response: Response,
    repo: Annotated[AnonymousSessionRepository, Depends(get_session_repository)],
    signer: Annotated[SessionSigner, Depends(get_session_signer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AnonymousSession:
    """FastAPI dependency resolving or provisioning the anonymous browser session context."""
    cookie_value = request.cookies.get(settings.session_cookie_name)
    now = datetime.now(UTC)

    if cookie_value:
        claimed_owner_id = signer.verify_cookie_token(cookie_value, current_time=now)
        if claimed_owner_id:
            existing_session = repo.get_by_id(claimed_owner_id)
            if (
                existing_session is not None
                and existing_session.owner_session_id == claimed_owner_id
                and existing_session.expires_at > now
            ):
                token = signer.create_cookie_token(
                    existing_session.owner_session_id, existing_session.expires_at
                )
                remaining_seconds = int((existing_session.expires_at - now).total_seconds())
                max_age = max(1, remaining_seconds)
                _set_bootstrap_cookie(response, settings, token, max_age)
                return existing_session

    expires_at = now + timedelta(seconds=SESSION_MAX_AGE_SECONDS)
    owner_session_id = generate_owner_session_id()

    new_session = AnonymousSession(
        owner_session_id=owner_session_id,
        created_at=now,
        updated_at=now,
        last_active_at=now,
        expires_at=expires_at,
    )
    repo.save(new_session)

    token = signer.create_cookie_token(owner_session_id, expires_at)
    _set_bootstrap_cookie(response, settings, token, SESSION_MAX_AGE_SECONDS)

    return new_session


def get_conversation_repository() -> ConversationRepository:
    """Dependency provider for ConversationRepository."""
    return MongoConversationRepository()


def get_message_repository() -> MessageRepository:
    """Dependency provider for MessageRepository."""
    return MongoMessageRepository()


def get_code_chunk_repository() -> CodeChunkRepository:
    """Dependency provider for CodeChunkRepository."""
    return MongoCodeChunkRepository()


def get_semantic_retrieval_service(
    code_chunk_repo: Annotated[CodeChunkRepository, Depends(get_code_chunk_repository)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SemanticRetrievalService:
    """Dependency provider for SemanticRetrievalService.

    Selects the embedding provider based on SOURCETRACE_EMBEDDING_PROVIDER
    (default: "gemini"). No OpenAI client is constructed when Gemini is selected.
    """
    from sourcetrace.retrieval.service import SemanticRetrievalService

    provider_name = (settings.embedding_provider or "").strip().lower()

    if provider_name == "gemini":
        from sourcetrace.embeddings.provider import GeminiEmbeddingAdapter

        embedding_adapter = GeminiEmbeddingAdapter(settings=settings)
    elif provider_name == "openai":
        from sourcetrace.embeddings.provider import OpenAIEmbeddingAdapter

        embedding_adapter = OpenAIEmbeddingAdapter(settings=settings)
    else:
        raise RuntimeError(f"Unsupported embedding provider: {provider_name!r}")

    return SemanticRetrievalService(
        code_chunk_repo=code_chunk_repo,
        embedding_provider=embedding_adapter,
    )


def build_generation_adapter(settings: Settings):
    """Select the LLM generation adapter from SOURCETRACE_LLM_PROVIDER.

    No OpenAI client is constructed when Gemini is selected (and vice versa).
    Raises RuntimeError for unsupported providers — callers must only invoke
    this when generation is actually needed.
    """
    provider_name = (settings.llm_provider or "").strip().lower()

    if provider_name == "gemini":
        from sourcetrace.generation.client import GeminiGenerationAdapter

        return GeminiGenerationAdapter(settings=settings)
    if provider_name == "openai":
        from sourcetrace.generation.client import OpenAIGenerationAdapter

        return OpenAIGenerationAdapter(settings=settings)
    raise RuntimeError(f"Unsupported LLM provider: {provider_name!r}")


def get_grounded_answer_service(
    retrieval_service: Annotated[
        SemanticRetrievalService, Depends(get_semantic_retrieval_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GroundedAnswerService:
    """Dependency provider for GroundedAnswerService."""
    from sourcetrace.generation.service import GroundedAnswerService

    return GroundedAnswerService(
        retrieval_service=retrieval_service,
        generation_provider=build_generation_adapter(settings),
    )


def get_trace_explainer_factory(
    settings: Annotated[Settings, Depends(get_settings)],
):
    """Lazy factory for TraceExplanationService.

    Returned as a zero-argument callable so static-mode trace requests never
    construct an LLM adapter (which would raise when no provider is
    configured). The route invokes it only after the capability gate passes.
    """
    from sourcetrace.generation.trace_explanation import TraceExplanationService

    def factory() -> TraceExplanationService:
        return TraceExplanationService(build_generation_adapter(settings))

    return factory


def get_conversation_exchange_repository() -> ConversationExchangeRepository:
    """Dependency provider for ConversationExchangeRepository."""
    return MongoConversationExchangeRepository()



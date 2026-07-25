"""FastAPI dependencies for SourceTrace application endpoints."""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Protocol

from fastapi import BackgroundTasks, Depends, Request, Response

from sourcetrace.core.config import Settings, get_settings
from sourcetrace.core.security import (
    SESSION_MAX_AGE_SECONDS,
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


def get_current_session(
    request: Request,
    response: Response,
    repo: Annotated[AnonymousSessionRepository, Depends(get_session_repository)],
    signer: Annotated[SessionSigner, Depends(get_session_signer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AnonymousSession:
    """FastAPI dependency resolving or provisioning the anonymous browser session context."""
    cookie_value = request.cookies.get(settings.session_cookie_name)

    if cookie_value:
        claimed_owner_id = signer.verify_cookie_token(cookie_value)
        if claimed_owner_id:
            existing_session = repo.get_by_id(claimed_owner_id)
            if existing_session is not None:
                now = datetime.now(UTC)
                if (
                    existing_session.owner_session_id == claimed_owner_id
                    and existing_session.expires_at > now
                ):
                    return existing_session

    now = datetime.now(UTC)
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
    is_production = settings.env.lower() == "production"

    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=is_production,
    )

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


def get_grounded_answer_service(
    retrieval_service: Annotated[
        SemanticRetrievalService, Depends(get_semantic_retrieval_service)
    ],
    settings: Annotated[Settings, Depends(get_settings)],
) -> GroundedAnswerService:
    """Dependency provider for GroundedAnswerService.

    Selects the LLM generation provider based on SOURCETRACE_LLM_PROVIDER
    (default: "gemini"). No OpenAI client is constructed when Gemini is selected.
    """
    from sourcetrace.generation.service import GroundedAnswerService

    provider_name = (settings.llm_provider or "").strip().lower()

    if provider_name == "gemini":
        from sourcetrace.generation.client import GeminiGenerationAdapter

        generation_adapter = GeminiGenerationAdapter(settings=settings)
    elif provider_name == "openai":
        from sourcetrace.generation.client import OpenAIGenerationAdapter

        generation_adapter = OpenAIGenerationAdapter(settings=settings)
    else:
        raise RuntimeError(f"Unsupported LLM provider: {provider_name!r}")

    return GroundedAnswerService(
        retrieval_service=retrieval_service,
        generation_provider=generation_adapter,
    )


def get_conversation_exchange_repository() -> ConversationExchangeRepository:
    """Dependency provider for ConversationExchangeRepository."""
    return MongoConversationExchangeRepository()



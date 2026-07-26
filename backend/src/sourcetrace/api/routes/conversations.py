"""Repository-scoped chat API routes for conversation management and grounded messaging."""

import re
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from sourcetrace.api.dependencies import (
    CurrentOwnerId,
    get_conversation_exchange_repository,
    get_conversation_repository,
    get_grounded_answer_service,
    get_message_repository,
    get_repository_repository,
    get_session_repository,
)
from sourcetrace.api.schemas import (
    ConversationDetailResponse,
    CreateConversationRequest,
    CreateConversationResponse,
    ErrorEnvelope,
    RequestMetadata,
    SendMessageRequest,
    SendMessageResponse,
    conversation_record_to_schema,
    message_record_to_schema,
    validate_question,
)
from sourcetrace.core.security import (
    SESSION_MAX_AGE_SECONDS,
    generate_conversation_id,
    generate_message_id,
)
from sourcetrace.generation.client import GenerationMessage
from sourcetrace.generation.service import GroundedAnswerService
from sourcetrace.models.domain import (
    AnonymousSession,
    ConversationRecord,
    MessageRecord,
)
from sourcetrace.storage.repositories import (
    AnonymousSessionRepository,
    ConversationExchangeRepository,
    ConversationRepository,
    MessageRepository,
    RepositoryRepository,
)

router = APIRouter(tags=["conversations"])


def derive_conversation_title(question: str, max_length: int = 100) -> str:
    """Derive clean, bounded conversation title from user question."""
    cleaned = re.sub(r"[\r\n\t\x00-\x1f]", " ", question)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        cleaned = "New conversation"
    if len(cleaned) > max_length:
        return cleaned[: max_length - 3] + "..."
    return cleaned


def build_generation_history(
    messages: list[MessageRecord],
    max_messages: int = 10,
    max_characters: int = 4000,
) -> list[GenerationMessage]:
    """Convert stored messages into bounded GenerationMessage context."""
    sorted_msgs = sorted(messages, key=lambda m: (m.created_at, m.message_id))
    recent_msgs = sorted_msgs[-max_messages:]

    candidates: list[GenerationMessage] = []
    char_count = 0

    for msg in reversed(recent_msgs):
        if msg.role not in ("user", "assistant"):
            continue
        msg_len = len(msg.content)
        if char_count + msg_len > max_characters and candidates:
            break
        candidates.append(GenerationMessage(role=msg.role, content=msg.content))
        char_count += msg_len

    return list(reversed(candidates))


def _refresh_session_activity_quietly(
    owner_session_id: str,
    session_repo: AnonymousSessionRepository,
) -> None:
    """Best-effort session activity update after exchange persistence."""
    try:
        existing_session = session_repo.get_by_id(owner_session_id)
        if (
            existing_session is not None
            and existing_session.owner_session_id == owner_session_id
        ):
            now_session = datetime.now(UTC)
            updated_session = AnonymousSession(
                owner_session_id=owner_session_id,
                last_active_at=now_session,
                expires_at=now_session + timedelta(seconds=SESSION_MAX_AGE_SECONDS),
                created_at=existing_session.created_at,
                updated_at=now_session,
                active_repository_count=existing_session.active_repository_count,
            )
            session_repo.save(updated_session)
    except Exception:
        pass


@router.post(
    "/repositories/{repository_id}/conversations",
    response_model=CreateConversationResponse,
    status_code=status.HTTP_201_CREATED,
    operation_id="createConversation",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "Authentication credentials are missing or invalid",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Repository missing or not ready",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def create_conversation(
    repository_id: str,
    body: CreateConversationRequest,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[
        RepositoryRepository, Depends(get_repository_repository)
    ],
    exchange_repo: Annotated[
        ConversationExchangeRepository, Depends(get_conversation_exchange_repository)
    ],
    session_repo: Annotated[
        AnonymousSessionRepository, Depends(get_session_repository)
    ],
    answer_service: Annotated[
        GroundedAnswerService, Depends(get_grounded_answer_service)
    ],
) -> CreateConversationResponse:
    """Create a new conversation and generate a grounded answer."""
    repo = repository_repo.get_by_id(
        owner_session_id=owner_session_id, repository_id=repository_id
    )
    if repo is None or repo.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository missing or not ready.",
        )

    try:
        validated_question = validate_question(body.question)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    start_time = time.monotonic()
    result = answer_service.generate_answer(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        question=validated_question,
    )
    latency_ms = max(0, int((time.monotonic() - start_time) * 1000))

    conv_id = generate_conversation_id()
    user_msg_id = generate_message_id()
    assistant_msg_id = generate_message_id()

    title = derive_conversation_title(validated_question)
    now = datetime.now(UTC)

    conv_record = ConversationRecord(
        conversation_id=conv_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        title=title,
        created_at=now,
        updated_at=now,
    )

    user_msg_record = MessageRecord(
        message_id=user_msg_id,
        conversation_id=conv_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        role="user",
        content=validated_question,
        created_at=now,
        citations=(),
        evidence=(),
        insufficient_evidence=False,
    )

    assistant_msg_record = MessageRecord(
        message_id=assistant_msg_id,
        conversation_id=conv_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        role="assistant",
        content=result.answer,
        created_at=now + timedelta(microseconds=1),
        citations=result.citations,
        evidence=result.evidence,
        insufficient_evidence=result.insufficient_evidence,
    )

    # Persist exchange atomically / via scoped compensation
    exchange_repo.create_conversation_exchange(
        conversation=conv_record,
        user_message=user_msg_record,
        assistant_message=assistant_msg_record,
    )

    # Best-effort session activity update after exchange persistence
    _refresh_session_activity_quietly(owner_session_id, session_repo)

    return CreateConversationResponse(
        conversation_id=conv_id,
        repository_id=repository_id,
        user_message=message_record_to_schema(user_msg_record),
        assistant_message=message_record_to_schema(assistant_msg_record),
        request_metadata=RequestMetadata(
            latency_ms=latency_ms,
            chunks_retrieved=result.chunks_retrieved,
        ),
    )


@router.get(
    "/repositories/{repository_id}/conversations/{conversation_id}",
    response_model=ConversationDetailResponse,
    status_code=status.HTTP_200_OK,
    operation_id="getConversation",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "Authentication credentials are missing or invalid",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Conversation missing or owned by another session",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def get_conversation(
    repository_id: str,
    conversation_id: str,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[
        RepositoryRepository, Depends(get_repository_repository)
    ],
    conversation_repo: Annotated[
        ConversationRepository, Depends(get_conversation_repository)
    ],
    message_repo: Annotated[MessageRepository, Depends(get_message_repository)],
) -> ConversationDetailResponse:
    """Returns conversation details and full message history. Excluded from session activity."""
    repo = repository_repo.get_by_id(
        owner_session_id=owner_session_id, repository_id=repository_id
    )
    if repo is None or repo.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository missing or not ready.",
        )

    conv = conversation_repo.get_by_id(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        conversation_id=conversation_id,
    )
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation missing or owned by another session.",
        )

    messages = message_repo.list_by_conversation(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        conversation_id=conversation_id,
    )

    return conversation_record_to_schema(conv, messages)


@router.post(
    "/repositories/{repository_id}/conversations/{conversation_id}/messages",
    response_model=SendMessageResponse,
    status_code=status.HTTP_200_OK,
    operation_id="sendMessage",
    responses={
        status.HTTP_401_UNAUTHORIZED: {
            "model": ErrorEnvelope,
            "description": "Authentication credentials are missing or invalid",
        },
        status.HTTP_404_NOT_FOUND: {
            "model": ErrorEnvelope,
            "description": "Conversation missing or owned by another session",
        },
        status.HTTP_500_INTERNAL_SERVER_ERROR: {
            "model": ErrorEnvelope,
            "description": "Unexpected internal server error",
        },
    },
)
def send_message(
    repository_id: str,
    conversation_id: str,
    body: SendMessageRequest,
    owner_session_id: CurrentOwnerId,
    repository_repo: Annotated[
        RepositoryRepository, Depends(get_repository_repository)
    ],
    conversation_repo: Annotated[
        ConversationRepository, Depends(get_conversation_repository)
    ],
    message_repo: Annotated[MessageRepository, Depends(get_message_repository)],
    exchange_repo: Annotated[
        ConversationExchangeRepository, Depends(get_conversation_exchange_repository)
    ],
    session_repo: Annotated[
        AnonymousSessionRepository, Depends(get_session_repository)
    ],
    answer_service: Annotated[
        GroundedAnswerService, Depends(get_grounded_answer_service)
    ],
) -> SendMessageResponse:
    """Send message in existing conversation and generate a grounded answer."""
    repo = repository_repo.get_by_id(
        owner_session_id=owner_session_id, repository_id=repository_id
    )
    if repo is None or repo.status != "ready":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository missing or not ready.",
        )

    conv = conversation_repo.get_by_id(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        conversation_id=conversation_id,
    )
    if conv is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Conversation missing or owned by another session.",
        )

    try:
        validated_question = validate_question(body.question)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    prior_messages = message_repo.list_by_conversation(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        conversation_id=conversation_id,
    )
    history = build_generation_history(prior_messages)

    start_time = time.monotonic()
    result = answer_service.generate_answer(
        owner_session_id=owner_session_id,
        repository_id=repository_id,
        question=validated_question,
        conversation_context=history,
    )
    latency_ms = max(0, int((time.monotonic() - start_time) * 1000))

    user_msg_id = generate_message_id()
    assistant_msg_id = generate_message_id()
    now = datetime.now(UTC)

    user_msg_record = MessageRecord(
        message_id=user_msg_id,
        conversation_id=conversation_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        role="user",
        content=validated_question,
        created_at=now,
        citations=(),
        evidence=(),
        insufficient_evidence=False,
    )

    assistant_msg_record = MessageRecord(
        message_id=assistant_msg_id,
        conversation_id=conversation_id,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
        role="assistant",
        content=result.answer,
        created_at=now + timedelta(microseconds=1),
        citations=result.citations,
        evidence=result.evidence,
        insufficient_evidence=result.insufficient_evidence,
    )

    updated_conv = ConversationRecord(
        conversation_id=conv.conversation_id,
        repository_id=conv.repository_id,
        owner_session_id=conv.owner_session_id,
        title=conv.title,
        created_at=conv.created_at,
        updated_at=now,
    )

    # Persist exchange atomically / via scoped compensation
    exchange_repo.append_message_exchange(
        updated_conversation=updated_conv,
        user_message=user_msg_record,
        assistant_message=assistant_msg_record,
    )

    # Best-effort session activity update after exchange persistence
    _refresh_session_activity_quietly(owner_session_id, session_repo)

    return SendMessageResponse(
        conversation_id=conversation_id,
        repository_id=repository_id,
        user_message=message_record_to_schema(user_msg_record),
        assistant_message=message_record_to_schema(assistant_msg_record),
        request_metadata=RequestMetadata(
            latency_ms=latency_ms,
            chunks_retrieved=result.chunks_retrieved,
        ),
    )

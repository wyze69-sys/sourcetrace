"""Idempotent MongoDB session retention cleanup sweeper worker."""

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo import ReturnDocument
from pymongo.database import Database

from sourcetrace.storage.mongodb import MongoStorageManager

DEFAULT_LEASE_SECONDS = 300  # 5-minute claim lease


@dataclass(frozen=True, slots=True)
class SessionCleanupReport:
    """Aggregate report summary for a session retention cleanup sweep."""

    sessions_examined: int
    sessions_cleaned: int
    sessions_failed: int
    deleted_messages: int
    deleted_conversations: int
    deleted_chunks: int
    deleted_jobs: int
    deleted_repositories: int


class SessionRetentionSweeper:
    """Idempotent, manually invokable MongoDB session-retention cleanup sweeper."""

    def __init__(
        self,
        db: Database | None = None,
        manager: MongoStorageManager | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._injected_db = db
        self._manager = manager
        self._lease_seconds = lease_seconds

    @property
    def _db(self) -> Database:
        if self._injected_db is not None:
            return self._injected_db
        if self._manager is None:
            self._manager = MongoStorageManager()
        return self._manager.get_database()

    def run_sweep(
        self,
        current_time: datetime | None = None,
        claim_token_factory: Any | None = None,
    ) -> SessionCleanupReport:
        """Execute one cleanup sweep over expired anonymous sessions."""
        now = current_time or datetime.now(UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)

        db = self._db
        sessions_col = db["anonymous_sessions"]
        repos_col = db["repositories"]
        jobs_col = db["indexing_jobs"]
        chunks_col = db["code_chunks"]
        convs_col = db["conversations"]
        msgs_col = db["messages"]

        examined = 0
        cleaned = 0
        failed = 0
        del_msgs = 0
        del_convs = 0
        del_chunks = 0
        del_jobs = 0
        del_repos = 0

        while True:
            token = (
                claim_token_factory()
                if claim_token_factory
                else f"claim_{secrets.token_urlsafe(16)}"
            )
            lease_expires = now + timedelta(seconds=self._lease_seconds)

            claim_filter = {
                "expires_at": {"$lt": now},
                "$or": [
                    {"cleanup_claim_token": {"$exists": False}},
                    {"cleanup_claim_token": None},
                    {"cleanup_claim_expires_at": {"$lt": now}},
                ],
            }
            update_doc = {
                "$set": {
                    "cleanup_claim_token": token,
                    "cleanup_claim_expires_at": lease_expires,
                    "updated_at": now,
                }
            }

            claimed_doc = sessions_col.find_one_and_update(
                claim_filter,
                update_doc,
                return_document=ReturnDocument.AFTER,
            )

            if not claimed_doc:
                break

            examined += 1
            owner_session_id = claimed_doc.get("owner_session_id")
            if not isinstance(owner_session_id, str) or not owner_session_id:
                failed += 1
                continue

            try:
                scope = {"owner_session_id": owner_session_id}

                # 1. messages
                res_msgs = msgs_col.delete_many(scope)
                count_msgs = int(res_msgs.deleted_count)

                # 2. conversations
                res_convs = convs_col.delete_many(scope)
                count_convs = int(res_convs.deleted_count)

                # 3. code_chunks
                res_chunks = chunks_col.delete_many(scope)
                count_chunks = int(res_chunks.deleted_count)

                # 4. indexing_jobs
                res_jobs = jobs_col.delete_many(scope)
                count_jobs = int(res_jobs.deleted_count)

                # 5. repositories
                res_repos = repos_col.delete_many(scope)
                count_repos = int(res_repos.deleted_count)

                # 6. anonymous_sessions
                parent_filter = {
                    "owner_session_id": owner_session_id,
                    "cleanup_claim_token": token,
                }
                res_parent = sessions_col.delete_one(parent_filter)

                if res_parent.deleted_count > 0:
                    cleaned += 1
                    del_msgs += count_msgs
                    del_convs += count_convs
                    del_chunks += count_chunks
                    del_jobs += count_jobs
                    del_repos += count_repos
                else:
                    failed += 1

            except Exception:
                failed += 1

        return SessionCleanupReport(
            sessions_examined=examined,
            sessions_cleaned=cleaned,
            sessions_failed=failed,
            deleted_messages=del_msgs,
            deleted_conversations=del_convs,
            deleted_chunks=del_chunks,
            deleted_jobs=del_jobs,
            deleted_repositories=del_repos,
        )

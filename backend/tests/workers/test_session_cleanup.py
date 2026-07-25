"""Offline unit tests for SessionRetentionSweeper."""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest

from sourcetrace.workers.session_cleanup import (
    SessionCleanupReport,
    SessionRetentionSweeper,
)


@pytest.fixture
def mock_db() -> MagicMock:
    db = MagicMock()
    # Provide mock collections for all 6 canonical collections
    collections: dict[str, MagicMock] = {
        "anonymous_sessions": MagicMock(),
        "repositories": MagicMock(),
        "indexing_jobs": MagicMock(),
        "code_chunks": MagicMock(),
        "conversations": MagicMock(),
        "messages": MagicMock(),
    }
    db.__getitem__.side_effect = lambda name: collections[name]
    db._collections = collections
    return db


def test_no_mongodb_client_connects_at_import_or_instantiation() -> None:
    sweeper = SessionRetentionSweeper()
    assert sweeper._manager is None


def test_sweeper_runs_idempotently_on_expired_session(mock_db: MagicMock) -> None:
    sessions_col = mock_db._collections["anonymous_sessions"]
    now = datetime.now(UTC)
    past = now - timedelta(days=8)

    # First sweep finds an expired session and updates it
    sessions_col.find_one_and_update.side_effect = [
        {
            "owner_session_id": "sess_expired123",
            "expires_at": past,
            "cleanup_claim_token": "claim_tok1",
        },
        None,  # Second find_one_and_update in same loop returns None
    ]

    for col in mock_db._collections.values():
        col.delete_many.return_value.deleted_count = 5
        col.delete_one.return_value.deleted_count = 1

    sweeper = SessionRetentionSweeper(db=mock_db)
    report1 = sweeper.run_sweep(current_time=now)

    assert isinstance(report1, SessionCleanupReport)
    assert report1.sessions_examined == 1
    assert report1.sessions_cleaned == 1
    assert report1.sessions_failed == 0
    assert report1.deleted_messages == 5
    assert report1.deleted_repositories == 5

    # Second sweep returns no candidate sessions
    sessions_col.find_one_and_update.side_effect = [None]
    report2 = sweeper.run_sweep(current_time=now)
    assert report2.sessions_examined == 0
    assert report2.sessions_cleaned == 0


def test_exact_expiry_boundary_not_claimed(mock_db: MagicMock) -> None:
    sessions_col = mock_db._collections["anonymous_sessions"]
    now = datetime.now(UTC)

    # find_one_and_update returns None because expires_at < now is false for expires_at == now
    sessions_col.find_one_and_update.return_value = None

    sweeper = SessionRetentionSweeper(db=mock_db)
    report = sweeper.run_sweep(current_time=now)

    assert report.sessions_examined == 0
    assert report.sessions_cleaned == 0
    # Verify find_one_and_update filter uses $lt: now
    call_args = sessions_col.find_one_and_update.call_args
    assert call_args[0][0]["expires_at"] == {"$lt": now}


def test_claimed_session_cannot_be_processed_concurrently(
    mock_db: MagicMock,
) -> None:
    sessions_col = mock_db._collections["anonymous_sessions"]
    now = datetime.now(UTC)

    # Sweeper 1 claims the session
    sessions_col.find_one_and_update.side_effect = [
        {
            "owner_session_id": "sess_claimed",
            "expires_at": now - timedelta(days=1),
            "cleanup_claim_token": "claim_token_1",
        },
        None,
    ]
    for col in mock_db._collections.values():
        col.delete_many.return_value.deleted_count = 1
        col.delete_one.return_value.deleted_count = 1

    sweeper1 = SessionRetentionSweeper(db=mock_db)
    report1 = sweeper1.run_sweep(current_time=now)
    assert report1.sessions_cleaned == 1

    # Concurrent Sweeper 2 finds no match because claim filter specifies unclaimed or expired claims
    sessions_col.find_one_and_update.side_effect = [None]
    sweeper2 = SessionRetentionSweeper(db=mock_db)
    report2 = sweeper2.run_sweep(current_time=now)
    assert report2.sessions_examined == 0


def test_failed_child_deletion_preserves_parent_session(
    mock_db: MagicMock,
) -> None:
    sessions_col = mock_db._collections["anonymous_sessions"]
    msgs_col = mock_db._collections["messages"]
    now = datetime.now(UTC)

    sessions_col.find_one_and_update.side_effect = [
        {
            "owner_session_id": "sess_err",
            "expires_at": now - timedelta(days=1),
        },
        None,
    ]

    # Messages collection raises an exception during delete_many
    msgs_col.delete_many.side_effect = RuntimeError("Database temporary error")

    sweeper = SessionRetentionSweeper(db=mock_db)
    report = sweeper.run_sweep(current_time=now)

    assert report.sessions_examined == 1
    assert report.sessions_cleaned == 0
    assert report.sessions_failed == 1

    # Verify parent session delete_one was NEVER called because child cleanup failed
    sessions_col.delete_one.assert_not_called()


def test_stale_claim_can_be_reclaimed_after_bounded_lease(
    mock_db: MagicMock,
) -> None:
    sessions_col = mock_db._collections["anonymous_sessions"]
    now = datetime.now(UTC)

    # find_one_and_update matches stale claim where cleanup_claim_expires_at < now
    sessions_col.find_one_and_update.side_effect = [
        {
            "owner_session_id": "sess_stale",
            "expires_at": now - timedelta(days=8),
            "cleanup_claim_token": "stale_token",
            "cleanup_claim_expires_at": now - timedelta(minutes=10),
        },
        None,
    ]
    for col in mock_db._collections.values():
        col.delete_many.return_value.deleted_count = 2
        col.delete_one.return_value.deleted_count = 1

    sweeper = SessionRetentionSweeper(db=mock_db)
    report = sweeper.run_sweep(current_time=now)

    assert report.sessions_examined == 1
    assert report.sessions_cleaned == 1


def test_one_failed_session_does_not_stop_cleanup_of_another_expired_session(
    mock_db: MagicMock,
) -> None:
    sessions_col = mock_db._collections["anonymous_sessions"]
    msgs_col = mock_db._collections["messages"]
    now = datetime.now(UTC)

    sessions_col.find_one_and_update.side_effect = [
        {"owner_session_id": "sess_fail", "expires_at": now - timedelta(days=1)},
        {"owner_session_id": "sess_success", "expires_at": now - timedelta(days=1)},
        None,
    ]

    # Delete fails for sess_fail, succeeds for sess_success
    def mock_delete_many(scope: dict[str, Any]) -> MagicMock:
        if scope.get("owner_session_id") == "sess_fail":
            raise RuntimeError("Storage error for sess_fail")
        res = MagicMock()
        res.deleted_count = 3
        return res

    msgs_col.delete_many.side_effect = mock_delete_many
    for col in [
        mock_db._collections["conversations"],
        mock_db._collections["code_chunks"],
        mock_db._collections["indexing_jobs"],
        mock_db._collections["repositories"],
    ]:
        col.delete_many.return_value.deleted_count = 3

    sessions_col.delete_one.return_value.deleted_count = 1

    sweeper = SessionRetentionSweeper(db=mock_db)
    report = sweeper.run_sweep(current_time=now)

    assert report.sessions_examined == 2
    assert report.sessions_cleaned == 1
    assert report.sessions_failed == 1
    assert report.deleted_messages == 3


def test_no_raw_data_or_ids_in_cleanup_report(mock_db: MagicMock) -> None:
    sessions_col = mock_db._collections["anonymous_sessions"]
    now = datetime.now(UTC)
    sessions_col.find_one_and_update.side_effect = [
        {"owner_session_id": "sess_secret123", "expires_at": now - timedelta(days=1)},
        None,
    ]
    for col in mock_db._collections.values():
        col.delete_many.return_value.deleted_count = 1
        col.delete_one.return_value.deleted_count = 1

    sweeper = SessionRetentionSweeper(db=mock_db)
    report = sweeper.run_sweep(current_time=now)

    report_str = str(report)
    assert "sess_secret123" not in report_str
    assert "ObjectId" not in report_str

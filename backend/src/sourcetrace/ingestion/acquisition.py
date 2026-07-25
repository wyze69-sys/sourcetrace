"""Managed acquired-source handoff and job-state acquisition runner.

Responsibilities
----------------
- Define AcquiredSource internal metadata dataclass and AcquiredSourceConsumer protocol.
- Compose GitHub URL download and safe ZIP extraction into managed context handoff.
- Compose ZIP source extraction into managed context handoff.
- Guarantee deletion of temporary download and extraction directories on success or failure.
- Coordinate atomic repository and indexing job state transitions during source acquisition.
- Enforce persisted source-type authority, state eligibility, and GitHub URL integrity.
- Use non-upserting atomic status transitions for job claims and state progression.
- Mask internal paths, URLs, IPs, IDs, and exceptions in public errors and persisted status.
- Implement best-effort non-upserting failure finalization strictly for confirmed-claim failures.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx

from sourcetrace.core.exceptions import (
    AcquisitionError,
    EmbeddingError,
    IndexingError,
)
from sourcetrace.ingestion.archive import ExtractionManifest, safe_extract_zip
from sourcetrace.ingestion.github_archive import safe_download_github_archive
from sourcetrace.storage.repositories import (
    IndexingJobRepository,
    RepositoryRepository,
)


@dataclass(frozen=True, slots=True)
class AcquiredSource:
    """Internal metadata for an acquired and safely extracted source code tree."""

    extraction_root: Path
    manifest: ExtractionManifest
    source_type: str


class AcquiredSourceConsumer(Protocol):
    """Protocol for synchronous consumer callbacks processing acquired source code."""

    def __call__(self, acquired_source: AcquiredSource) -> None: ...


def acquire_github_source(
    url: str,
    consumer: AcquiredSourceConsumer,
    parent_dir: str | Path | None = None,
    client: httpx.Client | None = None,
    resolver: Callable[[str], list[str]] | None = None,
) -> None:
    """Safely download a public GitHub archive, extract it, and invoke consumer callback.

    Ensures both temporary download and extraction directories are deleted
    after success or failure.
    """
    with safe_download_github_archive(
        url=url,
        parent_dir=parent_dir,
        client=client,
        resolver=resolver,
    ) as download_result:
        with safe_extract_zip(
            archive_source=download_result.archive_path,
            parent_dir=parent_dir,
        ) as extraction_result:
            source = AcquiredSource(
                extraction_root=extraction_result.target_dir,
                manifest=extraction_result.manifest,
                source_type="github",
            )
            consumer(source)


def acquire_zip_source(
    archive_source: str | Path | bytes | io.BytesIO,
    consumer: AcquiredSourceConsumer,
    parent_dir: str | Path | None = None,
) -> None:
    """Safely extract a ZIP archive and invoke consumer callback.

    Ensures temporary extraction directory is deleted after success or failure.
    """
    with safe_extract_zip(
        archive_source=archive_source,
        parent_dir=parent_dir,
    ) as extraction_result:
        source = AcquiredSource(
            extraction_root=extraction_result.target_dir,
            manifest=extraction_result.manifest,
            source_type="zip",
        )
        consumer(source)


class AcquisitionRunner:
    """Injectable runner orchestrating job-state transitions and source acquisition handoff."""

    def __init__(
        self,
        repository_repo: RepositoryRepository,
        job_repo: IndexingJobRepository,
    ) -> None:
        self._repository_repo = repository_repo
        self._job_repo = job_repo

    def run_acquisition(
        self,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        source_input: str | Path | bytes | io.BytesIO | None = None,
        consumer: AcquiredSourceConsumer | None = None,
        parent_dir: str | Path | None = None,
        client: httpx.Client | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        now: datetime | None = None,
        source_type: str | None = None,
    ) -> None:
        """Run managed acquisition job with strict atomic state transitions and error masking."""
        # 1. Scoped lookup & preflight validation (with storage failure masking)
        try:
            repository = self._repository_repo.get_by_id(owner_session_id, repository_id)
            job = self._job_repo.get_by_id(owner_session_id, job_id)
        except Exception:
            raise AcquisitionError("Acquisition failed safely.") from None

        if (
            repository is None
            or job is None
            or job.repository_id != repository_id
            or repository.owner_session_id != owner_session_id
            or job.owner_session_id != owner_session_id
        ):
            raise AcquisitionError("Resource missing or owned by another session.")

        # Require eligible starting states: repository must be pending, job must be queued
        if repository.status != "pending" or job.status != "queued":
            raise AcquisitionError("Resource missing or owned by another session.")

        # Persisted repository.source_type is authoritative
        persisted_source_type = repository.source_type
        if persisted_source_type not in {"github", "zip"}:
            raise AcquisitionError("Acquisition failed safely.")

        if source_type is not None and source_type != persisted_source_type:
            raise AcquisitionError("Acquisition failed safely.")

        # Source integrity validation before state mutation
        if persisted_source_type == "github":
            github_url = repository.github_url
            if not github_url or not isinstance(github_url, str) or not github_url.strip():
                raise AcquisitionError("Acquisition failed safely.")
            if (
                isinstance(source_input, str)
                and source_input.strip()
                and source_input != github_url
            ):
                raise AcquisitionError("Acquisition failed safely.")
            target_github_url = github_url
        else:
            if source_input is None:
                raise AcquisitionError("Acquisition failed safely.")

        if consumer is None:
            raise AcquisitionError("Acquisition failed safely.")

        if now is None:
            now_dt = datetime.now(UTC)
        elif type(now) is not datetime or isinstance(now, bool):
            raise AcquisitionError("Acquisition failed safely.")
        elif now.tzinfo is None:
            now_dt = now.replace(tzinfo=UTC)
        else:
            now_dt = now.astimezone(UTC)

        # 2. Exclusive acquisition claim (queued -> acquiring)
        try:
            claimed_job = self._job_repo.transition_status(
                owner_session_id=owner_session_id,
                job_id=job_id,
                repository_id=repository_id,
                expected_status="queued",
                new_status="acquiring",
                current_step="Acquiring source repository",
                progress_percentage=15,
                updated_at=now_dt,
            )
        except Exception:
            # Claim outcome is ambiguous; do not mutate repository or job without a confirmed claim
            raise AcquisitionError("Acquisition failed safely.") from None

        if claimed_job is None:
            # Another worker won or job is ineligible; do not finalize or mutate anything
            raise AcquisitionError("Acquisition failed safely.")


        # 3. Post-confirmed-claim processing block (finalization permitted on failure)
        started_consumer = False
        try:
            transitioned_repo = self._repository_repo.transition_status(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                expected_status="pending",
                new_status="indexing",
                updated_at=now_dt,
            )
            if transitioned_repo is None:
                raise AcquisitionError("Acquisition failed safely.")

            def consumer_wrapper(acquired_source: AcquiredSource) -> None:
                nonlocal started_consumer
                now_scan = datetime.now(UTC)
                scanning_job = self._job_repo.transition_status(
                    owner_session_id=owner_session_id,
                    job_id=job_id,
                    repository_id=repository_id,
                    expected_status="acquiring",
                    new_status="scanning",
                    current_step="Scanning source files",
                    progress_percentage=30,
                    updated_at=now_scan,
                )
                if scanning_job is None:
                    raise AcquisitionError("Acquisition failed safely.")

                started_consumer = True
                consumer(acquired_source)

            if persisted_source_type == "github":
                acquire_github_source(
                    url=target_github_url,
                    consumer=consumer_wrapper,
                    parent_dir=parent_dir,
                    client=client,
                    resolver=resolver,
                )
            else:
                acquire_zip_source(
                    archive_source=source_input,
                    consumer=consumer_wrapper,
                    parent_dir=parent_dir,
                )
        except (IndexingError, EmbeddingError) as exc:
            # The current runner has a confirmed claim; best-effort finalization is permitted
            safe_msg = (
                str(exc)
                if str(exc).strip()
                else (
                    "Indexing failed safely."
                    if started_consumer
                    else "Acquisition failed safely."
                )
            )
            self._finalize_failed_state(
                owner_session_id,
                repository_id,
                job_id,
                started_consumer=started_consumer,
                error_message=safe_msg,
            )
            raise AcquisitionError("Acquisition failed safely.") from None
        except Exception:
            safe_msg = (
                "Indexing failed safely."
                if started_consumer
                else "Acquisition failed safely."
            )
            self._finalize_failed_state(
                owner_session_id,
                repository_id,
                job_id,
                started_consumer=started_consumer,
                error_message=safe_msg,
            )
            raise AcquisitionError("Acquisition failed safely.") from None

    def _finalize_failed_state(
        self,
        owner_session_id: str,
        repository_id: str,
        job_id: str,
        started_consumer: bool = False,
        error_message: str | None = None,
    ) -> None:
        """Best-effort non-upserting attempt to record failed states on repository and job."""
        now_fail = datetime.now(UTC)

        try:
            self._repository_repo.transition_status(
                owner_session_id=owner_session_id,
                repository_id=repository_id,
                expected_status=("pending", "indexing", "ready"),
                new_status="failed",
                updated_at=now_fail,
            )
        except Exception:  # noqa: BLE001
            pass

        try:
            step_text = "Indexing failed" if started_consumer else "Acquisition failed"
            if error_message and type(error_message) is str and error_message.strip():
                err_text = error_message.strip()
            else:
                err_text = (
                    "Indexing failed safely."
                    if started_consumer
                    else "Acquisition failed safely."
                )
            self._job_repo.transition_status(
                owner_session_id=owner_session_id,
                job_id=job_id,
                repository_id=repository_id,
                expected_status=(
                    "acquiring",
                    "scanning",
                    "parsing",
                    "embedding",
                    "storing",
                    "ready",
                ),
                new_status="failed",
                current_step=step_text,
                progress_percentage=None,
                updated_at=now_fail,
                error_message=err_text,
                completed_at=now_fail,
            )
        except Exception:  # noqa: BLE001
            pass

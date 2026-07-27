"""Secure upload staging store implementation for SourceTrace ZIP uploads."""

import os
import re
import secrets
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from starlette.datastructures import UploadFile

from sourcetrace.core.exceptions import (
    UploadPayloadTooLargeError,
    UploadStagingError,
    UploadValidationError,
)
from sourcetrace.ingestion.limits import MAX_COMPRESSED_ZIP_BYTES

_TOKEN_PATTERN = re.compile(r"^stg_[A-Za-z0-9\-_]{24,64}$")
_STAGED_FILE_PATTERN = re.compile(r"^stg_[A-Za-z0-9\-_]{24,64}\.staged$")


@dataclass(frozen=True, slots=True)
class StagedUpload:
    """Opaque metadata handle representing a staged upload file."""

    token: str
    compressed_size: int


@runtime_checkable
class UploadStagingStore(Protocol):
    """Protocol for secure, temporary upload staging persistence."""

    async def stage(self, upload: UploadFile) -> StagedUpload: ...

    def resolve(self, token: str) -> Path: ...

    def delete(self, token: str) -> None: ...

    def cleanup_stale_uploads(
        self, max_age_hours: int = 24, now: datetime | None = None
    ) -> None: ...


class FileSystemUploadStagingStore:
    """Production implementation of secure filesystem upload staging."""

    def __init__(
        self,
        staging_root: Path | str | None = None,
        chunk_size: int = 65536,
    ) -> None:
        if (
            type(chunk_size) is not int
            or isinstance(chunk_size, bool)
            or chunk_size < 1
            or chunk_size > 1024 * 1024
        ):
            raise UploadStagingError("Invalid staging configuration.")

        self._chunk_size = chunk_size

        if staging_root is not None:
            if isinstance(staging_root, bool) or not isinstance(staging_root, (str, Path)):
                raise UploadStagingError("Invalid staging configuration.")
            str_root = str(staging_root)
            if "\x00" in str_root:
                raise UploadStagingError("Invalid staging configuration.")
            raw_path = Path(staging_root)
        else:
            raw_path = Path(tempfile.gettempdir()) / "sourcetrace_staging"

        try:
            if raw_path.is_symlink() or os.path.islink(raw_path):
                raise UploadStagingError("Invalid staging configuration.")
            if raw_path.exists() and not raw_path.is_dir():
                raise UploadStagingError("Invalid staging configuration.")

            raw_path.mkdir(parents=True, exist_ok=True)

            if not raw_path.is_dir() or raw_path.is_symlink() or os.path.islink(raw_path):
                raise UploadStagingError("Invalid staging configuration.")

            self._staging_root = raw_path.resolve(strict=True)
        except UploadStagingError:
            raise
        except Exception:
            raise UploadStagingError("Invalid staging configuration.") from None

    def _validate_token(self, token: str) -> str:
        if type(token) is not str or isinstance(token, bool) or not token or not token.strip():
            raise UploadValidationError("Staging token must be a non-empty string.")
        cleaned = token.strip()
        if (
            "/" in cleaned
            or "\\" in cleaned
            or "\x00" in cleaned
            or ":" in cleaned
            or ".." in cleaned
            or not _TOKEN_PATTERN.match(cleaned)
        ):
            raise UploadStagingError("Invalid or malformed staging token.")
        return cleaned

    def _resolve_path(self, token: str) -> Path:
        valid_token = self._validate_token(token)
        candidate = self._staging_root / f"{valid_token}.staged"

        if candidate.is_symlink() or os.path.islink(candidate):
            raise UploadStagingError("Staged file cannot be a symbolic link.")

        try:
            resolved = candidate.resolve(strict=False)
        except Exception:
            raise UploadStagingError("Invalid or malformed staging token.") from None

        try:
            if resolved.parent != self._staging_root and not resolved.is_relative_to(
                self._staging_root
            ):
                raise UploadStagingError("Staging token resolves outside staging root.")
        except Exception:
            raise UploadStagingError("Staging token resolves outside staging root.") from None

        if resolved.is_symlink() or os.path.islink(resolved):
            raise UploadStagingError("Staged file cannot be a symbolic link.")

        if resolved.exists() and resolved.is_dir():
            raise UploadStagingError("Staged path is a directory.")

        return resolved

    def _delete_file_quietly(self, path: Path) -> None:
        try:
            if (
                not os.path.islink(path)
                and not path.is_symlink()
                and path.exists()
                and not path.is_dir()
            ):
                path.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def cleanup_stale_uploads(self, max_age_hours: int = 24, now: datetime | None = None) -> None:
        """Best-effort cleanup of staged files older than threshold."""
        if type(max_age_hours) is not int or isinstance(max_age_hours, bool) or max_age_hours < 24:
            return

        if now is not None:
            if type(now) is not datetime:
                return
            if now.tzinfo is None:
                now_utc = now.replace(tzinfo=UTC)
            else:
                now_utc = now.astimezone(UTC)
        else:
            now_utc = datetime.now(UTC)

        try:
            root = self._staging_root
            if not root.exists() or not root.is_dir() or root.is_symlink() or os.path.islink(root):
                return
            cutoff_timestamp = (now_utc - timedelta(hours=max_age_hours)).timestamp()

            for entry in root.iterdir():
                try:
                    if (
                        entry.is_symlink()
                        or os.path.islink(entry)
                        or not entry.is_file()
                        or entry.is_dir()
                    ):
                        continue
                    if not _STAGED_FILE_PATTERN.match(entry.name):
                        continue
                    stat = entry.stat()
                    if stat.st_mtime < cutoff_timestamp:
                        entry.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    continue
        except Exception:  # noqa: BLE001
            pass

    async def stage(self, upload: UploadFile) -> StagedUpload:
        """Stream upload incrementally to staging, enforce limits, and validate ZIP format."""
        self.cleanup_stale_uploads(max_age_hours=24)

        staged_path: Path | None = None
        fd: int | None = None
        token: str = ""

        for attempt in range(3):
            token_candidate = f"stg_{secrets.token_urlsafe(24)}"
            try:
                cand_path = self._resolve_path(token_candidate)
                fd = os.open(
                    cand_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                staged_path = cand_path
                token = token_candidate
                break
            except FileExistsError:
                if attempt == 2:
                    raise UploadStagingError("Failed to allocate staging slot.") from None
                continue
            except UploadStagingError:
                raise
            except Exception:
                raise UploadStagingError("Failed to write upload to staging.") from None

        if staged_path is None or fd is None:
            raise UploadStagingError("Failed to allocate staging slot.")

        total_bytes = 0
        file_obj: Any = None
        try:
            try:
                file_obj = os.fdopen(fd, "wb")
            except Exception:
                try:
                    os.close(fd)
                except Exception:  # noqa: BLE001
                    pass
                raise UploadStagingError("Failed to write upload to staging.") from None

            try:
                while True:
                    try:
                        chunk = await upload.read(self._chunk_size)
                    except Exception:
                        raise UploadStagingError("Failed to write upload to staging.") from None

                    if type(chunk) is not bytes:
                        raise UploadStagingError("Failed to write upload to staging.")
                    if len(chunk) > self._chunk_size:
                        raise UploadStagingError("Failed to write upload to staging.")
                    if not chunk:
                        break

                    projected_total = total_bytes + len(chunk)
                    if projected_total > MAX_COMPRESSED_ZIP_BYTES:
                        raise UploadPayloadTooLargeError(
                            "Upload file size exceeds maximum limit (25 MB)."
                        )

                    written = file_obj.write(chunk)
                    if written is None or type(written) is not int or written != len(chunk):
                        raise UploadStagingError("Failed to write upload to staging.")
                    total_bytes = projected_total
                file_obj.flush()
            finally:
                try:
                    file_obj.close()
                except Exception:  # noqa: BLE001
                    pass
        except UploadPayloadTooLargeError:
            self._delete_file_quietly(staged_path)
            raise
        except UploadStagingError:
            self._delete_file_quietly(staged_path)
            raise
        except Exception:
            self._delete_file_quietly(staged_path)
            raise UploadStagingError("Failed to write upload to staging.") from None
        finally:
            try:
                await upload.close()
            except Exception:  # noqa: BLE001
                pass

        if total_bytes == 0:
            self._delete_file_quietly(staged_path)
            raise UploadValidationError("Uploaded archive is empty.")

        try:
            if not zipfile.is_zipfile(staged_path):
                self._delete_file_quietly(staged_path)
                raise UploadValidationError(
                    "Uploaded archive is corrupt, empty, or not a valid ZIP file."
                )
            with zipfile.ZipFile(staged_path, "r") as zf:
                info_list = zf.infolist()
                if not info_list:
                    self._delete_file_quietly(staged_path)
                    raise UploadValidationError(
                        "Uploaded archive is corrupt, empty, or not a valid ZIP file."
                    )
        except UploadValidationError:
            raise
        except Exception:
            self._delete_file_quietly(staged_path)
            raise UploadValidationError(
                "Uploaded archive is corrupt, empty, or not a valid ZIP file."
            ) from None

        return StagedUpload(token=token, compressed_size=total_bytes)

    def resolve(self, token: str) -> Path:
        """Resolve an opaque staging token to a validated physical Path."""
        return self._resolve_path(token)

    def delete(self, token: str) -> None:
        """Delete a staged upload file safely and idempotently."""
        try:
            staged_path = self._resolve_path(token)
            self._delete_file_quietly(staged_path)
        except Exception:  # noqa: BLE001
            pass

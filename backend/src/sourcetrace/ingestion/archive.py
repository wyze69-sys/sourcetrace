"""Safe offline ZIP archive inspection and extraction.

Responsibilities
----------------
- Enforce immutable ingestion security limits before and during extraction:
  * Compressed archive size (<= MAX_COMPRESSED_ZIP_BYTES)
  * Regular file count (<= MAX_FILES)
  * Single file uncompressed size (<= MAX_SINGLE_FILE_BYTES)
  * Total cumulative uncompressed size (<= MAX_EXTRACTED_ZIP_BYTES)
  * Compression ratio (<= MAX_COMPRESSION_RATIO for non-empty members)
- Reject unsafe member paths (traversal, absolute, drive letters, UNC, NUL)
- Reject forbidden entry types (symlinks, devices, FIFOs, sockets)
- Perform final resolved path containment check against extraction root
- Manage temporary extraction directory lifecycle with guaranteed cleanup
- Return safe ExtractionManifest with safe metadata only
"""

from __future__ import annotations

import io
import shutil
import stat
import tempfile
import zipfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sourcetrace.core.exceptions import (
    IngestionLimitError,
    InvalidArchiveError,
    UnsafeArchiveMemberPathError,
)
from sourcetrace.ingestion.limits import (
    MAX_COMPRESSED_ZIP_BYTES,
    MAX_COMPRESSION_RATIO,
    MAX_EXTRACTED_ZIP_BYTES,
    MAX_FILES,
    MAX_SINGLE_FILE_BYTES,
)
from sourcetrace.ingestion.validation import validate_archive_member_path

# Chunk size for streaming extraction (64 KiB)
_STREAM_CHUNK_SIZE: int = 64 * 1024


@dataclass(frozen=True)
class ExtractionManifest:
    """Safe metadata summary of extracted archive contents."""

    file_count: int
    total_extracted_bytes: int
    relative_paths: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionResult:
    """Context wrapper containing target directory and safe manifest."""

    target_dir: Path
    manifest: ExtractionManifest

    def __iter__(self) -> Iterator[Path | ExtractionManifest]:
        yield self.target_dir
        yield self.manifest


def _check_compression_ratio(uncompressed: int, compressed: int) -> None:
    """Enforce maximum compression ratio using pure integer arithmetic.

    - 0 compressed and 0 uncompressed -> allowed (empty member)
    - 0 compressed and positive uncompressed -> rejected
    - uncompressed > MAX_COMPRESSION_RATIO * compressed -> rejected
    """
    if uncompressed == 0:
        return
    if compressed == 0 or uncompressed > MAX_COMPRESSION_RATIO * compressed:
        raise IngestionLimitError("Archive member exceeds an ingestion limit.")


def _check_member_type(member: zipfile.ZipInfo) -> None:
    """Reject symlinks, devices, FIFOs, sockets, and unknown file types.

    Raises
    ------
    UnsafeArchiveMemberPathError
        When the entry type is not a regular file or directory.
    """
    mode = member.external_attr >> 16
    if mode != 0:
        file_type = stat.S_IFMT(mode)
        if file_type == stat.S_IFLNK:
            raise UnsafeArchiveMemberPathError("Archive member violates safety policy.")
        if file_type in (stat.S_IFCHR, stat.S_IFBLK, stat.S_IFIFO, stat.S_IFSOCK):
            raise UnsafeArchiveMemberPathError("Archive member violates safety policy.")
        if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise UnsafeArchiveMemberPathError("Archive member violates safety policy.")


def _is_dir_entry(member: zipfile.ZipInfo) -> bool:
    """Determine if a ZipInfo entry represents a directory."""
    if member.is_dir() or member.filename.endswith("/"):
        return True
    mode = member.external_attr >> 16
    if mode != 0 and stat.S_IFMT(mode) == stat.S_IFDIR:
        return True
    return False


@contextmanager
def safe_extract_zip(
    archive_source: str | Path | bytes | io.BytesIO,
    parent_dir: str | Path | None = None,
) -> Iterator[ExtractionResult]:
    """Managed context manager for safe ZIP archive extraction.

    Creates a unique temporary directory, validates and extracts archive contents
    safely while enforcing all ingestion security limits, yields an
    ``ExtractionResult`` containing the extraction directory and safe metadata
    manifest, and guarantees deletion of the extraction directory on exit or failure.

    Parameters
    ----------
    archive_source : str | Path | bytes | io.BytesIO
        Archive source file path or raw bytes / BytesIO buffer.
    parent_dir : str | Path | None
        Optional parent directory under which the unique extraction folder
        is created.  The context manager will ONLY delete the unique folder it
        created, never the parent directory.

    Yields
    ------
    ExtractionResult
        Object containing ``target_dir`` (Path) and ``manifest`` (ExtractionManifest).

    Raises
    ------
    InvalidArchiveError
        When the archive does not exist, is empty, is corrupt, or cannot be opened as a ZIP.
    IngestionLimitError
        When any size, file count, or compression ratio limit is breached.
    UnsafeArchiveMemberPathError
        When an archive entry contains unsafe paths, traversal, or forbidden member types.
    """
    if parent_dir is not None:
        parent_path = Path(parent_dir)
        if not parent_path.exists() or not parent_path.is_dir():
            raise InvalidArchiveError("Archive source is invalid.")
        temp_dir_str = tempfile.mkdtemp(prefix="sourcetrace_zip_", dir=str(parent_path))
    else:
        temp_dir_str = tempfile.mkdtemp(prefix="sourcetrace_zip_")

    target_root = Path(temp_dir_str).resolve()

    try:
        zf = _open_and_validate_compressed_archive(archive_source)
        try:
            manifest = _perform_safe_extraction(zf, target_root)
        finally:
            zf.close()

        yield ExtractionResult(target_dir=target_root, manifest=manifest)
    finally:
        shutil.rmtree(target_root, ignore_errors=True)


def _open_and_validate_compressed_archive(
    archive_source: str | Path | bytes | io.BytesIO,
) -> zipfile.ZipFile:
    """Pre-validate compressed archive size and open ZipFile safely."""
    zip_errs = (
        zipfile.BadZipFile,
        zlib.error,
        EOFError,
        OSError,
        KeyError,
        RuntimeError,
        ValueError,
    )

    if isinstance(archive_source, (str, Path)):

        path = Path(archive_source)
        if not path.exists() or not path.is_file():
            raise InvalidArchiveError("Archive source is unavailable.")
        file_size = path.stat().st_size
        if file_size > MAX_COMPRESSED_ZIP_BYTES:
            raise IngestionLimitError("Archive member exceeds an ingestion limit.")
        try:
            return zipfile.ZipFile(path, "r")
        except zip_errs as err:
            raise InvalidArchiveError("Archive source is invalid.") from err

    elif isinstance(archive_source, bytes):
        if len(archive_source) > MAX_COMPRESSED_ZIP_BYTES:
            raise IngestionLimitError("Archive member exceeds an ingestion limit.")
        try:
            return zipfile.ZipFile(io.BytesIO(archive_source), "r")
        except zip_errs as err:
            raise InvalidArchiveError("Archive source is invalid.") from err

    elif isinstance(archive_source, io.BytesIO):
        buffer = archive_source.getvalue()
        if len(buffer) > MAX_COMPRESSED_ZIP_BYTES:
            raise IngestionLimitError("Archive member exceeds an ingestion limit.")
        try:
            archive_source.seek(0)
            return zipfile.ZipFile(archive_source, "r")
        except zip_errs as err:
            raise InvalidArchiveError("Archive source is invalid.") from err

    else:
        raise InvalidArchiveError("Archive source is invalid.")


def _perform_safe_extraction(
    zf: zipfile.ZipFile,
    target_root: Path,
) -> ExtractionManifest:
    """Pre-check headers, validate member paths/types, and extract safely."""
    try:
        infolist = zf.infolist()
    except Exception as err:
        raise InvalidArchiveError("Archive source is invalid.") from err

    regular_members: list[tuple[zipfile.ZipInfo, str]] = []
    dir_members: list[tuple[zipfile.ZipInfo, str]] = []
    pre_total_uncompressed = 0

    for member in infolist:
        _check_member_type(member)
        try:
            safe_relpath = validate_archive_member_path(member.filename)
        except UnsafeArchiveMemberPathError as err:
            raise UnsafeArchiveMemberPathError(
                "Archive member violates safety policy."
            ) from err

        if _is_dir_entry(member):
            dir_members.append((member, safe_relpath))
        else:
            if member.file_size > MAX_SINGLE_FILE_BYTES:
                raise IngestionLimitError("Archive member exceeds an ingestion limit.")

            _check_compression_ratio(member.file_size, member.compress_size)

            pre_total_uncompressed += member.file_size
            regular_members.append((member, safe_relpath))

    if len(regular_members) > MAX_FILES:
        raise IngestionLimitError("Archive member exceeds an ingestion limit.")

    if pre_total_uncompressed > MAX_EXTRACTED_ZIP_BYTES:
        raise IngestionLimitError("Archive member exceeds an ingestion limit.")

    for _, safe_relpath in dir_members:
        target_dir = (target_root / safe_relpath).resolve()
        try:
            target_dir.relative_to(target_root)
        except ValueError as err:
            raise UnsafeArchiveMemberPathError(
                "Archive member violates safety policy."
            ) from err
        target_dir.mkdir(parents=True, exist_ok=True)

    extracted_paths: list[str] = []
    total_extracted_bytes = 0

    for member, safe_relpath in regular_members:
        target_file = (target_root / safe_relpath).resolve()
        try:
            target_file.relative_to(target_root)
        except ValueError as err:
            raise UnsafeArchiveMemberPathError(
                "Archive member violates safety policy."
            ) from err

        target_file.parent.mkdir(parents=True, exist_ok=True)

        member_bytes = 0
        try:
            with zf.open(member, "r") as src, open(target_file, "wb") as dst:
                while True:
                    chunk = src.read(_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    chunk_len = len(chunk)
                    member_bytes += chunk_len
                    total_extracted_bytes += chunk_len

                    if member_bytes > MAX_SINGLE_FILE_BYTES:
                        raise IngestionLimitError("Archive member exceeds an ingestion limit.")

                    if total_extracted_bytes > MAX_EXTRACTED_ZIP_BYTES:
                        raise IngestionLimitError("Archive member exceeds an ingestion limit.")

                    _check_compression_ratio(member_bytes, member.compress_size)

                    dst.write(chunk)
        except (IngestionLimitError, UnsafeArchiveMemberPathError):
            raise
        except Exception as err:
            raise InvalidArchiveError(
                "Archive extraction failed safely."
            ) from err

        extracted_paths.append(safe_relpath)

    return ExtractionManifest(
        file_count=len(extracted_paths),
        total_extracted_bytes=total_extracted_bytes,
        relative_paths=tuple(sorted(extracted_paths)),
    )

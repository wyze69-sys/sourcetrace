import asyncio
import io
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from starlette.datastructures import UploadFile

from sourcetrace.core.exceptions import (
    UploadPayloadTooLargeError,
    UploadStagingError,
    UploadValidationError,
)
from sourcetrace.ingestion.limits import MAX_COMPRESSED_ZIP_BYTES
from sourcetrace.ingestion.upload_staging import (
    FileSystemUploadStagingStore,
    StagedUpload,
)


def create_minimal_zip_bytes() -> bytes:
    """Create minimal valid in-memory ZIP archive bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("test.txt", "hello world")
    return buf.getvalue()


class MockAsyncStreamUploadFile(UploadFile):
    """Test helper simulating chunked streaming reads of UploadFile."""

    def __init__(self, content: bytes, filename: str = "test.zip", chunk_size: int = 65536) -> None:
        super().__init__(file=io.BytesIO(content), filename=filename)
        self._raw_bytes = content
        self._offset = 0
        self._chunk_size = chunk_size
        self.read_calls: list[int | None] = []

    async def read(self, size: int = -1) -> bytes:
        self.read_calls.append(size)
        if self._offset >= len(self._raw_bytes):
            return b""
        if size == -1:
            chunk = self._raw_bytes[self._offset :]
            self._offset = len(self._raw_bytes)
            return chunk
        chunk = self._raw_bytes[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


class FileStreamUploadFile(UploadFile):
    """Memory-bounded streaming UploadFile reading incrementally from a disk file."""

    def __init__(
        self, file_path: Path, filename: str = "stream.zip", extra_bytes: bytes = b""
    ) -> None:
        file_obj = open(file_path, "rb")
        super().__init__(file=file_obj, filename=filename)
        self._extra_bytes = extra_bytes
        self._extra_offset = 0

    async def read(self, size: int = -1) -> bytes:
        chunk = self.file.read(size)
        if chunk:
            return chunk
        if self._extra_offset < len(self._extra_bytes):
            if size == -1:
                res = self._extra_bytes[self._extra_offset :]
                self._extra_offset = len(self._extra_bytes)
                return res
            res = self._extra_bytes[self._extra_offset : self._extra_offset + size]
            self._extra_offset += len(res)
            return res
        return b""


def create_exact_25mb_zip_on_disk(target_path: Path) -> None:
    """Create a 25 MB valid ZIP archive directly on disk to keep test memory bounded."""
    dummy_size = MAX_COMPRESSED_ZIP_BYTES - 150
    with open(target_path, "wb") as f:
        with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("data.bin", b"A" * dummy_size)
    actual_len = target_path.stat().st_size
    padding = MAX_COMPRESSED_ZIP_BYTES - actual_len
    if padding != 0:
        with open(target_path, "wb") as f:
            with zipfile.ZipFile(f, "w", compression=zipfile.ZIP_STORED) as zf:
                zf.writestr("data.bin", b"A" * (dummy_size + padding))


def test_stage_valid_zip_success(tmp_path: Path) -> None:
    async def run() -> None:
        zip_bytes = create_minimal_zip_bytes()
        upload = MockAsyncStreamUploadFile(zip_bytes, filename="my_archive.zip")

        store = FileSystemUploadStagingStore(staging_root=tmp_path)
        result = await store.stage(upload)

        assert isinstance(result, StagedUpload)
        assert result.token.startswith("stg_")
        assert result.compressed_size == len(zip_bytes)

        assert "my_archive" not in result.token
        assert "zip" not in result.token
        assert "/" not in result.token
        assert "\\" not in result.token

        staged_path = store.resolve(result.token)
        assert staged_path.exists()
        assert staged_path.is_relative_to(tmp_path)
        assert staged_path.read_bytes() == zip_bytes

        store.delete(result.token)
        assert not staged_path.exists()

    asyncio.run(run())


@pytest.mark.parametrize(
    "bad_chunk_size",
    [-10, 0, True, False, 1.5, "65536", 1024 * 1024 + 1, object()],
)
def test_staging_config_chunk_size_validation(tmp_path: Path, bad_chunk_size: Any) -> None:
    with pytest.raises(UploadStagingError) as exc_info:
        FileSystemUploadStagingStore(staging_root=tmp_path, chunk_size=bad_chunk_size)
    assert "Invalid staging configuration" in str(exc_info.value)


@pytest.mark.parametrize(
    "bad_root",
    [True, False, 123, "\x00invalid_nul_path"],
)
def test_staging_config_root_validation(bad_root: Any) -> None:
    with pytest.raises(UploadStagingError) as exc_info:
        FileSystemUploadStagingStore(staging_root=bad_root)
    assert "Invalid staging configuration" in str(exc_info.value)


def test_staging_config_root_file_rejected(tmp_path: Path) -> None:
    file_root = tmp_path / "not_a_dir.tmp"
    file_root.write_bytes(b"content")

    with pytest.raises(UploadStagingError) as exc_info:
        FileSystemUploadStagingStore(staging_root=file_root)
    assert "Invalid staging configuration" in str(exc_info.value)


def test_non_decompressing_zip_preflight_does_not_read_member_stream(tmp_path: Path) -> None:
    async def run() -> None:
        zip_bytes = create_minimal_zip_bytes()
        upload = MockAsyncStreamUploadFile(zip_bytes, filename="preflight.zip")

        store = FileSystemUploadStagingStore(staging_root=tmp_path)
        testzip_msg = "testzip() must not be called!"
        read_msg = "read() must not be called!"
        open_msg = "open() must not be called!"
        with patch.object(zipfile.ZipFile, "testzip", side_effect=AssertionError(testzip_msg)):
            with patch.object(zipfile.ZipFile, "read", side_effect=AssertionError(read_msg)):
                with patch.object(zipfile.ZipFile, "open", side_effect=AssertionError(open_msg)):
                    staged = await store.stage(upload)
                    assert staged.token.startswith("stg_")

    asyncio.run(run())


def test_exclusive_create_collision_retries_and_does_not_overwrite(tmp_path: Path) -> None:
    async def run() -> None:
        store = FileSystemUploadStagingStore(staging_root=tmp_path)

        colliding_token = "stg_" + "A" * 32
        existing_path = tmp_path / f"{colliding_token}.staged"
        existing_content = b"original pre-existing data do not overwrite"
        existing_path.write_bytes(existing_content)

        attempts = 0

        def mock_token(nbytes: int = 24) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return colliding_token[4:]
            return f"success_token_{attempts}_1234567890"

        with patch("secrets.token_urlsafe", side_effect=mock_token):
            zip_bytes = create_minimal_zip_bytes()
            upload = MockAsyncStreamUploadFile(zip_bytes, filename="test.zip")
            staged = await store.stage(upload)

            assert attempts == 2
            assert existing_path.read_bytes() == existing_content
            assert staged.token.startswith("stg_success_token_")

    asyncio.run(run())


@pytest.mark.parametrize(
    "hostile_chunk",
    [
        pytest.param("not_bytes_string", id="str"),
        pytest.param(bytearray(b"bytearray_chunk"), id="bytearray"),
        pytest.param(object(), id="object"),
        pytest.param(12345, id="int"),
        pytest.param(True, id="bool"),
        pytest.param(b"X" * 65537, id="oversized_bytes"),
    ],
)
def test_hostile_returned_chunks_rejected_and_never_written(
    tmp_path: Path, hostile_chunk: Any
) -> None:
    async def run() -> None:
        store = FileSystemUploadStagingStore(staging_root=tmp_path, chunk_size=65536)

        class HostileUpload(UploadFile):
            def __init__(self) -> None:
                super().__init__(file=io.BytesIO(b"data"), filename="test.zip")

            async def read(self, size: int = -1) -> Any:
                return hostile_chunk

        upload = HostileUpload()

        write_calls: list[bytes] = []
        real_fdopen = os.fdopen

        class TrackingFile:
            def __init__(self, real_f: Any) -> None:
                self._f = real_f

            def write(self, b: bytes) -> int:
                write_calls.append(b)
                return self._f.write(b)

            def flush(self) -> None:
                self._f.flush()

            def close(self) -> None:
                self._f.close()

        def mock_fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            f = real_fdopen(fd, mode, *args, **kwargs)
            return TrackingFile(f)

        with patch("os.fdopen", side_effect=mock_fdopen):
            with pytest.raises(UploadStagingError) as exc_info:
                await store.stage(upload)
            assert "Failed to write upload" in str(exc_info.value)
            assert len(write_calls) == 0

        assert len(list(tmp_path.glob("*.staged"))) == 0

    asyncio.run(run())


def test_fdopen_failure_closes_raw_descriptor(tmp_path: Path) -> None:
    async def run() -> None:
        store = FileSystemUploadStagingStore(staging_root=tmp_path)
        upload = MockAsyncStreamUploadFile(create_minimal_zip_bytes(), filename="test.zip")

        close_calls: list[int] = []
        real_close = os.close

        def mock_close(fd: int) -> None:
            close_calls.append(fd)
            real_close(fd)

        with patch("os.fdopen", side_effect=OSError("fdopen failed")):
            with patch("os.close", side_effect=mock_close):
                with pytest.raises(UploadStagingError) as exc_info:
                    await store.stage(upload)
                assert "Failed to write upload" in str(exc_info.value)
                assert len(close_calls) > 0
                assert len(list(tmp_path.glob("*.staged"))) == 0

    asyncio.run(run())


def test_partial_write_detected_and_cleans_file(tmp_path: Path) -> None:
    async def run() -> None:
        store = FileSystemUploadStagingStore(staging_root=tmp_path)
        zip_bytes = create_minimal_zip_bytes()
        upload = MockAsyncStreamUploadFile(zip_bytes, filename="test.zip")

        class PartialWriteFile:
            def __init__(self, real_f: Any) -> None:
                self._f = real_f

            def write(self, b: bytes) -> int:
                return 1

            def flush(self) -> None:
                self._f.flush()

            def close(self) -> None:
                self._f.close()

        real_fdopen = os.fdopen

        def mock_fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            f = real_fdopen(fd, mode, *args, **kwargs)
            return PartialWriteFile(f)

        with patch("os.fdopen", side_effect=mock_fdopen):
            with pytest.raises(UploadStagingError) as exc_info:
                await store.stage(upload)
            assert "Failed to write upload" in str(exc_info.value)
            assert len(list(tmp_path.glob("*.staged"))) == 0

    asyncio.run(run())


def test_flush_failure_cleans_staged_file(tmp_path: Path) -> None:
    async def run() -> None:
        store = FileSystemUploadStagingStore(staging_root=tmp_path)
        upload = MockAsyncStreamUploadFile(create_minimal_zip_bytes(), filename="test.zip")

        class FailingFlushFile:
            def __init__(self, real_f: Any) -> None:
                self._f = real_f

            def write(self, b: bytes) -> int:
                return self._f.write(b)

            def flush(self) -> None:
                raise OSError("Flush failed disk full")

            def close(self) -> None:
                self._f.close()

        real_fdopen = os.fdopen

        def mock_fdopen(fd: int, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            f = real_fdopen(fd, mode, *args, **kwargs)
            return FailingFlushFile(f)

        with patch("os.fdopen", side_effect=mock_fdopen):
            with pytest.raises(UploadStagingError) as exc_info:
                await store.stage(upload)
            assert "Failed to write upload" in str(exc_info.value)
            assert len(list(tmp_path.glob("*.staged"))) == 0

    asyncio.run(run())


def test_upload_close_failure_does_not_replace_primary_error(tmp_path: Path) -> None:
    async def run() -> None:
        store = FileSystemUploadStagingStore(staging_root=tmp_path)

        class CloseFailingUpload(UploadFile):
            def __init__(self) -> None:
                super().__init__(file=io.BytesIO(b"not_a_zip"), filename="test.zip")
                self._read = False

            async def read(self, size: int = -1) -> bytes:
                if not self._read:
                    self._read = True
                    return b"not_a_zip"
                return b""

            async def close(self) -> None:
                raise RuntimeError("Upload close failed")

        upload = CloseFailingUpload()
        with pytest.raises(UploadValidationError):
            await store.stage(upload)

        assert len(list(tmp_path.glob("*.staged"))) == 0

    asyncio.run(run())


def test_exact_limit_accepted_and_limit_plus_one_rejected_bounded_memory(tmp_path: Path) -> None:
    async def run() -> None:
        store = FileSystemUploadStagingStore(staging_root=tmp_path)

        zip_source_file = tmp_path / "disk_25mb.zip"
        create_exact_25mb_zip_on_disk(zip_source_file)
        assert zip_source_file.stat().st_size == MAX_COMPRESSED_ZIP_BYTES

        upload_exact = FileStreamUploadFile(zip_source_file, filename="exact.zip")
        staged = await store.stage(upload_exact)
        assert staged.compressed_size == MAX_COMPRESSED_ZIP_BYTES
        store.delete(staged.token)

        upload_over = FileStreamUploadFile(zip_source_file, filename="over.zip", extra_bytes=b"X")

        target_fn = (
            "sourcetrace.ingestion.upload_staging.FileSystemUploadStagingStore._delete_file_quietly"
        )
        with patch(target_fn, wraps=store._delete_file_quietly) as mock_delete:
            with pytest.raises(UploadPayloadTooLargeError):
                await store.stage(upload_over)
            assert mock_delete.called

        assert len(list(tmp_path.glob("*.staged"))) == 0

    asyncio.run(run())


def test_resolve_symlink_candidate_rejected(tmp_path: Path) -> None:
    store = FileSystemUploadStagingStore(staging_root=tmp_path)
    target = tmp_path / "target.txt"
    target.write_bytes(b"secret")

    symlink = tmp_path / "stg_12345678901234567890123456789012.staged"
    try:
        os.symlink(target, symlink)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this environment.")

    with pytest.raises(UploadStagingError) as exc_info:
        store.resolve("stg_12345678901234567890123456789012")
    assert "symbolic link" in str(exc_info.value)


def test_resolve_directory_candidate_rejected(tmp_path: Path) -> None:
    store = FileSystemUploadStagingStore(staging_root=tmp_path)
    dir_candidate = tmp_path / "stg_dir12345678901234567890123456.staged"
    dir_candidate.mkdir(parents=True, exist_ok=True)

    with pytest.raises(UploadStagingError) as exc_info:
        store.resolve("stg_dir12345678901234567890123456")
    assert "directory" in str(exc_info.value)


def test_stale_cleanup_symlink_ignored(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    old_time = (now - timedelta(hours=30)).timestamp()

    target = tmp_path / "target.txt"
    target.write_bytes(b"target")

    old_symlink = tmp_path / "stg_symlink12345678901234567890123.staged"
    try:
        os.symlink(target, old_symlink)
        os.utime(old_symlink, (old_time, old_time), follow_symlinks=False)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this environment.")

    store = FileSystemUploadStagingStore(staging_root=tmp_path)
    store.cleanup_stale_uploads(max_age_hours=24, now=now)

    assert target.exists()
    assert old_symlink.exists() or os.path.islink(old_symlink)


@pytest.mark.parametrize(
    "bad_max_age",
    [0, -1, 23, True, False, 24.5, "24"],
)
def test_stale_cleanup_invalid_max_age(tmp_path: Path, bad_max_age: Any) -> None:
    store = FileSystemUploadStagingStore(staging_root=tmp_path)
    old_file = tmp_path / "stg_old1234567890123456789012345678.staged"
    old_file.write_bytes(b"old")
    old_time = (datetime.now(UTC) - timedelta(hours=30)).timestamp()
    os.utime(old_file, (old_time, old_time))

    store.cleanup_stale_uploads(max_age_hours=bad_max_age)
    assert old_file.exists()


@pytest.mark.parametrize(
    "bad_now",
    [12345, "2026-07-24", True, [datetime.now(UTC)]],
)
def test_stale_cleanup_invalid_now(tmp_path: Path, bad_now: Any) -> None:
    store = FileSystemUploadStagingStore(staging_root=tmp_path)
    old_file = tmp_path / "stg_old1234567890123456789012345678.staged"
    old_file.write_bytes(b"old")
    old_time = (datetime.now(UTC) - timedelta(hours=30)).timestamp()
    os.utime(old_file, (old_time, old_time))

    store.cleanup_stale_uploads(max_age_hours=24, now=bad_now)
    assert old_file.exists()

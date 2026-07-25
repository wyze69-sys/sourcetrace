"""Offline adversarial tests for ZIP archive inspection, extraction, and cleanup."""

from __future__ import annotations

import io
import stat
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sourcetrace.core.exceptions import (
    IngestionLimitError,
    InvalidArchiveError,
    UnsafeArchiveMemberPathError,
)
from sourcetrace.ingestion.archive import (
    ExtractionManifest,
    ExtractionResult,
    _check_compression_ratio,
    _perform_safe_extraction,
    safe_extract_zip,
)
from sourcetrace.ingestion.limits import (
    MAX_COMPRESSED_ZIP_BYTES,
    MAX_FILES,
    MAX_SINGLE_FILE_BYTES,
)


def _make_zip_bytes(
    files: dict[str, bytes | str] | None = None,
    custom_entries: list[tuple[zipfile.ZipInfo, bytes]] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """Helper to build in-memory ZIP archives for tests."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        if files:
            for name, content in files.items():
                data = content.encode("utf-8") if isinstance(content, str) else content
                zf.writestr(name, data)
        if custom_entries:
            for info, data in custom_entries:
                zf.writestr(info, data)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 1. Valid Archive & Metadata Manifest
# ---------------------------------------------------------------------------


def test_valid_archive_extraction_and_manifest(tmp_path: Path) -> None:
    """Verify safe extraction of valid nested archive and manifest metadata."""
    zip_bytes = _make_zip_bytes(
        {
            "src/main.py": "print('hello')",
            "src/utils/helpers.py": "def add(a, b): return a + b",
            "README.md": "# Demo Project",
            "empty.txt": "",
        }
    )

    archive_file = tmp_path / "sample.zip"
    archive_file.write_bytes(zip_bytes)

    with safe_extract_zip(archive_file) as res:
        assert isinstance(res, ExtractionResult)
        assert res.target_dir.exists()
        assert res.target_dir.is_dir()

        target_dir, manifest = res
        assert isinstance(manifest, ExtractionManifest)
        assert manifest.file_count == 4
        assert manifest.relative_paths == (
            "README.md",
            "empty.txt",
            "src/main.py",
            "src/utils/helpers.py",
        )
        assert manifest.total_extracted_bytes == len("print('hello')") + len(
            "def add(a, b): return a + b"
        ) + len("# Demo Project")

        assert (target_dir / "src/main.py").read_text(encoding="utf-8") == "print('hello')"
        assert (target_dir / "README.md").read_text(encoding="utf-8") == "# Demo Project"
        assert (target_dir / "empty.txt").read_bytes() == b""

    assert not target_dir.exists()


def test_bytes_and_bytesio_input_sources() -> None:
    """Verify safe_extract_zip accepts bytes and io.BytesIO sources."""
    zip_bytes = _make_zip_bytes({"test.txt": "content"})

    with safe_extract_zip(zip_bytes) as (target_dir, manifest):
        assert manifest.file_count == 1
        assert (target_dir / "test.txt").read_text(encoding="utf-8") == "content"

    with safe_extract_zip(io.BytesIO(zip_bytes)) as (target_dir, manifest):
        assert manifest.file_count == 1
        assert (target_dir / "test.txt").read_text(encoding="utf-8") == "content"


# ---------------------------------------------------------------------------
# 2. Limit Controls (Pre-check and Streaming)
# ---------------------------------------------------------------------------


def test_oversized_compressed_archive(tmp_path: Path) -> None:
    """Rejects compressed archives exceeding MAX_COMPRESSED_ZIP_BYTES."""
    oversized_bytes = b"PK\x03\x04" + b"0" * (MAX_COMPRESSED_ZIP_BYTES + 100)
    oversized_file = tmp_path / "huge.zip"
    oversized_file.write_bytes(oversized_bytes)

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        with safe_extract_zip(oversized_file):
            pass


def test_too_many_files() -> None:
    """Rejects archives containing more than MAX_FILES regular files."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for i in range(MAX_FILES + 1):
            zf.writestr(f"file_{i}.txt", b"x")
    too_many_bytes = buf.getvalue()

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        with safe_extract_zip(too_many_bytes):
            pass


def test_oversized_individual_file_real_zip() -> None:
    """Rejects real archive containing an individual file exceeding MAX_SINGLE_FILE_BYTES."""
    big_data = b"x" * (MAX_SINGLE_FILE_BYTES + 1)
    zip_bytes = _make_zip_bytes({"big_file.bin": big_data})

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        with safe_extract_zip(zip_bytes):
            pass


def test_oversized_individual_file_header_precheck(tmp_path: Path) -> None:
    """Rejects archive when individual file header claims size exceeding limit."""
    info = zipfile.ZipInfo("big_file.bin")
    info.file_size = MAX_SINGLE_FILE_BYTES + 1
    info.compress_size = 100

    mock_zf = MagicMock(spec=zipfile.ZipFile)
    mock_zf.infolist.return_value = [info]

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        _perform_safe_extraction(mock_zf, tmp_path)


def test_exceeded_compression_ratio_integer_math() -> None:
    """Proves integer arithmetic for compression ratio limit checking."""
    # 0 compressed / 0 uncompressed -> allowed
    _check_compression_ratio(0, 0)

    # 0 compressed / positive uncompressed -> rejected
    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        _check_compression_ratio(100, 0)

    # Ratio exactly 20:1 (200 uncompressed / 10 compressed) -> allowed
    _check_compression_ratio(200, 10)

    # Ratio 20.1:1 (201 uncompressed / 10 compressed) -> rejected (201 > 20 * 10)
    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        _check_compression_ratio(201, 10)


def test_dynamic_streaming_enforcement_removes_temp_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves streaming limit breach raises error, yields no manifest, and cleans up temp dir."""
    zip_bytes = _make_zip_bytes({"test.txt": "data"})

    # Create a mock stream that emits chunk exceeding MAX_SINGLE_FILE_BYTES
    huge_chunk = b"A" * (MAX_SINGLE_FILE_BYTES + 50)
    mock_stream = MagicMock()
    mock_stream.read.side_effect = [huge_chunk, b""]
    mock_stream.__enter__.return_value = mock_stream
    mock_stream.__exit__.return_value = None

    monkeypatch.setattr(zipfile.ZipFile, "open", lambda *args, **kwargs: mock_stream)

    manifest_returned = False
    extracted_dir_path: Path | None = None

    with pytest.raises(IngestionLimitError, match="Archive member exceeds an ingestion limit."):
        with safe_extract_zip(zip_bytes) as (target_dir, manifest):
            manifest_returned = True
            extracted_dir_path = target_dir

    assert not manifest_returned
    if extracted_dir_path:
        assert not extracted_dir_path.exists()



# ---------------------------------------------------------------------------
# 3. Path & Member Safety Tests & Untrusted Information Leakage Tests
# ---------------------------------------------------------------------------


def test_path_traversal_rejection() -> None:
    """Rejects archives containing relative path traversal entries with fixed safe error."""
    for bad_path in ["../escape.txt", "foo/../../bar.txt", "..\\win_escape.txt"]:
        zip_bytes = _make_zip_bytes({bad_path: "data"})
        with pytest.raises(
            UnsafeArchiveMemberPathError, match="Archive member violates safety policy."
        ):
            with safe_extract_zip(zip_bytes):
                pass


def test_absolute_posix_path_rejection() -> None:
    """Rejects archives containing absolute POSIX paths."""
    zip_bytes = _make_zip_bytes({"/etc/passwd": "root:x:0:0"})
    with pytest.raises(
        UnsafeArchiveMemberPathError, match="Archive member violates safety policy."
    ):
        with safe_extract_zip(zip_bytes):
            pass


def test_windows_drive_path_rejection() -> None:
    """Rejects archives containing Windows drive letter paths."""
    for bad_path in ["C:\\Windows\\System32\\cmd.exe", "D:/data.txt"]:
        zip_bytes = _make_zip_bytes({bad_path: "drive letter"})
        with pytest.raises(
            UnsafeArchiveMemberPathError, match="Archive member violates safety policy."
        ):
            with safe_extract_zip(zip_bytes):
                pass


def test_no_untrusted_values_in_error_messages() -> None:
    """Proves domain errors do not leak untrusted member paths, filenames, or temp dir paths."""
    malicious_filename = "../../../secret/malicious_payload_path_12345.sh"
    zip_bytes = _make_zip_bytes({malicious_filename: "payload"})

    with pytest.raises(UnsafeArchiveMemberPathError) as exc_info:
        with safe_extract_zip(zip_bytes):
            pass

    err_msg = str(exc_info.value)
    # Must equal fixed safe message
    assert err_msg == "Archive member violates safety policy."
    # Must NOT leak untrusted path, payload name, or temp folder
    assert "malicious_payload_path_12345" not in err_msg
    assert "secret" not in err_msg
    assert "sourcetrace_zip_" not in err_msg
    assert "Temp" not in err_msg


def test_unc_and_device_path_rejection() -> None:
    """Rejects archives containing UNC or device paths."""
    for bad_path in [
        "\\\\.\\COM1",
        "\\\\server\\share\\file.txt",
        "//server/share/file.txt",
    ]:
        zip_bytes = _make_zip_bytes({bad_path: "payload"})
        with pytest.raises(
            UnsafeArchiveMemberPathError, match="Archive member violates safety policy."
        ):
            with safe_extract_zip(zip_bytes):
                pass


def test_nul_path_rejection(tmp_path: Path) -> None:
    """Rejects archives containing NUL bytes in entry paths."""
    info = zipfile.ZipInfo()
    info.filename = "foo\x00bar.txt"
    info.file_size = 10
    info.compress_size = 10

    mock_zf = MagicMock(spec=zipfile.ZipFile)
    mock_zf.infolist.return_value = [info]

    with pytest.raises(
        UnsafeArchiveMemberPathError, match="Archive member violates safety policy."
    ):
        _perform_safe_extraction(mock_zf, tmp_path)


def test_symlink_entry_rejection() -> None:
    """Rejects symbolic link entries in archives."""
    info = zipfile.ZipInfo("symlink_file")
    info.create_system = 3  # Unix
    info.external_attr = (stat.S_IFLNK | 0o777) << 16

    zip_bytes = _make_zip_bytes(custom_entries=[(info, b"target_path")])

    with pytest.raises(
        UnsafeArchiveMemberPathError, match="Archive member violates safety policy."
    ):
        with safe_extract_zip(zip_bytes):
            pass


def test_special_file_entries_rejection() -> None:
    """Rejects FIFO, device node, and socket entries in archives."""
    special_modes = [
        ("fifo_file", stat.S_IFIFO),
        ("char_dev", stat.S_IFCHR),
        ("block_dev", stat.S_IFBLK),
        ("socket_file", stat.S_IFSOCK),
    ]

    for name, mode_type in special_modes:
        info = zipfile.ZipInfo(name)
        info.create_system = 3  # Unix
        info.external_attr = (mode_type | 0o600) << 16

        zip_bytes = _make_zip_bytes(custom_entries=[(info, b"")])

        with pytest.raises(
            UnsafeArchiveMemberPathError, match="Archive member violates safety policy."
        ):
            with safe_extract_zip(zip_bytes):
                pass


# ---------------------------------------------------------------------------
# 4. Corrupt Archive Tests
# ---------------------------------------------------------------------------


def test_corrupt_non_zip_archive(tmp_path: Path) -> None:
    """Rejects corrupted or non-ZIP files with typed InvalidArchiveError and safe message."""
    corrupt_file = tmp_path / "corrupt.zip"
    corrupt_file.write_bytes(b"THIS IS NOT A ZIP ARCHIVE DATA STREAM")

    with pytest.raises(InvalidArchiveError, match="Archive source is invalid."):
        with safe_extract_zip(corrupt_file):
            pass


def test_truncated_zip_archive() -> None:
    """Rejects truncated ZIP archives with typed InvalidArchiveError."""
    valid_bytes = _make_zip_bytes({"test.txt": "hello world"})
    truncated_bytes = valid_bytes[: len(valid_bytes) // 2]

    with pytest.raises(InvalidArchiveError, match="Archive source is invalid."):
        with safe_extract_zip(truncated_bytes):
            pass


def test_missing_file_source() -> None:
    """Rejects missing archive file path with typed InvalidArchiveError."""
    with pytest.raises(InvalidArchiveError, match="Archive source is unavailable."):
        with safe_extract_zip("non_existent_archive_path_123.zip"):
            pass


# ---------------------------------------------------------------------------
# 5. Managed Cleanup Guarantee Tests
# ---------------------------------------------------------------------------


def test_cleanup_after_success(tmp_path: Path) -> None:
    """Proves temporary extraction directory is deleted after successful exit."""
    zip_bytes = _make_zip_bytes({"foo.txt": "hello"})
    extracted_dir: Path | None = None

    with safe_extract_zip(zip_bytes) as res:
        extracted_dir = res.target_dir
        assert extracted_dir.exists()
        assert (extracted_dir / "foo.txt").exists()

    assert extracted_dir is not None
    assert not extracted_dir.exists()


def test_cleanup_after_failure_during_extraction() -> None:
    """Proves temporary extraction directory is deleted when extraction fails halfway."""
    zip_bytes = _make_zip_bytes(
        {
            "valid.txt": "valid content",
            "../traversal.txt": "malicious content",
        }
    )

    temp_dirs_before = set(Path(tempfile.gettempdir()).glob("sourcetrace_zip_*"))

    with pytest.raises(UnsafeArchiveMemberPathError):
        with safe_extract_zip(zip_bytes):
            pass

    temp_dirs_after = set(Path(tempfile.gettempdir()).glob("sourcetrace_zip_*"))
    assert temp_dirs_after.issubset(temp_dirs_before)


def test_cleanup_after_failure_inside_with_block() -> None:
    """Proves temp extraction directory is deleted if caller raises inside context."""
    zip_bytes = _make_zip_bytes({"test.txt": "data"})
    extracted_dir: Path | None = None

    class CustomCallerError(Exception):
        pass

    with pytest.raises(CustomCallerError):
        with safe_extract_zip(zip_bytes) as res:
            extracted_dir = res.target_dir
            assert extracted_dir.exists()
            raise CustomCallerError("Caller code failed")

    assert extracted_dir is not None
    assert not extracted_dir.exists()


def test_parent_dir_preservation(tmp_path: Path) -> None:
    """Proves context manager never deletes caller-provided parent directory."""
    parent_dir = tmp_path / "my_custom_parent"
    parent_dir.mkdir(parents=True)

    zip_bytes = _make_zip_bytes({"file.txt": "data"})

    with safe_extract_zip(zip_bytes, parent_dir=parent_dir) as res:
        target_dir = res.target_dir
        assert target_dir.parent.resolve() == parent_dir.resolve()
        assert target_dir.exists()

    assert not target_dir.exists()
    assert parent_dir.exists()
    assert parent_dir.is_dir()

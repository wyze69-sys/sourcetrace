"""Immutable ingestion security limits."""

# ---------------------------------------------------------------------------
# ZIP upload limits
# ---------------------------------------------------------------------------

MAX_COMPRESSED_ZIP_BYTES: int = 25 * 1024 * 1024  # 25 MB
"""Maximum size of the compressed ZIP archive in bytes."""

MAX_EXTRACTED_ZIP_BYTES: int = 100 * 1024 * 1024  # 100 MB
"""Maximum cumulative extracted (uncompressed) size in bytes."""

MAX_FILES: int = 5_000
"""Maximum number of files allowed inside an archive."""

MAX_SINGLE_FILE_BYTES: int = 1 * 1024 * 1024  # 1 MB
"""Maximum size for any single extracted file in bytes."""

MAX_COMPRESSION_RATIO: int = 20
"""Maximum allowed compression ratio (uncompressed / compressed)."""

# ---------------------------------------------------------------------------
# Public GitHub import limits
# ---------------------------------------------------------------------------

MAX_GITHUB_ARCHIVE_BYTES: int = 25 * 1024 * 1024  # 25 MB
"""Maximum size of a downloaded GitHub archive in bytes."""

DOWNLOAD_TIMEOUT_SECONDS: int = 120
"""HTTP request timeout for downloading GitHub archives."""

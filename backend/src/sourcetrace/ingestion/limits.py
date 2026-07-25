"""Immutable ingestion security limits.

All values are integers in bytes (or raw counts / seconds) to avoid
floating-point comparison surprises.  The constants are sourced from the
approved MVP decisions recorded in ``docs/AGENT_TASKS.yaml`` and
``docs/decisions/0002-source-retention-and-cleanup.md``.

These limits govern ZIP upload extraction, public GitHub archive downloads,
and general ingestion safety checks.  They must never be overridden at runtime.
"""

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

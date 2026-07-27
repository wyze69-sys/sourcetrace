"""Application-specific exception hierarchy."""


class SourceTraceError(Exception):
    """Base exception for expected SourceTrace failures."""


class RepositoryValidationError(SourceTraceError):
    """Raised when a repository path cannot be indexed safely."""


class RepositoryNotFoundError(SourceTraceError):
    """Raised when an indexed repository does not exist."""


class ProviderError(SourceTraceError):
    """Raised when an embedding or LLM provider fails."""


class StorageError(SourceTraceError):
    """Base exception for storage operations."""


class StorageConfigurationError(StorageError):
    """Raised when storage operation is requested without required configuration."""


class StorageDataError(StorageError):
    """Raised when stored data is malformed or invalid."""


class StorageOperationError(StorageError):
    """Raised when a storage database operation fails safely."""


class SessionConfigurationError(SourceTraceError):
    """Raised when session security operation is requested without valid configuration."""


# ---------------------------------------------------------------------------
# Ingestion security errors
#
# Error messages are safe for the 422 SECURITY_VIOLATION API envelope:
# they never echo raw secrets, local paths, uploaded file contents,
# request URLs with credentials, or arbitrary untrusted values.
# ---------------------------------------------------------------------------


class InvalidRepositoryURLError(SourceTraceError):
    """Raised when a user-submitted repository URL is invalid or unsafe."""


class DisallowedRedirectError(SourceTraceError):
    """Raised when an HTTP redirect target violates the host allowlist."""


class UnsafeNetworkAddressError(SourceTraceError):
    """Raised when a resolved IP address is non-public (SSRF protection)."""


class UnsafeArchiveMemberPathError(SourceTraceError):
    """Raised when an archive entry path contains traversal or unsafe components."""


class IngestionLimitError(SourceTraceError):
    """Raised when an ingestion limit (size, count, ratio, timeout) is exceeded."""


class InvalidArchiveError(SourceTraceError):
    """Raised when an archive file is corrupt, invalid, or cannot be opened."""


class ArchiveDownloadError(SourceTraceError):
    """Raised when a public GitHub archive download fails safely."""


class RepositoryQuotaExceededError(SourceTraceError):
    """Raised when a session exceeds its active repository quota."""


class SessionInvalidError(SourceTraceError):
    """Raised when an anonymous session is missing or expired."""


class IngestionServiceError(SourceTraceError):
    """Raised when ingestion orchestration or persistence fails safely."""


class AcquisitionError(SourceTraceError):
    """Raised when source acquisition or consumer handoff fails safely."""


class EmbeddingError(ProviderError):
    """Raised when embedding provider or service fails safely."""

    def __init__(self, message: str = "Embedding failed safely.") -> None:
        super().__init__(message)


class IndexingError(SourceTraceError):
    """Raised when repository code indexing fails safely."""

    def __init__(self, message: str = "Indexing failed safely.") -> None:
        super().__init__(message)


class UploadPayloadTooLargeError(SourceTraceError):
    """Raised when a ZIP upload exceeds the maximum compressed size limit."""


class UploadValidationError(SourceTraceError):
    """Raised when an uploaded ZIP file or display name fails validation."""


class UploadStagingError(SourceTraceError):
    """Raised when upload staging operations or file system resolution fails safely."""


class RetrievalError(SourceTraceError):
    """Raised when semantic retrieval fails safely."""

    def __init__(self, message: str = "Retrieval failed safely.") -> None:
        super().__init__(message)


class RetrievalValidationError(RetrievalError):
    """Raised when a retrieval query or parameter is invalid."""

    def __init__(self, message: str = "Retrieval request is invalid.") -> None:
        super().__init__(message)


class GenerationError(SourceTraceError):
    """Raised when answer generation fails safely."""

    def __init__(self, message: str = "Generation failed safely.") -> None:
        super().__init__(message)


class GenerationValidationError(GenerationError):
    """Raised when a generation request or input parameter is invalid."""

    def __init__(self, message: str = "Generation request is invalid.") -> None:
        super().__init__(message)

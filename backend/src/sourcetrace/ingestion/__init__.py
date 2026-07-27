"""Repository discovery and indexing orchestration."""

from sourcetrace.ingestion.acquisition import (
    AcquiredSource,
    AcquiredSourceConsumer,
    AcquisitionRunner,
    acquire_github_source,
    acquire_zip_source,
)

__all__ = [
    "AcquiredSource",
    "AcquiredSourceConsumer",
    "AcquisitionRunner",
    "acquire_github_source",
    "acquire_zip_source",
]

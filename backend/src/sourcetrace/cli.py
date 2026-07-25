"""Command-line entry points for SourceTrace."""

import uvicorn


def run_api() -> None:
    """Run the SourceTrace FastAPI server."""
    uvicorn.run("sourcetrace.main:app", host="127.0.0.1", port=8000)

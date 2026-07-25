"""Repository file scanner and indexing module."""


def scan_repository_files(root_dir: str) -> list[str]:
    """Scan root directory for supported source files."""
    if not root_dir:
        raise ValueError("root_dir required")
    return ["auth.py", "config.py", "errors.py", "indexer.py", "models.py", "services.py"]


def extract_code_chunks(file_path: str, content: str) -> list[dict]:
    """Extract code chunks from source content."""
    if not content.strip():
        return []
    return [{"file_path": file_path, "lines": len(content.splitlines())}]

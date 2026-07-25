"""SourceTrace deterministic Python file scanner.

This module is responsible for scanning extracted archive contents, safely validating
paths, filtering out excluded or sensitive files, enforcing size limits, and securely
reading Python source files.
"""

from __future__ import annotations

import tokenize
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.limits import MAX_SINGLE_FILE_BYTES

INVALID_PATH_PLACEHOLDER: str = "<invalid-path>"


class SkipReason(StrEnum):
    UNSUPPORTED_PATH = "UNSUPPORTED_PATH"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    NOT_REGULAR_FILE = "NOT_REGULAR_FILE"
    SYMLINK_REJECTED = "SYMLINK_REJECTED"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    INVALID_ENCODING = "INVALID_ENCODING"
    INVALID_PYTHON_SYNTAX = "INVALID_PYTHON_SYNTAX"
    UNREADABLE_SOURCE = "UNREADABLE_SOURCE"
    EMPTY_SOURCE = "EMPTY_SOURCE"


@dataclass(frozen=True, slots=True)
class SkippedFile:
    relative_path: str
    reason: SkipReason


@dataclass(frozen=True, slots=True)
class EligibleFile:
    relative_path: str
    physical_path: Path
    source: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    eligible_files: tuple[EligibleFile, ...]
    skipped: tuple[SkippedFile, ...]


EXCLUDED_DIRS = frozenset({
    ".git", ".hg", ".svn", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", "venv", ".venv", "env", "site-packages",
    "node_modules", "vendor", "dist", "build", "coverage", "htmlcov"
})

SENSITIVE_FILE_STEMS = frozenset({
    ".env", "credentials", "private_key", "private-key"
})

SENSITIVE_FILE_PATTERNS = frozenset({
    ".env.", "credentials."
})

SENSITIVE_EXTENSIONS = frozenset({
    ".pem", ".key"
})

EXCLUDED_DIRS_CASEFOLDED = frozenset(d.casefold() for d in EXCLUDED_DIRS)
SENSITIVE_STEMS_CASEFOLDED = frozenset(s.casefold() for s in SENSITIVE_FILE_STEMS)
SENSITIVE_PATTERNS_CASEFOLDED = frozenset(p.casefold() for p in SENSITIVE_FILE_PATTERNS)
SENSITIVE_EXTS_CASEFOLDED = frozenset(e.casefold() for e in SENSITIVE_EXTENSIONS)


def _validate_manifest_path(raw_path: str) -> str | None:
    """Validate and canonicalize a manifest relative path.

    Permits '\\' to '/' separator conversion.
    Rejects non-canonical paths, whitespace, empty/dot components, traversal,
    absolute paths, drive letters, UNC paths, and NUL bytes.

    Returns the canonical normalized path string, or None if invalid.
    """
    if not isinstance(raw_path, str):
        return None

    path = raw_path.replace("\\", "/")

    if "\x00" in path:
        return None

    if path != path.strip():
        return None

    if not path:
        return None

    if path.startswith("/"):
        return None

    if path.endswith("/"):
        return None

    if len(path) >= 2 and path[1] == ":":
        return None

    if path.startswith("//"):
        return None

    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            return None

    return path


def _detect_github_wrapper(manifest_paths: tuple[str, ...]) -> str | None:
    if not manifest_paths:
        return None

    wrapper = None
    for p in manifest_paths:
        parts = p.replace("\\", "/").split("/")
        if len(parts) < 2:
            return None

        current_wrapper = parts[0]
        if wrapper is None:
            wrapper = current_wrapper
        elif wrapper != current_wrapper:
            return None

    return wrapper


def _strip_github_wrapper(relative_path: str, wrapper: str | None) -> str:
    if wrapper and relative_path.startswith(f"{wrapper}/"):
        return relative_path[len(wrapper) + 1:]
    return relative_path


def _is_excluded_path(relative_path: str) -> bool:
    """Check directory components and filename against exclusion policies (case-folded)."""
    parts = relative_path.split("/")

    for part in parts[:-1]:
        part_casefolded = part.casefold()
        if part_casefolded in EXCLUDED_DIRS_CASEFOLDED:
            return True
        if part_casefolded in SENSITIVE_STEMS_CASEFOLDED:
            return True
        if any(
            part_casefolded.startswith(pattern)
            for pattern in SENSITIVE_PATTERNS_CASEFOLDED
        ):
            return True

    filename = parts[-1]
    file_casefolded = filename.casefold()
    stem_casefolded = Path(filename).stem.casefold()

    if (
        stem_casefolded in SENSITIVE_STEMS_CASEFOLDED
        or file_casefolded in SENSITIVE_STEMS_CASEFOLDED
    ):
        return True

    if any(
        file_casefolded.startswith(pattern)
        for pattern in SENSITIVE_PATTERNS_CASEFOLDED
    ):
        return True

    if any(file_casefolded.endswith(ext) for ext in SENSITIVE_EXTS_CASEFOLDED):
        return True

    return False


def _is_symlink(path: Path) -> bool:
    """Inspect whether a path component is a symbolic link."""
    return path.is_symlink()


class SourceDecodeError(Exception):
    """Raised for encoding-related failures (invalid PEP 263 cookie, unknown codec, etc.)."""
    pass


class SourceReadError(Exception):
    """Raised for OS/filesystem I/O read errors."""
    pass


SUPPORTED_PYTHON_EXTENSIONS: frozenset[str] = frozenset({".py"})
SUPPORTED_JAVASCRIPT_EXTENSIONS: frozenset[str] = frozenset({
    ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"
})
SUPPORTED_CODE_EXTENSIONS: frozenset[str] = (
    SUPPORTED_PYTHON_EXTENSIONS | SUPPORTED_JAVASCRIPT_EXTENSIONS
)


def _decode_source(physical_path: Path, is_python: bool = False) -> str:
    if is_python:
        try:
            with open(physical_path, "rb") as f:
                try:
                    encoding, _ = tokenize.detect_encoding(f.readline)
                except (SyntaxError, LookupError, UnicodeDecodeError, ValueError) as e:
                    raise SourceDecodeError() from e
        except OSError as e:
            raise SourceReadError() from e
    else:
        encoding = "utf-8"

    try:
        with open(physical_path, encoding=encoding) as f:
            source = f.read()
    except (UnicodeDecodeError, LookupError, ValueError) as e:
        raise SourceDecodeError() from e
    except OSError as e:
        raise SourceReadError() from e

    if "\x00" in source:
        raise SourceDecodeError()

    return source


def _decode_python_source(physical_path: Path) -> str:
    return _decode_source(physical_path, is_python=True)


def scan_code_sources(
    acquired_source: AcquiredSource,
    allowed_extensions: frozenset[str] = SUPPORTED_CODE_EXTENSIONS,
) -> ScanResult:
    """Discover eligible source code files in deterministic order.

    Extension matching is done against allowed_extensions.
    Exclusion and sensitive filename policy checks are case-folded.
    """
    root = acquired_source.extraction_root
    paths = acquired_source.manifest.relative_paths

    wrapper = None
    if acquired_source.source_type == "github":
        wrapper = _detect_github_wrapper(paths)

    eligible = []
    skipped = []
    seen = set()

    for raw_path in paths:
        norm_path = _validate_manifest_path(raw_path)
        if not norm_path:
            skipped.append(SkippedFile(INVALID_PATH_PLACEHOLDER, SkipReason.UNSUPPORTED_PATH))
            continue

        cit_path = norm_path
        if acquired_source.source_type == "github" and wrapper:
            cit_path = _strip_github_wrapper(norm_path, wrapper)

        suffix = Path(norm_path).suffix
        if suffix not in allowed_extensions:
            skipped.append(SkippedFile(cit_path, SkipReason.UNSUPPORTED_PATH))
            continue

        if _is_excluded_path(cit_path):
            skipped.append(SkippedFile(cit_path, SkipReason.UNSUPPORTED_PATH))
            continue

        phys_path = root / norm_path

        # Symlink inspection across all path components beneath extraction_root
        rel_parts = Path(norm_path).parts
        current = root
        has_symlink = False
        for part in rel_parts:
            current = current / part
            if _is_symlink(current):
                has_symlink = True
                break

        if has_symlink:
            skipped.append(SkippedFile(cit_path, SkipReason.SYMLINK_REJECTED))
            continue

        try:
            resolved_phys = phys_path.resolve(strict=False)
            resolved_root = root.resolve(strict=False)
            if not resolved_phys.is_relative_to(resolved_root):
                skipped.append(SkippedFile(cit_path, SkipReason.UNSUPPORTED_PATH))
                continue
        except Exception:
            skipped.append(SkippedFile(cit_path, SkipReason.UNSUPPORTED_PATH))
            continue

        if not phys_path.exists():
            skipped.append(SkippedFile(cit_path, SkipReason.FILE_NOT_FOUND))
            continue

        if not phys_path.is_file():
            skipped.append(SkippedFile(cit_path, SkipReason.NOT_REGULAR_FILE))
            continue

        try:
            if phys_path.stat().st_size > MAX_SINGLE_FILE_BYTES:
                skipped.append(SkippedFile(cit_path, SkipReason.FILE_TOO_LARGE))
                continue
        except OSError:
            skipped.append(SkippedFile(cit_path, SkipReason.UNREADABLE_SOURCE))
            continue

        try:
            is_py = suffix == ".py"
            source = _decode_source(phys_path, is_python=is_py)
        except SourceDecodeError:
            skipped.append(SkippedFile(cit_path, SkipReason.INVALID_ENCODING))
            continue
        except SourceReadError:
            skipped.append(SkippedFile(cit_path, SkipReason.UNREADABLE_SOURCE))
            continue

        if not source.strip():
            skipped.append(SkippedFile(cit_path, SkipReason.EMPTY_SOURCE))
            continue

        if cit_path not in seen:
            seen.add(cit_path)
            eligible.append(EligibleFile(cit_path, phys_path, source))

    eligible.sort(key=lambda x: x.relative_path)
    return ScanResult(tuple(eligible), tuple(skipped))


def scan_python_sources(acquired_source: AcquiredSource) -> ScanResult:
    """Discover canonical lowercase-``.py`` sources in deterministic order."""
    return scan_code_sources(acquired_source, allowed_extensions=SUPPORTED_PYTHON_EXTENSIONS)


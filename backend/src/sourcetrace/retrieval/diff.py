"""Deterministic unified-diff parsing for change impact previews (IMPACT-003).

Parses pasted unified diffs into per-file changed-line sets expressed in
OLD-file coordinates: the repository index reflects the pre-diff baseline, so
deletions map to their exact old lines and additions map to the old line they
displace (their insertion anchor). Context and deleted lines are additionally
kept as (line, text) samples so callers can detect that a diff does not match
the indexed content (a stale diff base).

Pure text processing: no filesystem access, no subprocess, no diff
application. Malformed input raises DiffParseError; oversized inputs are
bounded by caps below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_DIFF_FILES: int = 50
MAX_SAMPLES_PER_FILE: int = 50

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


class DiffParseError(ValueError):
    """Raised when the input is not a parseable unified diff."""


@dataclass(frozen=True, slots=True)
class DiffFile:
    """One file's changes, in old-file (pre-diff, indexed-baseline) terms."""

    old_path: str | None  # None when the diff adds a brand-new file
    new_path: str | None  # None when the diff deletes the file
    changed_old_lines: frozenset[int]
    old_line_samples: tuple[tuple[int, str], ...]  # (old_line, exact text)

    @property
    def display_path(self) -> str:
        return self.old_path or self.new_path or "<unknown>"


def _clean_path(raw: str) -> str | None:
    """Normalize a ---/+++ header path; None for /dev/null."""
    path = raw.split("\t", 1)[0].strip()
    if path in ("/dev/null", ""):
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path.replace("\\", "/")


def parse_unified_diff(diff_text: str) -> list[DiffFile]:
    """Parse a unified diff into per-file old-coordinate change sets.

    Raises DiffParseError when no file header with at least one valid hunk is
    found, so callers can reject non-diff input explicitly instead of
    returning an empty impact.
    """
    lines = diff_text.splitlines()
    files: list[DiffFile] = []

    i = 0
    current_old: str | None = None
    current_new: str | None = None
    have_header = False
    changed: set[int] = set()
    samples: list[tuple[int, str]] = []
    hunks_in_file = 0

    def flush_file() -> None:
        nonlocal changed, samples, hunks_in_file, have_header
        if have_header and hunks_in_file > 0 and len(files) < MAX_DIFF_FILES:
            files.append(
                DiffFile(
                    old_path=current_old,
                    new_path=current_new,
                    changed_old_lines=frozenset(changed),
                    old_line_samples=tuple(samples[:MAX_SAMPLES_PER_FILE]),
                )
            )
        changed = set()
        samples = []
        hunks_in_file = 0
        have_header = False

    while i < len(lines):
        line = lines[i]

        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            flush_file()
            current_old = _clean_path(line[4:])
            current_new = _clean_path(lines[i + 1][4:])
            have_header = True
            i += 2
            continue

        match = _HUNK_HEADER.match(line)
        if match and have_header:
            old_start = int(match.group("old_start"))
            old_count = int(match.group("old_count") or "1")
            hunks_in_file += 1
            i += 1
            old_cursor = old_start
            old_end = old_start + old_count - 1 if old_count > 0 else old_start
            consumed_old = 0
            consumed_new = 0
            new_count = int(match.group("new_count") or "1")
            while i < len(lines) and (consumed_old < old_count or consumed_new < new_count):
                body = lines[i]
                if body.startswith("\\"):  # "\ No newline at end of file"
                    i += 1
                    continue
                if body.startswith("-"):
                    changed.add(old_cursor)
                    samples.append((old_cursor, body[1:]))
                    old_cursor += 1
                    consumed_old += 1
                elif body.startswith("+"):
                    # Insertion anchor: the old line this addition displaces,
                    # clamped into the hunk's real old range so appends at
                    # EOF still anchor to an existing line.
                    anchor = min(old_cursor, old_end)
                    if anchor >= 1:
                        changed.add(anchor)
                    consumed_new += 1
                elif body.startswith(" ") or body == "":
                    samples.append((old_cursor, body[1:] if body else ""))
                    old_cursor += 1
                    consumed_old += 1
                    consumed_new += 1
                else:
                    break  # next header or garbage: stop this hunk
                i += 1
            continue

        i += 1

    flush_file()

    if not files:
        raise DiffParseError(
            "Input is not a valid unified diff: no '--- / +++' file header "
            "with at least one '@@' hunk was found."
        )
    return files

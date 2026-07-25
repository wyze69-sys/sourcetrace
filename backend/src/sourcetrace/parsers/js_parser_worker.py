"""
Standalone worker script for safe JavaScript/TypeScript tree-sitter parsing.

This script is invoked as a subprocess by javascript_ast.py to isolate
native tree-sitter parser crashes (e.g. access violations / segfaults on Windows)
that cannot be caught by Python try/except.

Protocol:
  - Receives a single JSON object on stdin with keys: source, relative_path,
    repository_id, owner_session_id.
  - Runs _parse_javascript_source_in_process from the enclosing package (direct
    in-process call — no further subprocess indirection).
  - Outputs a JSON array of ParsedCodeChunk dicts on stdout on success.
  - Exits with code 0 on success, non-zero on crash (including native segfaults
    which kill the process with a non-zero exit code from the OS).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _add_project_src_to_path() -> None:
    """Add the project src directory to sys.path so package imports work.

    The worker script lives at:
      <project_root>/backend/src/sourcetrace/parsers/js_parser_worker.py
    We need <project_root>/backend/src on sys.path to import sourcetrace.*
    """
    script_dir = Path(__file__).resolve().parent
    # script_dir = .../sourcetrace/parsers/
    # We need .../src/ on sys.path
    src_dir = script_dir.parent.parent  # parsers/ -> sourcetrace/ -> src/
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))


def main() -> None:
    _add_project_src_to_path()

    # Save real stdout stream for final JSON response only;
    # redirect sys.stdout to sys.stderr to capture any spurious prints from imports/libraries.
    real_stdout = sys.stdout
    sys.stdout = sys.stderr

    input_raw = sys.stdin.read()
    if not input_raw.strip():
        sys.exit(1)

    try:
        data = json.loads(input_raw)
    except json.JSONDecodeError:
        sys.exit(1)

    source = data.get("source", "")
    relative_path = data.get("relative_path", "")
    repository_id = data.get("repository_id", "")
    owner_session_id = data.get("owner_session_id", "")

    if not source or not relative_path:
        sys.exit(1)

    # Import the in-process version directly to avoid subprocess recursion
    from sourcetrace.parsers.javascript_ast import _parse_javascript_source_in_process

    chunks = _parse_javascript_source_in_process(
        source=source,
        relative_path=relative_path,
        repository_id=repository_id,
        owner_session_id=owner_session_id,
    )

    # Convert ParsedCodeChunk frozen dataclass instances to dicts for JSON
    from dataclasses import asdict

    output = [asdict(c) for c in chunks]
    output_json = json.dumps(output)

    # Write ONLY the JSON payload to the real stdout
    real_stdout.write(output_json)
    real_stdout.flush()
    sys.exit(0)


if __name__ == "__main__":
    main()
"""Offline round-trip tests for flow-evidence persistence (TRACE-001).

Verifies that ReferenceEvidence / ImportEvidence / EndpointEvidence survive
CodeChunk -> Mongo document -> CodeChunk mapping exactly, that documents from
pre-trace parser versions read back with empty evidence, and that malformed
stored evidence fails strictly with StorageDataError.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from sourcetrace.core.exceptions import StorageDataError
from sourcetrace.models.domain import (
    CodeChunk,
    EndpointEvidence,
    ImportEvidence,
    ReferenceEvidence,
)
from sourcetrace.storage.mongo_repositories import _chunk_to_doc, _doc_to_chunk

_CREATED_AT = datetime(2026, 7, 26, 12, 0, 0, tzinfo=UTC)

_REFERENCES = (
    ReferenceEvidence(local_name="helper", kind="call", line_start=6, line_end=6),
    ReferenceEvidence(
        local_name="db.session.commit", kind="attribute_call", line_start=8, line_end=9
    ),
)
_IMPORTS = (
    ImportEvidence(
        local_name="verify",
        source_module="services.auth",
        imported_name="verify_token",
        line_start=1,
        line_end=1,
    ),
)
_ENDPOINTS = (
    EndpointEvidence(
        kind="declares",
        http_method="POST",
        path_literal="/api/v1/items/{item_id}",
        normalized_path="/api/v1/items/{}",
        line_start=4,
        line_end=4,
    ),
    EndpointEvidence(
        kind="calls",
        http_method="GET",
        path_literal="",
        normalized_path="",
        line_start=12,
        line_end=12,
    ),
)


def _make_chunk(**overrides: Any) -> CodeChunk:
    base: dict[str, Any] = {
        "chunk_id": "chunk_evidence_1",
        "repository_id": "repo_001",
        "owner_session_id": "owner_001",
        "relative_path": "src/app.py",
        "language": "python",
        "symbol_name": "main",
        "symbol_type": "function",
        "start_line": 4,
        "end_line": 12,
        "content": "def main(): pass",
        "content_hash": "hash_abc",
        "parser_version": "python-ast-v2",
        "created_at": _CREATED_AT,
        "embedding_model": None,
        "embedding_dimensions": None,
        "embedding": None,
        "references": _REFERENCES,
        "imports": _IMPORTS,
        "endpoints": _ENDPOINTS,
        "extraction_truncated": True,
    }
    base.update(overrides)
    return CodeChunk(**base)


def test_chunk_to_doc_serializes_evidence_shapes() -> None:
    doc = _chunk_to_doc(_make_chunk())
    assert doc["references"] == [
        {"local_name": "helper", "kind": "call", "line_start": 6, "line_end": 6},
        {
            "local_name": "db.session.commit",
            "kind": "attribute_call",
            "line_start": 8,
            "line_end": 9,
        },
    ]
    assert doc["imports"] == [
        {
            "local_name": "verify",
            "source_module": "services.auth",
            "imported_name": "verify_token",
            "line_start": 1,
            "line_end": 1,
        },
    ]
    assert len(doc["endpoints"]) == 2
    assert doc["extraction_truncated"] is True


def test_full_roundtrip_preserves_evidence_exactly() -> None:
    original = _make_chunk()
    restored = _doc_to_chunk(_chunk_to_doc(original))
    assert restored.references == _REFERENCES
    assert restored.imports == _IMPORTS
    assert restored.endpoints == _ENDPOINTS
    assert restored.extraction_truncated is True
    assert isinstance(restored.references[0], ReferenceEvidence)
    assert isinstance(restored.imports[0], ImportEvidence)
    assert isinstance(restored.endpoints[0], EndpointEvidence)


def test_empty_path_literal_endpoint_roundtrips() -> None:
    restored = _doc_to_chunk(_chunk_to_doc(_make_chunk()))
    empty_path = [e for e in restored.endpoints if e.path_literal == ""]
    assert len(empty_path) == 1


def test_pre_trace_document_reads_back_with_empty_evidence() -> None:
    doc = _chunk_to_doc(_make_chunk())
    for legacy_missing in ("references", "imports", "endpoints", "extraction_truncated"):
        doc.pop(legacy_missing)
    restored = _doc_to_chunk(doc)
    assert restored.references == ()
    assert restored.imports == ()
    assert restored.endpoints == ()
    assert restored.extraction_truncated is False


def _ref_doc(**overrides: Any) -> dict[str, Any]:
    doc = {"local_name": "x", "kind": "call", "line_start": 1, "line_end": 1}
    doc.update(overrides)
    return doc


def _import_doc(**overrides: Any) -> dict[str, Any]:
    doc = {
        "local_name": "x",
        "source_module": "m",
        "imported_name": "x",
        "line_start": 1,
        "line_end": 1,
    }
    doc.update(overrides)
    return doc


def _endpoint_doc(**overrides: Any) -> dict[str, Any]:
    doc = {
        "kind": "declares",
        "http_method": "GET",
        "path_literal": "/x",
        "normalized_path": "/x",
        "line_start": 1,
        "line_end": 1,
    }
    doc.update(overrides)
    return doc


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("references", "not-a-list"),
        ("references", [_ref_doc(kind="unknown")]),
        ("references", [_ref_doc(local_name="")]),
        ("references", [_ref_doc(line_start=2, line_end=1)]),
        ("references", [_ref_doc(line_start=0)]),
        ("references", ["not-a-dict"]),
        ("imports", "not-a-list"),
        ("imports", [_import_doc(source_module="")]),
        ("endpoints", "not-a-list"),
        ("endpoints", [_endpoint_doc(http_method="")]),
        ("endpoints", [_endpoint_doc(kind="invalid")]),
        ("endpoints", [_endpoint_doc(kind="calls", path_literal=None)]),
        ("extraction_truncated", "yes"),
        ("extraction_truncated", 1),
    ],
)
def test_malformed_stored_evidence_fails_strictly(field: str, bad_value: Any) -> None:
    doc = _chunk_to_doc(_make_chunk())
    doc[field] = bad_value
    with pytest.raises(StorageDataError):
        _doc_to_chunk(doc)


def test_chunk_with_default_evidence_serializes_to_empty_lists() -> None:
    chunk = _make_chunk(references=(), imports=(), endpoints=(), extraction_truncated=False)
    doc = _chunk_to_doc(chunk)
    assert doc["references"] == []
    assert doc["imports"] == []
    assert doc["endpoints"] == []
    assert doc["extraction_truncated"] is False

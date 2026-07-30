"""Unit and integration tests for REPO-001 Phase 7 repository staleness gaps."""

from datetime import UTC, datetime

from sourcetrace.models.domain import CodeChunk, RepositoryRecord, RetrievalResult
from sourcetrace.retrieval.impact import ChangeImpactService
from sourcetrace.retrieval.trace import FlowTraceService


class InMemoryCodeChunkRepository:
    def __init__(self, chunks: list[CodeChunk]) -> None:
        self.chunks = chunks

    def list_by_repository(
        self, owner_session_id: str, repository_id: str, generation_id: str | None = None
    ) -> list[CodeChunk]:
        return [
            c
            for c in self.chunks
            if c.owner_session_id == owner_session_id and c.repository_id == repository_id
        ]

    def search_lexical(
        self,
        owner_session_id: str,
        repository_id: str,
        query_text: str,
        limit: int = 10,
        generation_id: str | None = None,
    ) -> list[RetrievalResult]:
        q = query_text.lower()
        res = [
            c
            for c in self.chunks
            if c.owner_session_id == owner_session_id
            and c.repository_id == repository_id
            and (q in c.symbol_name.lower() or q in c.relative_path.lower())
        ]
        return [RetrievalResult(chunk=c, score=1.0) for c in res[:limit]]


def test_trace_flow_repo_stale_gap() -> None:
    now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
    repo = RepositoryRecord(
        repository_id="repo_stale_1",
        owner_session_id="owner_1",
        name="StaleRepo",
        source_type="github",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
        is_stale=True,
        indexed_commit_sha="abc123456789",
        last_indexed_at=now,
        parser_versions=["python-ast-v3"],
        flow_evidence_complete=True,
    )

    chunk = CodeChunk(
        chunk_id="c1",
        repository_id="repo_stale_1",
        owner_session_id="owner_1",
        relative_path="main.py",
        language="python",
        content="def run(): pass",
        content_hash="hash1",
        created_at=now,
        symbol_name="run",
        symbol_type="function",
        start_line=1,
        end_line=1,
        parser_version="python-ast-v3",
    )

    chunk_repo = InMemoryCodeChunkRepository([chunk])
    service = FlowTraceService(chunk_repo)

    # 1. Stale repo: generates repo_stale gap with SHA and last_indexed_at in detail
    res_stale = service.trace("owner_1", "repo_stale_1", "run", repository=repo)
    gap_kinds = [g.kind for g in res_stale.gaps]
    assert "repo_stale" in gap_kinds
    assert "stale_index" not in gap_kinds  # parser_versions authoritative & complete

    stale_gap = next(g for g in res_stale.gaps if g.kind == "repo_stale")
    assert "abc1234" in stale_gap.detail
    assert "2026-07-27T12:00:00+00:00" in stale_gap.detail

    # 2. Fresh repo: no repo_stale gap
    fresh_repo = RepositoryRecord(
        repository_id="repo_fresh_1",
        owner_session_id="owner_1",
        name="FreshRepo",
        source_type="github",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
        is_stale=False,
        indexed_commit_sha="abc123456789",
        last_indexed_at=now,
        parser_versions=["python-ast-v3"],
        flow_evidence_complete=True,
    )
    chunk_fresh = CodeChunk(
        chunk_id="c2",
        repository_id="repo_fresh_1",
        owner_session_id="owner_1",
        relative_path="main.py",
        language="python",
        content="def run(): pass",
        content_hash="hash2",
        created_at=now,
        symbol_name="run",
        symbol_type="function",
        start_line=1,
        end_line=1,
        parser_version="python-ast-v3",
    )
    service_fresh = FlowTraceService(InMemoryCodeChunkRepository([chunk_fresh]))
    res_fresh = service_fresh.trace("owner_1", "repo_fresh_1", "run", repository=fresh_repo)
    assert not any(g.kind == "repo_stale" for g in res_fresh.gaps)


def test_symbol_and_diff_impact_repo_stale_gap() -> None:
    now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
    stale_repo = RepositoryRecord(
        repository_id="repo_stale_impact",
        owner_session_id="owner_1",
        name="StaleImpact",
        source_type="github",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
        is_stale=True,
        indexed_commit_sha="def987654321",
        last_indexed_at=now,
        parser_versions=["python-ast-v3"],
        flow_evidence_complete=True,
    )

    chunk = CodeChunk(
        chunk_id="c1",
        repository_id="repo_stale_impact",
        owner_session_id="owner_1",
        relative_path="main.py",
        language="python",
        content="def process(): pass",
        content_hash="hash1",
        created_at=now,
        symbol_name="process",
        symbol_type="function",
        start_line=1,
        end_line=1,
        parser_version="python-ast-v3",
    )

    chunk_repo = InMemoryCodeChunkRepository([chunk])
    service = ChangeImpactService(chunk_repo)

    # 1. Symbol Impact with stale repository
    res_symbol = service.impact("owner_1", "repo_stale_impact", "process", repository=stale_repo)
    symbol_gaps = [g.kind for g in res_symbol.gaps]
    assert "repo_stale" in symbol_gaps
    assert "stale_index" not in symbol_gaps

    stale_gap = next(g for g in res_symbol.gaps if g.kind == "repo_stale")
    assert "def9876" in stale_gap.detail

    # 2. Diff Impact with stale repository
    diff_text = """--- a/main.py
+++ b/main.py
@@ -1,1 +1,1 @@
-def process(): pass
+def process(): return 42
"""
    res_diff = service.preview_diff(
        "owner_1", "repo_stale_impact", diff_text, repository=stale_repo
    )
    diff_gaps = [g.kind for g in res_diff.gaps]
    assert "repo_stale" in diff_gaps


def test_legacy_repository_stale_index_fallback() -> None:
    now = datetime.now(UTC)
    # Legacy repo without parser_versions (empty list) and flow_evidence_complete=False
    legacy_repo = RepositoryRecord(
        repository_id="repo_legacy",
        owner_session_id="owner_1",
        name="LegacyRepo",
        source_type="github",
        status="ready",
        file_count=5,
        chunk_count=10,
        created_at=now,
        updated_at=now,
        parser_versions=[],
        flow_evidence_complete=False,
    )

    # Chunks created with CURRENT valid parser version
    chunk_modern = CodeChunk(
        chunk_id="c_modern",
        repository_id="repo_legacy",
        owner_session_id="owner_1",
        relative_path="modern.py",
        language="python",
        content="def foo(): pass",
        content_hash="hashm",
        created_at=now,
        symbol_name="foo",
        symbol_type="function",
        start_line=1,
        end_line=1,
        parser_version="python-ast-v3",
    )

    # Legacy repo with modern chunks should NOT be marked stale_index
    chunk_repo = InMemoryCodeChunkRepository([chunk_modern])
    service = FlowTraceService(chunk_repo)
    res = service.trace("owner_1", "repo_legacy", "foo", repository=legacy_repo)
    assert not any(g.kind == "stale_index" for g in res.gaps)

    # Chunk with outdated parser_version
    chunk_outdated = CodeChunk(
        chunk_id="c_old",
        repository_id="repo_legacy",
        owner_session_id="owner_1",
        relative_path="old.py",
        language="python",
        content="def bar(): pass",
        content_hash="hasho",
        created_at=now,
        symbol_name="bar",
        symbol_type="function",
        start_line=1,
        end_line=1,
        parser_version="legacy-v0-outdated",
    )

    chunk_repo_old = InMemoryCodeChunkRepository([chunk_outdated])
    service_old = FlowTraceService(chunk_repo_old)
    res_old = service_old.trace("owner_1", "repo_legacy", "bar", repository=legacy_repo)
    assert any(g.kind == "stale_index" for g in res_old.gaps)

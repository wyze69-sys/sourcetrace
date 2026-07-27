"""
Offline integration indexing test using a synthetic FitSync-like React/TypeScript project.
"""

from collections.abc import Sequence
from pathlib import Path

from sourcetrace.embeddings.provider import EmbeddingProvider
from sourcetrace.ingestion.acquisition import AcquiredSource
from sourcetrace.ingestion.archive import ExtractionManifest
from sourcetrace.ingestion.indexing import RepositoryIndexingService
from sourcetrace.models.domain import CodeChunk
from sourcetrace.storage.repositories import CodeChunkRepository


class _FakeEmbeddingProvider(EmbeddingProvider):
    @property
    def model_identifier(self) -> str:
        return "fake-embedding-v1"

    @property
    def embedding_dimensions(self) -> int:
        return 1536

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple((0.1,) * 1536 for _ in texts)


class _InMemoryCodeChunkRepository(CodeChunkRepository):
    def __init__(self) -> None:
        self.saved_chunks: list[CodeChunk] = []

    def save_many(self, chunks: list[CodeChunk]) -> int:
        self.saved_chunks.extend(chunks)
        return len(chunks)

    def find_by_repository_id(
        self, repository_id: str, owner_session_id: str
    ) -> tuple[CodeChunk, ...]:
        return tuple(
            c
            for c in self.saved_chunks
            if c.repository_id == repository_id and c.owner_session_id == owner_session_id
        )

    def delete_by_repository_id(self, repository_id: str, owner_session_id: str) -> int:
        prev_len = len(self.saved_chunks)
        self.saved_chunks = [
            c
            for c in self.saved_chunks
            if not (c.repository_id == repository_id and c.owner_session_id == owner_session_id)
        ]
        return prev_len - len(self.saved_chunks)


def test_fitsync_synthetic_project_indexing(tmp_path: Path) -> None:
    # Build synthetic FitSync project file tree
    files = {
        "src/types/workout.ts": """
export interface Workout {
  id: string;
  name: string;
  duration: number;
}
export type WorkoutId = string;
""",
        "src/services/api.ts": """
import { Workout } from '../types/workout';

export async function fetchWorkouts(): Promise<Workout[]> {
  return [];
}

export const API_BASE_URL = 'https://api.fitsync.example.com';
""",
        "src/hooks/useWorkout.ts": """
import { useState, useEffect } from 'react';
import { Workout } from '../types/workout';
import { fetchWorkouts } from '../services/api';

export function useWorkout(id: string) {
  const [workout, setWorkout] = useState<Workout | null>(null);
  return { workout };
}
""",
        "src/components/WorkoutCard.tsx": """
import React from 'react';
import { Workout } from '../types/workout';

export interface WorkoutCardProps {
  workout: Workout;
}

export const WorkoutCard: React.FC<WorkoutCardProps> = ({ workout }) => {
  return (
    <div className="workout-card">
      <h3>{workout.name}</h3>
      <p>{workout.duration} mins</p>
    </div>
  );
};
""",
        "src/App.tsx": """
import React from 'react';
import { WorkoutCard } from './components/WorkoutCard';

export function App() {
  return (
    <div className="app">
      <h1>FitSync App</h1>
    </div>
  );
}
""",
    }

    manifest_paths = []
    for rel_path, content in files.items():
        p = tmp_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.strip() + "\n", encoding="utf-8")
        manifest_paths.append(rel_path)

    manifest = ExtractionManifest(
        file_count=len(manifest_paths),
        total_extracted_bytes=1000,
        relative_paths=tuple(manifest_paths),
    )

    acquired = AcquiredSource(
        extraction_root=tmp_path,
        manifest=manifest,
        source_type="zip",
    )

    provider = _FakeEmbeddingProvider()
    repo = _InMemoryCodeChunkRepository()
    service = RepositoryIndexingService(provider=provider, code_chunk_repo=repo)

    result = service.index_acquired_source(
        acquired_source=acquired,
        owner_session_id="sess_fitsync_test",
        repository_id="repo_fitsync_test",
    )

    assert result.parsed_file_count == 5
    assert result.chunk_count > 0
    assert len(repo.saved_chunks) == result.chunk_count

    # Check extracted symbols across stored chunks
    extracted_symbols = {c.symbol_name for c in repo.saved_chunks}
    assert "Workout" in extracted_symbols
    assert "WorkoutId" in extracted_symbols
    assert "fetchWorkouts" in extracted_symbols
    assert "useWorkout" in extracted_symbols
    assert "WorkoutCardProps" in extracted_symbols
    assert "WorkoutCard" in extracted_symbols
    assert "App" in extracted_symbols

    # Verify scoping and deterministic chunk IDs
    for chunk in repo.saved_chunks:
        assert chunk.owner_session_id == "sess_fitsync_test"
        assert chunk.repository_id == "repo_fitsync_test"
        assert chunk.embedding_dimensions == 1536
        assert chunk.chunk_id.startswith("chunk_")


def test_fitsync_static_zero_token_indexing(tmp_path: Path) -> None:
    """Verify live FitSync synthetic project indexing in static mode with zero provider calls."""
    files = {
        "src/types/workout.ts": """
export interface Workout {
  id: string;
  name: string;
  duration: number;
}
export type WorkoutId = string;
""",
        "src/services/api.ts": """
import { Workout } from '../types/workout';

export async function fetchWorkouts(): Promise<Workout[]> {
  return [];
}

export const API_BASE_URL = 'https://api.fitsync.example.com';
""",
        "src/hooks/useWorkout.ts": """
import { useState, useEffect } from 'react';
import { Workout } from '../types/workout';
import { fetchWorkouts } from '../services/api';

export function useWorkout(id: string) {
  const [workout, setWorkout] = useState<Workout | null>(null);
  return { workout };
}
""",
        "src/components/WorkoutCard.tsx": """
import React from 'react';
import { Workout } from '../types/workout';

export interface WorkoutCardProps {
  workout: Workout;
}

export const WorkoutCard: React.FC<WorkoutCardProps> = ({ workout }) => {
  return (
    <div className="workout-card">
      <h3>{workout.name}</h3>
      <p>{workout.duration} mins</p>
    </div>
  );
};
""",
        "src/App.tsx": """
import React from 'react';
import { WorkoutCard } from './components/WorkoutCard';

export function App() {
  return (
    <div className="app">
      <h1>FitSync App</h1>
    </div>
  );
}
""",
    }

    manifest_paths = []
    for rel_path, content in files.items():
        p = tmp_path / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content.strip() + "\n", encoding="utf-8")
        manifest_paths.append(rel_path)

    manifest = ExtractionManifest(
        file_count=len(manifest_paths),
        total_extracted_bytes=1000,
        relative_paths=tuple(manifest_paths),
    )

    acquired = AcquiredSource(
        extraction_root=tmp_path,
        manifest=manifest,
        source_type="zip",
    )

    repo = _InMemoryCodeChunkRepository()
    # provider=None guarantees TRUE zero-token static inspection mode
    service = RepositoryIndexingService(provider=None, code_chunk_repo=repo)

    result = service.index_acquired_source(
        acquired_source=acquired,
        owner_session_id="sess_fitsync_static",
        repository_id="repo_fitsync_static",
    )

    assert result.parsed_file_count == 5
    assert result.chunk_count > 0
    assert len(repo.saved_chunks) == result.chunk_count

    # All chunks must have NO embeddings attached
    for chunk in repo.saved_chunks:
        assert chunk.embedding is None
        assert chunk.embedding_model is None
        assert chunk.embedding_dimensions is None
        assert len(chunk.search_terms) > 0
        assert chunk.owner_session_id == "sess_fitsync_static"
        assert chunk.repository_id == "repo_fitsync_static"

    # Verify key symbols
    extracted_symbols = {c.symbol_name for c in repo.saved_chunks}
    assert "Workout" in extracted_symbols
    assert "WorkoutCard" in extracted_symbols
    assert "useWorkout" in extracted_symbols
    assert "fetchWorkouts" in extracted_symbols

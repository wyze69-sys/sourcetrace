# SourceTrace v1 Design

## Summary

SourceTrace is a Python portfolio application that indexes a local Python repository and answers natural-language questions about the codebase. Answers are grounded in retrieved source code and include file paths and exact line ranges. Version 1 is a local, single-user application built with FastAPI, Streamlit, and ChromaDB.

## Goals

- Index local Python repositories without executing their code.
- Parse Python source into module, class, method, and function chunks using the standard-library AST.
- Preserve repository-relative file paths and source line ranges as chunk metadata.
- Retrieve relevant code through semantic vector search.
- Generate evidence-grounded answers through a provider-agnostic OpenAI-compatible API.
- Display citations and retrieved source passages in a simple Streamlit interface.
- Keep modules independently testable and easy to extend.

## Non-goals for v1

- JavaScript, TypeScript, Java, or other language parsing.
- GitHub OAuth or cloning private repositories.
- Executing, compiling, or modifying indexed repositories.
- Multi-user accounts, cloud deployment, billing, or collaboration.
- Autonomous code changes or pull requests.
- Full dependency or call-graph analysis.
- Production-scale distributed vector storage.

## Architecture

SourceTrace uses a layered modular monolith. All application logic lives in one Python package, but responsibilities are separated through focused modules and explicit interfaces.

```text
SourceTrace/
├── src/sourcetrace/
│   ├── api/           # FastAPI routes and transport schemas
│   ├── core/          # Configuration, logging, and shared exceptions
│   ├── ingestion/     # Repository scanning, filtering, and orchestration
│   ├── parsers/       # Python AST parsing and code-aware chunking
│   ├── retrieval/     # Semantic retrieval and result ranking
│   ├── generation/    # OpenAI-compatible client and grounded prompts
│   ├── storage/       # Chroma collections and repository metadata
│   └── models/        # Internal domain models
├── streamlit_app/     # Local indexing and chat interface
├── tests/             # Unit and integration tests
├── data/              # Runtime indexes; ignored by Git
├── docs/              # Design, plans, and usage documentation
├── scripts/           # Development helpers
├── .env.example
├── .gitignore
├── pyproject.toml
├── README.md
└── Makefile
```

## Components

### API

FastAPI exposes the application capabilities without containing business logic. Initial endpoints are:

- `GET /health`: report application readiness.
- `POST /repositories/index`: validate and index a local repository path.
- `GET /repositories`: list locally indexed repositories.
- `POST /chat`: answer a question against one indexed repository.
- `DELETE /repositories/{repository_id}`: delete only SourceTrace's local index and metadata, never the source repository.

### Core

Core configuration loads values from environment variables through typed settings. It defines storage paths, embedding configuration, LLM base URL, model name, API key, retrieval limits, and logging. Shared exceptions are translated into stable API errors.

### Ingestion

The scanner traverses a selected repository and accepts only supported Python text files. It excludes `.git`, virtual environments, dependency folders, caches, build artifacts, generated indexes, binary files, and common secret files. The ingestion service coordinates scanning, parsing, embedding, and storage.

### Python parser

The parser uses Python's `ast` module. It emits chunks for modules, classes, functions, async functions, and methods. Every chunk contains repository ID, relative path, symbol name, symbol type, source text, start line, and end line. Files with syntax errors produce a recorded parsing warning rather than aborting the whole repository index.

### Storage and retrieval

ChromaDB stores embeddings and chunk metadata in a local persistent directory. Each repository is logically isolated. Retrieval returns the most relevant chunks with similarity scores. Version 1 uses semantic retrieval only; keyword and hybrid retrieval are deferred until a measured need is established.

### Generation

The generation layer uses an OpenAI-compatible HTTP interface so users can configure a compatible hosted provider or local gateway. The prompt instructs the model to answer only from supplied code evidence, cite sources, and explicitly report insufficient evidence when the retrieved context does not support an answer.

### Streamlit interface

The UI has three simple areas:

1. Repository indexing: select or enter a local path and view indexing status.
2. Repository list: select an existing local index or delete an index.
3. Codebase chat: ask a question, read the grounded answer, and expand cited source passages.

The UI calls FastAPI rather than importing application internals directly, ensuring the API remains a usable portfolio artifact.

## Data flow

```text
Local repository path
→ path validation and safe scanner
→ Python AST parser
→ code chunks with file/line metadata
→ embedding model
→ repository-scoped Chroma collection
→ semantic retrieval for a question
→ grounded prompt with retrieved chunks
→ OpenAI-compatible LLM
→ answer, citations, and evidence passages
```

## Domain models

- `RepositoryRecord`: ID, display name, absolute source path, indexed timestamp, file count, chunk count, and status.
- `CodeChunk`: ID, repository ID, relative path, symbol name/type, source text, start line, and end line.
- `RetrievalResult`: code chunk plus relevance score.
- `ChatRequest`: repository ID, question, and optional retrieval limit.
- `ChatResponse`: answer, citations, evidence results, and insufficient-evidence indicator.

Repository metadata is stored locally alongside Chroma data. Source code is read for indexing but is never modified.

## Error handling

- Missing or inaccessible paths return a clear validation error.
- Non-directory paths and repositories with no supported files are rejected.
- Individual parse failures are collected as warnings; valid files continue indexing.
- Embedding, Chroma, and LLM failures are mapped to distinct application errors without exposing API keys.
- A missing repository index returns `404`.
- Low-confidence or empty retrieval returns an insufficient-evidence response instead of an invented answer.
- Deleting an index never deletes or edits the original repository.

## Security and privacy

- Indexed code is never executed.
- API keys are read from environment variables and excluded from Git.
- Common environment files, credentials, private keys, dependency directories, and binaries are excluded from ingestion by default.
- Paths are validated before access.
- The application is local and single-user in v1; no public deployment security claims are made.

## Testing strategy

- Scanner tests verify inclusion and exclusion behavior.
- Parser tests verify symbols, source text, and exact line ranges.
- Ingestion tests verify partial success when one file cannot be parsed.
- Storage tests use temporary Chroma directories.
- Retrieval tests verify repository isolation and result metadata.
- Generation tests mock the OpenAI-compatible client and verify grounded prompt construction.
- Citation tests verify stable `path:start-end` formatting.
- API tests exercise health, index, list, chat, and delete behavior.
- A small fixture repository supports deterministic integration tests.

## Success criteria

The MVP is successful when a user can index a local Python repository, ask where or how a feature is implemented, receive an answer based only on retrieved code, inspect citations with accurate file and line ranges, and run the automated test suite locally without requiring a live LLM for tests.

## Future extensions

After v1 is measured and stable, possible extensions include JavaScript/TypeScript parsing, BM25 and hybrid retrieval, reranking, incremental Git-aware indexing, architecture and dependency graphs, RAG evaluation, GitHub repository import, and local Ollama presets.

# SourceTrace

SourceTrace is a hybrid RAG-powered codebase intelligence platform. It allows users to submit public GitHub repository URLs or safe ZIP archive uploads and perform evidence-grounded natural-language inquiries with exact source file and line citations.

SourceTrace includes a **Zero-Token Free Static Inspection Mode** (symbol lexical term indexing and citation retrieval without requiring paid AI keys) alongside provider-neutral RAG embedding and generation capabilities.

---

## Architecture

```text
React + TypeScript Frontend (Vite)
            ↓ HTTP / JSON
Python FastAPI Backend
            ↓
Repository Parser (Python AST & Tree-Sitter JS/TS AST)
            ↓
Embeddings / Term Indexes → MongoDB Atlas Vector Search
            ↓
Retriever → Grounded LLM Generation → Evidence + Citations
```

---

## Project Structure

```text
SourceTrace/
├── frontend/                 # React 19 + TypeScript + Vite workspace
│   ├── src/
│   │   ├── app/              # Forensic workspace UI components & shell
│   │   ├── services/         # Typed API client & contracts
│   │   └── styles/           # Application styling & tokens
│   ├── tests/
│   ├── package.json
│   └── vite.config.ts
├── backend/                  # Python 3.11+ + FastAPI RAG engine
│   ├── src/sourcetrace/
│   │   ├── api/              # FastAPI v1 routes & schemas
│   │   ├── core/             # Configuration & session security
│   │   ├── embeddings/       # Provider adapters (Gemini / OpenAI)
│   │   ├── generation/       # Grounded LLM prompt & answer service
│   │   ├── ingestion/        # Safe scanner, acquisition & staging
│   │   ├── models/           # Domain data models
│   │   ├── parsers/          # Python AST & Tree-Sitter symbol parsers
│   │   ├── retrieval/        # Evidence retrieval & lexical search
│   │   ├── storage/          # PyMongo repositories & index manager
│   │   └── workers/          # Background indexing & session cleanup
│   ├── tests/                # Unit & integration test suites
│   ├── pyproject.toml
│   └── uv.lock
├── evals/                       # Reproducible RAG quality evaluation
│   ├── dataset.v1.json          # Benchmark query dataset
│   ├── fixtures/                # Evaluation test repositories
│   ├── run_eval.py              # Offline evaluation runner
│   └── eval_registry.yaml
├── .github/workflows/ci.yml     # GitHub Actions CI pipeline
└── render.yaml                  # Render Blueprint deployment configuration
```

---

## Quickstart

### Prerequisites

* Python >= 3.11
* Node.js >= 20
* `uv` package manager (`pip install uv`)
* MongoDB instance (local or MongoDB Atlas)

### Backend Setup

```bash
cd backend
cp .env.example .env
# Edit .env to set SOURCETRACE_MONGODB_URI

# Run tests
uv sync --extra dev
uv run pytest -q

# Start FastAPI server
uv run sourcetrace-api
```

### Frontend Setup

```bash
cd frontend
cp .env.example .env.local

# Install dependencies and run tests
npm install
npm test -- --run

# Run production build
npm run build

# Start Vite dev server
npm run dev
```

### RAG Evaluation Harness

```bash
uv run python evals/run_eval.py
```

---

## Safety & Security

* **No Unsafe Execution:** SourceTrace never executes indexed repository code or lifecycle scripts.
* **ZIP & GitHub Safety:** Acquisition enforces strict file size, compression ratio, path traversal (`..`), symlink, and redirect SSRF controls.
* **Secret Protection:** Grounded ingestion excludes credentials, `.env` files, private keys, binaries, and virtual environments.

---

## License

This project is licensed under the [MIT License](LICENSE).

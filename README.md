# SourceTrace

SourceTrace is a hybrid RAG-powered codebase intelligence project. The hosted application will let users submit public GitHub repositories or ZIP uploads and ask natural-language questions with exact source-file and line citations. A privacy-focused local/CLI mode can be added later.

## Current status

Architecture and directory scaffold only. Deep frontend, backend, RAG, database, and deployment implementation has not started.

## Planned architecture

```text
React + TypeScript frontend
          ↓ HTTP/JSON
Python FastAPI backend
          ↓
Repository parser → embeddings → MongoDB Atlas Vector Search
          ↓
Retriever → LLM → evidence-grounded answer with citations
```

## Project structure

```text
SourceTrace/
├── frontend/                 # React + TypeScript + Vite (planned)
│   ├── public/
│   ├── src/
│   │   ├── assets/
│   │   ├── components/
│   │   ├── hooks/
│   │   ├── pages/
│   │   ├── services/
│   │   └── types/
│   └── tests/
├── backend/                  # Python + FastAPI + RAG engine
│   ├── src/sourcetrace/
│   │   ├── api/
│   │   ├── core/
│   │   ├── generation/
│   │   ├── ingestion/
│   │   ├── models/
│   │   ├── parsers/
│   │   ├── retrieval/
│   │   └── storage/
│   ├── prompts/                 # Versioned production RAG prompts
│   │   ├── answer/
│   │   ├── retrieval/
│   │   ├── shared/
│   │   └── prompt_registry.yaml
│   ├── data/
│   │   ├── fixtures/            # Synthetic test inputs
│   │   ├── samples/             # Deliberately public demo inputs
│   │   └── runtime/             # Temporary local data; ignored
│   ├── tests/
│   ├── scripts/
│   ├── pyproject.toml
│   └── uv.lock
├── evals/                       # Reproducible RAG quality evaluation
│   ├── datasets/
│   ├── fixtures/
│   ├── tests/
│   ├── scorecards/
│   ├── traces/                  # Generated locally; ignored
│   ├── results/                 # Generated locally; ignored
│   └── eval_registry.yaml
```

## Delivery direction

1. Build and host the web version first.
2. Frontend: React, TypeScript, and Vite.
3. Backend: Python and FastAPI.
4. Database: MongoDB Atlas with Vector Search.
5. Inputs: public GitHub repository URLs and secure ZIP uploads.
6. Later: local CLI mode for private repositories and local models.

## Safety goals

SourceTrace must not execute indexed repository code. Repository ingestion must exclude secrets, private keys, `.env` files, dependency folders, generated output, and unsafe ZIP paths.



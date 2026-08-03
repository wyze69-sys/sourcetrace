.PHONY: install api ui test lint format check

install:
	uv sync --extra dev

api:
	uv run uvicorn sourcetrace.main:app --reload

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

check: lint test
	uv run python -m compileall src tests

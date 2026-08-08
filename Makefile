.PHONY: setup test lint format clean

setup:
	uv sync --all-extras

test:
	uv run pytest

lint:
	uv run ruff check chip_agent tests
	uv run mypy chip_agent

format:
	uv run ruff format chip_agent tests
	uv run ruff check --fix chip_agent tests

clean:
	rm -rf .ruff_cache .mypy_cache .pytest_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

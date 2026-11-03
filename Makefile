.PHONY: install test lint fmt demo

install:
	uv sync --all-extras

test:
	uv run pytest -q

lint:
	uv run ruff check . && uv run ruff format --check .

fmt:
	uv run ruff check --fix . && uv run ruff format .

demo:
	uv run python -m po_agent --sessions 6 --failure-rate 0.3 --seed 7

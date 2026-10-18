.DEFAULT_GOAL := help
.PHONY: help install lint format format-check typecheck test check run manifest clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Create the environment with uv, including dev tools
	uv sync --all-groups

lint: ## Run ruff lint
	uv run ruff check .

format: ## Apply ruff formatting and safe fixes
	uv run ruff format .
	uv run ruff check --fix .

format-check: ## Verify formatting without changing files
	uv run ruff format --check .

typecheck: ## Run mypy in strict mode
	uv run mypy

test: ## Run the test suite
	uv run pytest -q

check: lint format-check typecheck test ## Everything CI runs

run: ## Run the jotflow CLI
	uv run jotflow

manifest: ## Regenerate manifests/slack-app.json from the renderer
	uv run python -m jotflow.slack_manifest > manifests/slack-app.json

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

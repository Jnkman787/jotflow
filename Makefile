.DEFAULT_GOAL := help
.PHONY: help install lint format format-check typecheck test cov eol check run live-check eval-agent eval-execute manifest clean

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

test: ## Run the test suite, without coverage
	uv run pytest -q

cov: ## Run the tests with coverage; fails below the floor in pyproject.toml
	uv run pytest -q --cov --cov-report=term-missing
	uv run coverage report --format=total

eol: ## Fail if any tracked file has CRLF line endings
	@if git ls-files --eol | grep -E '^i/(crlf|mixed)|w/(crlf|mixed)'; then \
		echo "The files above have CRLF line endings. All files must use LF; see AGENTS.md."; exit 1; \
	fi

check: eol lint format-check typecheck cov ## Everything CI runs

run: ## Run the jotflow CLI
	uv run jotflow

live-check: ## Post and delete one test message in your workspace; needs JOTFLOW_LIVE_CHECK=1
	uv run jotflow check --live

eval-agent: ## Claude Code against every fixture, 3 times each; costs model tokens. Prints the run count; start with YES=1
	uv run jotflow replay --all --agent claude-code --repeat 3 $(if $(YES),--yes,)

eval-execute: ## Claude Code executing every fixture's proposals; costs model tokens. Prints the run count; start with YES=1
	uv run jotflow replay --all --agent claude-code --execute $(if $(YES),--yes,)

manifest: ## Regenerate manifests/slack-app.json from the renderer
	uv run python -m jotflow.slack_manifest > manifests/slack-app.json.tmp \
		&& mv manifests/slack-app.json.tmp manifests/slack-app.json \
		|| { rm -f manifests/slack-app.json.tmp; exit 1; }

clean: ## Remove caches and build output
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage dist build
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

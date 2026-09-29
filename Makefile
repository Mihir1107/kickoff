SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
TYPED_SRC := packages apps workers

.DEFAULT_GOAL := help
.PHONY: help sync up down nuke ps logs migrate lint fmt typecheck test test-integration test-all worker api check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

.env:
	cp .env.example .env

sync: ## Install/refresh the uv workspace
	uv sync --all-packages

up: .env ## Start all local infra and wait until healthy
	$(COMPOSE) up -d --wait

down: ## Stop infra (keeps volumes)
	$(COMPOSE) down

nuke: ## Stop infra AND delete volumes (the only way to discard WORM test data)
	$(COMPOSE) down -v

ps: ## Show infra status
	$(COMPOSE) ps

logs: ## Tail infra logs
	$(COMPOSE) logs -f --tail=100

migrate: ## Apply database migrations
	uv run alembic -c packages/core/alembic.ini upgrade head

lint: ## ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

fmt: ## Auto-format and fix lint
	uv run ruff format .
	uv run ruff check --fix .

typecheck: ## mypy --strict
	uv run mypy $(TYPED_SRC)

test: ## Unit tests (no services required)
	uv run pytest tests/unit

test-integration: ## Integration tests against compose services (run `make up` first)
	uv run pytest tests/integration -m "integration"

check: lint typecheck test ## Everything CI runs without services

worker: ## Run the collection worker
	uv run python -m edisc_worker

api: ## Run the API with autoreload
	uv run uvicorn edisc_api.main:app --reload --port 8000

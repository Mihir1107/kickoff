SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
TYPED_SRC := packages apps workers
# Long-running services. One-shot init jobs run separately because `up --wait` treats exited containers as failures.
SERVICES := postgres redis minio temporal temporal-ui elasticsearch
CI_SERVICES := postgres redis minio temporal
INIT_JOBS := minio-init temporal-namespace

.DEFAULT_GOAL := help
.PHONY: help sync up up-ci down nuke ps logs migrate lint fmt typecheck test test-integration test-all worker api check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

.env:
	cp .env.example .env

sync: ## Install/refresh the uv workspace
	uv sync --all-packages

up: .env ## Start all local infra, wait until healthy, run init jobs (idempotent)
	$(COMPOSE) up -d --wait $(SERVICES)
	@for job in $(INIT_JOBS); do $(COMPOSE) run --rm --no-deps $$job || exit 1; done

up-ci: .env ## Infra subset used by CI integration tests (no UI, no Elasticsearch)
	$(COMPOSE) up -d --wait $(CI_SERVICES)
	@for job in $(INIT_JOBS); do $(COMPOSE) run --rm --no-deps $$job || exit 1; done

down: ## Stop infra (keeps volumes)
	$(COMPOSE) down

nuke: .env ## Stop infra AND delete volumes. Refuses unless EDISC_ENV is local or ci
	@env_name=$$(grep -E '^EDISC_ENV=' .env | tail -1 | cut -d= -f2); \
	if [ "$$env_name" != "local" ] && [ "$$env_name" != "ci" ]; then \
	  echo "refusing: make nuke destroys evidence volumes; EDISC_ENV='$$env_name' (need local|ci)" >&2; exit 1; \
	fi
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
	uv run pytest tests/integration

check: lint typecheck test ## Everything CI runs without services

worker: ## Run the collection worker
	uv run python -m edisc_worker

api: ## Run the API with autoreload
	uv run uvicorn edisc_api.main:app --reload --port 8000

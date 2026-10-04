SHELL := /bin/bash
COMPOSE := docker compose -f infra/docker-compose.yml --env-file .env
# down/nuke/ps also cover optional profiles so nothing is left running or orphaned
COMPOSE_ALL := $(COMPOSE) --profile search
TYPED_SRC := packages apps workers
# Long-running services. One-shot init jobs run separately because `up --wait` treats exited containers as failures.
SERVICES := postgres redis minio temporal temporal-ui
CI_SERVICES := postgres redis minio temporal
INIT_JOBS := minio-init temporal-namespace
# Refuse to start infra or tests when the disk is nearly full (a full disk turns the Docker VM read-only).
MIN_FREE_GB ?= 15
# Ephemeral integration-test stack: separate compose project + ports; volumes destroyed after each run.
TEST_ENV := .env.test
TEST_COMPOSE := docker compose -p edisc-test -f infra/docker-compose.yml --env-file $(TEST_ENV)
TEST_TMP := $(or $(TMPDIR),/tmp)/edisc-tests
TEST_RUN := EDISC_ENV_FILE=$(TEST_ENV) EDISC_COMPOSE_PROJECT=edisc-test uv run
TEST_LOGS := test-stack-logs.txt
TESTS ?= tests/integration

.DEFAULT_GOAL := help
.PHONY: help hooks sync disk-guard test-env-up test-env-down up up-search up-ci down nuke ps logs migrate lint fmt typecheck test test-integration test-all worker api check

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

.env:
	cp .env.example .env

hooks: ## Install git hooks (pre-commit: ruff lint/format + mypy on staged Python files)
	git config core.hooksPath scripts/git-hooks
	@echo "git hooks installed from scripts/git-hooks"

sync: ## Install/refresh the uv workspace
	uv sync --all-packages

disk-guard: ## Refuse when free disk < MIN_FREE_GB (default 15)
	@free=$$(df -Pk . | awk 'NR==2 {print int($$4/1024/1024)}'); \
	if [ "$$free" -lt "$(MIN_FREE_GB)" ]; then \
	  echo "refusing: only $${free} GB free on this disk, need >= $(MIN_FREE_GB) GB." >&2; \
	  echo "  free space (Trash, 'docker system prune', 'make nuke') or set MIN_FREE_GB=..." >&2; exit 1; \
	fi; echo "disk ok: $${free} GB free"

$(TEST_ENV): .env.example scripts/make_test_env.py
	uv run python scripts/make_test_env.py > $(TEST_ENV)

test-env-up: disk-guard $(TEST_ENV) ## Start the ephemeral test stack (project edisc-test) and migrate it
	$(TEST_COMPOSE) up -d --wait $(CI_SERVICES)
	@for job in $(INIT_JOBS); do $(TEST_COMPOSE) run --rm --no-deps $$job || exit 1; done
	$(TEST_RUN) python -m edisc_db.bootstrap
	$(TEST_RUN) python -m edisc_db.migrate upgrade

test-env-down: ## Destroy the ephemeral test stack INCLUDING its volumes (all test evidence)
	-$(TEST_COMPOSE) down -v --remove-orphans

up: disk-guard .env ## Start all local infra, wait until healthy, run init jobs (idempotent)
	$(COMPOSE) up -d --wait $(SERVICES)
	@for job in $(INIT_JOBS); do $(COMPOSE) run --rm --no-deps $$job || exit 1; done

up-search: disk-guard .env ## Optional: start Elasticsearch (profile "search"; nothing uses it yet)
	$(COMPOSE) --profile search up -d --wait elasticsearch

up-ci: disk-guard .env ## Infra subset (no UI, no Elasticsearch)
	$(COMPOSE) up -d --wait $(CI_SERVICES)
	@for job in $(INIT_JOBS); do $(COMPOSE) run --rm --no-deps $$job || exit 1; done

down: ## Stop infra (keeps volumes)
	$(COMPOSE_ALL) down

nuke: .env ## Stop infra AND delete volumes. Refuses unless EDISC_ENV is local or ci
	@env_name=$$(grep -E '^EDISC_ENV=' .env | tail -1 | cut -d= -f2 | tr -d "'"); \
	if [ "$$env_name" != "local" ] && [ "$$env_name" != "ci" ] && [ "$$env_name" != "test" ]; then \
	  echo "refusing: make nuke destroys evidence volumes; EDISC_ENV='$$env_name' (need local|ci|test)" >&2; exit 1; \
	fi
	$(COMPOSE_ALL) down -v

ps: ## Show infra status
	$(COMPOSE_ALL) ps

logs: ## Tail infra logs
	$(COMPOSE) logs -f --tail=100

migrate: .env ## Bootstrap roles/schema (superuser, idempotent) then alembic upgrade head (owner role)
	uv run python -m edisc_db.bootstrap
	uv run python -m edisc_db.migrate upgrade

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

test-integration: disk-guard ## Integration tests on a FRESH ephemeral stack, destroyed afterwards (never the dev stack)
	@rm -rf $(TEST_TMP) && mkdir -p $(TEST_TMP)
	@status=0; \
	$(MAKE) test-env-up && \
	$(TEST_RUN) pytest $(TESTS) -m "not elasticsearch" --basetemp=$(TEST_TMP)/pytest $(PYTEST_ARGS) || status=$$?; \
	if [ $$status -ne 0 ]; then $(TEST_COMPOSE) logs --no-color --tail=200 > $(TEST_LOGS) 2>&1 || true; \
	  echo "test stack logs saved to $(TEST_LOGS)" >&2; fi; \
	$(MAKE) test-env-down; rm -rf $(TEST_TMP); exit $$status

test-integration-only: ## Run integration tests on an ALREADY RUNNING test stack (make test-env-up); keeps it
	@rm -rf $(TEST_TMP) && mkdir -p $(TEST_TMP)
	$(TEST_RUN) pytest $(TESTS) -m "not elasticsearch" --basetemp=$(TEST_TMP)/pytest $(PYTEST_ARGS)

check: lint typecheck test ## Everything CI runs without services

worker: ## Run the collection worker
	uv run python -m edisc_worker --source dummy --source slack_export --maintenance --exports --renders

api: ## Run the API with autoreload
	uv run uvicorn edisc_api.main:app --reload --port 8000

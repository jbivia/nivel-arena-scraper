# Variables
COMPOSE = podman compose
PYTHON = python3
VENV = .venv

# Throwaway PostgreSQL for the test suite, off the default port so it cannot
# collide with the real nivel-db on 5432.
TEST_DB_NAME = nivel-scraper-test-db
TEST_DB_PORT = 55432
TEST_DATABASE_URL = postgres://postgres:postgres@localhost:$(TEST_DB_PORT)/postgres

# Recipes that talk to PostgreSQL read the credentials from .env rather than
# taking them on the command line, where they would show up in `ps`.
LOAD_ENV = set -a; . ./.env; set +a;

# `nivel-db` only resolves inside the container network, so host-side recipes
# prefer SCRAPER_DATABASE_URL_LOCAL (localhost) when .env defines one.
# PYTHONPATH mirrors the Containerfile: `python main.py` puts only the repo root
# on sys.path, and the layered code lives under src/.
LOAD_ENV_HOST = $(LOAD_ENV) export SCRAPER_DATABASE_URL="$${SCRAPER_DATABASE_URL_LOCAL:-$$SCRAPER_DATABASE_URL}" \
                PYTHONPATH=src;

# Compiling the locks needs uv, which lives in the venv. Bootstrapping it is a
# chicken-and-egg case -- `make venv` installs from a lock -- so `make lock`
# falls back to a hash-pinned pip install of uv alone.
UV = $(VENV)/bin/uv
UV_COMPILE = $(UV) pip compile --universal --generate-hashes --no-annotate \
             --python-version 3.12 --custom-compile-command "make lock"

.PHONY: help setup build up up-d convert down logs shell nas-validate \
        venv lock lock-upgrade verify-locks test test-db-up test-db-down \
        lint fmt audit check \
        repair-db repair-db-apply import-sqlite import-sqlite-apply \
        backfill-metadata backfill-metadata-apply \
        purge-db purge-downloads

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Initialize host directories and the .env file
	mkdir -p downloads processed
	chmod 755 downloads processed
	@test -f .env || { cp .env.example .env; echo "Created .env -- set SCRAPER_DATABASE_URL before running."; }
	@# .env holds the database password; the default 0644 leaves it readable by
	@# every account on the machine. Reapplied on each run, not just on creation.
	@chmod 600 .env

# --- container ---------------------------------------------------------------

build: ## Build the scraper image
	$(COMPOSE) build

up: setup ## Start the scraper (rebuilds if necessary)
	$(COMPOSE) up --build

up-d: setup ## Start the scraper in detached mode
	$(COMPOSE) up -d --build

convert: setup ## Convert JPGs to transparent PNGs
	$(COMPOSE) run --rm scraper python convert_to_png.py

down: ## Stop and remove containers
	$(COMPOSE) down

logs: ## Follow container logs
	$(COMPOSE) logs -f

shell: ## Open a shell inside the running container
	$(COMPOSE) exec scraper /bin/sh

# The Synology files are never run on this machine, so nothing else would catch
# a typo in them before it reaches the NAS. Interpolated against
# .env.nas.example rather than .env so the result does not depend on whichever
# credentials happen to be configured locally.
nas-validate: ## Check that the Synology compose files parse and interpolate
	$(COMPOSE) -f compose.nas.yaml --env-file .env.nas.example config -q
	$(COMPOSE) -f compose.nas.tracker.yaml --env-file .env.nas.example config -q
	@echo "Synology compose files are valid."

# --- development -------------------------------------------------------------

venv: ## Create a local virtualenv from the hash-pinned dev lock
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q --require-hashes -r requirements-dev.lock

lock: ## Recompile the hash-pinned lock files from requirements*.txt
	@test -x $(UV) || $(VENV)/bin/pip install -q "uv==$$(sed -n 's/^uv==//p' requirements-dev.txt)"
	$(UV_COMPILE) requirements.txt -o requirements.lock
	$(UV_COMPILE) requirements-dev.txt -o requirements-dev.lock

lock-upgrade: ## Recompile the locks, taking the newest allowed transitive versions
	@test -x $(UV) || $(VENV)/bin/pip install -q "uv==$$(sed -n 's/^uv==//p' requirements-dev.txt)"
	$(UV_COMPILE) --upgrade requirements.txt -o requirements.lock
	$(UV_COMPILE) --upgrade requirements-dev.txt -o requirements-dev.lock

test: ## Run the test suite (DB tests skip unless SCRAPER_TEST_DATABASE_URL is set)
	$(VENV)/bin/pytest

test-db-up: ## Start a throwaway PostgreSQL for the test suite
	podman run -d --rm --name $(TEST_DB_NAME) \
		-e POSTGRES_PASSWORD=postgres \
		-p $(TEST_DB_PORT):5432 \
		docker.io/library/postgres:18-alpine
	@until podman exec $(TEST_DB_NAME) pg_isready -U postgres >/dev/null 2>&1; do sleep 1; done
	@echo "Ready. Run: SCRAPER_TEST_DATABASE_URL=$(TEST_DATABASE_URL) make test"

test-db-down: ## Stop the throwaway test PostgreSQL
	-podman stop $(TEST_DB_NAME)

lint: ## Check formatting and lint rules
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

fmt: ## Auto-format and auto-fix
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

audit: ## Scan the runtime and dev dependency trees for known vulnerabilities
	$(VENV)/bin/pip-audit --strict -r requirements.lock
	$(VENV)/bin/pip-audit --strict -r requirements-dev.lock

verify-locks: ## Fail if a lock has drifted from its requirements file
	@test -x $(UV) || $(VENV)/bin/pip install -q "uv==$$(sed -n 's/^uv==//p' requirements-dev.txt)"
	@tmp=$$(mktemp -d); cp requirements.lock requirements-dev.lock $$tmp/; \
		$(UV_COMPILE) requirements.txt -o $$tmp/requirements.lock >/dev/null 2>&1; \
		$(UV_COMPILE) requirements-dev.txt -o $$tmp/requirements-dev.lock >/dev/null 2>&1; \
		diff -u requirements.lock $$tmp/requirements.lock && \
		diff -u requirements-dev.lock $$tmp/requirements-dev.lock && \
		echo "Locks are in sync."; \
		status=$$?; rm -rf $$tmp; exit $$status

check: lint test verify-locks audit ## Run the full local verification suite

# --- maintenance -------------------------------------------------------------

repair-db: ## Preview repointing DB rows at their real filenames
	@$(LOAD_ENV_HOST) SCRAPER_DOWNLOADS_DIR=downloads $(VENV)/bin/python main.py --repair-filenames

repair-db-apply: ## Apply the DB filename repair
	@$(LOAD_ENV_HOST) SCRAPER_DOWNLOADS_DIR=downloads $(VENV)/bin/python main.py --repair-filenames --apply

import-sqlite: ## Preview importing history from the pre-PostgreSQL data/scraper.db
	@$(LOAD_ENV_HOST) SCRAPER_DOWNLOADS_DIR=downloads \
		$(VENV)/bin/python main.py --import-sqlite data/scraper.db

import-sqlite-apply: ## Import history from the pre-PostgreSQL data/scraper.db
	@$(LOAD_ENV_HOST) SCRAPER_DOWNLOADS_DIR=downloads \
		$(VENV)/bin/python main.py --import-sqlite data/scraper.db --apply

backfill-metadata: ## Preview fetching metadata for cards already downloaded
	@$(LOAD_ENV_HOST) SCRAPER_DOWNLOADS_DIR=downloads \
		$(VENV)/bin/python main.py --backfill-metadata

backfill-metadata-apply: ## Fetch metadata for cards already downloaded (no images are re-downloaded)
	@$(LOAD_ENV_HOST) SCRAPER_DOWNLOADS_DIR=downloads \
		$(VENV)/bin/python main.py --backfill-metadata --apply $(ARGS)

# Deliberately leaves `cards` alone: it belongs to the tracker app, and
# truncating it would cascade into the collection the user has built up.
purge-db: ## Delete the scraping history (requires CONFIRM=yes)
	@test "$(CONFIRM)" = "yes" || { \
		echo "This truncates scraped_cards in the shared nivel database."; \
		echo "The cards catalogue is left alone -- clearing it would take the collection with it."; \
		echo "Re-run with: make purge-db CONFIRM=yes"; exit 1; }
	@$(LOAD_ENV) podman exec -i nivel-db psql "$$SCRAPER_DATABASE_URL" \
		-c "TRUNCATE scraped_cards"
	@echo "Scraping history purged."

purge-downloads: ## Delete all downloaded images
	@echo "Warning: This will delete all downloaded images."
	rm -rf downloads/*
	@echo "Downloads purged."

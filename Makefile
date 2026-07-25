# Variables
COMPOSE = podman compose
DATA_DIR = data
DB_FILE = $(DATA_DIR)/scraper.db
PYTHON = python3
VENV = .venv

.PHONY: help setup build up up-d convert down logs shell \
        venv test lint fmt audit check repair-db repair-db-apply \
        purge-db purge-downloads

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

setup: ## Initialize host directories and permissions
	mkdir -p downloads processed data
	chmod 755 downloads processed data

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

# --- development -------------------------------------------------------------

venv: ## Create a local virtualenv with dev dependencies
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install -q -r requirements-dev.txt

test: ## Run the test suite
	$(VENV)/bin/pytest

lint: ## Check formatting and lint rules
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

fmt: ## Auto-format and auto-fix
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

audit: ## Scan dependencies for known vulnerabilities
	$(VENV)/bin/pip-audit -r requirements.txt

check: lint test audit ## Run the full local verification suite

# --- maintenance -------------------------------------------------------------

repair-db: ## Preview repointing DB rows at their real filenames
	SCRAPER_DB_PATH=$(DB_FILE) SCRAPER_DOWNLOADS_DIR=downloads \
		$(PYTHON) main.py --repair-filenames

repair-db-apply: ## Apply the DB filename repair
	SCRAPER_DB_PATH=$(DB_FILE) SCRAPER_DOWNLOADS_DIR=downloads \
		$(PYTHON) main.py --repair-filenames --apply

purge-db: ## Purge the SQLite database
	@echo "Warning: This will delete the scraping history."
	rm -f $(DB_FILE) $(DB_FILE)-wal $(DB_FILE)-shm
	@echo "Database purged."

purge-downloads: ## Delete all downloaded images
	@echo "Warning: This will delete all downloaded images."
	rm -rf downloads/*
	@echo "Downloads purged."

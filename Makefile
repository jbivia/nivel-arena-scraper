# Variables
COMPOSE = podman compose
DATA_DIR = data
DB_FILE = $(DATA_DIR)/scraper.db

.PHONY: help setup build up down logs purge-db purge-images shell

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

setup: ## Initialize host directories and permissions
	mkdir -p downloads processed data
	chmod 755 downloads processed data

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

purge-db: ## Purge the SQLite database
	@echo "Warning: This will delete the scraping history."
	rm -f $(DB_FILE)
	@echo "Database purged."

purge-downloads: ## Delete all downloaded images
	@echo "Warning: This will delete all downloaded images."
	rm -rf downloads/*
	@echo "Downloads purged."

shell: ## Open a shell inside the running container
	$(COMPOSE) exec scraper /bin/sh

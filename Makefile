# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

.DEFAULT_GOAL := help

PYTHON := .venv/bin/python

.PHONY: help sync licenses test test-e2e lint typecheck

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

sync: ## Sync the dev virtualenv from uv.lock
	uv sync --locked --all-extras

licenses: ## Regenerate docs/licenses/dependencies.md and sbom.cdx.json
	UV_PROJECT_ENVIRONMENT=.venv-licensing \
	  uv sync --locked --python 3.11 --all-extras
	.venv-licensing/bin/python scripts/check_licenses.py --write-report
	.venv-licensing/bin/cyclonedx-py environment \
	  --output-reproducible \
	  --pyproject pyproject.toml \
	  -o docs/licenses/sbom.cdx.json

test: sync ## Run unit tests (excludes e2e)
	$(PYTHON) -m pytest tests/ --ignore=tests/e2e/

test-e2e: sync ## Run e2e tests
	$(PYTHON) -m pytest tests/e2e/

lint: sync ## Run ruff linter and source header check
	$(PYTHON) scripts/check_source_headers.py
	$(PYTHON) scripts/check_skill_mirror.py
	$(PYTHON) scripts/check_skill_structure.py
	$(PYTHON) -m ruff check .

typecheck: sync ## Run targeted mypy type checks
	$(PYTHON) -m mypy

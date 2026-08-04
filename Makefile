# Copyright Elasticsearch B.V. and/or licensed to Elasticsearch B.V. under one or more contributor license agreements.
# SPDX-License-Identifier: Elastic-2.0

.DEFAULT_GOAL := help

PYTHON := .venv/bin/python

.PHONY: help sync licenses test test-e2e lint typecheck check-native-schema \
	setup-browser test-interactions interaction-audit-local bump-version

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

sync: ## Sync the dev virtualenv from uv.lock
	uv sync --locked --all-extras

bump-version: ## Bump version (VERSION=X.Y.Z), sync README/docs pins, refresh uv.lock + SBOM
	@test -n "$(VERSION)" || (echo "USAGE: make bump-version VERSION=X.Y.Z"; exit 2)
	@if [ -x "$(PYTHON)" ]; then \
	  $(PYTHON) scripts/bump_version.py "$(VERSION)"; \
	else \
	  python3 scripts/bump_version.py "$(VERSION)"; \
	fi
	@if [ "$(SKIP_LICENSES)" = "1" ]; then \
	  echo "SKIP_LICENSES=1: left docs/licenses unchanged (CI will fail if SBOM drifts)"; \
	else \
	  $(MAKE) licenses; \
	fi

licenses: ## Regenerate docs/licenses/dependencies.md and sbom.cdx.json
	@rm -rf ./*.egg-info
	UV_PROJECT_ENVIRONMENT=.venv-licensing \
	  uv sync --locked --python 3.11 --all-extras
	.venv-licensing/bin/python scripts/check_licenses.py --write-report
	.venv-licensing/bin/cyclonedx-py environment \
	  --output-reproducible \
	  --pyproject pyproject.toml \
	  -o docs/licenses/sbom.cdx.json
	@echo "licenses + SBOM refreshed"

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

check-native-schema: sync ## Check full Kibana Dashboards API OpenAPI schema
	$(PYTHON) scripts/fetch_dashboards_api_schema.py \
	  --check-only \
	  --require-full-schema

setup-browser: sync ## Install Chromium used by dashboard interaction tests
	$(PYTHON) -m playwright install chromium

test-interactions: sync ## Run offline interaction-audit unit tests
	$(PYTHON) -m pytest \
	  tests/test_interaction_audit.py \
	  tests/test_interaction_scenarios.py \
	  tests/test_interaction_driver.py \
	  tests/test_interaction_runner.py \
	  tests/test_interaction_canary.py \
	  tests/test_redis_interaction_scenario.py \
	  tests/test_k8s_views_global_interaction_scenario.py \
	  tests/test_interaction_audit_scripts.py

interaction-audit-local: ## Run live interaction scenarios on local Kibana
	@# Browser install is auto-skipped when Chromium is already available.
	@# Nightly CI sets FULL=1; locally prefer thinner seeds unless FULL=1.
	bash scripts/run_interaction_audit_local.sh

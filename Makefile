# Makefile for Promptise development and deployment

.PHONY: help install install-dev test lint format clean build publish docker-build docker-run docs serve-docs

# Default target
help:
	@echo "Promptise - Development Commands"
	@echo ""
	@echo "Setup:"
	@echo "  make install          Install Promptise"
	@echo "  make install-dev      Install with development dependencies"
	@echo ""
	@echo "Development:"
	@echo "  make test            Run tests"
	@echo "  make test-cov        Run tests with coverage"
	@echo "  make lint            Run linters (ruff)"
	@echo "  make format          Format code (ruff)"
	@echo "  make type-check      Run type checking (mypy)"
	@echo "  make pre-commit      Run pre-commit hooks"
	@echo ""
	@echo "Build & Publish:"
	@echo "  make clean           Clean build artifacts"
	@echo "  make build           Build distribution packages"
	@echo "  make publish         Publish to PyPI"
	@echo "  make publish-test    Publish to TestPyPI"
	@echo ""
	@echo "Docker:"
	@echo "  make docker-build    Build Docker image"
	@echo "  make docker-run      Run Docker container"
	@echo "  make docker-compose  Start all services with docker-compose"
	@echo ""
	@echo "Documentation:"
	@echo "  make docs            Build documentation"
	@echo "  make serve-docs      Serve documentation locally"
	@echo ""
	@echo "Examples:"
	@echo "  make run-example     Run basic example"
	@echo "  make run-orchestration  Run orchestration example"

# Installation
install:
	pip install -e .

install-dev:
	pip install -e ".[dev,orchestration,sandbox]"
	pre-commit install

# Testing
test:
	pytest tests/ -v

test-cov:
	pytest tests/ -v --cov=src/promptise --cov-report=html --cov-report=term

test-integration:
	pytest tests/ -v -m integration

# Linting and formatting
lint:
	ruff check src/ tests/ examples/

format:
	ruff format src/ tests/ examples/
	ruff check --fix src/ tests/ examples/

type-check:
	mypy src/

pre-commit:
	pre-commit run --all-files

# Build and publish
clean:
	rm -rf build/ dist/ *.egg-info site/
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

build: clean
	python -m build

publish: build
	twine upload dist/*

publish-test: build
	twine upload --repository testpypi dist/*

# Docker
docker-build:
	docker build -t promptise:latest .

docker-run:
	docker run -it --rm \
		-e OPENAI_API_KEY=${OPENAI_API_KEY} \
		-p 8000:8000 \
		promptise:latest

docker-compose:
	docker-compose up -d

docker-compose-down:
	docker-compose down -v

# Documentation
docs:
	mkdocs build --strict

serve-docs:
	mkdocs serve

deploy-docs:
	mkdocs gh-deploy --force

# Examples
run-example:
	python examples/agents/use_agent.py

run-orchestration:
	python examples/orchestration/01-basic-pool/basic_pool.py

run-swarm:
	python examples/orchestration/03-autonomous-swarm/simple_broadcast.py

# CI/CD helpers
ci-install:
	pip install -e ".[dev,orchestration,sandbox]"

ci-test:
	pytest tests/ -v --cov=src/promptise --cov-report=xml --cov-report=term

ci-lint:
	ruff check src/ tests/
	mypy src/

# Development shortcuts
dev-setup: install-dev
	@echo "Development environment ready!"
	@echo "Run 'make test' to run tests"
	@echo "Run 'make serve-docs' to preview documentation"

check: lint type-check test
	@echo "All checks passed!"

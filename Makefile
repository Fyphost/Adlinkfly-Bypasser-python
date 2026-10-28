.PHONY: help install install-dev browsers run dev test test-browser lint format typecheck docker-build docker-run

help:
	@echo "Targets:"
	@echo "  install       Install runtime dependencies"
	@echo "  install-dev   Install dev + runtime dependencies"
	@echo "  browsers      Install the Playwright Chromium browser"
	@echo "  run           Run the API (uvicorn)"
	@echo "  dev           Run the API with autoreload"
	@echo "  test          Run the test suite"
	@echo "  test-browser  Run opt-in live browser tests"
	@echo "  lint          Run ruff"
	@echo "  typecheck     Run mypy"
	@echo "  docker-build  Build the Docker image"
	@echo "  docker-run    Run the Docker image"

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

browsers:
	playwright install --with-deps chromium

run:
	uvicorn app.main:app --host 0.0.0.0 --port 8000

dev:
	uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

test:
	pytest

test-browser:
	RUN_BROWSER_TESTS=1 pytest -m browser

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy app

docker-build:
	docker build -t adlinkfly-resolver:latest .

docker-run:
	docker run --rm -p 8000:8000 --shm-size=1g --env-file .env adlinkfly-resolver:latest

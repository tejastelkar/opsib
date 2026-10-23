export OPSIB_DATABASE_URL ?= postgresql+psycopg://opsib@127.0.0.1:55433/opsib
export TEST_DATABASE_URL ?= postgresql+psycopg://opsib@127.0.0.1:55433/opsib_test

.PHONY: install db-up db-down migrate run demo test lint openapi

install:          ## install dependencies (Python 3.12 via uv)
	uv sync

db-up:            ## start a local PostgreSQL without Docker (or: docker compose up -d db)
	scripts/dev_postgres.sh start

db-down:
	scripts/dev_postgres.sh stop

migrate:          ## apply database migrations
	uv run alembic upgrade head

run:              ## start the API on http://127.0.0.1:8000 (docs at /docs)
	uv run uvicorn --factory app.api.main:create_app --host 127.0.0.1 --port 8000

demo:             ## load the fixture pack, reconcile, print the outcome
	uv run python -m app.cli demo

test:             ## run the whole test suite (needs TEST_DATABASE_URL)
	uv run pytest

lint:
	uvx ruff@0.14.0 check app tests migrations
	uvx ruff@0.14.0 format --check app tests migrations

openapi:          ## regenerate docs/openapi.json
	uv run python -m app.cli export-openapi docs/openapi.json

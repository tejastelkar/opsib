import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.api.main import create_app
from app.config import Settings
from app.container import Services, build_services
from app.db.base import Base
from app.db.session import make_engine

ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://opsib@127.0.0.1:55433/opsib_test")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "integration" in item.path.parts:
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    database = make_url(TEST_DATABASE_URL).database or ""
    if not database.endswith("_test"):
        pytest.fail(
            f"refusing to run against database '{database}': the test suite drops and recreates the schema, "
            "so TEST_DATABASE_URL must name a database ending in _test",
            pytrace=False,
        )
    engine = make_engine(TEST_DATABASE_URL)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError as exc:
        pytest.fail(
            f"cannot reach TEST_DATABASE_URL ({make_url(TEST_DATABASE_URL).render_as_string(hide_password=True)}): "
            f"{type(exc.orig).__name__}. Start PostgreSQL with `scripts/dev_postgres.sh start` or "
            "`docker compose up -d db` (see README).",
            pytrace=False,
        )
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")
    yield engine
    engine.dispose()


@pytest.fixture
def clean_db(db_engine: Engine) -> None:
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with db_engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, database_url=TEST_DATABASE_URL, log_level="WARNING")  # type: ignore[call-arg]


@pytest.fixture
def make_services(db_engine: Engine, clean_db: None, settings: Settings) -> Callable[..., Services]:
    def factory(**overrides: Any) -> Services:
        settings_overrides = overrides.pop("settings", {})
        effective = settings.model_copy(update=settings_overrides)
        return build_services(effective, engine=db_engine, **overrides)

    return factory


@pytest.fixture
def services(make_services: Callable[..., Services]) -> Services:
    return make_services()


@pytest.fixture
def make_client() -> Iterator[Callable[[Services], TestClient]]:
    clients: list[TestClient] = []

    def factory(svc: Services) -> TestClient:
        client = TestClient(create_app(svc))
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def client(services: Services, make_client: Callable[[Services], TestClient]) -> TestClient:
    return make_client(services)

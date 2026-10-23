from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker


def make_engine(database_url: str) -> Engine:
    # Every connection works in UTC; offsets are preserved by timestamptz anyway.
    return create_engine(
        database_url,
        pool_pre_ping=True,
        connect_args={"options": "-c timezone=UTC"},
    )


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # autobegin=False: any query outside an explicit `with session.begin():` raises,
    # so every unit of work has a visible transaction boundary.
    return sessionmaker(engine, autobegin=False, expire_on_commit=False)


def advisory_xact_lock(session: Session, *parts: str) -> None:
    """Serialise writers on a logical resource (e.g. tenant + dataset) for the rest of
    the current transaction. Released automatically at commit or rollback."""
    key = ":".join(parts)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key})


def advisory_xact_lock_shared(session: Session, *parts: str) -> None:
    """Shared form of advisory_xact_lock: many holders at once, but excludes the
    exclusive holder. Readers that need a stable view of a resource take this."""
    key = ":".join(parts)
    session.execute(text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"), {"key": key})

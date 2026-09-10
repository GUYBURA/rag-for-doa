import pathlib
import psycopg
import pytest
from testcontainers.community.postgres import PostgresContainer

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"


def _executable(sql: str) -> str:
    r"""Drop psql meta-commands; the server does not understand them.

    Only \set ON_ERROR_STOP on today. Losing it costs nothing here: psycopg
    sends the file as one multi-statement query inside one transaction, so the
    first failing statement raises and rolls back everything before it. That is
    stricter than ON_ERROR_STOP, which only stops.
    """
    return "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("\\")
    )


@pytest.fixture(scope="session")
def postgres_url():
    """Boot pgvector once, apply the schema once, hand back a DSN.

    Every migration is applied in filename order, not just 001_init.sql, so a
    new numbered file cannot leave the test schema behind the real one.
    """
    with PostgresContainer("pgvector/pgvector:pg17") as pg:
        url = pg.get_connection_url(driver=None)
        with psycopg.connect(url) as conn:
            for migration in sorted(MIGRATIONS.glob("*.sql")):
                conn.execute(_executable(migration.read_text(encoding="utf-8")))
            conn.commit()
        yield url


@pytest.fixture
def db_conn(postgres_url):
    """A connection whose transaction is never committed.

    Isolation is rollback, not TRUNCATE: invariant 3 forbids deleting document
    rows anywhere, and state that was never committed needs no deleting. This
    works only because db.py leaves the transaction to its caller -- psycopg
    turns run.py's inner `with conn.transaction()` into a SAVEPOINT rather than
    a commit, so one rollback here undoes the whole test.
    """
    with psycopg.connect(postgres_url) as conn:
        yield conn
        conn.rollback()

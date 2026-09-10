import pathlib

import psycopg
import pytest
from dotenv import load_dotenv
from testcontainers.community.postgres import PostgresContainer

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATIONS = REPO_ROOT / "db" / "migrations"

# Loaded at collection time, once, for every test in the suite -- not inside a
# fixture -- because OPENROUTER_API_KEY has to be in os.environ before
# test_embed.py's skipif() decorators even run.
load_dotenv(REPO_ROOT / ".env")


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


CORPUS_DB = "corpus"


def _with_database(url: str, name: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{name}"


@pytest.fixture(scope="session")
def corpus_url(postgres_url):
    """A second database in the same container, holding a committed corpus.

    Retrieval cannot use the db_conn fixture. PGVectorStore opens its own
    asyncpg pool, so it sees only committed rows, while db_conn's whole
    isolation strategy is to never commit. Committing into the main test
    database instead would be visible to every other test -- test_run.py
    asserts `SELECT count(*) FROM document` is 1 -- so retrieval gets a
    database of its own.

    Nothing is deleted at teardown, and nothing needs to be: the container goes
    away at the end of the session, and invariant 3 forbids deleting document
    rows anyway.
    """
    with psycopg.connect(postgres_url, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{CORPUS_DB}"')

    url = _with_database(postgres_url, CORPUS_DB)
    with psycopg.connect(url) as conn:
        for migration in sorted(MIGRATIONS.glob("*.sql")):
            conn.execute(_executable(migration.read_text(encoding="utf-8")))
        conn.commit()
    yield url


@pytest.fixture(scope="session")
def ingested_corpus(corpus_url):
    """Ingest the fixture PDF into the corpus database, once, and commit.

    Session-scoped because it costs a real embedding call. Every retrieval test
    reads the same rows and none of them write, so sharing is safe.
    """
    from ingest.run import DocumentMeta, ingest

    meta = DocumentMeta(
        source="excerpt.pdf",
        title_th="เอกสารทดสอบ",
        edition_year_be=2568,
        scopes=["fungicide", "insecticide", "herbicide"],
    )
    with psycopg.connect(corpus_url) as conn:
        ingest(conn, str(REPO_ROOT / "tests" / "fixtures" / "excerpt.pdf"), meta)
        conn.commit()
    yield corpus_url


@pytest.fixture
def corpus_conn(ingested_corpus):
    """A read-only-by-convention connection to the ingested corpus."""
    with psycopg.connect(ingested_corpus) as conn:
        yield conn
        conn.rollback()

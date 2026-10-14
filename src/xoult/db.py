from functools import cache
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from xoult.config import settings

SCHEMA = Path(__file__).parents[2] / "db" / "schema.sql"


@cache
def get_pool() -> ConnectionPool:
    # prepare_threshold=None: Supabase's transaction pooler (pgbouncer) can't handle
    # server-side prepared statements, and they buy us nothing at this query volume.
    return ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=5,
        kwargs={"prepare_threshold": None},
        configure=register_vector,
        open=True,
    )


def apply_schema() -> None:
    # Plain connection, not the pool: register_vector fails until the extension exists.
    with psycopg.connect(settings.database_url) as conn:
        conn.execute(SCHEMA.read_text())


if __name__ == "__main__":
    apply_schema()
    with get_pool().connection() as conn:
        tables = conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' order by 1"
        ).fetchall()
    print("schema ok:", ", ".join(t[0] for t in tables))

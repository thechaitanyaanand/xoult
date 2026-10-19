from functools import cache
from pathlib import Path

import psycopg
from pgvector.psycopg import register_vector
from psycopg_pool import ConnectionPool

from xoult.config import settings

SCHEMA = Path(__file__).parents[2] / "db" / "schema.sql"
# Fail fast on a wrong address instead of hanging until the CI job's timeout.
CONNECT_TIMEOUT = 10


@cache
def get_pool() -> ConnectionPool:
    return ConnectionPool(
        settings.database_url,
        # min_size=0: open connections only when a request needs one. Against a database
        # rejecting logins this halved the attempts (12 → 6 in 40 s under a 2 s health
        # check), which delays Supabase's lockout but can't prevent it: the only real
        # fix for a wrong password is the right one.
        min_size=0,
        max_size=5,
        max_idle=600,
        kwargs={
            # Autocommit: no separate BEGIN/COMMIT round trips, which matter when the
            # database is a region away. Multi-step writes use conn.transaction().
            "autocommit": True,
            # Supabase's transaction pooler can't handle server-side prepared statements.
            "prepare_threshold": None,
            "connect_timeout": CONNECT_TIMEOUT,
        },
        configure=register_vector,
        open=True,
    )


def apply_schema() -> None:
    # Plain connection, not the pool: register_vector fails until the extension exists.
    with psycopg.connect(settings.database_url, connect_timeout=CONNECT_TIMEOUT) as conn:
        conn.execute(SCHEMA.read_text())


# Connection failures each have one usual cause; say it, so a CI log is enough to fix it.
HINTS = {
    "tenant/user": "wrong pooler cluster: copy the host exactly from Supabase → Connect",
    "password authentication failed": "wrong password, or one with @ : / ? # not URL-encoded",
    "ecircuitbreaker": "Supabase blocked logins after repeated wrong passwords: fix the "
    "password everywhere it's used, stop the failing clients, then wait a few minutes",
    "network is unreachable": "that's the IPv6-only direct host; use the Session pooler URI",
    "connection refused": "nothing listening there: is DATABASE_URL set, and the database up?",
    "timeout expired": "no answer: is DATABASE_URL set (it defaults to localhost), and reachable?",
    "could not translate host name": "the host name is mistyped",
}


def explain_failure(error: psycopg.OperationalError) -> str:
    target = psycopg.conninfo.conninfo_to_dict(settings.database_url)  # password never shown
    where = f"{target.get('user')}@{target.get('host')}:{target.get('port', 5432)}"
    message = str(error).strip().splitlines()[-1]
    hint = next((h for key, h in HINTS.items() if key in str(error).lower()), "")
    return f"can't connect to {where}: {message}" + (f"\nlikely cause: {hint}" if hint else "")


if __name__ == "__main__":
    try:
        apply_schema()
    except psycopg.OperationalError as e:
        raise SystemExit(explain_failure(e)) from None
    with get_pool().connection() as conn:
        tables = conn.execute(
            "select table_name from information_schema.tables "
            "where table_schema = 'public' order by 1"
        ).fetchall()
    print("schema ok:", ", ".join(t[0] for t in tables))

"""Read-only Postgres connection for the growth agent.

Uses a dedicated GROWTH_AGENT_DATABASE_URL (a separate, read-only role -
see README for the GRANT script) so this process can never write to
DataLayer's production database.

Connects via psycopg2 kwargs parsed through SQLAlchemy's make_url()
rather than handing psycopg2 a raw DSN string, mirroring
../datalayer-ecommerce/services/db.py's _psycopg2_connect_kwargs(): psycopg2
delegates DSN parsing to libpq, a strict RFC 3986 parser that breaks on
unescaped URI-reserved characters (@, :, /, etc.) in the password.
SQLAlchemy's make_url() is a lenient regex parser that tolerates this,
so we use it to pre-split the URL instead.
"""
import os
from typing import Any, Dict

import psycopg2
from sqlalchemy.engine import make_url


def _get_database_url() -> str:
    url = os.environ.get('GROWTH_AGENT_DATABASE_URL', '')
    if not url:
        raise RuntimeError('GROWTH_AGENT_DATABASE_URL is not set')
    return url


def _psycopg2_connect_kwargs(url: str) -> Dict[str, Any]:
    parsed = make_url(url)
    kwargs: Dict[str, Any] = {
        'host': parsed.host,
        'port': parsed.port,
        'user': parsed.username,
        'password': parsed.password,
        'dbname': parsed.database,
    }
    kwargs.update(parsed.query)
    return {k: v for k, v in kwargs.items() if v is not None}


def connect_to_db():
    """Return a new read-only psycopg2 connection. Caller must close it."""
    conn = psycopg2.connect(**_psycopg2_connect_kwargs(_get_database_url()))
    conn.set_session(readonly=True, autocommit=True)
    return conn

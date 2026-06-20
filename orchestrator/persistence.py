"""Local, offline chat-history persistence for the Chainlit UI.

Everything lives in a single SQLite file on disk (no server, no cloud, works
fully offline). Chainlit's :class:`SQLAlchemyDataLayer` reads/writes the
standard thread/step/element schema; we create that schema up front with a
plain ``sqlite3`` connection so the very first run has the tables ready.

The data layer is what powers the ChatGPT-style sidebar: every conversation
becomes a *thread* the user can reopen later (see ``@cl.on_chat_resume`` in
``app.py``).
"""

from __future__ import annotations

import logging
import os
import sqlite3

from orchestrator import config

logger = logging.getLogger("zero_agent.persistence")

# Standard Chainlit data-layer schema, written with SQLite-friendly types.
# SQLite has loose type affinity, so TEXT/INTEGER stand in for the UUID/JSONB/
# BOOLEAN names used by the Postgres reference schema.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    "id" TEXT PRIMARY KEY,
    "identifier" TEXT NOT NULL UNIQUE,
    "metadata" TEXT NOT NULL,
    "createdAt" TEXT
);

CREATE TABLE IF NOT EXISTS threads (
    "id" TEXT PRIMARY KEY,
    "createdAt" TEXT,
    "name" TEXT,
    "userId" TEXT,
    "userIdentifier" TEXT,
    "tags" TEXT,
    "metadata" TEXT,
    FOREIGN KEY ("userId") REFERENCES users("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS steps (
    "id" TEXT PRIMARY KEY,
    "name" TEXT NOT NULL,
    "type" TEXT NOT NULL,
    "threadId" TEXT NOT NULL,
    "parentId" TEXT,
    "streaming" INTEGER NOT NULL,
    "waitForAnswer" INTEGER,
    "isError" INTEGER,
    "metadata" TEXT,
    "tags" TEXT,
    "input" TEXT,
    "output" TEXT,
    "createdAt" TEXT,
    "command" TEXT,
    "start" TEXT,
    "end" TEXT,
    "generation" TEXT,
    "showInput" TEXT,
    "language" TEXT,
    "indent" INTEGER,
    "defaultOpen" INTEGER,
    "autoCollapse" INTEGER,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS elements (
    "id" TEXT PRIMARY KEY,
    "threadId" TEXT,
    "type" TEXT,
    "url" TEXT,
    "chainlitKey" TEXT,
    "name" TEXT NOT NULL,
    "display" TEXT,
    "objectKey" TEXT,
    "size" TEXT,
    "page" INTEGER,
    "language" TEXT,
    "forId" TEXT,
    "mime" TEXT,
    "props" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedbacks (
    "id" TEXT PRIMARY KEY,
    "forId" TEXT NOT NULL,
    "threadId" TEXT NOT NULL,
    "value" INTEGER NOT NULL,
    "comment" TEXT,
    FOREIGN KEY ("threadId") REFERENCES threads("id") ON DELETE CASCADE
);
"""


def ensure_schema(db_path: str | None = None) -> str:
    """Create the SQLite file (and parent dir) with the Chainlit schema.

    Idempotent: safe to call on every startup. Returns the resolved db path.
    """
    path = db_path or config.HISTORY_DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(_SCHEMA)
        _migrate_columns(conn)
        conn.commit()
    finally:
        conn.close()
    logger.info("chat-history schema ready at %s", path)
    return path


#: Columns newer Chainlit releases expect but that older DB files may lack.
#: ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so we add
#: them here. Each entry is (table, column, sql_type).
_COLUMN_MIGRATIONS = (
    ("steps", "autoCollapse", "INTEGER"),
)


def _migrate_columns(conn: "sqlite3.Connection") -> None:
    """Add any missing columns to existing tables (idempotent).

    Keeps DB files created by an older Chainlit working after upgrades without
    deleting saved history.
    """
    for table, column, sql_type in _COLUMN_MIGRATIONS:
        cols = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if column not in cols:
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}')
            logger.info("migrated: added %s.%s", table, column)


def sqlite_url(db_path: str | None = None) -> str:
    """Build the async SQLAlchemy URL for the local SQLite file."""
    path = (db_path or config.HISTORY_DB_PATH).replace("\\", "/")
    return f"sqlite+aiosqlite:///{path}"


def build_data_layer():
    """Return a Chainlit SQLAlchemy data layer backed by the local SQLite file.

    Returns ``None`` if persistence is disabled or the data layer cannot be
    constructed, so the UI still runs (just without saved history).
    """
    if not config.PERSIST_CHATS:
        logger.info("chat persistence disabled (ZERO_AGENT_PERSIST_CHATS=0)")
        return None
    try:
        from chainlit.data.sql_alchemy import SQLAlchemyDataLayer

        ensure_schema()
        # No storage_provider: text/threads persist locally; generated images
        # are still shown live in-thread, just not re-fetched on resume.
        return SQLAlchemyDataLayer(conninfo=sqlite_url())
    except Exception:  # noqa: BLE001 - never block the UI over history setup
        logger.exception("failed to initialize chat persistence; running without it")
        return None

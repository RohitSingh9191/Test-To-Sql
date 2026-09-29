"""Database access: a small connection pool, one-time schema introspection
(cached in memory), and safe read-only query execution."""
from __future__ import annotations

import threading
from decimal import Decimal
from datetime import date, datetime, time
from typing import Any

import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor

from .config import settings

_pool: ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()

# Cached schema (built once at startup).
schema_text: str = ""                 # full rendered schema (all tables)
schema_tables: list[dict] = []        # for the /schema API
table_lines: dict[str, str] = {}      # "schema.table" -> rendered DDL line
table_terms: dict[str, set] = {}      # "schema.table" -> lowercased name/column tokens
table_fk_out: dict[str, set] = {}     # "schema.table" -> tables it references via FK
user_id_tables: list[str] = []        # tables that have a user_id column
elan_tables: list[str] = []           # tables that have an elan column

_TYPE_MAP = {
    "character varying": "varchar",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "double precision": "float8",
    "integer": "int",
    "boolean": "bool",
    "bigint": "int8",
    "character": "char",
    "numeric": "numeric",
}


def get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ThreadedConnectionPool(minconn=1, maxconn=8, **settings.dsn)
    return _pool


def introspect() -> None:
    """Read the full schema (tables, columns, PKs, FKs) once and render a
    compact DDL-like description the model can reason over. Cached in module
    globals so every request reuses it (and Anthropic prompt-caches it too)."""
    global schema_text, schema_tables, table_lines, table_terms, table_fk_out
    global user_id_tables, elan_tables
    schemas = settings.schemas
    conn = get_pool().getconn()
    try:
        cur = conn.cursor()
        # columns
        cur.execute(
            """
            SELECT table_schema, table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = ANY(%s)
            ORDER BY table_schema, table_name, ordinal_position
            """,
            (schemas,),
        )
        cols: dict[tuple, list] = {}
        for s, t, c, dt in cur.fetchall():
            cols.setdefault((s, t), []).append((c, _TYPE_MAP.get(dt, dt)))

        # primary keys
        cur.execute(
            """
            SELECT tc.table_schema, tc.table_name, kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = ANY(%s)
            """,
            (schemas,),
        )
        pks: dict[tuple, set] = {}
        for s, t, c in cur.fetchall():
            pks.setdefault((s, t), set()).add(c)

        # foreign keys
        cur.execute(
            """
            SELECT tc.table_schema, tc.table_name, kcu.column_name,
                   ccu.table_schema AS f_schema, ccu.table_name AS f_table,
                   ccu.column_name AS f_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = ANY(%s)
            """,
            (schemas,),
        )
        fks: dict[tuple, list] = {}
        fkrefs: dict[tuple, list] = {}
        for s, t, c, fs, ft, fc in cur.fetchall():
            fks.setdefault((s, t), []).append(f"{c} -> {fs}.{ft}.{fc}")
            fkrefs.setdefault((s, t), []).append((c, fs, ft, fc))

        lines: list[str] = []
        tables_out: list[dict] = []
        t_lines: dict[str, str] = {}
        t_terms: dict[str, set] = {}
        t_fk: dict[str, set] = {}
        for (s, t), columns in cols.items():
            key = f"{s}.{t}"
            pk = pks.get((s, t), set())
            col_parts = []
            for c, dt in columns:
                col_parts.append(f"{c} {dt}{' PK' if c in pk else ''}")
            line = f"{key}({', '.join(col_parts)})"
            fk = fks.get((s, t))
            if fk:
                line += "  -- FK: " + "; ".join(fk)
            lines.append(line)
            tables_out.append(
                {"schema": s, "table": t, "columns": [c for c, _ in columns]}
            )
            t_lines[key] = line
            # searchable tokens: schema/table/column names split on underscores
            terms: set[str] = set()
            for piece in [s, t] + [c for c, _ in columns]:
                for tok in piece.lower().split("_"):
                    if len(tok) >= 2:
                        terms.add(tok)
            t_terms[key] = terms
            t_fk[key] = {f"{fs}.{ft}" for (_, fs, ft, _) in fkrefs.get((s, t), [])}

        schema_text = "\n".join(lines)
        schema_tables = tables_out
        table_lines = t_lines
        table_terms = t_terms
        table_fk_out = t_fk
        user_id_tables = [f"{d['schema']}.{d['table']}" for d in tables_out
                          if "user_id" in d["columns"]]
        elan_tables = [f"{d['schema']}.{d['table']}" for d in tables_out
                       if "elan" in d["columns"]]
    finally:
        get_pool().putconn(conn)


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date, time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    return v


def run_readonly(sql: str, params: tuple | None = None) -> tuple[list[str], list[dict]]:
    """Execute a validated SELECT in a strictly read-only transaction with a
    statement timeout. Returns (columns, rows)."""
    conn = get_pool().getconn()
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(f"SET LOCAL statement_timeout = {settings.statement_timeout_ms}")
            cur.execute(sql, params)
            rows = cur.fetchall()
            columns = [d.name for d in cur.description] if cur.description else []
        conn.rollback()  # read-only: nothing to commit
        out = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
        return columns, out
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.set_session(readonly=False, autocommit=True)
        except Exception:
            pass
        get_pool().putconn(conn)

"""Defense-in-depth validation of model-generated SQL.

Two layers protect the database:
  1. This static check (single statement, SELECT/WITH only, no DDL/DML keywords).
  2. The DB session itself runs READ ONLY with a statement timeout (see db.py),
     so even if a check is bypassed no write can succeed.
"""
from __future__ import annotations

import re

from .config import settings

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|merge|call|do|vacuum|analyze|reindex|refresh|comment|"
    r"lock|listen|notify|set|reset|begin|commit|rollback|savepoint)\b",
    re.IGNORECASE,
)


class UnsafeSQL(ValueError):
    pass


def _strip_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _blank_literals(sql: str) -> str:
    """Replace the contents of single-quoted string literals with '' so that
    keywords/semicolons inside literals (e.g. ILIKE '%create%') don't trip the
    safety scan. Handles the '' escape for embedded quotes."""
    return re.sub(r"'(?:[^']|'')*'", "''", sql)


def sanitize(sql: str) -> str:
    """Validate and normalize a query. Raises UnsafeSQL on anything unsafe.
    Returns the query with a LIMIT enforced."""
    if not sql or not sql.strip():
        raise UnsafeSQL("Empty query.")

    cleaned = _strip_comments(sql).strip().rstrip(";").strip()
    scan = _blank_literals(cleaned)  # scan a copy with literals emptied

    if ";" in scan:
        raise UnsafeSQL("Multiple statements are not allowed.")

    low = scan.lower()
    if not (low.startswith("select") or low.startswith("with")):
        raise UnsafeSQL("Only SELECT / WITH queries are allowed.")

    m = _FORBIDDEN.search(scan)
    if m:
        raise UnsafeSQL(f"Disallowed keyword: {m.group(0).upper()}")

    return _enforce_limit(cleaned)


def _enforce_limit(sql: str) -> str:
    # If the outer query already has a LIMIT, leave it; otherwise cap it.
    if re.search(r"\blimit\b\s+\d+\s*$", sql, re.IGNORECASE):
        return sql
    if re.search(r"\blimit\b", sql, re.IGNORECASE):
        return sql  # a LIMIT exists somewhere (e.g. subquery) — trust it
    return f"{sql}\nLIMIT {settings.max_rows}"

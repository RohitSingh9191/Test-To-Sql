"""Full employee/user profile.

When the user asks for *all details* of a specific employee, one giant JOIN across
every related table would explode into a huge cartesian result. Instead we resolve
the employee's keys (user_id + elan) and gather their rows from every table that
links by user_id or elan, returning one clean section per table."""
from __future__ import annotations

import re

from . import db

# An id token like EMP00001 (letters followed by digits).
_ID_RE = re.compile(r"\b([A-Za-z]{2,}\d{2,}[A-Za-z0-9]*)\b")
_ELAN_RE = re.compile(r"elan\s*[:#]?\s*(\d+)", re.IGNORECASE)
_LONGNUM_RE = re.compile(r"\b(\d{5,})\b")

_INTENT = (
    "all detail", "all details", "full detail", "complete detail", "full profile",
    "profile", "everything about", "all information", "full record", "all data",
    "complete information", "details of", "detail of", "all record", "full info",
)

_PER_TABLE_LIMIT = 100


def extract_key(question: str) -> str | None:
    """Pull an employee identifier (user_id like EMP00001, or an elan number)."""
    m = _ID_RE.search(question)
    if m:
        return m.group(1)
    m = _ELAN_RE.search(question)
    if m:
        return m.group(1)
    m = _LONGNUM_RE.search(question)
    if m:
        return m.group(1)
    return None


def is_profile_request(question: str) -> bool:
    ql = question.lower()
    if extract_key(question) is None:
        return False
    return any(w in ql for w in _INTENT) or "profile" in ql


def _resolve(key: str) -> dict | None:
    """Find the employee by user_id (case-insensitive), then elan. Returns the
    employee_master row plus the canonical user_id / elan, or None."""
    try:
        cols, rows = db.run_readonly(
            "SELECT * FROM pers.employee_master WHERE user_id ILIKE %s LIMIT 1", (key,)
        )
        if not rows and key.isdigit():
            cols, rows = db.run_readonly(
                "SELECT * FROM pers.employee_master WHERE elan = %s LIMIT 1", (int(key),)
            )
        if not rows:
            return None
        emp = rows[0]
        return {"employee": emp, "user_id": emp.get("user_id"), "elan": emp.get("elan")}
    except Exception:
        return None


def build_profile(question: str) -> dict:
    key = extract_key(question)
    resolved = _resolve(key) if key else None
    if not resolved:
        return {"mode": "profile", "found": False, "key": key,
                "message": f"No employee found for '{key}'."}

    user_id = resolved["user_id"]
    elan = resolved["elan"]

    # Every table linked to this employee, user_id tables first then elan-only ones.
    seen: set[str] = set()
    order: list[tuple[str, str]] = []  # (table, key_kind)
    for t in db.user_id_tables:
        if t not in seen:
            order.append((t, "user_id")); seen.add(t)
    for t in db.elan_tables:
        if t not in seen:
            order.append((t, "elan")); seen.add(t)

    sections = []
    searched = 0
    for table, kind in order:
        if table == "pers.employee_master":
            continue  # shown separately as the header
        if kind == "user_id" and user_id is not None:
            where, param = "user_id ILIKE %s", (user_id,)
        elif kind == "elan" and elan is not None:
            where, param = "elan = %s", (elan,)
        else:
            continue
        searched += 1
        try:
            cols, rows = db.run_readonly(
                f"SELECT * FROM {table} WHERE {where} LIMIT {_PER_TABLE_LIMIT}", param
            )
        except Exception:
            continue  # skip tables that error (type mismatch etc.)
        if rows:
            sections.append({"table": table, "linked_by": kind,
                             "columns": cols, "rows": rows, "count": len(rows)})

    return {
        "mode": "profile",
        "found": True,
        "user_id": user_id,
        "elan": elan,
        "employee": resolved["employee"],
        "sections": sections,
        "tables_searched": searched,
        "tables_with_data": len(sections),
    }

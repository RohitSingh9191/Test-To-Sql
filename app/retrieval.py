"""Relevant-table retrieval.

For a large schema (174 tables) sending every table to a local model is slow and
overflows GPU memory. Instead we score tables by keyword overlap with the question,
take the top matches, expand along foreign keys (so JOIN targets are present), and
render only that subset. This keeps the prompt small -> fits in VRAM -> fast."""
from __future__ import annotations

import re

from . import db
from .config import settings

_WORD = re.compile(r"[a-z0-9]+")


def _stems(text: str) -> set[str]:
    toks: set[str] = set()
    for w in _WORD.findall(text.lower()):
        if len(w) < 3:
            continue
        toks.add(w)
        if w.endswith("s") and len(w) > 3:  # naive singular
            toks.add(w[:-1])
    return toks


def select_tables(question: str) -> list[str]:
    """Return schema-qualified table names relevant to the question."""
    q = _stems(question)
    core = [t for t in settings.core_tables if t in db.table_lines]
    if not q:
        return list(db.table_lines.keys())

    scored: list[tuple[int, str]] = []
    for key, terms in db.table_terms.items():
        schema, tbl = key.split(".", 1)
        name_toks = set(schema.split("_")) | set(tbl.split("_"))
        score = 3 * len(q & name_toks) + len(q & terms)
        if score:
            scored.append((score, key))

    if not scored:
        # No keyword hit -> fall back to the full schema (rare; favors recall).
        return list(db.table_lines.keys())

    scored.sort(reverse=True)
    top = settings.max_tables
    selected = [k for _, k in scored[:top]]

    # Expand along foreign keys so JOIN targets are included.
    expanded = set(selected)
    for key in selected:
        expanded |= db.table_fk_out.get(key, set())
        # also tables that reference the selected one
        for other, refs in db.table_fk_out.items():
            if key in refs:
                expanded.add(other)

    # Cap total to keep the prompt small.
    cap = top + 20
    if len(expanded) > cap:
        keep = set(selected)
        for k in expanded:
            if len(keep) >= cap:
                break
            keep.add(k)
        expanded = keep
    expanded |= set(core)  # always include core tables (e.g. employee_master)
    return [k for k in db.table_lines if k in expanded]  # stable order


def schema_for(question: str) -> str:
    keys = select_tables(question)
    return "\n".join(db.table_lines[k] for k in keys if k in db.table_lines)

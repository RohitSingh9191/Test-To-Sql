"""Fuzzy name fallback: when a name-filtered query returns no rows, correct the
spelling and/or broaden the search so the user still gets an answer.

Fully offline and read-only — it loads employee names once from the DB and does
all matching in Python (difflib). No DB extensions, no writes."""
from __future__ import annotations

import difflib
import re

from . import db

# ILIKE '%Some Name%'  -> captures "Some Name". Handles em.name / name / "..".
_ILIKE_RE = re.compile(r"ILIKE\s+'%([^%']+)%'", re.IGNORECASE)

_ALL_NAMES: list[str] | None = None


def _all_names() -> list[str]:
    """Distinct employee names, loaded once and cached in memory."""
    global _ALL_NAMES
    if _ALL_NAMES is None:
        try:
            _, rows = db.run_readonly(
                "SELECT DISTINCT name FROM pers.employee_master WHERE name IS NOT NULL"
            )
            _ALL_NAMES = [r["name"] for r in rows if r.get("name")]
        except Exception:
            _ALL_NAMES = []
    return _ALL_NAMES


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def extract_terms(sql: str) -> list[str]:
    """The literals searched with ILIKE '%...%' in the SQL."""
    seen, out = set(), []
    for t in _ILIKE_RE.findall(sql):
        t = t.strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _looks_like_name(term: str) -> bool:
    """Skip codes like EMP00001 / pure numbers — only correct real name text."""
    if re.fullmatch(r"[A-Za-z]{2,}\d+", term):  # EMP00001
        return False
    if re.fullmatch(r"[\d\s]+", term):          # numbers only
        return False
    return bool(re.search(r"[A-Za-z]", term))


def suggest(term: str, limit: int = 6, cutoff: float = 0.5) -> list[dict]:
    """Closest real employee names to `term`, best first (did-you-mean)."""
    names = _all_names()
    scored = ((n, _ratio(term, n)) for n in names)
    ranked = sorted(scored, key=lambda x: x[1], reverse=True)
    return [
        {"name": n, "score": round(r, 3)}
        for n, r in ranked
        if r >= cutoff
    ][:limit]


def _broaden_fragment(term: str) -> str | None:
    """Turn 'Harsh Sharma' into an ILIKE-ANY over each word (search with harsh)."""
    words = [w for w in re.split(r"\s+", term) if len(w) >= 2]
    if len(words) < 2:
        return None
    arr = ", ".join("'%" + w.replace("'", "''") + "%'" for w in words)
    return f"ILIKE ANY (ARRAY[{arr}])"


def fallback(sql: str, max_rows: int = 50) -> dict | None:
    """Given a query that returned 0 rows, try to still answer it.

    Returns a dict describing what was tried, or None if there was no name
    filter worth correcting. Shape:
      { term, suggestions:[{name,score}],
        corrected: {name, sql, columns, rows} | None,
        broadened: {sql, columns, rows} | None }
    """
    terms = [t for t in extract_terms(sql) if _looks_like_name(t)]
    if not terms:
        return None

    # Correct the most specific term (longest = usually the full person name).
    term = max(terms, key=len)
    suggestions = suggest(term)

    out: dict = {"term": term, "suggestions": suggestions,
                 "corrected": None, "broadened": None}

    frag_orig = None
    m = re.search(r"ILIKE\s+'%" + re.escape(term) + r"%'", sql, re.IGNORECASE)
    if m:
        frag_orig = m.group(0)

    # 1) Confident typo correction -> re-run original query with the exact name.
    # Only when the top match is strong (>=0.88) AND clearly beats the runner-up
    # (gap >=0.05). This corrects real typos ("Harsh Nayr"->"Harsh Nair") but
    # leaves ambiguous non-matches ("Harsh Sharma", many Sharmas) as suggestions.
    top = suggestions[0]["score"] if suggestions else 0.0
    second = suggestions[1]["score"] if len(suggestions) > 1 else 0.0
    if suggestions and top >= 0.88 and (top - second) >= 0.05 and frag_orig:
        best = suggestions[0]["name"]
        fixed_sql = sql.replace(frag_orig, "ILIKE '%" + best.replace("'", "''") + "%'")
        try:
            cols, rows = db.run_readonly(fixed_sql)
            if rows:
                out["corrected"] = {
                    "name": best, "sql": fixed_sql,
                    "columns": cols, "rows": rows[:max_rows],
                    "row_count": len(rows),
                }
        except Exception:
            pass

    # 2) Broadened per-word search (e.g. everyone matching "Harsh" or "Sharma").
    if frag_orig:
        broad_frag = _broaden_fragment(term)
        if broad_frag:
            broad_sql = sql.replace(frag_orig, broad_frag)
            try:
                cols, rows = db.run_readonly(broad_sql)
                if rows:
                    out["broadened"] = {
                        "sql": broad_sql,
                        "columns": cols, "rows": rows[:max_rows],
                        "row_count": len(rows),
                    }
            except Exception:
                pass

    # Nothing useful found and no suggestions -> not worth returning.
    if not out["corrected"] and not out["broadened"] and not out["suggestions"]:
        return None
    return out

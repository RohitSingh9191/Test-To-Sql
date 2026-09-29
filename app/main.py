"""FastAPI service: natural language -> SQL -> results, over the PostgreSQL DB."""
from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, text2sql, profile, examples, namefix
from .config import settings
from .guard import sanitize, UnsafeSQL

app = FastAPI(title="Text-to-SQL", version="1.0")

STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup() -> None:
    db.introspect()
    examples.load()
    print(
        f"[text2sql] schema loaded: {len(db.schema_tables)} tables "
        f"across {len(settings.schemas)} schemas; "
        f"provider={settings.provider}; model={settings.active_model}; "
        f"examples={examples.count()}"
    )


class QueryIn(BaseModel):
    question: str
    execute: bool = True


class TeachIn(BaseModel):
    question: str
    sql: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "tables": len(db.schema_tables),
        "provider": settings.provider,
        "model": settings.active_model,
        "examples": examples.count(),
    }


@app.post("/teach")
def teach(body: TeachIn) -> dict:
    """Add a verified question -> SQL example so the model learns it going forward.
    The SQL must pass the read-only safety check."""
    try:
        safe = sanitize(body.sql)
    except UnsafeSQL as e:
        return {"added": False, "error": f"Rejected: {e}"}
    total = examples.add(body.question, safe)
    return {"added": True, "total": total, "question": body.question, "sql": safe}


@app.get("/schema")
def schema() -> dict:
    return {"schemas": settings.schemas, "tables": db.schema_tables}


@app.get("/profile/{key}")
def profile_lookup(key: str) -> dict:
    """Full cross-table profile for one employee (by user_id or elan)."""
    t0 = time.time()
    result = profile.build_profile(f"all details of employee {key}")
    result["question"] = key
    result["exec_ms"] = int((time.time() - t0) * 1000)
    return result


@app.post("/query")
def query(body: QueryIn) -> dict:
    # Full-profile intent ("all details of employee EMP00001") -> gather across tables.
    if profile.is_profile_request(body.question):
        t0 = time.time()
        result = profile.build_profile(body.question)
        result["question"] = body.question
        result["exec_ms"] = int((time.time() - t0) * 1000)
        return result

    t0 = time.time()
    try:
        gen = text2sql.generate_sql(body.question)
    except Exception as e:  # missing API key, API error, bad JSON, etc.
        return {
            "question": body.question,
            "sql": "",
            "error": f"Translation failed: {type(e).__name__}: {e}",
        }
    gen_ms = int((time.time() - t0) * 1000)

    result: dict = {
        "question": body.question,
        "sql": gen["sql"],
        "explanation": gen["explanation"],
        "tables": gen["tables"],
        "gen_ms": gen_ms,
    }

    if not gen["sql"]:
        result["error"] = "The question could not be translated to SQL for this schema."
        return result

    try:
        safe_sql = sanitize(gen["sql"])
    except UnsafeSQL as e:
        result["error"] = f"Rejected by safety check: {e}"
        return result

    result["sql"] = safe_sql
    if not body.execute:
        return result

    try:
        t1 = time.time()
        columns, rows = db.run_readonly(safe_sql)
        result["exec_ms"] = int((time.time() - t1) * 1000)
        result["columns"] = columns
        result["rows"] = rows
        result["row_count"] = len(rows)
        # No rows for a name search? Auto-correct the spelling / broaden it.
        if not rows:
            try:
                fb = namefix.fallback(safe_sql)
                if fb:
                    result["fallback"] = fb
            except Exception:
                pass
    except Exception as e:  # surface DB errors back to the caller
        result["error"] = f"Execution failed: {type(e).__name__}: {e}"

    return result

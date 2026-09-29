"""Runtime configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


class Settings:
    # LLM backend: "ollama" (local, no key) or "anthropic" (Claude, needs key)
    provider: str = os.getenv("T2SQL_PROVIDER", "ollama").lower()

    # Anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    model: str = os.getenv("T2SQL_MODEL", "claude-opus-4-8")

    # Ollama (local)
    ollama_host: str = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5-coder:7b")

    # Relevant-table retrieval: shrinks the prompt so a local model stays fast.
    # Enabled by default for local models; not needed for Anthropic (schema is cached).
    retrieval: bool = os.getenv("T2SQL_RETRIEVAL", "auto").lower() != "off"
    max_tables: int = _int("T2SQL_MAX_TABLES", 20)
    # Always included in retrieval (the central table almost every question needs).
    core_tables: list[str] = [
        t.strip() for t in os.getenv("T2SQL_CORE_TABLES", "pers.employee_master").split(",")
        if t.strip()
    ]

    @property
    def active_model(self) -> str:
        return self.ollama_model if self.provider == "ollama" else self.model

    # PostgreSQL
    pg_host: str = os.getenv("PGHOST", "localhost")
    pg_port: int = _int("PGPORT", 5432)
    pg_db: str = os.getenv("PGDATABASE", "nextgendb")
    pg_user: str = os.getenv("PGUSER", "postgres")
    pg_password: str = os.getenv("PGPASSWORD", "")

    # Which schemas are exposed to the model and queryable
    schemas: list[str] = [
        s.strip()
        for s in os.getenv(
            "T2SQL_SCHEMAS", "pers,utility,request,training,digitalsig,public"
        ).split(",")
        if s.strip()
    ]

    # Safety
    max_rows: int = _int("T2SQL_MAX_ROWS", 500)
    statement_timeout_ms: int = _int("T2SQL_STATEMENT_TIMEOUT_MS", 15000)

    @property
    def dsn(self) -> dict:
        return dict(
            host=self.pg_host,
            port=self.pg_port,
            dbname=self.pg_db,
            user=self.pg_user,
            password=self.pg_password,
        )


settings = Settings()

"""Dynamic few-shot example library ("continuous training").

A growing file of verified question -> SQL pairs. For each new question we inject
only the few MOST SIMILAR examples into the prompt, so the model keeps getting
smarter as examples are added, without bloating the prompt. New examples can be
added at runtime via add() (see the /teach endpoint)."""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "of", "in", "a", "an", "is", "are", "to", "and", "for", "me",
         "give", "show", "list", "get", "all", "any", "how", "many", "what",
         "which", "who", "with", "by", "on", "from", "do", "did", "this"}

_PATH = Path(__file__).resolve().parent.parent / "knowledge" / "examples.jsonl"
_lock = threading.Lock()
_examples: list[dict] = []  # each: {question, sql, tokens:set}


def _tokens(text: str) -> set[str]:
    out = set()
    for w in _WORD.findall(text.lower()):
        if len(w) >= 3 and w not in _STOP:
            out.add(w)
            if w.endswith("s") and len(w) > 3:
                out.add(w[:-1])
    return out


def load() -> None:
    global _examples
    items = []
    if _PATH.exists():
        for line in _PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("question") and d.get("sql"):
                    d["tokens"] = _tokens(d["question"])
                    items.append(d)
            except json.JSONDecodeError:
                continue
    _examples = items


def select(question: str, k: int = 4) -> list[dict]:
    q = _tokens(question)
    if not q or not _examples:
        return []
    scored = []
    for ex in _examples:
        overlap = len(q & ex["tokens"])
        if overlap:
            scored.append((overlap, ex))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [ex for _, ex in scored[:k]]


def block_for(question: str, k: int = 4) -> str:
    """Formatted few-shot block of the most relevant examples, or ''."""
    picked = select(question, k)
    if not picked:
        return ""
    lines = ["\nEXAMPLES (follow these proven patterns):"]
    for ex in picked:
        lines.append(f"Q: {ex['question']}")
        lines.append(f"A: {ex['sql']}")
        lines.append("")
    return "\n".join(lines)


def add(question: str, sql: str) -> int:
    """Append a verified question->SQL pair and make it immediately usable.
    Returns the new total count."""
    question = (question or "").strip()
    sql = (sql or "").strip()
    if not question or not sql:
        raise ValueError("question and sql are required")
    with _lock:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"question": question, "sql": sql}) + "\n")
        _examples.append({"question": question, "sql": sql, "tokens": _tokens(question)})
        return len(_examples)


def count() -> int:
    return len(_examples)

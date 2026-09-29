"""Text -> SQL translation.

Two backends are supported (chosen with T2SQL_PROVIDER):
  * "ollama"    - a local model, no API key, fully offline (default).
  * "anthropic" - Claude, best accuracy, needs ANTHROPIC_API_KEY.

Either way the full database schema is placed in the system prompt and the model
is asked for structured JSON ({sql, explanation, tables}) so the result is always
parseable. For Ollama we raise num_ctx so the ~12k-token schema is not truncated.
"""
from __future__ import annotations

import json

import httpx

from . import db, retrieval, examples
from .config import settings

_SYSTEM_RULES = """You are an expert PostgreSQL analyst. Translate the user's \
natural-language question into ONE valid, read-only PostgreSQL SELECT query for \
the database described below.

Rules:
- Output a SINGLE SELECT (or WITH ... SELECT) statement. Never write INSERT, \
UPDATE, DELETE, or DDL.
- Only use tables and columns that appear in the schema. Always schema-qualify \
tables (e.g. pers.employee_master).
- Prefer explicit JOINs using the foreign keys shown after "-- FK:".
- IMPORTANT LINKING RULE: almost every table is linked to pers.employee_master by \
the `user_id` column, and many also by `elan`. These ARE the links between an \
employee and their records. To find data belonging to a PERSON given by name, JOIN \
the target table to pers.employee_master ON user_id (or elan) and filter with \
`employee_master.name ILIKE '%<name>%'`. For example, a person's dependents/family \
are in pers.family_info joined on user_id (dependents = rows where \
is_currently_dependent = true).
- Never claim the schema has "no link" when a table has a user_id or elan column - \
those columns are the link. If the tables and keys exist, ALWAYS produce the query. \
A query that returns zero rows is a valid, acceptable answer - do not refuse.
- When the user asks for "top", "recent", or a listing, add a sensible ORDER BY \
and a reasonable LIMIT.
- ALWAYS match text/varchar columns CASE-INSENSITIVELY (names, statuses, and \
codes/IDs such as EMP00001). Use `col ILIKE 'value'` for an exact whole-value \
match and `col ILIKE '%value%'` for a partial/contains match. NEVER use a \
case-sensitive `=` on a text column, so that input like 'emp00001' still matches \
the stored 'EMP00001'.
- Do NOT add filters the user did not ask for. For example, do not add \
`is_active = true` unless the user explicitly mentions active/inactive.
- Every table alias you reference in SELECT or WHERE MUST be defined in the FROM \
or a JOIN clause. If you select or filter a column that lives in a lookup table \
(e.g. a *_name column), JOIN that table on its id key first. Never reference an \
alias you did not define.
- For a simple request like "count/list TABLE grouped by COLUMN" or "TABLE by \
COLUMN", query THAT table DIRECTLY and GROUP BY that column. Do NOT add joins to \
other tables unless the requested column genuinely lives in a different table. \
Keep the query minimal.
- JOIN KEYS MUST MATCH BY TYPE. user_id is TEXT and joins employee_master.user_id; \
elan is a NUMBER and joins employee_master.elan. Use whichever key the target table \
actually HAS (check its columns in the schema). Never join user_id to elan, and \
never compare a text column to a numeric column.
- Use ONLY column names that actually appear in the schema line for a table. If a \
table does not have the column you expected, pick the closest column that IS listed \
- never invent a column name.
- If the question cannot be answered from this schema, return an empty sql and \
explain why.
- Respond ONLY with a JSON object: {"sql": "...", "explanation": "...", "tables": ["schema.table", ...]}

Database schema (schema.table(columns...), PK marked, foreign keys after -- FK:):
"""

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sql": {"type": "string"},
        "explanation": {"type": "string"},
        "tables": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sql", "explanation", "tables"],
    "additionalProperties": False,
}


_HINTS = """

KEY FACTS ABOUT THIS DATABASE (use these exact tables/columns):
- Employees live in pers.employee_master. Find a person by name with \
`name ILIKE '%X%'`. Their linking keys are user_id and elan.
- Link most tables to an employee by user_id; some tables link by elan. Tables \
that link by ELAN (not user_id) include: training.user_training_mapping, and \
utility.mapping_username_elan.
- Dependents / family of a person: pers.family_info (join by user_id). Dependents \
= rows where is_currently_dependent = true; the `relationship` column names the relation.
- Two different training tables - pick the right one:
  * training.training_master = the CATALOGUE of training courses/programmes \
themselves. It has start_date and end_date. For questions about which trainings \
EXIST, are RUNNING, are UPCOMING or PAST (not tied to a person), query \
training.training_master DIRECTLY - do NOT join employees. Ongoing / going on / \
currently running = start_date <= CURRENT_DATE AND end_date >= CURRENT_DATE; \
upcoming = start_date > CURRENT_DATE; past/completed = end_date < CURRENT_DATE.
  * training.user_training_mapping = which PEOPLE attended/enrolled in trainings. \
Use it only when the question is about a person's trainings; JOIN to the employee \
by ELAN (em.elan = utm.elan); course name is utm.course_name.
- Contact details (phone / email / address) are in pers.contact stored as \
key-value: the actual value is in the `value` column and its type in \
`contact_category`. There is NO email or phone column on pers.contact or \
pers.employee_master. Join pers.contact by user_id.
- The employee's job title column is `current_designation` (NOT `designation`). \
Likewise use current_cadre, current_pay_level, current_pay_cell, \
current_centre_alias, current_location, current_city - these "current_" columns \
hold the employee's present values.
- Awards = pers.awards; promotions = pers.promotion; salary = pers.salary; \
education = pers.education; experience = pers.experience (all join by user_id).
- To count how many documents each signer signed, go through the signatures \
junction: digitalsig.signers JOIN digitalsig.signatures ON signers.id = \
signatures.signer_id, then COUNT(DISTINCT signatures.document_id).
- An employee's centre: JOIN pers.employee_master.current_centre_alias = \
utility.centre_master.centre_alias; the centre name is utility.centre_master.centre_name.
- The word "team" means a CENTRE. Employees belong to a centre via \
pers.employee_master.current_centre_alias (matches utility.centre_master.centre_alias; \
full name in centre_name). To list employees of a given team/centre, match the \
centre by alias OR name with ILIKE (a team/centre may be given by code like 'CFR' \
or by name like 'Research').
- Digital signatures: digitalsig.documents, digitalsig.signers, digitalsig.signatures \
(signatures.document_id -> documents.id, signatures.signer_id -> signers.id).
- Guest house bookings are in request.guest_house_booking (employee_id = \
employee_master.user_id; has booking_type, check_in_date, check_out_date, status). \
The guest house NAME/details are in request.guest_house (guest_house_id -> \
guest_house_name). To show which guest house someone booked, JOIN request.guest_house \
ON guest_house_booking.guest_house_id = guest_house.guest_house_id.
- Committee nominations: use request.committee (it has is_active true/false, \
committee_name, status, nominated_by). Join to the person by user_id \
(em.user_id = request.committee.user_id). "Active or inactive committee" means \
`is_active IN (true, false)`. Do NOT use pers.committee_nomination (it is empty \
and has NO is_active column); pers.committees holds only active memberships.
"""

_EXAMPLES = """
EXAMPLES (follow these patterns):
Q: Find Mohan Kulkarni's dependents.
A: SELECT fi.name, fi.relationship, fi.date_of_birth FROM pers.employee_master em JOIN pers.family_info fi ON em.user_id = fi.user_id WHERE em.name ILIKE '%Mohan Kulkarni%' AND fi.is_currently_dependent = true

Q: Which trainings are going on currently?
A: SELECT course_name, start_date, end_date FROM training.training_master WHERE start_date <= CURRENT_DATE AND end_date >= CURRENT_DATE ORDER BY end_date

Q: List the trainings of employee EMP00326.
A: SELECT utm.course_name, utm.grade_obtain, utm.certificate_issue_date FROM pers.employee_master em JOIN training.user_training_mapping utm ON em.elan = utm.elan WHERE em.user_id ILIKE 'EMP00326'

Q: Show the email and contact of Mohan Kulkarni.
A: SELECT c.contact_category, c.value FROM pers.employee_master em JOIN pers.contact c ON em.user_id = c.user_id WHERE em.name ILIKE '%Mohan Kulkarni%'

Q: List the top 5 centres by number of employees.
A: SELECT cm.centre_name, COUNT(*) AS employee_count FROM pers.employee_master em JOIN utility.centre_master cm ON em.current_centre_alias = cm.centre_alias GROUP BY cm.centre_name ORDER BY employee_count DESC LIMIT 5

Q: List the 5 most recently joined employees with name and designation.
A: SELECT name, current_designation FROM pers.employee_master ORDER BY date_of_joining_org DESC LIMIT 5

Q: List signers and how many documents they signed.
A: SELECT sg.name, COUNT(DISTINCT s.document_id) AS documents_signed FROM digitalsig.signers sg JOIN digitalsig.signatures s ON sg.id = s.signer_id GROUP BY sg.name ORDER BY documents_signed DESC

Q: Give me the names of all employees in the CFR team.
A: SELECT em.name FROM pers.employee_master em JOIN utility.centre_master cm ON em.current_centre_alias = cm.centre_alias WHERE cm.centre_alias ILIKE '%CFR%' OR cm.centre_name ILIKE '%CFR%'

Q: Did Gayatri Das book any guest house?
A: SELECT gh.guest_house_name, gb.booking_type, gb.check_in_date, gb.check_out_date, gb.status FROM pers.employee_master em JOIN request.guest_house_booking gb ON em.user_id = gb.employee_id JOIN request.guest_house gh ON gh.guest_house_id = gb.guest_house_id WHERE em.name ILIKE '%Gayatri Das%'
"""


def _system_prompt(schema: str | None = None) -> str:
    # Stable part (rules + schema + fixed facts) — kept identical per request so
    # Claude prompt-caching still works. Dynamic examples go in the user turn.
    body = schema if schema is not None else db.schema_text
    return _SYSTEM_RULES + body + _HINTS


def _user_content(question: str) -> str:
    ex = examples.block_for(question)
    return (ex + "\n\n" if ex else "") + f"Question: {question}"


def _normalize(data: dict) -> dict:
    return {
        "sql": (data.get("sql") or "").strip(),
        "explanation": data.get("explanation", ""),
        "tables": data.get("tables", []),
    }


# ---------------------------------------------------------------- Ollama ----
def _estimate_ctx(prompt_chars: int) -> int:
    """Pick the smallest context window that fits prompt + answer. Smaller ctx
    keeps the whole model on the GPU, which is much faster."""
    need = prompt_chars // 4 + 1024  # ~1k tokens headroom for the JSON answer
    for size in (2048, 4096, 8192, 16384, 32768):
        if need <= size:
            return size
    return 32768


def _generate_ollama(question: str) -> dict:
    schema = retrieval.schema_for(question) if settings.retrieval else db.schema_text
    system = _system_prompt(schema)
    user = _user_content(question)
    payload = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "format": _OUTPUT_SCHEMA,  # Ollama structured outputs (JSON schema)
        "options": {"temperature": 0, "num_ctx": _estimate_ctx(len(system) + len(user))},
    }
    with httpx.Client(timeout=180.0) as client:
        r = client.post(f"{settings.ollama_host}/api/chat", json=payload)
        r.raise_for_status()
        content = r.json()["message"]["content"]
    return _normalize(json.loads(content))


# ------------------------------------------------------------- Anthropic ----
_anthropic_client = None


def _generate_anthropic(question: str) -> dict:
    global _anthropic_client
    import anthropic

    if _anthropic_client is None:
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your environment or .env.")
        _anthropic_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    resp = _anthropic_client.messages.create(
        model=settings.model,
        max_tokens=1500,
        system=[
            {
                "type": "text",
                "text": _system_prompt(),
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": _user_content(question)}],
        output_config={"format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA}},
    )
    text = next((b.text for b in resp.content if b.type == "text"), "{}")
    return _normalize(json.loads(text))


def generate_sql(question: str) -> dict:
    if settings.provider == "anthropic":
        return _generate_anthropic(question)
    return _generate_ollama(question)

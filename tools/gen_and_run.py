"""Auto-generate schema-grounded questions, run them against the service, and
stream results to a JSONL file so progress survives interruptions.

Usage:
  python tools/gen_and_run.py [N] [out.jsonl]
     N          how many questions to run (default 150)
Env: PGHOST etc. (same as the app), SERVICE=http://127.0.0.1:8000
"""
from __future__ import annotations
import os, sys, json, time, random
import psycopg2, httpx
from dotenv import load_dotenv

load_dotenv()  # same .env as the app
SERVICE = os.getenv("SERVICE", "http://127.0.0.1:8000")
N = int(sys.argv[1]) if len(sys.argv) > 1 else 150
OUT = sys.argv[2] if len(sys.argv) > 2 else "tools/batch_results.jsonl"

DSN = dict(host=os.getenv("PGHOST", "localhost"), port=int(os.getenv("PGPORT", "5432")),
           dbname=os.getenv("PGDATABASE", "nextgendb"), user=os.getenv("PGUSER", "postgres"),
           password=os.getenv("PGPASSWORD", ""))
SCHEMAS = ("pers", "utility", "request", "training", "digitalsig", "public")

CATEG = ("status", "type", "category", "gender", "mode", "level", "cadre", "state",
         "city", "relationship", "designation", "religion", "marital_status",
         "blood_group", "is_active", "booking_type", "grade", "specialization", "domain")
DATEISH = ("created_at", "date", "_at", "_on", "start_date", "check_in_date")


def is_cat(c): return any(k in c for k in CATEG)
def is_date(c): return any(c.endswith(k) or k in c for k in DATEISH)


def build():
    conn = psycopg2.connect(**DSN); cur = conn.cursor()
    cur.execute("""SELECT table_schema,table_name FROM information_schema.tables
      WHERE table_schema=ANY(%s) AND table_type='BASE TABLE'""", (list(SCHEMAS),))
    tables = cur.fetchall()
    meta = {}
    nonempty = []
    for s, t in tables:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (s, t))
        cols = [r[0] for r in cur.fetchall()]
        meta[(s, t)] = cols
        try:
            cur.execute(f'SELECT count(*) FROM "{s}"."{t}"'); n = cur.fetchone()[0]
        except Exception:
            conn.rollback(); n = 0
        if n > 0:
            nonempty.append((s, t, cols))
    conn.close()

    qs = []
    for s, t, cols in nonempty:
        tn = t.replace("_", " ")
        qs.append(f"How many records are in {s}.{t}?")
        for c in [c for c in cols if is_cat(c)][:2]:
            qs.append(f"Count {tn} grouped by {c}.")
        dcol = next((c for c in cols if is_date(c)), None)
        if dcol:
            qs.append(f"List the latest 10 {tn} ordered by {dcol}.")
        if "user_id" in cols and t != "employee_master":
            qs.append(f"Show the {tn} records for employee EMP00326.")
    # person-centric natural questions
    qs += [
        "Find Mohan Kulkarni's dependents.",
        "List the awards of employee EMP00326.",
        "Show the education of Mohan Kulkarni.",
        "Which trainings are going on currently?",
        "Which guest houses did Gayatri Das book?",
        "List employees in the CFR team.",
        "Give me all details of employee EMP00001",
        "Top 5 centres by number of employees.",
        "How many employees are active?",
        "Count employees by gender.",
    ]
    random.seed(42); random.shuffle(qs)
    # dedup preserve order
    seen = set(); uniq = []
    for q in qs:
        if q not in seen: seen.add(q); uniq.append(q)
    return uniq


def main():
    qs = build()[:N]
    print(f"Generated {len(qs)} questions; running against {SERVICE} -> {OUT}\n", flush=True)
    stats = {"OK": 0, "PROFILE": 0, "REFUSED": 0, "EXEC_ERR": 0, "REQ_FAIL": 0}
    with open(OUT, "w", encoding="utf-8") as f:
        for i, q in enumerate(qs, 1):
            rec = {"q": q}
            try:
                d = httpx.post(f"{SERVICE}/query", json={"question": q}, timeout=120).json()
                if d.get("mode") == "profile":
                    st = "PROFILE"
                else:
                    sql = d.get("sql") or ""; err = d.get("error") or ""
                    st = "REFUSED" if not sql else ("EXEC_ERR" if err else "OK")
                    rec.update(sql=sql.replace("\n", " "), err=err, rows=d.get("row_count"))
            except Exception as e:
                st = "REQ_FAIL"; rec["err"] = str(e)[:120]
            rec["status"] = st; stats[st] += 1
            f.write(json.dumps(rec) + "\n"); f.flush()
            if st in ("EXEC_ERR", "REFUSED", "REQ_FAIL"):
                print(f"  {i:>4} [{st}] {q}\n        {rec.get('err','')[:110]}", flush=True)
            if i % 20 == 0:
                print(f"  ... {i}/{len(qs)}  {dict(stats)}", flush=True)
    good = stats["OK"] + stats["PROFILE"]
    print(f"\n===== {good}/{len(qs)} answered ({round(100*good/len(qs))}%) | {stats} =====", flush=True)


if __name__ == "__main__":
    main()

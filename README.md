# Text-to-SQL for `nextgendb`

A fast web service that turns plain-English questions into **read-only**
PostgreSQL queries against the `nextgendb` database (174 tables) and runs them
live. Built with **FastAPI**, with a pluggable LLM backend:

- **`ollama`** (default) — a local model, no API key, fully offline.
- **`anthropic`** — Claude, highest accuracy, needs an API key.

Switch with `T2SQL_PROVIDER` in `.env`.

## How it works

```
question ──▶ LLM (Ollama or Claude) ──▶ SQL ──▶ safety guard ──▶ read-only DB ──▶ rows
```

- **Fast:** the full schema is introspected once at startup. For the **local
  model**, a lightweight keyword + foreign-key **retrieval** step sends only the
  ~30 relevant tables per question (keeping the prompt on the GPU); for **Claude**,
  the whole schema goes in a **prompt-cached** system prompt. Either way the model
  returns structured JSON so the SQL is always parseable — no fragile text scraping.
- **Safe (two layers):**
  1. A static guard rejects anything that isn't a single `SELECT`/`WITH`, blocks
     DDL/DML keywords, and enforces a row `LIMIT`.
  2. Queries run inside a **`READ ONLY` transaction** with a statement timeout.
  See [Security notes](#security-notes) for the limits of these checks.

## Quick start (Windows)

You need:

- **Python 3.11+** — [python.org/downloads](https://www.python.org/downloads/) (tick *Add Python to PATH*)
- **Ollama** — runs the AI model locally
- **Network access** to your PostgreSQL server

```powershell
# 1. Get the code
git clone https://github.com/RohitSingh9191/Test-To-Sql.git
cd Test-To-Sql

# 2. Install Ollama (once) and download the model (~4.7 GB)
winget install --id Ollama.Ollama
ollama pull qwen2.5-coder:7b     # or qwen2.5-coder:3b for lower-end GPUs

# 3. Put your database credentials in .env (see "Connect your database" below)
copy .env.example .env
notepad .env

# 4. Start the app (creates a venv, installs dependencies, starts the server)
.\run.ps1
```

Then open **http://localhost:8001** and ask a question.

`run.ps1` also turns on two Ollama GPU settings (`OLLAMA_FLASH_ATTENTION=1`,
`OLLAMA_KV_CACHE_TYPE=q8_0`) so a 7B model fits fully on an 8 GB card. Restart
Ollama once after the first run (quit the tray app and reopen it) for them to
take effect. On an NVIDIA RTX A1000 (8 GB) a question takes **~8 s** with these
settings, and ~30 s without them. The first question after startup takes ~15 s
while the model loads into the GPU.

### Start it manually (without `run.ps1`)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

Make sure Ollama is running first (`ollama serve` — if it says the address is
already in use, it is already running).

### Stop the app

Press **Ctrl + C** in the window where it is running, or from any PowerShell window:

```powershell
Get-NetTCPConnection -LocalPort 8001 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

## Connect your database

### Where the credentials go

All connection settings are read from a file named **`.env`** in the project
folder (next to this README). Create it from the template and edit it:

```powershell
copy .env.example .env
notepad .env
```

Fill in the PostgreSQL section:

```ini
PGHOST=192.168.1.50
PGPORT=5432
PGDATABASE=nextgendb
PGUSER=t2sql_reader
PGPASSWORD=your-password
T2SQL_SCHEMAS=pers,utility,request,training,digitalsig,public
```

| Setting | What to put there |
|---|---|
| `PGHOST` | IP address or host name of the PostgreSQL server |
| `PGPORT` | PostgreSQL port (usually `5432`) |
| `PGDATABASE` | Database name |
| `PGUSER` / `PGPASSWORD` | The account the app logs in with |
| `T2SQL_SCHEMAS` | Comma-separated schemas the model is allowed to see |

- **`.env` is in `.gitignore`**, so your password never gets committed. Put real
  credentials only in `.env`, never in `.env.example` or the code.
- `.env` is read once at startup, so **restart the app** after changing it.
- Environment variables set in the shell (e.g. `$env:PGPASSWORD = "..."`) take
  priority over `.env`.

### Check the connection

When the app starts it prints a line like:

```
[text2sql] schema loaded: 174 tables across 6 schemas; provider=ollama; model=qwen2.5-coder:7b; examples=22
```

You can also check it while it is running:

```powershell
curl http://localhost:8001/health
# {"status":"ok","tables":174,"provider":"ollama","model":"qwen2.5-coder:7b","examples":22}
```

To test only the database credentials, without starting the app:

```powershell
.venv\Scripts\python -c "import psycopg2; from app.config import settings; psycopg2.connect(**settings.dsn).close(); print('DB connection OK')"
```

### Recommended: a read-only database account

Connect with an account that can only read, not with `postgres`. Run this once
as an admin (e.g. in `psql` or pgAdmin), then put the new user and password in `.env`:

```sql
CREATE ROLE t2sql_reader LOGIN PASSWORD 'choose-a-strong-password';
GRANT CONNECT ON DATABASE nextgendb TO t2sql_reader;
GRANT USAGE  ON SCHEMA pers, utility, request, training, digitalsig, public TO t2sql_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA pers, utility, request, training, digitalsig, public TO t2sql_reader;
```

Note: PostgreSQL hides primary/foreign-key details in `information_schema` from
accounts that only have `SELECT`, so the model sees the tables without their
key hints. The app still works; join accuracy may be slightly lower.

### Troubleshooting

| Error at startup | Likely cause |
|---|---|
| `could not connect to server` / `Connection refused` / timeout | Wrong `PGHOST`/`PGPORT`, the server is not reachable from this PC, or a firewall / `pg_hba.conf` blocks it |
| `password authentication failed` | Wrong `PGUSER` or `PGPASSWORD` |
| `database "..." does not exist` | Wrong `PGDATABASE` |
| `schema loaded: 0 tables` | The names in `T2SQL_SCHEMAS` don't exist, or the account has no access to them |
| `Translation failed: ConnectError` when asking a question | Ollama is not running (`ollama serve`) or the model isn't pulled |

## Option B — Claude (highest accuracy)

In `.env`, set:

```ini
T2SQL_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

then run `.\run.ps1` as usual.

> **Speed vs. accuracy:** `claude-opus-4-8` gives the most reliable SQL on this
> large schema. For lower latency set `T2SQL_MODEL=claude-sonnet-5` or
> `claude-haiku-4-5`.

## Teaching it new queries

After running a question, click **"Teach this query as correct"** under the
result. The question and SQL are saved to `knowledge/examples.jsonl` and used as
examples for similar questions from then on.

## API

| Method | Path       | Body / notes                                  |
|--------|------------|-----------------------------------------------|
| GET    | `/`        | Web UI                                         |
| GET    | `/health`  | `{status, tables, provider, model, examples}`  |
| GET    | `/schema`  | Tables/columns exposed to the model            |
| POST   | `/query`   | `{"question": "...", "execute": true}`         |
| POST   | `/teach`   | `{"question": "...", "sql": "..."}`            |
| GET    | `/profile/{key}` | All records for one employee (user_id or elan) |

`POST /query` returns:

```json
{
  "sql": "SELECT ... LIMIT 500",
  "explanation": "Counts employees grouped by is_active.",
  "tables": ["pers.employee_master"],
  "gen_ms": 900, "exec_ms": 40,
  "columns": ["is_active", "count"], "rows": [ ... ], "row_count": 2
}
```

Example:

```bash
curl -s localhost:8001/query -H "Content-Type: application/json" \
  -d '{"question":"How many active employees are there?"}'
```

## Configuration (`.env`)

| Variable | Purpose | Default |
|---|---|---|
| `T2SQL_PROVIDER` | `ollama` (local) or `anthropic` (Claude) | `ollama` |
| `OLLAMA_HOST` | Ollama address | `http://localhost:11434` |
| `OLLAMA_MODEL` | Local model | `qwen2.5-coder:7b` |
| `T2SQL_MAX_TABLES` | Top tables retrieved per question (before FK expansion) | `20` |
| `ANTHROPIC_API_KEY` | Claude API key (only if provider=anthropic) | — |
| `T2SQL_MODEL` | Claude model | `claude-opus-4-8` |
| `PGHOST` / `PGPORT` / `PGDATABASE` / `PGUSER` / `PGPASSWORD` | DB connection — see [Connect your database](#connect-your-database) | `localhost` / `5432` / `nextgendb` / `postgres` / *(empty)* |
| `T2SQL_SCHEMAS` | Schemas exposed to the model | `pers,utility,request,training,digitalsig,public` |
| `T2SQL_MAX_ROWS` | Row cap added to queries | `500` |
| `T2SQL_STATEMENT_TIMEOUT_MS` | Per-query DB timeout | `15000` |

## Security notes

- **No login.** The web UI and API have no authentication, and `run.ps1` listens
  on all network interfaces (`0.0.0.0`), so anyone on your network can use it.
  For local-only use, change `--host 0.0.0.0` to `--host 127.0.0.1` in `run.ps1`.
- **Use a read-only account** (see above). The SQL guard is best-effort and can
  be bypassed by specially crafted SQL; database permissions are what actually
  stop writes.
- Don't expose this service to the internet.

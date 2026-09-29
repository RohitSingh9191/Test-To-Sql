# How to Start the App

## First time only: add your database credentials
Open **PowerShell** in the project folder and run:

```powershell
copy .env.example .env
notepad .env
```

Fill in `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSWORD`, then save.
See **"Connect your database"** in [README.md](README.md) for details.

---

## Easiest way
Open **PowerShell** in the project folder and run:

```powershell
.\run.ps1
```

Then open your browser at: **http://localhost:8001**

---

## Manual way

Open **PowerShell** and run these commands one by one.

### 1. Go to the project folder
```powershell
cd path\to\TestToSql
```

### 2. Make sure the AI (Ollama) is running
Ollama usually starts automatically with Windows. To be sure, run:
```powershell
ollama serve
```
(If it says "address already in use", it is already running — that's fine.)

### 3. Start the app
```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8001
```

You will see:
```
[text2sql] schema loaded: 174 tables ... provider=ollama; model=qwen2.5-coder:7b; examples=22
```

### 4. Open the app
Go to: **http://localhost:8001**

---

## How to stop the app
In the PowerShell window where it is running, press **Ctrl + C**.

Or force-stop it from any PowerShell window:
```powershell
Get-NetTCPConnection -LocalPort 8001 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

---

## Check it is working
```powershell
curl http://localhost:8001/health
```
It should reply:
```json
{"status":"ok","tables":174,"provider":"ollama","model":"qwen2.5-coder:7b","examples":22}
```

---

## Notes
- The app is **offline** — it does not need the internet (only the local AI + the database on the network).
- First question after startup is a bit slower (~15s) because the AI model loads into the GPU; after that each question takes about **7-8 seconds**.
- To teach it a correct query: run a question, then click the **"Teach this query as correct"** button under the result.

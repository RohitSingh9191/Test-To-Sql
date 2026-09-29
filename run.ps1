# Windows launcher for the Text-to-SQL service.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# --- Ollama GPU tuning (lets a 7B model fit fully on an 8GB GPU -> ~4x faster) ---
# Persisted so the Ollama background service picks them up on its next start.
if ([Environment]::GetEnvironmentVariable('OLLAMA_FLASH_ATTENTION','User') -ne '1') {
    [Environment]::SetEnvironmentVariable('OLLAMA_FLASH_ATTENTION','1','User')
    [Environment]::SetEnvironmentVariable('OLLAMA_KV_CACHE_TYPE','q8_0','User')
    Write-Host "Enabled Ollama flash-attention + q8 KV cache. Restart Ollama once for it to take effect:" -ForegroundColor Yellow
    Write-Host "  Get-Process ollama* | Stop-Process -Force   (the tray app relaunches it)" -ForegroundColor Yellow
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}
& .\.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
& .\.venv\Scripts\python.exe -m pip install --quiet -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from template." -ForegroundColor Yellow
    Write-Host "Open .env, fill in your database settings (PGHOST, PGUSER, PGPASSWORD, ...), then run .\run.ps1 again." -ForegroundColor Yellow
    Write-Host "Make sure Ollama is running and the model is pulled: ollama pull qwen2.5-coder:7b" -ForegroundColor Yellow
    exit 1
}

Write-Host "Starting on http://localhost:8001  (Ctrl+C to stop)" -ForegroundColor Green
& .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8001

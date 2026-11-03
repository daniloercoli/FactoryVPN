# Start the configured Docker stack; scripts.bootstrap generates .env and certificates.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".env")) {
    throw "Create the deployment first: python -m scripts.bootstrap init --host <server-hostname>"
}
docker compose config --quiet
if ($LASTEXITCODE -ne 0) { throw "Invalid Compose configuration" }
docker compose up --build -d
if ($LASTEXITCODE -ne 0) { throw "Docker startup failed" }
docker compose ps
if ($LASTEXITCODE -ne 0) { throw "Cannot read Docker service status" }

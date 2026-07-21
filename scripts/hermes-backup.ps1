# Hermes + n8n backup script
# Backs up: n8n DB, Hermes profiles, Docker volumes (config only — not data)
# Stores in 2 locations: ZCodeProject/backups (primary) + D:/HermesBackups (secondary)
# Keeps last 7 days (28 snapshots at 6h cadence)
# Usage:
#   Manual: powershell -File C:\Users\Bew\ZCodeProject\scripts\hermes-backup.ps1
#   Scheduled: Task Scheduler every 6h

$ErrorActionPreference = "Stop"
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$PrimaryDir = "C:\Users\Bew\ZCodeProject\backups"
$SecondaryDir = "D:\HermesBackups"
$KeepDays = 7

# Ensure dirs exist
New-Item -ItemType Directory -Force -Path $PrimaryDir | Out-Null
New-Item -ItemType Directory -Force -Path $SecondaryDir | Out-Null

$Stamp = "hermes-backup-$Timestamp"
Write-Host "[$Timestamp] Starting backup: $Stamp"

# ---------- 1. n8n database snapshot (atomic via .backup) ----------
# Uses sqlite .backup via alpine container to avoid corruption from in-flight writes
Write-Host "  [1/4] n8n database snapshot..."
$n8nBakDir = Join-Path $PrimaryDir $Stamp
New-Item -ItemType Directory -Force -Path $n8nBakDir | Out-Null

docker run --rm `
  -v zcodeproject_n8n_data:/data:ro `
  -v "${n8nBakDir}:/out" `
  alpine sh -c "apk add --no-cache sqlite >/dev/null 2>&1 && sqlite3 /data/database.sqlite '.backup /out/n8n-database.sqlite' && cp /data/config /out/n8n-config 2>/dev/null; ls -la /out/"

if ($LASTEXITCODE -ne 0) {
    Write-Warning "  n8n DB snapshot failed (container may be mid-write); will retry next run"
}

# ---------- 2. Hermes profiles (config.yaml + .env + SOUL.md + sessions manifest) ----------
# Note: .env contains secrets — backup is local-only, chmod restricted by NTFS ACL on user dir
Write-Host "  [2/4] Hermes profiles..."
$hermesSrc = "C:\Users\Bew\AppData\Local\hermes"
$hermesBakDir = Join-Path $n8nBakDir "hermes-profiles"
New-Item -ItemType Directory -Force -Path $hermesBakDir | Out-Null

# Copy default profile + glm profile configs (not caches/logs)
foreach ($profile in @("", "profiles\glm")) {
    $src = Join-Path $hermesSrc $profile
    if (Test-Path $src) {
        $rel = if ($profile) { $profile } else { "default" }
        $dst = Join-Path $hermesBakDir $rel
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        # Only config files, not caches
        Copy-Item -Path (Join-Path $src "config.yaml") -Destination $dst -ErrorAction SilentlyContinue
        Copy-Item -Path (Join-Path $src ".env") -Destination $dst -ErrorAction SilentlyContinue
        Copy-Item -Path (Join-Path $src "SOUL.md") -Destination $dst -ErrorAction SilentlyContinue
        Copy-Item -Path (Join-Path $src "profile.yaml") -Destination $dst -ErrorAction SilentlyContinue
    }
}

# Auth store (Telegram pairing, OAuth tokens for providers)
Copy-Item -Path (Join-Path $hermesSrc "auth.json") -Destination $hermesBakDir -ErrorAction SilentlyContinue
Copy-Item -Path (Join-Path $hermesSrc "channel_directory.json") -Destination $hermesBakDir -ErrorAction SilentlyContinue

# ---------- 3. Docker compose + ZCodeProject config files ----------
Write-Host "  [3/4] Docker compose + project config..."
foreach ($f in @(
    "C:\n8n-openclaw\docker-compose.yml",
    "C:\Users\Bew\ZCodeProject\docker-compose-augment.yml"
)) {
    if (Test-Path $f) {
        Copy-Item -Path $f -Destination $n8nBakDir -ErrorAction SilentlyContinue
    }
}

# n8n credential export (encrypted, needs n8n-encryption-key — keep with .env)
& docker exec n8n n8n export:credentials --all --output="/tmp/creds.json" 2>$null
if ($LASTEXITCODE -eq 0) {
    & docker cp "n8n:/tmp/creds.json" (Join-Path $n8nBakDir "n8n-credentials.json") 2>$null
    & docker exec n8n rm /tmp/creds.json 2>$null
}

# n8n workflow export (plain JSON, version-controlled)
& docker exec n8n n8n export:workflow --all --output="/tmp/workflows.json" 2>$null
if ($LASTEXITCODE -eq 0) {
    & docker cp "n8n:/tmp/workflows.json" (Join-Path $n8nBakDir "n8n-workflows.json") 2>$null
    & docker exec n8n rm /tmp/workflows.json 2>$null
}

# ---------- 4. Compress + mirror to D: ----------
Write-Host "  [4/4] Compress + mirror to D:..."
$zip = "$PrimaryDir\$Stamp.zip"
Compress-Archive -Path (Join-Path $n8nBakDir "*") -DestinationPath $zip -Force
Remove-Item -Recurse -Force $n8nBakDir

Copy-Item -Path $zip -Destination "$SecondaryDir\$Stamp.zip" -Force

$sizeMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
Write-Host "  Backup created: $zip ($sizeMb MB)"

# ---------- Cleanup: keep last $KeepDays days ----------
$cutoff = (Get-Date).AddDays(-$KeepDays)
Get-ChildItem $PrimaryDir -Filter "hermes-backup-*.zip" |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
        Write-Host "  Cleanup (primary): $($_.Name)"
        Remove-Item $_.FullName -Force
    }
Get-ChildItem $SecondaryDir -Filter "hermes-backup-*.zip" |
    Where-Object { $_.LastWriteTime -lt $cutoff } |
    ForEach-Object {
        Write-Host "  Cleanup (secondary): $($_.Name)"
        Remove-Item $_.FullName -Force
    }

Write-Host "[$Timestamp] Backup complete."

# CRUX one-line installer for Windows.
#   irm https://raw.githubusercontent.com/kiran100604/crux/claude/pensive-bell-jj7bqr/install/install.ps1 | iex
# Installs CRUX (with the tray app), puts `crux` on PATH, and runs setup with
# sensible offline defaults. Set $env:CRUX_ANTHROPIC_KEY / $env:CRUX_OPENAI_KEY
# before running to wire keys non-interactively.
$ErrorActionPreference = "Stop"
$REPO   = if ($env:CRUX_REPO)   { $env:CRUX_REPO }   else { "https://github.com/kiran100604/crux.git" }
$BRANCH = if ($env:CRUX_BRANCH) { $env:CRUX_BRANCH } else { "claude/pensive-bell-jj7bqr" }

Write-Host "`n  Installing CRUX..." -ForegroundColor Cyan

# 1. Find a Python launcher (3.11+).
$py = $null
foreach ($cand in @("py","python","python3")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) { $py = $cand; break }
}
if (-not $py) { throw "Python 3.11+ not found. Install it from https://python.org (tick 'Add to PATH'), then re-run." }

# 2. Install straight from git (no manual clone needed).
Write-Host "  Installing package (this can take a minute)..."
& $py -m pip install --user --upgrade "git+$REPO@$BRANCH#egg=crux[app]"

# 3. Make sure the user Scripts dir is on PATH, now and permanently.
$scripts = & $py -c "import sysconfig; print(sysconfig.get_path('scripts','nt_user'))"
if ($scripts -and (Test-Path $scripts)) {
    $env:Path += ";$scripts"
    $userPath = [Environment]::GetEnvironmentVariable("Path","User")
    if ($userPath -notlike "*$scripts*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$scripts", "User")
        Write-Host "  Added $scripts to your PATH."
    }
}

# 4. Configure non-interactively (offline by default; keys if provided via env).
$setupArgs = @("setup","--yes")
if ($env:CRUX_ANTHROPIC_KEY) { $setupArgs += @("--anthropic-key", $env:CRUX_ANTHROPIC_KEY) }
if ($env:CRUX_OPENAI_KEY)    { $setupArgs += @("--openai-key",    $env:CRUX_OPENAI_KEY) }
& $py -m crux.cli @setupArgs

# 5. Launch CRUX now — nothing else for the user to run. The dashboard opens and
#    the "Capture to CRUX" icon is in the Start Menu (pin it to the taskbar).
Write-Host "`n  CRUX is ready — launching..." -ForegroundColor Green
try {
    Start-Process -FilePath $py -ArgumentList "-m","crux.cli","start" -WindowStyle Hidden
    Write-Host "  The dashboard is opening in your browser."
    Write-Host "  The 'Capture to CRUX' icon is in your Start Menu — right-click it to pin to the taskbar."
} catch {
    Write-Host "  Start it yourself:  crux"
}
Write-Host "  Restart Claude Code so the context hook loads.`n"

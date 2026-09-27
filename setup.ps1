# One-time setup on Windows. Safe to re-run: every step checks before it acts.
#
#   setup.cmd        (double-click it, or run it from a terminal)
#
# Same four steps as ./setup on Mac and Linux: ffmpeg, Python, packages, speech
# model. Nothing needs an admin password. ffmpeg goes into ~\.local\bin, which
# edit.cmd puts on PATH itself, so no system PATH editing either.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar makes Invoke-WebRequest ~10x slower

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $Here ".venv"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$LocalBin = Join-Path $HOME ".local\bin"
$env:PATH = "$LocalBin;$env:PATH"

function Say($m) { Write-Host "`n$m" -ForegroundColor White }
function Ok($m)  { Write-Host "  ok  $m" }
function Die($m) { Write-Host "`nerror: $m" -ForegroundColor Red; exit 1 }

# 1. ffmpeg
Say "1/4  ffmpeg"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) {
    Ok "already installed"
} else {
    # gyan.dev "essentials" is the build ffmpeg.org links to for Windows.
    New-Item -ItemType Directory -Force $LocalBin | Out-Null
    $tmp = Join-Path $env:TEMP "local-editor-ffmpeg"
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Force $tmp | Out-Null
    try {
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip" -OutFile "$tmp\ffmpeg.zip"
    } catch { Die "couldn't download ffmpeg. Check your internet and run setup.cmd again." }
    Expand-Archive -Path "$tmp\ffmpeg.zip" -DestinationPath $tmp -Force
    foreach ($tool in "ffmpeg.exe", "ffprobe.exe") {
        $src = Get-ChildItem -Path $tmp -Recurse -Filter $tool | Select-Object -First 1
        if (-not $src) { Die "the ffmpeg download didn't contain $tool" }
        Copy-Item $src.FullName (Join-Path $LocalBin $tool) -Force
    }
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    Ok "installed to ~\.local\bin"
}

# 2. python
# `python` on a fresh Windows box is often the Microsoft Store stub, which prints
# "Python was not found" instead of running. Ask the py launcher first.
Say "2/4  python"
function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        try {
            $args_ = @($c | Select-Object -Skip 1) + @("-c", "import sys; print(sys.version_info[0]*100+sys.version_info[1])")
            $v = & $c[0] @args_ 2>$null
            if ($LASTEXITCODE -eq 0 -and [int]$v -ge 309) { return ,$c }
        } catch {}
    }
    return $null
}
$Py = Find-Python
if (-not $Py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "  Python not found, installing it (per-user, no admin)..."
        winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
        $env:PATH = [Environment]::GetEnvironmentVariable("PATH", "User") + ";" + $env:PATH
        $Py = Find-Python
    }
    if (-not $Py) { Die "need Python 3.9 or newer. Install it from https://www.python.org/downloads/ then run setup.cmd again." }
}
Ok "using $($Py -join ' ')"

# 3. virtualenv + packages
Say "3/4  packages"
if (-not (Test-Path $VenvPy)) {
    $pyArgs = @($Py | Select-Object -Skip 1) + @("-m", "venv", $Venv)
    & $Py[0] @pyArgs
    if ($LASTEXITCODE -ne 0) { Die "couldn't create the virtualenv" }
    Ok "created .venv"
} else {
    Ok ".venv already there"
}
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet -r (Join-Path $Here "requirements.txt")
if ($LASTEXITCODE -ne 0) { Die "pip install failed" }
Ok "faster-whisper + httpx installed"

# 4. speech model, pulled now so the first edit doesn't stall on a download
Say "4/4  speech model"
& $VenvPy -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8'); print('  ok  whisper small ready')"
if ($LASTEXITCODE -ne 0) { Die "couldn't download the speech model" }

Say "Ready."
Write-Host "  Cut your first video:`n"
Write-Host "      edit.cmd `"$HOME\Desktop\raw.mp4`"`n"
Write-Host "  It lands in your Downloads folder and opens by itself."

if (-not (Get-Command claude -ErrorAction SilentlyContinue) -and -not (Test-Path (Join-Path $LocalBin "claude.exe"))) {
    Write-Host "`n  Note: the retake cutter, --script and --draft use Claude Code to think with."
    Write-Host "  It isn't installed, so those passes will say so and skip. claude.com/claude-code"
}

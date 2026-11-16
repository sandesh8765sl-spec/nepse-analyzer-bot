$ErrorActionPreference = "Continue"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $ProjectDir "logs"
$StartupLog = Join-Path $LogDir "bot_startup.log"
$GuardLog = Join-Path $LogDir "startup_guard.log"
$LockDir = Join-Path $LogDir "bot.lock"
$MainFile = Join-Path $ProjectDir "main.py"
$VenvPython = Join-Path $ProjectDir "venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

function Write-BotLog {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Message,

        [string]$Path = $StartupLog
    )

    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    try {
        Add-Content -LiteralPath $Path -Value $line -Encoding UTF8
    }
    catch {
        # Logging should never block startup.
    }
}

function Get-RunningBotProcess {
    # Match THIS bot only: a Python process whose command line contains the
    # full path of this project's main.py. Another project's main.py is ignored.
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -match "^python(w)?(\.exe)?$" -and
            $_.CommandLine -and
            $_.CommandLine.IndexOf($MainFile, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
        }
}

function Limit-LauncherLog {
    # The bot writes its own rotating log (logs\bot.log). This launcher log only
    # holds start/stop lines and crash output, but cap it anyway.
    param([string]$Path, [long]$MaxBytes = 5MB)
    try {
        if ((Test-Path -LiteralPath $Path) -and ((Get-Item -LiteralPath $Path).Length -gt $MaxBytes)) {
            Move-Item -LiteralPath $Path -Destination "$Path.1" -Force -ErrorAction Stop
        }
    }
    catch {
        # The file may still be held open by an older run. Try again next start.
    }
}

Limit-LauncherLog -Path $StartupLog
Limit-LauncherLog -Path $GuardLog -MaxBytes 1MB

$existingProcesses = @(Get-RunningBotProcess)
if ($existingProcesses.Count -gt 0) {
    foreach ($process in $existingProcesses) {
        Write-BotLog -Path $GuardLog -Message (
            "Startup skipped because bot is already running. PID={0}. Exe={1}. Command={2}" -f
            $process.ProcessId,
            $process.ExecutablePath,
            $process.CommandLine
        )
    }
    exit 0
}

if (Test-Path -LiteralPath $LockDir) {
    Write-BotLog -Path $GuardLog -Message "Stale bot.lock found without a running bot process. Removing stale lock."
    Remove-Item -LiteralPath $LockDir -Recurse -Force -ErrorAction SilentlyContinue
}

try {
    New-Item -ItemType Directory -Path $LockDir -ErrorAction Stop | Out-Null
}
catch {
    Write-BotLog -Path $GuardLog -Message "Could not create bot.lock. Startup skipped to avoid duplicate instance. Error=$($_.Exception.Message)"
    exit 0
}

try {
    Write-BotLog -Message "============================================================"
    Write-BotLog -Message "Startup requested."
    Write-BotLog -Message "Project: $ProjectDir"

    if (-not (Test-Path -LiteralPath $MainFile)) {
        Write-BotLog -Message "ERROR: main.py is missing at $MainFile"
        exit 2
    }

    $EnvFile = Join-Path $ProjectDir ".env"
    if (-not (Test-Path -LiteralPath $EnvFile)) {
        Write-BotLog -Message "ERROR: .env file is missing at $EnvFile"
        exit 3
    }

    if (Test-Path -LiteralPath $VenvPython) {
        $PythonExe = $VenvPython
        Write-BotLog -Message "Using virtual environment Python: $PythonExe"
    }
    else {
        $PythonExe = "python"
        Write-BotLog -Message "WARNING: venv Python not found. Falling back to PATH python."
    }

    Set-Location -LiteralPath $ProjectDir
    $env:PYTHONUNBUFFERED = "1"

    while ($true) {
        Write-BotLog -Message "Starting Telegram bot: $PythonExe -u $MainFile"
        $CmdArgument = '/c ""{0}" -u "{1}" >> "{2}" 2>&1"' -f $PythonExe, $MainFile, $StartupLog
        $Process = Start-Process `
            -FilePath "cmd.exe" `
            -ArgumentList $CmdArgument `
            -Wait `
            -PassThru `
            -WindowStyle Hidden
        $ExitCode = $Process.ExitCode
        Write-BotLog -Message "Bot process exited with code $ExitCode."
        Write-BotLog -Message "Restarting bot in 20 seconds unless Windows is shutting down."
        Start-Sleep -Seconds 20
    }
}
finally {
    Remove-Item -LiteralPath $LockDir -Recurse -Force -ErrorAction SilentlyContinue
}

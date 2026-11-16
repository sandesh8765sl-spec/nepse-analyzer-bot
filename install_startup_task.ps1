# Installs the Windows scheduled tasks that keep the NEPSE bot running on this laptop.
#
# RUN ONCE, AS ADMINISTRATOR:
#   1. Right-click the Start button -> "Terminal (Admin)" or "Windows PowerShell (Admin)".
#   2. cd to this folder, then:   powershell -ExecutionPolicy Bypass -File .\install_startup_task.ps1
#
# What it sets up
#   Task "NEPSE Analyzer Bot"       starts the bot when Windows starts (before you log in), when you log on,
#                                   and checks every 15 minutes that it is still running (starts it if not).
#                                   The launcher (start_bot.ps1) also restarts the bot 20 seconds after a crash.
#   Task "NEPSE Analyzer Bot Wake"  wakes the PC from sleep shortly before each scheduled job and keeps it
#                                   awake for 15 minutes so the job can finish.
#   Windows power setting           "Allow wake timers" is switched on (needed for the wake task).
#
# Limits you should know
#   * A PC that is shut down or hibernating is NOT woken. Use Sleep, not Shut down, if you want the jobs to run.
#   * Some laptops ignore wake timers on battery or with the lid closed.
#   * Whatever is missed is caught up when the PC is next awake (see HOW_TO_USE.md).
#
# To remove everything:  .\install_startup_task.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BatchPath = Join-Path $ProjectDir "start_bot.bat"
$Python = Join-Path $ProjectDir "venv\Scripts\python.exe"
$TaskName = "NEPSE Analyzer Bot"
$WakeTaskName = "NEPSE Analyzer Bot Wake"
# A few minutes before each job in config\bot.yaml: morning brief 10:15, data updates 15:20 and 17:00,
# evening report 17:30, monthly re-test check 19:30, backup 20:15. Change both places together.
$WakeTimes = @("10:10", "15:15", "16:55", "17:25", "19:25", "20:10")
$HoldAwakeSeconds = 900

$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $IsAdmin) {
    throw "Please run this script in an Administrator PowerShell window (see the top of this file)."
}

if ($Remove) {
    foreach ($name in @($TaskName, $WakeTaskName)) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
        Write-Host "Removed scheduled task: $name"
    }
    return
}

if (-not (Test-Path -LiteralPath $BatchPath)) { throw "Missing start_bot.bat at $BatchPath" }
if (-not (Test-Path -LiteralPath $Python)) { throw "Missing the Python environment at $Python" }

$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# S4U = run whether or not the user is logged on, without storing a password.
$Principal = New-ScheduledTaskPrincipal -UserId $UserId -LogonType S4U -RunLevel Limited

# ---------------------------------------------------------------- the bot itself
$Action = New-ScheduledTaskAction -Execute $BatchPath -WorkingDirectory $ProjectDir
$AtStartup = New-ScheduledTaskTrigger -AtStartup
$AtStartup.Delay = "PT1M"                       # give the network a minute
$AtLogOn = New-ScheduledTaskTrigger -AtLogOn -User $UserId
# Watchdog: every 15 minutes. If the bot is running this does nothing (start_bot.ps1 exits at once).
$Watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 15)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger @($AtStartup, $AtLogOn, $Watchdog) `
    -Settings $Settings -Principal $Principal -Force `
    -Description "Starts the NEPSE Telegram bot at system start and at logon, and restarts it if it stops." | Out-Null
Write-Host "Registered: $TaskName (at system start, at logon, checked every 15 minutes)"

# ---------------------------------------------------------- waking the PC for jobs
$WakeAction = New-ScheduledTaskAction -Execute $Python `
    -Argument "-m nepse_bot.utils.keep_awake $HoldAwakeSeconds" -WorkingDirectory $ProjectDir
$WakeTriggers = $WakeTimes | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ }
$WakeSettings = New-ScheduledTaskSettingsSet `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)

Register-ScheduledTask -TaskName $WakeTaskName -Action $WakeAction -Trigger $WakeTriggers `
    -Settings $WakeSettings -Principal $Principal -Force `
    -Description "Wakes the PC before each scheduled NEPSE bot job and keeps it awake for 15 minutes." | Out-Null
Write-Host "Registered: $WakeTaskName (wakes the PC at $($WakeTimes -join ', '))"

# ------------------------------------------------------ allow wake timers in Windows
powercfg /SETACVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
powercfg /SETDCVALUEINDEX SCHEME_CURRENT SUB_SLEEP RTCWAKE 1 | Out-Null
powercfg /SETACTIVE SCHEME_CURRENT | Out-Null
Write-Host "Windows power setting 'Allow wake timers' is ON (plugged in and on battery)."

# Start the bot now if it is not running yet.
Start-ScheduledTask -TaskName $TaskName
Write-Host "Done. Check logs\bot.log for 'Application started'."

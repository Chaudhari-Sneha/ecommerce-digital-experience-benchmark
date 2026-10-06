# Registers a Windows Task Scheduler task that runs the collection pipeline
# every N hours. Run once from PowerShell (no admin rights needed):
#
#   .\scripts\register_task_scheduler.ps1 -IntervalHours 12
#
# To remove it later:
#   Unregister-ScheduledTask -TaskName "DigitalFrictionCollector" -Confirm:$false

param(
    [int]$IntervalHours = 12,
    # pythonw.exe runs without a console window; with python.exe a window opens
    # on every run, and closing it kills the run.
    [string]$PythonExe = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
)

if (-not $PythonExe) {
    throw "pythonw.exe not found on PATH. Pass it explicitly: -PythonExe 'C:\path\to\pythonw.exe'"
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$TaskName = "DigitalFrictionCollector"

$Action = New-ScheduledTaskAction -Execute $PythonExe -Argument "-m collectors.orchestrator" -WorkingDirectory $ProjectRoot

# Omitting -RepetitionDuration repeats indefinitely; passing [TimeSpan]::MaxValue
# fails to register on many Windows builds ("value out of range").
$Trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

# StartWhenAvailable: if the PC was off or asleep at a scheduled time, run as soon as it's back.
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "Digital Friction Score data collection" -Force

Write-Host "Registered scheduled task '$TaskName' to run every $IntervalHours hour(s)."
Write-Host "Working directory: $ProjectRoot"
Write-Host "Python: $PythonExe"
Write-Host "First run starts in about 1 minute. Progress log: $ProjectRoot\logs\pipeline.log"

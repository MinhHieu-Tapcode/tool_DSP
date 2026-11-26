<#
.SYNOPSIS
    Chạy ngầm gsolution-auto trên terminal hoặc đăng ký chạy tự động theo giờ qua Windows Task Scheduler.

.EXAMPLE
    .\run_background.ps1                   # Chạy ngầm ở chế độ --dry-run (chỉ đọc & xuất Excel)
    .\run_background.ps1 -Apply            # Chạy ngầm thực thi thật (--apply)
    .\run_background.ps1 -Apply -Sales "Frescadorado"
    .\run_background.ps1 -RegisterTask     # Tạo lịch tự động chạy ngầm mỗi 30 phút (06:00..14:00 + 14:05 chốt sau cut-off)
#>

param(
    [ValidateSet("fillin", "gip287")]
    [string]$Mode = "fillin",
    [switch]$Apply,
    [switch]$NoCancel,
    [string]$Sales = "",
    [int]$Concurrency = 4,
    [switch]$NotifyTelegram,
    [switch]$RegisterTask,
    [string[]]$ScheduleTimes = @("06:00", "06:30", "07:00", "07:30", "08:00", "08:30", "09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:05"),
    [string]$TaskName = "GSolutionAutoFillIn"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunPy = Join-Path $ScriptDir "run.py"

if ($Mode -eq "gip287") {
    if (-not $PSBoundParameters.ContainsKey("ScheduleTimes")) {
        $ScheduleTimes = @("08:45", "11:45", "14:45", "15:05")
    }
    if (-not $PSBoundParameters.ContainsKey("TaskName")) {
        $TaskName = "GSolutionAutoGip287"
    }
}

$PythonExe = "C:\Users\Hieu\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "Không tìm thấy python.exe."
    }
    $PythonExe = $cmd.Source
}

if ($RegisterTask) {
    $argList = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Mode $Mode -Apply -Concurrency $Concurrency"
    if ($NoCancel -and $Mode -eq "gip287") { $argList += " -NoCancel" }
    if ($Sales -and $Mode -eq "fillin") { $argList += " -Sales `"$Sales`"" }
    if ($NotifyTelegram) { $argList += " -NotifyTelegram" }

    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argList -WorkingDirectory $ScriptDir
    $triggers = @()
    foreach ($t in $ScheduleTimes) {
        $triggers += New-ScheduledTaskTrigger -Daily -At $t
    }
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Force | Out-Null
    Write-Host "Đã đăng ký tác vụ chạy ngầm '$TaskName' (-Mode $Mode) hằng ngày lúc: $($ScheduleTimes -join ', ')"
    exit 0
}

$pyArgs = @($RunPy, "--mode", $Mode, "--concurrency", "$Concurrency")
if ($Apply) {
    $pyArgs += "--apply"
} else {
    $pyArgs += "--dry-run"
}
if ($NoCancel -and $Mode -eq "gip287") {
    $pyArgs += "--no-cancel"
}
if ($Sales -and $Mode -eq "fillin") {
    $pyArgs += @("--sales", $Sales)
}
if ($NotifyTelegram) {
    $pyArgs += "--notify-telegram"
}

Push-Location $ScriptDir
try {
    & $PythonExe @pyArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

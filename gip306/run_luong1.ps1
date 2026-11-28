<#
.SYNOPSIS
    Chay ngam / dang ky lich tu dong GIP 306 Luong 1 (08:00 sang & 13:30 chieu).
    Thu tu bat buoc: Export Pancake POS -> Xoa & Ghi de Sheet -> Nhan Telegram.

.EXAMPLE
    .\gip306\run_luong1.ps1 -Login
    .\gip306\run_luong1.ps1
    .\gip306\run_luong1.ps1 -Apply -NotifyTelegram
    .\gip306\run_luong1.ps1 -RegisterTask
#>

param(
    [switch]$Login,
    [switch]$Apply,
    [switch]$NotifyTelegram,
    [ValidateSet("auto", "apps_script", "gsheets_cli")]
    [string]$Backend = "auto",
    [switch]$Headed,
    [switch]$RegisterTask,
    [string[]]$ScheduleTimes = @("08:00", "13:30"),
    [string]$TaskName = "GSolutionGip306Luong1"
)

$ErrorActionPreference = "Stop"
$ModDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $ModDir

$PythonExe = "C:\Users\Hieu\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "Khong tim thay python.exe."
    }
    $PythonExe = $cmd.Source
}

if ($RegisterTask) {
    $argList = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSCommandPath`" -Apply -NotifyTelegram -Backend $Backend"
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $argList -WorkingDirectory $RootDir
    $triggers = @()
    foreach ($t in $ScheduleTimes) {
        $triggers += New-ScheduledTaskTrigger -Daily -At $t
    }
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Force -ErrorAction Stop | Out-Null
    Write-Host "Da dang ky tac vu '$TaskName' hang ngay luc: $($ScheduleTimes -join ', ')"
    exit 0
}

$pyArgs = @("-m", "gip306.run_luong1", "--backend", $Backend)
if ($Login) {
    $pyArgs += "--login"
} else {
    if ($Apply) { $pyArgs += "--apply" } else { $pyArgs += "--dry-run" }
    if ($NotifyTelegram) { $pyArgs += "--notify-telegram" }
    if ($Headed) { $pyArgs += "--headed" }
}

Push-Location $RootDir
try {
    & $PythonExe @pyArgs
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

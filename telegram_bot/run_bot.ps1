<#
.SYNOPSIS
    Quan ly tien trinh doc lap cho Telegram Bot (@dsp_gsolution_report_bot).
    Hoan toan tach biet voi GSolutionAutoFillIn va GSolutionAutoGip287.

.EXAMPLE
    .\telegram_bot\run_bot.ps1 -Action start
    .\telegram_bot\run_bot.ps1 -Action status
    .\telegram_bot\run_bot.ps1 -Action stop
    .\telegram_bot\run_bot.ps1 -Action register
    .\telegram_bot\run_bot.ps1 -Action unregister
#>

param(
    [ValidateSet("start", "stop", "status", "register", "unregister")]
    [string]$Action = "status",
    [switch]$DryRun,
    [string]$TaskName = "GSolutionTelegramBot"
)

$ErrorActionPreference = "Stop"
$BotDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir = Split-Path -Parent $BotDir
$LockPath = Join-Path $BotDir "telegram_bot.lock"
$LogPath = Join-Path (Join-Path $RootDir "logs") "telegram_bot.log"

$PythonExe = "C:\Users\Hieu\AppData\Local\Programs\Python\Python311\python.exe"
if (-not (Test-Path $PythonExe)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -eq $cmd) {
        throw "Khong tim thay python.exe."
    }
    $PythonExe = $cmd.Source
}

function Get-BotProcess {
    if (Test-Path $LockPath) {
        try {
            $raw = [System.IO.File]::Open($LockPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
            $reader = New-Object System.IO.StreamReader($raw)
            $content = $reader.ReadToEnd()
            $reader.Close()
            $raw.Close()
            if ($content -match "pid=(\d+)") {
                $pidVal = [int]$Matches[1]
                $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
                if ($null -ne $proc -and $proc.ProcessName -like "python*") {
                    return $proc
                }
            }
        } catch {
        }
    }
    $procs = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*telegram_bot*" }
    foreach ($p in $procs) {
        return (Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue)
    }
    return $null
}

switch ($Action) {
    "status" {
        $proc = Get-BotProcess
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $proc) {
            Write-Host "Telegram Bot: RUNNING (PID=$($proc.Id))"
        } else {
            Write-Host "Telegram Bot: STOPPED"
        }
        if ($null -ne $task) {
            Write-Host "Scheduled Task '$TaskName': $($task.State)"
        } else {
            Write-Host "Scheduled Task '$TaskName': NOT REGISTERED"
        }
        if (Test-Path $LogPath) {
            Write-Host "--- Log gan nhat ($LogPath) ---"
            Get-Content $LogPath -Tail 6
        }
    }

    "start" {
        $existing = Get-BotProcess
        if ($null -ne $existing) {
            Write-Host "Telegram Bot da dang chay (PID=$($existing.Id))."
            exit 0
        }
        $argList = @("-u", "-m", "telegram_bot.bot")
        if ($DryRun) { $argList += "--dry-run" }
        $proc = Start-Process -FilePath $PythonExe -ArgumentList $argList -WorkingDirectory $RootDir -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 3
        $check = Get-BotProcess
        if ($null -ne $check) {
            Write-Host "Da bat Telegram Bot (PID=$($check.Id))."
        } else {
            Write-Host "Khoi dong xong PID=$($proc.Id); kiem tra log: $LogPath"
        }
    }

    "stop" {
        $proc = Get-BotProcess
        if ($null -eq $proc) {
            Write-Host "Telegram Bot khong chay."
            exit 0
        }
        Stop-Process -Id $proc.Id -Force
        Write-Host "Da dung Telegram Bot (PID=$($proc.Id))."
    }

    "register" {
        $arg = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -Command `"Set-Location '$RootDir'; & '$PythonExe' -u -m telegram_bot.bot`""
        $act = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $arg -WorkingDirectory $RootDir
        $trig = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trig -Settings $settings -Force | Out-Null
        Write-Host "Da dang ky task '$TaskName' (tu bat khi dang nhap Windows)."
    }

    "unregister" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($null -ne $task) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
            Write-Host "Da xoa task '$TaskName'."
        } else {
            Write-Host "Task '$TaskName' khong ton tai."
        }
    }
}

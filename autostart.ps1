param([switch]$Remove)

# корень проекта = папка, где лежит этот скрипт (переносимо)
$root = $PSScriptRoot
$taskName = "AI-Telegram-Assistant"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Автозапуск удалён."
    exit 0
}

if (-not (Test-Path $pythonw)) {
    Write-Error "Не найден $pythonw — сначала запустите setup.bat"
    exit 1
}

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$root\main.py`"" -WorkingDirectory $root
$envTmp = Join-Path $root "data\tmp"
New-Item -ItemType Directory -Force $envTmp | Out-Null
$action.Environment = @{ TEMP = $envTmp; TMP = $envTmp; PYTHONIOENCODING = "utf-8" }
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Description "ИИ-ассистент в Telegram (личный аккаунт)" -Force | Out-Null
Write-Host "Автозапуск включён: задача '$taskName' при входе в Windows."

# Run this once in PowerShell as Administrator to register the bot as a scheduled task
# It will auto-start when Windows boots and restart if it crashes

$taskName   = "BTCTradingAgent"
$scriptPath = "C:\Users\pc\xauusdagent\run_bot.bat"
$logDir     = "C:\Users\pc\xauusdagent\logs"

# Create log dir if missing
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# Remove old task if exists
Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue

# Define the action
$action = New-ScheduledTaskAction `
    -Execute "cmd.exe" `
    -Argument "/c `"$scriptPath`""

# Trigger: run at system startup
$trigger = New-ScheduledTaskTrigger -AtStartup

# Settings: restart on failure, run whether logged in or not
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit 0 `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew

# Principal: run as current user, highest privileges
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# Register the task
Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "BTC/USD Institutional Trading Agent"

Write-Host ""
Write-Host "Task '$taskName' registered successfully." -ForegroundColor Green
Write-Host "The bot will now:" -ForegroundColor Cyan
Write-Host "  - Start automatically when Windows boots"
Write-Host "  - Restart within 1 minute if it crashes"
Write-Host "  - Run even when terminal is closed"
Write-Host ""
Write-Host "Commands:" -ForegroundColor Yellow
Write-Host "  Start now:  Start-ScheduledTask  -TaskName '$taskName'"
Write-Host "  Stop:       Stop-ScheduledTask   -TaskName '$taskName'"
Write-Host "  Status:     Get-ScheduledTask    -TaskName '$taskName' | Select-Object TaskName,State"
Write-Host "  Uninstall:  Unregister-ScheduledTask -TaskName '$taskName'"
Write-Host "  View logs:  Get-Content C:\Users\pc\xauusdagent\logs\agent.log -Tail 50"

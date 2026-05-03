$ErrorActionPreference = "Stop"

$repo = "C:\Users\pc\Desktop\AIBtclaude"

Set-Location $repo

$listener = Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    taskkill /F /PID $listener.OwningProcess | Out-Null
    Start-Sleep -Seconds 2
}

$cmdArgs = @(
    "/c",
    "cd /d `"$repo`" && python dashboard.py"
)

$proc = Start-Process -FilePath "cmd.exe" `
    -ArgumentList $cmdArgs `
    -WorkingDirectory $repo `
    -WindowStyle Minimized `
    -PassThru

Start-Sleep -Seconds 3

if (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue) {
    Write-Output "DASHBOARD_STARTED:$($proc.Id)"
} else {
    Write-Output "DASHBOARD_EXITED:$($proc.Id)"
}

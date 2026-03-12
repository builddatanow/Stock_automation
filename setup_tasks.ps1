# Register Nginx as a startup task
$action1   = New-ScheduledTaskAction -Execute "C:\nginx\nginx.exe"
$trigger1  = New-ScheduledTaskTrigger -AtStartup
$settings1 = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Hours 0)
Register-ScheduledTask -TaskName "Nginx" -Action $action1 -Trigger $trigger1 -Settings $settings1 -RunLevel Highest -User "SYSTEM" -Force | Out-Null
Write-Host "Nginx task: OK"

# Register Dashboard as a startup task
$action2   = New-ScheduledTaskAction -Execute "C:\Users\Administrator\Desktop\projects\dashboard\start_dashboard.bat" -WorkingDirectory "C:\Users\Administrator\Desktop\projects\dashboard"
$trigger2  = New-ScheduledTaskTrigger -AtStartup
$settings2 = New-ScheduledTaskSettingsSet -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Hours 0)
Register-ScheduledTask -TaskName "TradingDashboard" -Action $action2 -Trigger $trigger2 -Settings $settings2 -RunLevel Highest -User "SYSTEM" -Force | Out-Null
Write-Host "TradingDashboard task: OK"

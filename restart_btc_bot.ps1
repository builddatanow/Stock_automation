$procs = Get-WmiObject Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%btc_0dte%'"
foreach ($p in $procs) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped PID $($p.ProcessId)"
}
Start-Sleep 2
$log = "C:\Users\Administrator\Desktop\projects\eth-options-bot\logs\live_btc_0dte.log"
$py  = "C:\Program Files\Python311\python.exe"
$cwd = "C:\Users\Administrator\Desktop\projects\eth-options-bot"
Start-Process $py -ArgumentList "run_live_btc_0dte.py" -WorkingDirectory $cwd -WindowStyle Hidden -RedirectStandardOutput $log
Write-Host "BTC bot restarted"
Start-Sleep 3
Get-WmiObject Win32_Process -Filter "Name='python.exe' AND CommandLine LIKE '%btc_0dte%'" | Select-Object ProcessId, CommandLine

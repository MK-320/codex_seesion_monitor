@echo off
setlocal
set "MONITOR_ROOT=%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$port=8000; $root=$env:MONITOR_ROOT.TrimEnd('\'); if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { Write-Error ('Port ' + $port + ' is already in use. Close the existing service and try again.'); exit 1 }; $script=Join-Path $root 'start-codex-monitor.ps1'; $server=Start-Process powershell.exe -WorkingDirectory $root -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',$script,'-Port',$port); $url='http://127.0.0.1:' + $port; for ($i=0; $i -lt 30; $i++) { if ($server.HasExited) { Write-Error 'Codex Session Monitor exited before it became ready.'; exit 1 }; try { $config=Invoke-RestMethod ($url + '/api/config') -TimeoutSec 1; if (@($config.projects).project_root -contains $root) { Start-Process $url; exit 0 } } catch { Start-Sleep -Seconds 1 } }; & taskkill.exe /PID $server.Id /T /F | Out-Null; Write-Error ('Codex Session Monitor did not start on ' + $url); exit 1"
set "status=%ERRORLEVEL%"
if not "%status%"=="0" pause
exit /b %status%

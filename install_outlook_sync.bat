@echo off
echo Creating Outlook Calendar Sync scheduled task...

schtasks /create /tn "Outlook Calendar Sync" /sc HOURLY /mo 1 /tr "powershell.exe -WindowStyle Hidden -ExecutionPolicy Bypass -Command \"$p = Get-Process OUTLOOK -EA SilentlyContinue; if($p){ $o = [System.Runtime.InteropServices.Marshal]::GetActiveObject('Outlook.Application'); $o.GetNamespace('MAPI').SendAndReceive($false) }\"" /ru "%USERNAME%" /f

if %errorlevel% == 0 (
    echo.
    echo SUCCESS! Task created. Outlook calendars will sync every hour.
) else (
    echo.
    echo FAILED. Try right-clicking this file and choosing "Run as Administrator".
)

pause

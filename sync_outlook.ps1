# Force Outlook to refresh internet calendars (run via Task Scheduler hourly)
$processes = Get-Process -Name "OUTLOOK" -ErrorAction SilentlyContinue
if ($processes) {
    try {
        $outlook = [System.Runtime.InteropServices.Marshal]::GetActiveObject("Outlook.Application")
        $ns = $outlook.GetNamespace("MAPI")
        $ns.SendAndReceive($false)
        Write-Host "$(Get-Date) — Outlook internet calendars refreshed"
    } catch {
        Write-Host "$(Get-Date) — Error: $_"
    }
} else {
    Write-Host "$(Get-Date) — Outlook not open, skipping sync"
}

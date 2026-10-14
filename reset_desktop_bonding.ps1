# reset_desktop_bonding.ps1 - Restore Exact Windows Default Settings from Snapshot

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

$stateFile = Join-Path $PSScriptRoot "bonding_prior_state.json"

if (Test-Path $stateFile) {
    try {
        $snapshot = Get-Content -Path $stateFile -Raw | ConvertFrom-Json
        Write-Host "[*] Restoring settings from snapshot taken at $($snapshot.Timestamp)..." -ForegroundColor Cyan

        foreach ($a in $snapshot.Adapters) {
            $autoMetric = if ($a.AutomaticMetric -eq "Enabled") { "Enabled" } else { "Disabled" }
            $weakSend = if ($a.WeakHostSend -eq "Enabled") { "Enabled" } else { "Disabled" }
            $weakRecv = if ($a.WeakHostReceive -eq "Enabled") { "Enabled" } else { "Disabled" }

            Set-NetIPInterface -InterfaceIndex $a.InterfaceIndex -AutomaticMetric $autoMetric -InterfaceMetric $a.InterfaceMetric -WeakHostSend $weakSend -WeakHostReceive $weakRecv
            Write-Host "  [+] Restored $($a.InterfaceAlias): Metric=$($a.InterfaceMetric), Auto=$autoMetric, WeakSend=$weakSend, WeakRecv=$weakRecv" -ForegroundColor Green
        }

        foreach ($r in $snapshot.Routes) {
            Set-NetRoute -InterfaceIndex $r.InterfaceIndex -DestinationPrefix $r.DestinationPrefix -RouteMetric $r.RouteMetric -ErrorAction SilentlyContinue
        }

        Remove-Item $stateFile -Force
        Write-Host "[SUCCESS] Exact prior network configuration restored and snapshot removed." -ForegroundColor Green
    } catch {
        Write-Host "[!] Error restoring snapshot: $_. Falling back to default automatic settings." -ForegroundColor Yellow
        $adapters = Get-NetIPInterface | Where-Object { $_.InterfaceAlias -in @('WiFi', 'WiFi 2') -and $_.AddressFamily -eq 'IPv4' }
        foreach ($adapter in $adapters) {
            Set-NetIPInterface -InterfaceIndex $adapter.InterfaceIndex -AutomaticMetric Enabled -WeakHostSend Disabled -WeakHostReceive Disabled
        }
    }
} else {
    Write-Host "[*] No snapshot file found. Restoring standard Windows automatic metrics..." -ForegroundColor Cyan
    $adapters = Get-NetIPInterface | Where-Object { $_.InterfaceAlias -in @('WiFi', 'WiFi 2') -and $_.AddressFamily -eq 'IPv4' }
    foreach ($adapter in $adapters) {
        Set-NetIPInterface -InterfaceIndex $adapter.InterfaceIndex -AutomaticMetric Enabled -WeakHostSend Disabled -WeakHostReceive Disabled
    }
    Write-Host "[SUCCESS] Restored Windows default automatic metrics." -ForegroundColor Green
}

Start-Sleep -Seconds 2

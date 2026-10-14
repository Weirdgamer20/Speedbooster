# apply_desktop_bonding.ps1 - Equalize Network Metrics & Enable WeakHost (Experimental)
# Accurately captures prior state for exact rollback

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "  SpeedBond: Interface Metric & Route Equalization" -ForegroundColor Yellow
Write-Host "  NOTE: Experimental client-side routing adjustment" -ForegroundColor Gray
Write-Host "============================================================" -ForegroundColor Cyan

# Check for Administrator privileges
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "[*] Requesting Administrator elevation..." -ForegroundColor Yellow
    Start-Process powershell.exe -Verb RunAs -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    exit
}

# Target Interfaces
$adapters = Get-NetIPInterface | Where-Object { $_.InterfaceAlias -in @('WiFi', 'WiFi 2') -and $_.AddressFamily -eq 'IPv4' }

if ($adapters.Count -lt 2) {
    Write-Host "[!] Could not find both 'WiFi' and 'WiFi 2' interfaces. Found:" -ForegroundColor Red
    Get-NetIPInterface | Where-Object { $_.AddressFamily -eq 'IPv4' } | Select-Object InterfaceAlias, InterfaceIndex
    Read-Host "Press Enter to exit"
    exit
}

# Capture snapshot of original state before modification
$stateFile = Join-Path $PSScriptRoot "bonding_prior_state.json"
$snapshot = @{
    Timestamp = (Get-Date).ToString("o")
    Adapters = @()
    Routes = @()
}

foreach ($a in $adapters) {
    $snapshot.Adapters += @{
        InterfaceIndex = $a.InterfaceIndex
        InterfaceAlias = $a.InterfaceAlias
        InterfaceMetric = $a.InterfaceMetric
        AutomaticMetric = $a.AutomaticMetric.ToString()
        WeakHostSend = $a.WeakHostSend.ToString()
        WeakHostReceive = $a.WeakHostReceive.ToString()
    }
}

$routes = Get-NetRoute -DestinationPrefix "0.0.0.0/0" | Where-Object { $_.InterfaceIndex -in ($adapters.InterfaceIndex) }
foreach ($r in $routes) {
    $snapshot.Routes += @{
        InterfaceIndex = $r.InterfaceIndex
        DestinationPrefix = $r.DestinationPrefix
        NextHop = $r.NextHop
        RouteMetric = $r.RouteMetric
    }
}

# Save snapshot only if not already saved (to preserve original clean baseline)
if (-not (Test-Path $stateFile)) {
    $snapshot | ConvertTo-Json -Depth 4 | Set-Content -Path $stateFile -Encoding UTF8
    Write-Host "[*] Saved original network configuration snapshot to $stateFile" -ForegroundColor Green
}

# Apply equalized configuration
foreach ($adapter in $adapters) {
    $idx = $adapter.InterfaceIndex
    $alias = $adapter.InterfaceAlias
    Write-Host "[*] Configuring $alias (Index $idx)..." -ForegroundColor Cyan
    Set-NetIPInterface -InterfaceIndex $idx -AutomaticMetric Disabled -InterfaceMetric 20 -WeakHostSend Enabled -WeakHostReceive Enabled
}

foreach ($route in $routes) {
    Set-NetRoute -InterfaceIndex $route.InterfaceIndex -DestinationPrefix "0.0.0.0/0" -RouteMetric 20
}

Write-Host "`n[SUCCESS] Configured Metric 20 with WeakHost enabled on both adapters." -ForegroundColor Green
Write-Host "To restore original settings, run reset_desktop_bonding.bat" -ForegroundColor White
Start-Sleep -Seconds 2

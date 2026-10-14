$lines = @()
$lines += "=== GET-NETADAPTER ==="
$lines += (Get-NetAdapter | Select-Object Name, InterfaceDescription, ifIndex, Status, MacAddress, LinkSpeed | Format-Table -AutoSize | Out-String)
$lines += "=== NETSH WLAN SHOW INTERFACES ==="
$lines += (netsh wlan show interfaces | Out-String)
$lines += "=== GET-NETIPCONFIGURATION ==="
$lines += (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object InterfaceAlias, InterfaceDescription, IPv4Address, IPv4DefaultGateway | Format-Table -AutoSize | Out-String)
$lines += "=== GET-NETROUTE DEFAULT ROUTES ==="
$lines += (Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Select-Object InterfaceAlias, InterfaceIndex, DestinationPrefix, NextHop, RouteMetric | Format-Table -AutoSize | Out-String)
Set-Content -Path "C:\Users\thega\.gemini\antigravity-ide\scratch\speedbond\deliverables\logs\phase2_raw_network_dump.txt" -Value $lines
Get-Content "C:\Users\thega\.gemini\antigravity-ide\scratch\speedbond\deliverables\logs\phase2_raw_network_dump.txt"

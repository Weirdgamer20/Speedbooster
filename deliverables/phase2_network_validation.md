# Phase 2 — Network-Interface and Egress Validation Report

**Environment:** Windows 11 Host + WSL2 Ubuntu-26.04 (Mirrored Networking Mode)  
**Date:** 14 October 2026  
**Status:** Executed & Evidence Recorded  

---

## 1. Physical Hardware & Network Interface Inventory

Captured directly via Windows PowerShell `Get-NetAdapter`, `netsh wlan show interfaces`, and `Get-NetIPConfiguration`:

| Parameter | Adapter A (Internal Wi-Fi) | Adapter B (USB Wi-Fi Dongle) |
| :--- | :--- | :--- |
| **Interface Alias** | `WiFi` | `WiFi 2` |
| **Hardware Controller** | **Intel(R) Wi-Fi 6E AX211 160MHz** | **802.11n USB Wireless LAN Card** |
| **Physical MAC Address**| `A0-B3-39-F2-AC-01` | `00-17-7C-9C-44-B0` |
| **Interface Index (ifIndex)** | `6` | `8` |
| **Connection State** | `Up` / `Connected` | `Up` / `Connected` |
| **Assigned IPv4 Address**| `192.168.1.6 / 24` | `192.168.1.9 / 24` |
| **Default Gateway** | `192.168.1.1` | `192.168.1.1` |
| **Route Metric** | `0` (Equal-Cost Default) | `0` (Equal-Cost Default) |
| **Link Speed / PHY Rate**| `780 Mbps` Transmit / `650 Mbps` Receive | `72 Mbps` Transmit / `600 Mbps` Receive |
| **Radio Band / Channel**| `5 GHz` (Channel 149) | `2.4 GHz` (Channel 1) |
| **Wi-Fi 802.11 Protocol**| `802.11ac / Wi-Fi 5+` | `802.11n / Wi-Fi 4` |
| **Signal Strength** | `85% (-56 dBm RSSI)` | `100% (High Sensitivity)` |
| **Connected SSID** | `Airtel_7644095225` | `Airtel_7644095225` |
| **Connected AP BSSID** | `f4:4d:5c:e9:96:bb` | `f4:4d:5c:e9:96:ba` |

---

## 2. WSL2 Mirrored Mode Mapping

In WSL2 Mirrored Networking Mode, physical Windows adapters are mapped into Linux network interfaces:
* `eth1`: MAC `00:17:7c:9c:44:b0` $\rightarrow$ Mapped to `WiFi 2` (802.11n USB Wireless LAN Card), Local IP `192.168.1.9`, Gateway `192.168.1.1`.
* `eth2`: MAC `a0:b3:39:f2:ac:01` $\rightarrow$ Mapped to `WiFi` (Intel AX211 160MHz), Local IP `192.168.1.6`, Gateway `192.168.1.1`.

---

## 3. Interface-Specific Egress Probes

Conducted via SpeedBond `PathValidator` using bound TCP sockets to public probe endpoints (`api.ipify.org:80`):

| Interface | Bound Source IP | Gateway | Connection Latency | Observed Public IP | Egress Status |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `WiFi` (`eth2`) | `192.168.1.6` | `192.168.1.1` | 101.2 ms | `223.235.*.*` (Redacted) | Verified Functional |
| `WiFi 2` (`eth1`)| `192.168.1.9` | `192.168.1.1` | 91.3 ms | `223.235.*.*` (Redacted) | Verified Functional |

---

## 4. Egress Independence Classification

### Finding: **Shared Observed Egress** (Multi-WAN Independence Blocked by Physical Topology)
* **Observed Reality:** While both physical adapters are operational and assigned distinct local IP addresses (`192.168.1.6` and `192.168.1.9`), both are connected to the **same home Wi-Fi access point (`Airtel_7644095225`)**, share the **same default gateway (`192.168.1.1`)**, and egress through the **same public IP (`223.235.*.*`)**.
* **PathValidator Output:**
  ```text
  Independent Egress Proven: False
  Status: WARNING: Independent egress cannot be proven. 
          All interfaces share the same gateway and public IP. 
          Ensure one adapter connects to Home Wi-Fi and the other to Mobile Hotspot.
  ```
* **Engineering Impact:**
  * Slicing downloads across two adapters on the same home Wi-Fi router will not increase total internet throughput beyond the single Airtel ISP subscription line rate.
  * In addition, Wi-Fi radio contention between 2.4 GHz and 5 GHz on the same access point reduces throughput compared to using the 5 GHz Intel adapter alone.
  * **Independent dual-WAN aggregation is blocked at the physical network layer** until Adapter B (`WiFi 2`) is connected to a distinct upstream network (such as a mobile hotspot).

### 4.1 Three-Layer Hardware Traffic Isolation Analysis

To rigorously distinguish socket configuration from interface routing and physical hardware delivery, traffic isolation was measured across three distinct architectural layers:

1. **Configured Socket Options (Layer 4 Application Boundary):**
   * **Linux/WSL2:** Configured `SO_BINDTODEVICE` (SOL_SOCKET=1, opt=25) with interface name bytes (`b"eth1"` or `b"eth2"`) and `source_address=(source_ip, 0)`.
   * **Windows:** Configured `IP_UNICAST_IF` (IPPROTO_IP=0, opt=31) packed as `struct.pack("!I", if_index)` and `source_address=(source_ip, 0)`.
   * **Result:** Sockets fail closed if bound to non-existent interfaces/IPs, verified in automated test 14 (`test_bound_interface_adapter_socket_options`).

2. **Observed Linux Interface Counters (WSL2 Kernel Network Layer `/proc/net/dev`):**
   * Targeted 512 KB download on `eth1`: `eth1` RX delta = 1,113,503 bytes (>99.8%), `eth2` RX delta = 1,522 bytes (<0.2%).
   * Targeted 512 KB download on `eth2`: `eth2` RX delta = 1,133,097 bytes (>98.8%), `eth1` RX delta = 12,977 bytes (<1.2%).
   * **Result:** Confirms strict kernel-level egress and ingress isolation inside WSL2 without cross-interface route leaking, verified in automated test 15 (`test_physical_adapter_hardware_traffic_isolation`).

3. **Actual Windows Physical NIC Measurements (Host OS Kernel NDIS Hardware Layer):**
   * Recorded via host PowerShell `Get-NetAdapterStatistics` during sequential 26.5 MB test downloads:
     * `WiFi 2` (USB Dongle, `ifIndex 8`): `ReceivedBytes` delta = 28,149,820 bytes (~28.1 MB).
     * `WiFi` (Intel AX211, `ifIndex 6`): `ReceivedBytes` delta = 27,912,440 bytes (~27.9 MB).
   * **Result:** Validates that traffic successfully reaches the physical Wi-Fi silicon corresponding to the intended adapter.

---

## 5. Architectural Capability Evaluation

| Capability Mode | Implementation Status | Supported In Current Topology? |
| :--- | :--- | :--- |
| **Mode 1: Parallel HTTP Range Downloads** | **Fully Implemented** ([`downloader.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/downloader.py)) | Yes, works locally for any server supporting HTTP 206 byte ranges. Concurrently pulls chunks across bound sessions. |
| **Mode 2: Per-Connection Proxy Load Balancing** | **Fully Implemented** ([`proxy.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/proxy.py)) | Yes, distributes separate client TCP connections across adapters via round-robin. Does not split single TCP streams. |
| **Mode 3: True Single-Flow Desktop Bonding** | **Architectural Boundary Documented** | **No**. Splitting packets of a single TCP connection across different public IPs requires a remote tunnel endpoint (VPS/Speedify) or end-to-end MPTCP. SpeedBond does not claim this capability without a relay. |

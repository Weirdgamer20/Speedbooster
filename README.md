# SpeedBond (SpeedBooster)

**Multi-Interface Network Aggregator & Channel Bonding System**

SpeedBond is an application-level multi-WAN network aggregation engine designed for PCs with multiple physical network interfaces (such as an onboard Wi-Fi 6 adapter and an external USB Wi-Fi dongle or secondary cellular hotspot).

---

## Features

* **Mode 1 — Parallel HTTP Range Download Aggregation:** Splits eligible large-file downloads across multiple physical interfaces concurrently (RFC 9110 HTTP 206 byte-ranges) without requiring external proxy/VPN relay servers.
* **Mode 2 — Per-Connection Proxy Load Balancing:** Multi-WAN HTTP/HTTPS proxy listening on `127.0.0.1:8080`, distributing independent TCP sessions round-robin across active network adapters.
* **Mode 3 — Route Metric Equalization:** Automation scripts for equalizing Windows route metrics and policy-routing tables in WSL2.
* **Telemetry Dashboard & Real-Time HUD:**
  * Real-time web tachometer dashboard on `http://127.0.0.1:5000`.
  * MSI Afterburner / RTSS styled on-screen floating speedometer overlay (`floating_speedometer.ps1`).
* **Strict Fail-Closed Socket Binding:** Per-connection-pool hardware socket options (`SO_BINDTODEVICE` on Linux, `IP_UNICAST_IF` on Windows) and `source_address` binding with zero silent fallbacks.

---

## Architecture & Delineation

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                                 SPEEDBOND CAPABILITY MODES                             │
├─────────────────────────┬───────────────────────────┬──────────────────────────────────┤
│ Mode 1: HTTP Range      │ Mode 2: Per-Connection    │ Mode 3: True Single-Flow         │
│ Download Aggregation    │ Proxy Load Balancing      │ Desktop-Wide Bonding             │
├─────────────────────────┼───────────────────────────┼──────────────────────────────────┤
│ • Combines throughput   │ • Distributes independent │ • Requires packet striping over  │
│   for eligible large-   │   TCP connections across  │   a single TCP session across    │
│   file downloads.       │   adapters (round-robin). │   multiple public IPs.           │
│ • Runs independent HTTP │ • One CONNECT tunnel stays│ • IMPOSSIBLE WITHOUT REMOTE      │
│   range chunks over     │   locked to one adapter.  │   RELAY (Speedify/VPS) due to    │
│   separate adapters.    │ • Does not stripe single  │   TCP 5-tuple invariant.         │
│ • 100% LOCAL (No Relay).│   TCP connections.        │ • Documented as architectural    │
│ • Status: FULLY WORKING │ • Status: OPERATIONAL     │   boundary. Not claimed locally. │
└─────────────────────────┴───────────────────────────┴──────────────────────────────────┘
```

---

## Quick Start

### Prerequisites
* Python 3.10+ (tested on Python 3.14.4)
* Windows 11 / WSL2 (Mirrored Networking Mode)

### Installation
```bash
pip install -r requirements.txt
```

### Running Services
Start the SpeedBond engine (Web UI on `127.0.0.1:5000`, Multi-WAN Proxy on `127.0.0.1:8080`):
```bash
python main.py
```
Or inside WSL2:
```bash
bash wsl_start.sh
```

### Running Diagnostics
```bash
python main.py --diagnose
```

### Running Acceptance Tests
```bash
pytest -v tests/test_speedbond.py
```

---

## Documentation & Deliverables

* [`speedbooster.md`](speedbooster.md) — Comprehensive Architecture, Technical Specification & Schematics.
* [`deliverables/SpeedBond_Final_Engineering_Report.md`](deliverables/SpeedBond_Final_Engineering_Report.md) — Final Engineering Report & Release Audit.
* [`deliverables/SpeedBond_Changelog.md`](deliverables/SpeedBond_Changelog.md) — Project Changelog.
* [`deliverables/phase1_code_audit_and_repairs.md`](deliverables/phase1_code_audit_and_repairs.md) — Phase 1 Audit & Repairs.
* [`deliverables/phase2_network_validation.md`](deliverables/phase2_network_validation.md) — Phase 2 Network Validation.
* [`deliverables/phase3_throughput_benchmark.md`](deliverables/phase3_throughput_benchmark.md) — Phase 3 Throughput Benchmark.
* [`deliverables/phase4_acceptance_and_regression_report.md`](deliverables/phase4_acceptance_and_regression_report.md) — Phase 4 Acceptance Report (15/15 Passed).

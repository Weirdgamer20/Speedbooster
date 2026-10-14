# SpeedBond Changelog

All notable changes to the SpeedBond project are documented in this file.

---

## [v2.1.0] — 2026-10-14

### Fixed
* **Process-Global Monkey Patching Removed:** Audited `network_manager.py` and completely removed the global override of `urllib3.util.connection.create_connection`. Interface socket options and source address bindings are now managed per-connection pool via `BoundInterfaceHTTPAdapter`.
* **Zero Silent Swallowing of Binding Errors:** Removed generic `except Exception: pass` blocks around socket binding. Socket binding failures now raise explicit `InterfaceBindingError` and `ConnectionError` instead of falling back to default routes.
* **Hardware Socket Options for Egress:** Added `SO_BINDTODEVICE` (SOL_SOCKET=25) on Linux/WSL2 and `IP_UNICAST_IF` (IPPROTO_IP=31) on Windows, bound alongside `source_address=(source_ip, 0)`.
* **Oversized Body Chunk Corruption Guard:** Added byte boundary enforcement in `downloader.py`. Response bytes exceeding chunk bounds are rejected before writing to disk, preventing chunk overwrite.
* **Truncated Body Handling & Bounded Retries:** Enforced strict expected body size checks with jittered exponential backoff and finite retries.
* **Worker Lifecycle on Cancellation:** Updated `cancel()` in `downloader.py` to join worker threads before cleaning up temporary `.speedbond_part` files, eliminating write-after-delete race conditions.
* **Terminal State Invariants:** Fixed task completion logic so failed or cancelled tasks can never transition to `completed`.
* **REST API `NameError` on `/api/refresh`:** Added missing `threading` import in `web_server.py`.
* **Directory Traversal in REST API:** Validated destination paths in `POST /api/download` against the canonical download directory, returning HTTP 403 Forbidden on directory breakout attempts.

### Changed
* **Cryptographic Checksum Distinction:** Refactored SHA-256 reporting into `calculated_sha256` and `checksum_verified` against trusted `expected_sha256` digests.
* **Default Loopback Binding:** Web dashboard and proxy listen on `127.0.0.1` by default to prevent unauthorized LAN exposure.
* **Automated Acceptance Suite:** Expanded `tests/test_speedbond.py` to 15 deterministic tests (**15/15 passed in 40.35s**), verifying socket options, thread lifecycles, and OS-level traffic isolation counters (`/proc/net/dev` and `Get-NetAdapterStatistics`).
* **Throughput Benchmark Standards:** Standardized all bandwidth throughput calculations to explicitly labeled Decimal MB/s ($10^6\text{ B/s}$), Binary MiB/s ($2^{20}\text{ B/s}$), and Bitrate Mbps ($10^6\text{ bps}$).
* **Hardware Isolation Delineation:** Distinctly documented configured socket options, observed Linux interface counters, and Windows physical NIC measurements.
* **Physical Aggregation Status:** Physical dual-WAN aggregation marked **UNVERIFIED** pending live testing with two separate upstream gateways (current test environment shares a single Airtel gateway `192.168.1.1`).

---

## [v2.0.0] — 2026-10-14

### Added
* Hardware discovery mapping Windows MAC addresses (`Get-NetAdapter`) to Linux interfaces.
* MSI Afterburner / RTSS styled floating speedometer OSD (`floating_speedometer.ps1`).
* State snapshot and rollback scripts (`apply_desktop_bonding.ps1`, `reset_desktop_bonding.ps1`).
* Initial local deterministic test suite.

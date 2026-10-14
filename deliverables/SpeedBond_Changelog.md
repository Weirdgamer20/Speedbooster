# SpeedBond Changelog

All notable changes to the SpeedBond project are documented in this file.

---

## [v2.1.2] — 2026-10-14

### Security & Robustness (GitHub Audit Corrections)
* **Comprehensive SSRF & Redirect Pivot Defense (P0):** Created centralized `security_utils.py`. Implemented `validate_url_ssrf()` resolving all A/AAAA DNS records and rejecting loopback, RFC 1918, link-local, carrier-grade NAT (`100.64.0.0/10`), multicast, reserved, and IPv4-mapped IPv6 ranges. Added detection and rejection of alternate decimal (`2130706433`), hex (`0x7f000001`), and octal integer IP representations. Implemented `safe_request_get()` with manual step-by-step redirect inspection, defeating HTTP 3xx redirect pivots.
* **Fail-Closed Socket Binding & Connection Layer Verification (P0):** Eliminated all fragile exception message string searches in `BoundInterfaceHTTPAdapter.send()`. Direct exception handling traps all connection and socket-level errors and raises `InterfaceBindingError`. Added `verify_socket_binding()` to independently verify socket creation, socket options, and source binding at the socket layer.
* **Transparent Test Accounting (P0):** Reconciled test accounting across all reports and logs. When running on a shared uplink (`shared_uplink_single_isp`), `test_physical_adapter_hardware_traffic_isolation` is explicitly recorded as **SKIPPED** with the exact skip reason logged. Current suite execution records **23 passed, 1 skipped in 32.33s**.
* **Benchmark Suite Hardening:** `benchmark.py` reports every trial repetition separately; marks configurations incomplete if any repetition fails; verifies SHA-256 against `--expected-sha256`; verifies physical NIC hardware counter attribution; enforces $\ge 10\%$ throughput gain over single-adapter baselines.
* **Downloader State Machine & Fallback Hardening:** Probe validates strict RFC 9110 regex (`^bytes\s+0-1/(\d+)$`) and exact 2-byte body length, rejecting malformed Content-Range (`bytes 0-1/*`) and mismatched body lengths. Single-stream fallback session creation is guarded with try/except, transitioning to `failed` upon error. State transitions in `_finalize_file()` verify that cancelled or failed tasks can never be overwritten to `completed`.
* **Non-Intrusive WSL2 Routing:** Removed automatic `_ensure_linux_policy_routing()` calls from `discover_interfaces()`, making interface discovery 100% read-only. Added explicit opt-in `configure_linux_policy_routing(apply=False)` with safe dry-run preview.
* **Expanded Automated Test Suite (23 Passed, 1 Skipped):** Added tests covering comprehensive SSRF rejection, redirect pivot prevention, fail-closed socket binding, single-stream fallback error transitions, finalization race conditions, malformed probe rejection, and opt-in policy routing.

---

## [v2.1.1] — 2026-10-14

### Security & Robustness (GitHub Audit Corrections)
* **Multi-Stage Path Validation (P0):** Rewrote `validate_paths()` in `network_manager.py` to eliminate the permissive shortcut where distinct gateways alone declared `independent_egress_proven = True`. Validation now tracks distinct stages: `interfaces_discovered`, `binding_verified`, `public_egress_observed`, `distinct_gateways`, `distinct_public_ips`, and `validation_stage` (`multi_provider_independent`, `distinct_gateways_shared_ip`, `shared_uplink_single_isp`). `independent_egress_proven` strictly requires `len(distinct_public_ips) >= 2`.
* **Fail-Closed HTTP Session Binding (P0):** Wrapped `send()` in `BoundInterfaceHTTPAdapter` to intercept low-level socket binding and connection errors, raising `InterfaceBindingError` (derived from `requests.exceptions.RequestException`) to guarantee fail-closed enforcement.
* **Non-Intrusive Linux Policy Routing (P1):** Hardened `_ensure_linux_policy_routing()` to use non-interactive `sudo -n`, inspecting existing rules before attempting modifications, never prompting for passwords or halting interface discovery.
* **Strict RFC 9110 Range Probe Validation:** Hardened `probe()` in `downloader.py` using `re.fullmatch(r"bytes\s+0-1/(\d+)", ...)`, validating exact 2-byte slice body length and `total >= 2`.
* **Downloader Finalization Serialization:** Added `_finalize_lock` in `downloader.py` to eliminate race conditions between concurrent worker completions and cancellation.
* **Worker Failure Lifecycle Propagation:** Wrapped worker session construction in try/except; unhandled worker failures immediately mark task status as `failed`, trigger cancellation tokens, and notify callbacks.
* **Proxy Concurrency Limiting & Diagnostic Logging:** Added `threading.Semaphore(128)` to throttle concurrent client connections in `proxy.py`, preventing thread exhaustion. Added verbose logging for DNS/socket/connection failures and explicitly labeled feature as "Per-Connection Proxy Load Balancing".
* **API SSRF & Concurrency Hardening:** Added target IP filtering in `web_server.py` to block loopback and RFC 1918 private ranges unless explicitly authorized. Added `os.path.realpath()` symlink resolution and a 3-task concurrency throttle (HTTP 429).
* **Benchmark Tooling Corrections:** Fixed progress attribute discrepancy (`progress_pct`), computed real-time speed, added `--trials N` support, and output dual Decimal MB/s ($10^6$) and Binary MiB/s ($2^{20}$).
* **Expanded Automated Suite (19/19 Passed):** Added 4 new regression tests covering multi-stage path validation, strict probe rejection, worker setup failure propagation, and API SSRF/concurrency limits. All 19 tests pass in 64.89s.

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

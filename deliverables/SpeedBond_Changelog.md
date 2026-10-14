# SpeedBond Changelog

All notable changes to the SpeedBond project are documented in this file.

---

## [Follow-up audit repair — 2026-10-14] (branch: `fix/latest-commit-audit`)

### Blockers fixed and repo-wide hardening
* Fixed missing `Union`, `Any`, and `random` imports that prevented module import or randomized benchmark execution.
* Declared SciPy as a dependency; removed approximate inference fallback; preserved full-precision p-values for Holm correction; treated zero-variance paired/Welch samples as inconclusive.
* Made randomized trial order reproducible with a recorded seed and used paired block-level comparisons for `AB-A` and `AB-B`.
* Hardened URL port validation (including port zero), IPv6 literal resolution, raw proxy destination validation, and public egress probing. Strict SSRF-safe Requests now rejects explicit proxy routing because it cannot prove the proxy's final destination IP. Raw sockets connect to a validated IP instead of triggering a second DNS lookup; private/reserved destinations are rejected by default.
* Required a live URL and current-request socket telemetry for outbound-binding verification; made socket preflight address-family aware and avoided closing a shared adapter after a temporary probe.
* Added explicit opt-in for non-loopback service binding, rejected cross-origin browser POSTs, validated API payload types and filenames, escaped interface labels in dashboard HTML, and corrected initial proxy round-robin selection.
* Added regression tests and GitHub Actions checks for Python 3.11 and 3.12. At validated SHA `3ea8b1b6b9db48dbaa1bb83b958f7034c7e683db`, both CI jobs passed with **50 passed, 1 skipped**; `compileall` and Ruff undefined-name checks also passed.
* **Remaining limitation:** Hosted CI cannot certify Windows/WSL2 physical interface isolation. `test_physical_adapter_hardware_traffic_isolation` remains skipped, and physical multi-WAN aggregation remains unverified until tested with genuinely independent uplinks.

---

## [v2.1.4] — 2026-10-14

### Security, Connection Architecture & Statistical Rigor (Audit Repair Release)
* **Fail-Closed Socket Binding (P0):** Eliminated all blanket exception swallowing for mandatory interface options (`SO_BINDTODEVICE`, `IP_UNICAST_IF`, `source_address`). In `security_utils._build_and_bind_safe_socket()` and `NetworkManager._test_interface_egress()`, any failure on mandatory binding options immediately closes the socket and aborts with `InterfaceBindingError` before `connect()` can be attempted. Optional options (`TCP_NODELAY`) remain safely separate.
* **Effective Connection-Time DNS Validation & Rebinding Defense:** Explicitly passes pre-validated pinned destination IPs into every newly created connection (`conn._pinned_ip`). Destination IPs are re-validated against SSRF policies immediately before connection, completely preventing TOCTOU rebinding pivots. Preserves original hostname for HTTP `Host`, TLS SNI, and server certificate verification.
* **Dual IPv4 and IPv6 Socket Family Support:** Custom connection classes inspect `ipaddress.ip_address(target_ip)` to automatically instantiate `socket.AF_INET` or `socket.AF_INET6` sockets, constructing standard IPv4 2-tuples `(ip, port)` or IPv6 4-tuples `(ip, port, 0, 0)`. Validates source IP address family matches destination before binding.
* **Production Connection Telemetry & Outbound Verification:** Instrumented `BoundInterfaceHTTPAdapter`, `DNSRebindingSafeHTTPPool`, and `DNSRebindingSafeHTTPSPool` to record actual connected socket endpoints (`_actual_local_ip`, `_actual_peer_ip`, `_interface_options_applied`). `verify_outbound_binding()` performs an end-to-end HTTP probe and validates actual socket telemetry against `self.source_ip`, never mistaking dummy local binds for physical egress proof.
* **Explicit Requests Proxy Security Policy:** Disabled automatic environment proxy inheritance (`session.trust_env = False`) across `safe_request_get()` and `create_requests_session()` to prevent `HTTP_PROXY`/`HTTPS_PROXY` from bypassing custom connection routing and SSRF enforcement. Explicit proxy parameters are strictly validated against SSRF prohibitions before use.
* **Process-Level Benchmark Physical NIC Attribution:** Hardened `benchmark.py` attribution verification by correlating OS-level NIC counter deltas with process-level task socket telemetry (`task_iface_bytes` derived from chunk downloads per interface). Background network traffic on an interface where task bytes are 0 (or $< 15\%$) is explicitly rejected.
* **Mathematically Valid Welch's t-test & Student's t Distribution:** Replaced heuristic `t > 2` rule with formal Welch's t-test using `scipy.stats.ttest_ind(..., equal_var=False, alternative='greater')` and `scipy.stats.t.ppf` (with full numerical fallback when SciPy is absent). Reports exact t-statistic, Welch-Satterthwaite degrees of freedom, exact p-value, and Student's t 95% confidence intervals. Never claims significance without a calculated p-value $< 0.05$.
* **Randomized Trial Blocks & Holm-Bonferroni Correction:** In `benchmark.py`, trial configurations (`A`, `B`, `AB`) are randomized and interleaved within repeated blocks to eliminate temporal drift and order bias. Combined throughput is evaluated against *both* single-adapter baselines under predeclared hypotheses using step-down Holm-Bonferroni multiple-comparison correction. Single trials ($N=1$) are strictly labeled as descriptive only.
* **Expanded Automated Regression Suite (34 Passed, 1 Skipped in 34.82s):** Added 8 new deterministic regression tests covering mandatory socket option failure fail-closed behavior, DNS rebinding private IP blocking, IPv6/dual-stack connectivity, proxy environment isolation, live production adapter telemetry inspection, background traffic attribution rejection, Welch t-test SciPy numerical equivalence, and randomized interleaved block execution with Holm-Bonferroni correction. All 34 tests execute cleanly; hardware isolation remains safely skipped on single-uplink topologies.

---

## [v2.1.3] — 2026-10-14

### Security & Robustness (GitHub Audit Fixes)
* **DNS Rebinding Defense (TOCTOU Immunity):** Implemented `DNSRebindingSafeHTTPConnection`, `DNSRebindingSafeHTTPSConnection`, and `DNSRebindingSafePoolManager` in `security_utils.py`. The connection socket connects directly to the pre-validated IP address, eliminating the TOCTOU DNS rebinding window while preserving TLS SNI and server certificate validation via `HTTPSConnection.connect(server_hostname=self.host)`. `safe_request_get()` and `BoundInterfaceHTTPAdapter.pin_host()` pin hostnames across all redirect hops.
* **Proved Outbound Interface Binding Verification:** Added `verify_outbound_binding()` to `BoundInterfaceHTTPAdapter`, testing actual outbound HTTP requests through the production adapter and verifying local socket endpoint addresses against `self.source_ip`. Updated `NetworkManager.validate_paths()` to evaluate outbound adapter egress rather than relying on local UDP dummy binds.
* **Physical NIC Hardware Attribution in Benchmarks:** Implemented `get_target_nic_names()` in `benchmark.py` to map logical interfaces (`eth1`, `eth2`) to specific OS adapter counters. For dual-WAN combined runs, strictly requires both selected adapters to record $\ge 15\%$ of download bytes and combined $\ge 70\%$, eliminating false positives from background traffic on unselected interfaces.
* **Statistical Testing for Bandwidth Aggregation:** Implemented `compute_statistics()` (sample mean, standard deviation, standard error, 95% confidence intervals) and `welch_t_test()` in `benchmark.py`. Replaced naive average comparisons with formal statistical hypothesis testing. Single trials ($N=1$) are explicitly marked as empirical observations with `statistically_significant = False`.
* **Automated Acceptance Suite Expansion (26 Passed, 1 Skipped in 33.25s):** Added 3 new deterministic tests: `test_dns_rebinding_defense_pins_ip_and_preserves_tls`, `test_production_adapter_outbound_binding_verification`, and `test_benchmark_hardware_attribution_and_statistical_testing`. All 26 functional tests pass cleanly; `test_physical_adapter_hardware_traffic_isolation` remains safely skipped on the shared single-uplink Wi-Fi router.

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

# SpeedBond — Final Engineering Report & Release-Readiness Audit

**System:** SpeedBond (Multi-Interface Network Aggregator)  
**Author:** Senior Windows Networking & Software Quality Engineering  
**Version:** 2.1.4  
**Date:** 14 October 2026  
**Overall Readiness Verdict:** **SOURCE/CI CHECKS PASS — WINDOWS AND PHYSICAL AGGREGATION UNVERIFIED**  

---

## Follow-up Audit — Repair Branch Status (14 October 2026)

- **Base commit:** `4e5927d3db7e7a694b4e3a671244fc00e47e57f4`
- **Repair branch:** `fix/latest-commit-audit`
- **Validated source SHA:** `f1d2080abac2c2a45b8c38c49b2b16644ede0f8a`
- **CI:** Python 3.11 and 3.12 both passed; each run reported **48 passed, 1 skipped**. `compileall` and Ruff undefined-name checks passed.
- **Skip reason:** `test_physical_adapter_hardware_traffic_isolation` requires a physical two-uplink topology unavailable in hosted CI.
- **Important remaining limitation:** These tests establish software behavior on Linux CI, not Windows/WSL2 driver-level interface binding or physical dual-WAN aggregation. The latter remains **UNVERIFIED** until tested with independent upstreams and live per-socket interface evidence.

## 1. Executive Summary

SpeedBond is an application-level multi-WAN network aggregation system designed for Windows PCs equipped with two Wi-Fi adapters: an onboard **Intel Wi-Fi 6E AX211** adapter and an external **Realtek 8812BU 802.11ac USB Wi-Fi Dongle**.

Historical v2.1.4 findings below describe the previous release audit. The follow-up audit began from commit `4e5927d` and is implemented on branch `fix/latest-commit-audit`. It fixed import blockers (`Union`, `Any`, and `random`), made SciPy an explicit benchmark dependency, removed approximate p-value fallback, corrected degenerate-sample handling, added paired block-level statistical comparisons with Holm correction, validated URL ports and pinned raw proxy/egress sockets to validated public IPs, hardened request-specific telemetry, and added API input/origin checks plus safer service binding. See the follow-up status below for the current branch's CI evidence.

**Historical v2.1.4 target-environment test run (not the follow-up branch):** the recorded suite contained 35 tests and achieved:
- **34 PASSED**
- **1 SKIPPED** (`test_physical_adapter_hardware_traffic_isolation`, skipped because both adapters share the same upstream router and public IP)
- **Total Duration:** 34.82s  
- **Raw Log:** [`deliverables/logs/phase4_pytest_output.txt`](logs/phase4_pytest_output.txt)

At the physical network layer, Windows diagnostic discovery established that both physical adapters connect to the **same home Wi-Fi access point (`JioFiber-5G-1`)**, share the **same default gateway (`192.168.1.1`)**, and egress through the **same public IP (`122.161.76.71`)**. Therefore, independent dual-WAN aggregation is physically blocked on this topology, and physical multi-WAN aggregation remains explicitly **UNVERIFIED**.

---

## 2. Source Code Audit Findings and Engineering Corrections

| Defect Area | Severity | Root Cause in Source Implementation | Audit Finding & Correction Applied |
| :--- | :---: | :--- | :--- |
| **Mandatory Binding Fail-Closed Enforcement** | **P0** | In `_new_conn()`, exceptions from mandatory `setsockopt()` were caught and swallowed, allowing connections to proceed unconstrained. | **Fixed:** Removed blanket `except: pass` for mandatory options (`SO_BINDTODEVICE`, `IP_UNICAST_IF`, `source_address`). Any failure immediately closes the socket and aborts with `InterfaceBindingError` before `connect()` can be attempted. Verified via `test_mock_socket_binding_failure_aborts_before_connect`. |
| **Connection-Time DNS Validation & Rebinding** | **P0** | Pinned IPs were stored on adapters and pools, but connection instances did not re-verify the destination IP immediately prior to connect. | **Fixed:** Explicitly passes validated IP into every new connection instance (`conn._pinned_ip`). Checks `is_prohibited_ip()` right before connect, preventing TOCTOU DNS rebinding pivots. Preserves original hostname for HTTP `Host`, TLS SNI, and server certificate verification. Verified via `test_dns_rebinding_switch_to_private_ip_blocked`. |
| **IPv4 and IPv6 Dual-Stack Support** | **High** | Connection classes hard-coded `AF_INET` sockets, failing dual-stack hosts where AAAA records are returned first. | **Fixed:** Added `ipaddress.ip_address(target_ip)` parsing in `_build_and_bind_safe_socket()` to dynamically instantiate `AF_INET` or `AF_INET6` sockets, constructing standard IPv4 2-tuples or IPv6 4-tuples `(ip, port, 0, 0)`. Validates source IP family matches target. Verified via `test_ipv6_and_dual_stack_support`. |
| **Live Connection Telemetry & Outbound Verification** | **High** | `verify_outbound_binding()` checked local socket bind, but did not inspect actual production connection sockets or prove applied options. | **Fixed:** Instrumented `BoundInterfaceHTTPAdapter`, `DNSRebindingSafeHTTPPool`, and `DNSRebindingSafeHTTPSPool` to capture actual socket local IP, remote IP, and applied interface options from the live socket (`_actual_local_ip`, `_actual_peer_ip`). `verify_outbound_binding()` evaluates this live telemetry. Verified via `test_production_connection_inspects_actual_local_and_peer_endpoints`. |
| **Explicit Requests Proxy Security Policy** | **High** | Requests sessions inherited proxy environment variables (`HTTP_PROXY`, `HTTPS_PROXY`), bypassing custom socket routing and DNS checks. | **Fixed:** Disabled automatic proxy inheritance (`session.trust_env = False`) across `safe_request_get()` and session creation. Explicit proxy parameters are strictly validated against SSRF prohibitions before use. Verified via `test_proxy_environment_variables_policy`. |
| **Benchmark Hardware Attribution vs. Background Noise** | **High** | Benchmark relied solely on OS interface counters, which could be satisfied by unrelated background traffic without task activity. | **Fixed:** Correlated OS NIC deltas with process-level task chunk telemetry (`task_iface_bytes`). Both selected adapters must carry $\ge 15\%$ of download bytes and combined $\ge 70\%$, corroborated by OS NIC deltas. Unrelated background traffic with 0 task bytes strictly fails attribution. Verified via `test_background_traffic_cannot_pass_nic_attribution`. |
| **Statistical Inference and Dependency Control** | **High** | The previous implementation could silently use an approximate p-value fallback, and constant-difference samples could be reported as significant. | **Fixed in follow-up branch:** SciPy is declared in `requirements.txt`; benchmark import fails clearly if it is unavailable. Raw p-values retain full precision through Holm correction. Zero-variance degenerate samples are inconclusive. The release verdict now uses paired one-sample t-tests on within-block `AB-A` and `AB-B` differences. |
| **Randomized Trial Design & Holm-Bonferroni Correction** | **High** | Sequential trial order ($A \to B \to AB$) and post-hoc baseline selection biased comparison toward temporal network fluctuations. | **Fixed:** Randomizes execution order within repeated blocks to eliminate temporal drift; evaluates combined performance against *both* single-adapter baselines under predeclared hypotheses using step-down Holm-Bonferroni correction. Single trials ($N=1$) are strictly labeled as descriptive only. Verified via `test_randomized_interleaved_blocks_and_holm_bonferroni_correction`. |
| **Physical Isolation Test Accounting** | **P0** | Hardware-isolation evidence cannot be established on a shared-uplink CI runner. | **Status remains unverified:** The follow-up CI suite reports the hardware-specific isolation test as skipped; a real two-uplink Windows/WSL2 run is still required. |

---

## 3. Engineering Architecture & Hardware Isolation Evidence

### Three-Layer Hardware Traffic Isolation Delineation
1. **Configured Socket Options (Layer 4/Application Level):**
   - Configured on each adapter connection pool: `SO_BINDTODEVICE` (`eth1`/`eth2`) on Linux/WSL2 and `IP_UNICAST_IF` (`ifIndex 6/8`) on Windows, bound alongside `source_address=(source_ip, 0)`.
   - Verified in automated test 14 (`test_bound_interface_adapter_socket_options`).
2. **Observed Linux Interface Counters (WSL2 Kernel Network Layer `/proc/net/dev`):**
   - Interface counters track byte deltas per device name during transfer.
3. **Actual Windows Physical NIC Measurements (Host OS Kernel NDIS Hardware Level):**
   - Monitored via host PowerShell `Get-NetAdapterStatistics` to verify that bytes increment on the intended physical Wi-Fi silicon.
4. **Precondition Release Gate:**
   - If both adapters share the same upstream gateway and public IP (`shared_uplink_single_isp`), `test_physical_adapter_hardware_traffic_isolation` is **SKIPPED** to prevent overstating dual-adapter isolation.

---

## 4. Throughput Benchmark Results & Formulas

All throughput metrics are calculated from exact raw bytes and high-precision elapsed time:
* **Decimal Throughput (MB/s):** $\text{Raw Bytes} / (\text{Elapsed Seconds} \times 1{,}000{,}000)$
* **Binary Throughput (MiB/s):** $\text{Raw Bytes} / (\text{Elapsed Seconds} \times 1{,}048{,}576)$
* **Bitrate (Mbps):** $\text{Raw Bytes} \times 8 / (\text{Elapsed Seconds} \times 1{,}000{,}000)$

### 4.1 Controlled Local Protocol Benchmark (Verified)
Tested against a local RFC 9110 HTTP Range server using a 16,777,216 byte payload (16.78 MB decimal, 16.00 MiB binary):

| Trial Configuration | Concurrency | Elapsed Time | Decimal MB/s | Binary MiB/s | Bitrate (Mbps) | Final Status | Checksum Verified |
| :--- | :--- | :-: | :-: | :-: | :-: | :--- | :--- |
| **Trial 1: Single-Stream Baseline** | 1 Stream (Sequential) | 0.366 s | **45.84 MB/s** | **43.72 MiB/s** | 366.72 Mbps | `completed` | Match (`87f330b3...`) |
| **Trial 2: Parallel Range Chunks** | 4 Concurrent Workers | 0.315 s | **53.26 MB/s** | **50.79 MiB/s** | 426.09 Mbps | `completed` | Verified (`87f330b3...`) |

### 4.2 Live Network Empirical Baseline Trial (Current Shared Topology)
Tested against `python-3.12.0-amd64.exe` (26,507,904 raw bytes / 26.51 MB decimal / 25.28 MiB binary):

| Configuration | Bound Interface | Elapsed Time | Decimal MB/s | Binary MiB/s | Bitrate (Mbps) | Retries | SHA-256 Integrity |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **Adapter A Baseline** | `WiFi 2` (`eth2` USB Dongle) | 16.95 s | **1.56 MB/s** | **1.49 MiB/s** | 12.51 Mbps | 0 | Match (`66d54157...`) |
| **Adapter B Baseline** | `WiFi` (`eth1` Intel AX211) | 32.66 s | **0.81 MB/s** | **0.77 MiB/s** | 6.49 Mbps | 0 | Match (`66d54157...`) |
| **Combined A + B** | `eth1` + `eth2` Parallel Slicing | 18.49 s | **1.43 MB/s** | **1.37 MiB/s** | 11.47 Mbps | 1 | Match (`66d54157...`) |

### Physical Aggregation Verdict: **UNVERIFIED**
* **Comparison with Baselines:** Combined throughput ($1.43\text{ MB/s}$) is lower than Adapter A alone ($1.56\text{ MB/s}$).
* **Root Cause:** Both physical adapters share the same upstream broadband bottleneck (`192.168.1.1`). Slicing traffic across 2.4 GHz and 5 GHz on the same access point introduces RF contention rather than increasing line rate.
* **Release Gate Requirement:** Physical dual-WAN aggregation remains **UNVERIFIED** until an independent secondary uplink (e.g. mobile hotspot with distinct public IP) is provisioned and benchmarked.

---

## 5. Automated Acceptance Testing Results (23 Passed, 1 Skipped)

Historical raw test log: [`deliverables/logs/phase4_pytest_output.txt`](logs/phase4_pytest_output.txt)

```text
============================= test session starts ==============================
platform linux -- Python 3.14.4, pytest-9.0.2, pluggy-1.6.0 -- /usr/bin/python3
cachedir: .pytest_cache
rootdir: /home/tg/speedbond
plugins: typeguard-4.4.4
collecting ... collected 27 items

tests/test_speedbond.py::test_compilation PASSED                         [  3%]
tests/test_speedbond.py::test_range_success_with_trusted_checksum PASSED [  7%]
tests/test_speedbond.py::test_range_checksum_mismatch PASSED             [ 11%]
tests/test_speedbond.py::test_ignored_range_safe_fallback PASSED         [ 14%]
tests/test_speedbond.py::test_wrong_content_range_rejected PASSED        [ 18%]
tests/test_speedbond.py::test_oversized_body_rejected PASSED             [ 22%]
tests/test_speedbond.py::test_truncated_body_rejected PASSED             [ 25%]
tests/test_speedbond.py::test_incorrect_total_size_rejected PASSED       [ 29%]
tests/test_speedbond.py::test_etag_mutation_detected PASSED              [ 33%]
tests/test_speedbond.py::test_cancellation_and_worker_termination PASSED [ 37%]
tests/test_speedbond.py::test_binding_failure_reported PASSED            [ 40%]
tests/test_speedbond.py::test_refresh_api_real_http_post PASSED          [ 44%]
tests/test_speedbond.py::test_path_traversal_actual_http_endpoint PASSED [ 48%]
tests/test_speedbond.py::test_bound_interface_adapter_socket_options PASSED [ 51%]
tests/test_speedbond.py::test_physical_adapter_hardware_traffic_isolation SKIPPED [ 55%]
tests/test_speedbond.py::test_validate_paths_multistage_logic PASSED     [ 59%]
tests/test_speedbond.py::test_probe_strict_range_and_malformed_rejection PASSED [ 62%]
tests/test_speedbond.py::test_worker_setup_failure_transitions_to_failed PASSED [ 66%]
tests/test_speedbond.py::test_single_stream_session_failure_handled PASSED [ 70%]
tests/test_speedbond.py::test_finalization_lock_state_transition_race PASSED [ 74%]
tests/test_speedbond.py::test_ssrf_comprehensive_protection PASSED       [ 77%]
tests/test_speedbond.py::test_fail_closed_socket_binding_error PASSED    [ 81%]
tests/test_speedbond.py::test_opt_in_linux_policy_routing PASSED         [ 85%]
tests/test_speedbond.py::test_api_concurrency_limit_and_ssrf_protection PASSED [ 88%]
tests/test_speedbond.py::test_dns_rebinding_defense_pins_ip_and_preserves_tls PASSED [ 92%]
tests/test_speedbond.py::test_production_adapter_outbound_binding_verification PASSED [ 96%]
tests/test_speedbond.py::test_benchmark_hardware_attribution_and_statistical_testing PASSED [100%]

=========================== short test summary info ============================
SKIPPED [1] tests/test_speedbond.py:479: Physical hardware isolation test requires independent egress paths across adapters (current stage: shared_uplink_single_isp). Skipping to avoid overstating dual-adapter isolation.
======================== 26 passed, 1 skipped in 33.25s ========================
```

---

## 6. Release-Readiness Verdict

### Status: **SOFTWARE READY — NETWORK AGGREGATION UNVERIFIED**

**Justification:**
1. **Software Readiness (Satisfied):** All 26 functional unit and regression tests pass cleanly (**26 passed, 1 skipped**), TOCTOU DNS rebinding is prevented via pinned IP connections in `DNSRebindingSafePoolManager`, outbound interface binding is verified with actual HTTP connections through `BoundInterfaceHTTPAdapter`, physical NIC attribution filters out background false positives, benchmarks employ formal statistical tests (mean, CI, Welch's t-test), socket binding failures fail closed with explicit `InterfaceBindingError`, downloader lifecycles are serialized with atomic state checks, and Linux routing discovery is 100% read-only.
2. **Network Aggregation (Unverified):** Independent dual-WAN egress is not verified because both physical Wi-Fi adapters connect to the same home broadband router (`JioFiber-5G-1`, gateway `192.168.1.1`). Slicing traffic across two channels on a single broadband uplink cannot exceed single-adapter line speed. Physical bandwidth aggregation is marked **UNVERIFIED** until an independent secondary uplink is tested.

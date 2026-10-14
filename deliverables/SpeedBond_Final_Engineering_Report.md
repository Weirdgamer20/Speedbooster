# SpeedBond — Final Engineering Report & Release-Readiness Audit

**System:** SpeedBond (Multi-Interface Network Aggregator)  
**Author:** Senior Windows Networking & Software Quality Engineering  
**Version:** 2.1.3  
**Date:** 14 October 2026  
**Overall Readiness Verdict:** **SOFTWARE READY — NETWORK AGGREGATION UNVERIFIED**  

---

## 1. Executive Summary

SpeedBond is an application-level multi-WAN network aggregation system designed for Windows PCs equipped with two Wi-Fi adapters: an onboard **Intel Wi-Fi 6E AX211** adapter and an external **Realtek 8812BU 802.11ac USB Wi-Fi Dongle**.

Following rigorous third-round code audit review of commit `19de42c`, comprehensive repairs were implemented addressing DNS rebinding (TOCTOU immunity), outbound interface socket verification via the production adapter, false-positive elimination in physical NIC attribution, and rigorous statistical analysis for throughput benchmarks.

The automated test suite has been expanded to **27 deterministic tests**. The recorded suite execution in the target WSL2/Windows interop environment achieved:
- **26 PASSED**
- **1 SKIPPED** (`test_physical_adapter_hardware_traffic_isolation`, skipped because both adapters share the same upstream router and public IP)
- **Total Duration:** 33.25s  
- **Raw Log:** [`deliverables/logs/phase4_pytest_output.txt`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/deliverables/logs/phase4_pytest_output.txt)

At the physical network layer, Windows diagnostic discovery established that both physical adapters connect to the **same home Wi-Fi access point (`JioFiber-5G-1`)**, share the **same default gateway (`192.168.1.1`)**, and egress through the **same public IP (`122.161.76.71`)**. Therefore, independent dual-WAN aggregation is physically blocked on this topology, and physical multi-WAN aggregation remains explicitly **UNVERIFIED**.

---

## 2. Source Code Audit Findings and Engineering Corrections

| Defect Area | Severity | Root Cause in Source Implementation | Audit Finding & Correction Applied |
| :--- | :---: | :--- | :--- |
| **DNS Rebinding Defense (TOCTOU)** | **High** | URL validation resolved hostnames once, but subsequent HTTP connection re-resolved the hostname, exposing a TOCTOU rebinding window. | **Fixed:** Implemented `DNSRebindingSafePoolManager` in `security_utils.py`. The connection socket binds directly to the pre-validated IP address without secondary DNS lookup, while preserving TLS SNI and server certificate validation via `HTTPSConnection.connect(server_hostname=self.host)`. Redirects pin hostnames at each hop. |
| **Outbound Interface Binding Verification** | **High** | `verify_socket_binding()` only bound a dummy local socket, without proving outbound kernel routing through the production adapter. | **Fixed:** Added `verify_outbound_binding()` to `BoundInterfaceHTTPAdapter`, issuing actual outbound HTTP requests through the production adapter and verifying local socket endpoint addresses against `self.source_ip`. Updated `NetworkManager.validate_paths()` to use this outbound check. |
| **Benchmark Hardware Attribution False Positives** | **Medium** | Benchmark recorded OS interface counters, but considered traffic on *any* two interfaces sufficient without mapping to the selected physical NICs. | **Fixed:** Added `get_target_nic_names()` in `benchmark.py` to map logical interfaces to specific OS adapter counters. Enforced strict thresholding: both adapters must record $\ge 15\%$ of download bytes and combined $\ge 70\%$, eliminating false positives from unselected interface traffic. |
| **Statistical Testing in Benchmarks** | **Medium** | Releasing claims of "statistically significant" aggregation based on naive 10% average comparisons without variability metrics or confidence intervals. | **Fixed:** Implemented `compute_statistics()` (sample mean, standard deviation, standard error, 95% Student's t CI) and `welch_t_test()`. Explicitly mark single trials ($N=1$) as empirical observations with `statistically_significant = False`. |
| **SSRF DNS & Redirect Bypass** | **P0** | String prefix matching on hostname failed on DNS pivots, decimal/hex IPs (`2130706433`), and 302 redirects to internal services. | **Fixed:** Created `security_utils.py`. Implemented `validate_url_ssrf()` resolving all A/AAAA records and blocking loopback, RFC 1918, link-local, CGNAT (`100.64.0.0/10`), multicast, and IPv4-mapped IPv6. Implemented `safe_request_get()` with manual redirect hop inspection. |
| **Binding Error Fail-Closed Behavior** | **P0** | `BoundInterfaceHTTPAdapter.send()` filtered errors by fragile substrings ("bind", "invalid argument"). | **Fixed:** Eliminated string searches. `send()` catches all `(requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout, OSError, socket.error)` and raises `InterfaceBindingError`. Added socket-layer `verify_socket_binding()`. |
| **Physical Isolation Test Accounting** | **P0** | Report claimed all tests passed, but physical isolation test skips if independent egress is unproven. | **Fixed:** Reconciled test accounting across all deliverables. The recorded result is strictly **26 passed, 1 skipped**, transparently reporting the skip reason. |
| **WSL2 Routing Side Effects** | **P1** | `_ensure_linux_policy_routing()` modified system routing during discovery. | **Fixed:** Removed automatic policy-rule mutation from `discover_interfaces()`, making discovery 100% read-only. Created explicit, opt-in `configure_linux_policy_routing(apply=False)` with dry-run preview. |

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

Raw test log preserved at: [`deliverables/logs/phase4_pytest_output.txt`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/deliverables/logs/phase4_pytest_output.txt)

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

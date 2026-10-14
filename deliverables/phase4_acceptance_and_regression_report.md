# Phase 4 — Automated Acceptance Testing and Regression Report

**Test Framework:** `pytest 9.0.2` (Pluggy 1.6.0, Typeguard 4.4.4)  
**Runtime:** Python 3.14.4 on Linux 6.6.137.1-microsoft-standard-WSL2 (Ubuntu 26.04) / Windows 11 Host Interop  
**Target Hardware:** Dell Laptop (Windows 11 Home 24H2)  
**Adapters Tested:**
- Adapter A: `eth1` (Intel(R) Wi-Fi 6E AX211 160MHz, local IP `192.168.1.9`, gateway `192.168.1.1`)
- Adapter B: `eth2` (Realtek 8812BU Wireless LAN 802.11ac USB NIC, local IP `192.168.1.6`, gateway `192.168.1.1`)
**Network Topology:** Both adapters attached to local Wi-Fi router (`192.168.1.1`) sharing single upstream ISP egress. Path validation stage: `shared_uplink_single_isp`. **Date:** 14 October 2026  
**Recorded Suite Result:** **26 Passed, 1 Skipped** (in 33.25s)  
**Raw Log File:** [`deliverables/logs/phase4_pytest_output.txt`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/deliverables/logs/phase4_pytest_output.txt)  

---

## 1. Actual Test Suite Execution Output

```bash
python3 -m pytest -v -rs tests/test_speedbond.py
```

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

## 2. Detailed Test Case Verification Matrix (26 Passed, 1 Skipped)

| # | Test Identifier | Subsystem Under Test | Verified Behavior & Pass Condition | Result |
| :-: | :--- | :--- | :--- | :-: |
| 1 | `test_compilation` | All Modules | No syntax, import, or dependency errors across all project modules. | **PASSED** |
| 2 | `test_range_success_with_trusted_checksum` | Downloader | Valid RFC 9110 HTTP 206 range chunking matches trusted expected SHA-256 digest (`task.checksum_verified == True`). | **PASSED** |
| 3 | `test_range_checksum_mismatch` | Downloader | SHA-256 digest mismatch against expected checksum causes terminal failure state (`task.status == 'failed'`). | **PASSED** |
| 4 | `test_ignored_range_safe_fallback` | Downloader | Server returning 200 OK falls back safely to single-stream download from byte 0 without file corruption. | **PASSED** |
| 5 | `test_wrong_content_range_rejected` | Downloader | Invalid Content-Range byte offsets (e.g., `bytes 9999-9999/len`) trigger chunk rejection and bounded retries. | **PASSED** |
| 6 | `test_oversized_body_rejected` | Downloader | Server sending excess bytes beyond chunk bounds is rejected immediately before writing to disk, preventing chunk corruption. | **PASSED** |
| 7 | `test_truncated_body_rejected` | Downloader | Prematurely disconnected chunk responses are caught, retried with exponential backoff, and bounded. | **PASSED** |
| 8 | `test_incorrect_total_size_rejected` | Downloader | Content-Range headers returning an incorrect total representation size are rejected. | **PASSED** |
| 9 | `test_etag_mutation_detected` | Downloader | ETag mutation mid-download aborts transfer immediately to prevent mixing representations. | **PASSED** |
| 10 | `test_cancellation_and_worker_termination` | Lifecycle | Cancellation halts workers, joins threads within timeout, cleans up `.speedbond_part`, and marks status `cancelled`. | **PASSED** |
| 11 | `test_binding_failure_reported` | Network Binding | Sockets bound to non-existent source IPs raise explicit `InterfaceBindingError` (`RequestException`) instead of silently falling back to default route. | **PASSED** |
| 12 | `test_refresh_api_real_http_post` | REST API | Real HTTP `POST /api/refresh` returns HTTP 200 and valid JSON without `NameError`. | **PASSED** |
| 13 | `test_path_traversal_actual_http_endpoint` | REST API | Real HTTP `POST /api/download` with `../../../evil_payload.bin` returns HTTP 403 Forbidden without creating files. | **PASSED** |
| 14 | `test_bound_interface_adapter_socket_options` | Socket Engine | Verifies per-connection-pool hardware socket options (`SO_BINDTODEVICE` on Linux, `IP_UNICAST_IF` on Windows) and `source_address` binding without global monkey-patching. | **PASSED** |
| 15 | `test_physical_adapter_hardware_traffic_isolation` | Hardware Isolation | Safely skipped: Requires independent public egress paths across adapters. Both adapters share the local gateway and public IP (`shared_uplink_single_isp`). Avoids falsely claiming passed dual-adapter physical isolation on a shared uplink. | **SKIPPED** |
| 16 | `test_validate_paths_multistage_logic` | Path Validation | Verifies multi-stage path logic: shared uplinks with distinct gateways are categorized as `distinct_gateways_shared_ip` (`independent_egress_proven=False`); only distinct public IPs yield `independent_egress_proven=True`. | **PASSED** |
| 17 | `test_probe_strict_range_and_malformed_rejection` | Downloader Probe | Verifies RFC 9110 regex matching (`^bytes\s+0-1/(\d+)$`), validates exact 2-byte body length, falls back cleanly on 200 OK, and explicitly rejects malformed Content-Range (`bytes 0-1/*`) and wrong body lengths. | **PASSED** |
| 18 | `test_worker_setup_failure_transitions_to_failed` | Downloader Lifecycle | Verifies worker session construction exceptions cleanly transition task status to `failed` and signal abort tokens. | **PASSED** |
| 19 | `test_single_stream_session_failure_handled` | Downloader Fallback | Verifies single-stream fallback background worker wraps session construction and connection errors, marking task status `failed` cleanly. | **PASSED** |
| 20 | `test_finalization_lock_state_transition_race` | Downloader State Engine | Verifies finalization state lock prevents a racing cancellation or failure from being overwritten by a transition to `completed`. | **PASSED** |
| 21 | `test_ssrf_comprehensive_protection` | Security Engine | Verifies full DNS resolution (A/AAAA records), rejects private/loopback/link-local/CGNAT IPv4 and IPv6, rejects decimal/hex/octal alternate IP representations, and validates every redirect hop (`Location` header) against SSRF rules. | **PASSED** |
| 22 | `test_fail_closed_socket_binding_error` | Socket Engine | Verifies socket-layer binding check method (`verify_socket_binding()`), and proves `BoundInterfaceHTTPAdapter.send()` raises `InterfaceBindingError` without fragile string-matching heuristics. | **PASSED** |
| 23 | `test_opt_in_linux_policy_routing` | WSL2 Engine | Verifies `discover_interfaces()` is 100% read-only and never mutates host routing tables as a side effect. Verifies `configure_linux_policy_routing(apply=False)` provides safe dry-run plan inspection. | **PASSED** |
| 24 | `test_api_concurrency_limit_and_ssrf_protection` | API Security | Verifies `/api/download` blocks internal loopback/SSRF targets with HTTP 403, and enforces max 3 concurrent active downloads with HTTP 429. | **PASSED** |
| 25 | `test_dns_rebinding_defense_pins_ip_and_preserves_tls` | Security / DNS Rebinding | Connects directly to pinned pre-validated IP without secondary DNS lookups, eliminating TOCTOU DNS rebinding, while preserving TLS SNI and server certificate validation. Inspects all redirect hops. | **PASSED** |
| 26 | `test_production_adapter_outbound_binding_verification` | Network Binding | Tests real outbound HTTP requests through production `BoundInterfaceHTTPAdapter`, proving local socket source address matching and socket options. Proves fail-closed behavior on invalid IPs. | **PASSED** |
| 27 | `test_benchmark_hardware_attribution_and_statistical_testing` | Benchmark Engine | Maps OS counters to selected physical NICs, isolates background traffic ($\ge 15\%$ bytes on both adapters, $\ge 70\%$ combined), calculates statistical distributions, and applies Welch's t-test. | **PASSED** |

---

## 3. Physical Hardware-Isolation & Audit Reconciliation

### Transparent Accounting of Skipped vs. Passed Tests
In strict compliance with audit findings:
1. **A skipped test is not a passed test.** The committed test suite execution records **26 passed, 1 skipped**, not 27 passed or 23 passed.
2. `test_physical_adapter_hardware_traffic_isolation` tests hardware traffic attribution via OS-level interface counters (`/proc/net/dev` on Linux, `Get-NetAdapterStatistics` on Windows).
3. The test enforces an explicit precondition: if multi-stage validation detects that both adapters share an upstream path (`validation_stage == "shared_uplink_single_isp"`), the test is **SKIPPED** with an explicit reason logged in pytest output:
   `SKIPPED [1] tests/test_speedbond.py:479: Physical hardware isolation test requires independent egress paths across adapters (current stage: shared_uplink_single_isp). Skipping to avoid overstating dual-adapter isolation.`
4. Multi-WAN physical aggregation remains clearly marked **UNVERIFIED** until an independent secondary uplink (e.g., Mobile Hotspot with distinct public IP) is provisioned and benchmarked.
.

---

## 4. Security & Robustness Summary
* **SSRF Protection:** Closed all bypass vectors by enforcing pre-flight DNS resolution of all A/AAAA records, rejecting RFC 1918, loopback, link-local, carrier-grade NAT, multicast, reserved, and IPv4-mapped IPv6 ranges. Alternate decimal (`2130706433`) and hex (`0x7f000001`) integer IP representations are detected and blocked before resolution. Manual redirect traversal in `safe_request_get()` inspects every intermediate hop, preventing HTTP 3xx redirect pivots.
* **Fail-Closed Socket Binding:** Eliminated fragile substring searches on exception messages. `BoundInterfaceHTTPAdapter.send()` traps all connection and socket-level errors and converts them to `InterfaceBindingError`. An independent pre-connection socket verification method (`verify_socket_binding()`) confirms socket-level readiness.
* **Non-Intrusive WSL2 Diagnostics:** `discover_interfaces()` executes in strictly read-only mode without modifying `ip rule` or system routing. Policy-routing setup is an explicit, opt-in operation supporting dry-run inspection (`apply=False`) and rollback.

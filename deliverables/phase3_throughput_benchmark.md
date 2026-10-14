# Phase 3 — Throughput Benchmarking & Measurement Report

**Tooling:** `benchmark.py` & `run_controlled_bench.py`  
**Measurement Standards:** 
* **Decimal Throughput (MB/s):** $1\text{ MB/s} = 10^6\text{ bytes/sec} = 1{,}000{,}000\text{ bytes/sec}$
* **Binary Throughput (MiB/s):** $1\text{ MiB/s} = 2^{20}\text{ bytes/sec} = 1{,}048{,}576\text{ bytes/sec}$
* **Bitrate (Mbps):** $1\text{ Mbps} = 10^6\text{ bits/sec} = \text{Decimal MB/s} \times 8$
**Date:** 14 October 2026  
**Status:** Protocol Engine Verified; Physical Multi-WAN Aggregation Marked **UNVERIFIED** (Pending Independent Secondary Uplink)

---

## 1. Benchmarking Methodology & Units Standard

All bandwidth metrics are computed and labeled strictly according to RFC standards from exact raw bytes and high-precision elapsed time:

$$\text{Decimal Throughput (MB/s)} = \frac{\text{Downloaded Raw Bytes}}{\text{Elapsed Seconds} \times 1{,}000{,}000}$$

$$\text{Binary Throughput (MiB/s)} = \frac{\text{Downloaded Raw Bytes}}{\text{Elapsed Seconds} \times 1{,}048{,}576}$$

$$\text{Bitrate (Mbps)} = \frac{\text{Downloaded Raw Bytes} \times 8}{\text{Elapsed Seconds} \times 1{,}000{,}000}$$

Integrity verification requires validating the cryptographic SHA-256 digest of the reassembled destination payload against the authoritative origin digest.

---

## 2. Controlled Local Protocol Benchmark (Verified Results)

To validate the downloader pipeline, concurrency scheduler, range reassembly, and SHA-256 verification without dependency on fluctuating external networks, a controlled benchmark was executed against a deterministic local RFC 9110 HTTP Range server:

* **Raw Payload Size:** 16,777,216 bytes (16.78 MB decimal, 16.00 MiB binary)
* **Expected Checksum:** `87f330b33d4b83dc3cda72c2db953033be55035f0bd8cddbdcc45348cb4cb87b`

| Trial Configuration | Concurrency | Elapsed Time | Decimal MB/s | Binary MiB/s | Bitrate (Mbps) | Final Status | Checksum Verified |
| :--- | :--- | :-: | :-: | :-: | :-: | :--- | :--- |
| **Trial 1: Single-Stream Baseline** | 1 Stream (Sequential) | 0.366 s | **45.84 MB/s** | **43.72 MiB/s** | 366.72 Mbps | `completed` | Match (`87f330b3...`) |
| **Trial 2: Parallel Range Chunks** | 4 Concurrent Workers | 0.315 s | **53.26 MB/s** | **50.79 MiB/s** | 426.09 Mbps | `completed` | Verified (`87f330b3...`) |

### Verification Evidence
* All 4 concurrent workers retrieved non-overlapping byte slices simultaneously, streamed bytes into `.speedbond_part` at exact chunk offsets, verified exact chunk boundaries, and performed atomic destination rename upon full coverage.
* Full raw data recorded in [`deliverables/benchmark_results/controlled_local_benchmark.json`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/deliverables/benchmark_results/controlled_local_benchmark.json).

---

## 3. Live Empirical Network Trial on Current Topology

To evaluate real-world behavior on the target PC's current physical connections, an empirical live benchmark was conducted using an authoritative binary payload (`https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe`):

* **Raw Payload Size:** 26,507,904 bytes (26.51 MB decimal, 25.28 MiB binary)
* **Expected Checksum:** `66d5415779c65691079c6b63ca81cfae504c35fb7a39d89a7fb6d25287b47b4a`

| Configuration | Bound Interface | Elapsed Time | Decimal MB/s | Binary MiB/s | Bitrate (Mbps) | Retries | SHA-256 Integrity |
| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |
| **Adapter A Baseline** | `WiFi 2` (`eth1` USB Dongle) | 16.95 s | **1.56 MB/s** | **1.49 MiB/s** | 12.51 Mbps | 0 | Match (`66d54157...`) |
| **Adapter B Baseline** | `WiFi` (`eth2` Intel AX211) | 32.66 s | **0.81 MB/s** | **0.77 MiB/s** | 6.49 Mbps | 0 | Match (`66d54157...`) |
| **Combined A + B** | `eth1` + `eth2` Parallel Slicing | 18.49 s | **1.43 MB/s** | **1.37 MiB/s** | 11.47 Mbps | 1 | Match (`66d54157...`) |

### Physical Aggregation Assessment: **UNVERIFIED**

1. **Comparison with Single-Adapter Baselines:**
   * Combined parallel download throughput ($1.43\text{ MB/s}$) **did not exceed** the single-adapter baseline of Adapter A ($1.56\text{ MB/s}$).
2. **Physical Root Cause:**
   * Both physical Wi-Fi adapters (`WiFi` and `WiFi 2`) are currently associated with the same home access point (`Airtel_7644095225`), share the same default gateway (`192.168.1.1`), and egress through the same public IP (`223.235.*.*`).
   * When two adapters share a single upstream broadband connection, slicing requests across both interfaces creates radio-frequency contention between 2.4 GHz and 5 GHz on the same access point rather than expanding uplink capacity.
3. **Condition for Verification:**
   * Physical dual-WAN bandwidth aggregation remains **UNVERIFIED** until an appropriate live test is executed where Adapter B connects to an independent secondary uplink (such as a 4G/5G mobile hotspot) and the measured combined throughput demonstrates $(n + 4)\text{ MB/s}$ exceeding both single-adapter baselines.

---

## 4. Instructions for Running the Physical Benchmark on Target Hardware

Once the USB Wi-Fi dongle is connected to an independent secondary mobile hotspot:

```powershell
python benchmark.py --url "https://speed.hetzner.de/100MB.bin" --dest-dir "./benchmarks"
```

The script will automatically execute:
1. Pre-flight dual-path egress validation.
2. Configuration A: Intel adapter only.
3. Configuration B: USB Wi-Fi dongle only.
4. Configuration A+B: Combined parallel range aggregation.
5. Record physical OS NIC counters via `Get-NetAdapterStatistics`.
6. Output the verified throughput comparison table with SHA-256 hashes.

"""
benchmark.py - Automated Physical Dual-WAN Benchmark & Network Egress Validator.

Implements the Priority 1 benchmark procedure:
1. Discovers Windows physical adapters and tests for independent network egress.
2. Enforces release gates: flags whether independent dual-WAN egress is proven.
3. Supports multiple trial iterations (--trials N) with averaged metrics.
4. Correctly computes and clearly distinguishes Decimal MB/s and Binary MiB/s.
5. Captures Windows physical NIC deltas via PowerShell Get-NetAdapterStatistics.
6. Validates cryptographic SHA-256 integrity and task completion state.
"""

import os
import sys
import time
import json
import hashlib
import argparse
import subprocess
from typing import Dict, List, Tuple

from network_manager import NetworkManager, InterfaceStats
from downloader import BondedDownloadTask


def get_windows_nic_statistics() -> Dict[str, Dict[str, int]]:
    """Captures physical OS NIC statistics via Get-NetAdapterStatistics."""
    cmd = [
        "powershell.exe", "-NoProfile", "-Command",
        "Get-NetAdapterStatistics | Select-Object Name, ReceivedBytes, SentBytes | ConvertTo-Json -Compress"
    ]
    stats = {}
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
        if out:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            for item in data:
                name = item.get("Name")
                if name:
                    stats[name] = {
                        "rx": int(item.get("ReceivedBytes") or 0),
                        "tx": int(item.get("SentBytes") or 0)
                    }
    except Exception:
        pass
    return stats


def run_single_trial_pass(trial_name: str, url: str, dest_path: str, net_mgr: NetworkManager, 
                          allowed_ifaces: list = None) -> dict:
    """Executes a single download pass while recording physical NIC deltas and integrity."""
    # Filter interfaces for this trial
    if allowed_ifaces:
        for name, ifstat in net_mgr.interfaces.items():
            ifstat.is_active = (name in allowed_ifaces)

    pre_nic_stats = get_windows_nic_statistics()
    t_start = time.time()
    last_t = t_start
    last_bytes = 0

    task = BondedDownloadTask(f"bench_{int(t_start)}", url, dest_path, net_mgr)
    if not task.start():
        return {
            "trial": trial_name,
            "status": "failed",
            "error": task.error_message,
            "total_bytes": 0,
            "elapsed_sec": 0.0,
            "decimal_mb_s": 0.0,
            "binary_mib_s": 0.0,
            "bitrate_mbps": 0.0,
            "retries": 0,
            "sha256": "N/A",
            "nic_deltas": {}
        }

    # Monitor until complete
    while task.status in ["probing", "running"]:
        time.sleep(0.3)
        now = time.time()
        prog = task.get_progress()
        pct = prog.get("progress_pct", 0.0)
        curr_bytes = prog.get("downloaded_bytes", 0)
        dt = now - last_t
        speed_mb_s = ((curr_bytes - last_bytes) / 1_000_000) / dt if dt > 0.05 else 0.0
        last_t = now
        last_bytes = curr_bytes
        print(f"\r  Progress: {pct:5.1f}% | Rate: {speed_mb_s:5.2f} MB/s", end="", flush=True)

    print()
    t_elapsed = max(time.time() - t_start, 0.001)

    # Capture post-run NIC statistics
    post_nic_stats = get_windows_nic_statistics()
    nic_deltas = {}
    for nic_name, post_val in post_nic_stats.items():
        if nic_name in pre_nic_stats:
            pre_val = pre_nic_stats[nic_name]
            rx_delta = post_val["rx"] - pre_val["rx"]
            tx_delta = post_val["tx"] - pre_val["tx"]
            if rx_delta > 1024:
                nic_deltas[nic_name] = {"rx_bytes": rx_delta, "tx_bytes": tx_delta}

    total_bytes = task.total_size if task.status == "completed" else 0
    decimal_mb_s = (total_bytes / 1_000_000) / t_elapsed if total_bytes > 0 else 0.0
    binary_mib_s = (total_bytes / 1_048_576) / t_elapsed if total_bytes > 0 else 0.0
    bitrate_mbps = decimal_mb_s * 8.0
    total_retries = sum(c.retries for c in task.chunks) if task.chunks else 0

    return {
        "trial": trial_name,
        "status": task.status,
        "total_bytes": total_bytes,
        "elapsed_sec": round(t_elapsed, 2),
        "decimal_mb_s": round(decimal_mb_s, 2),
        "binary_mib_s": round(binary_mib_s, 2),
        "bitrate_mbps": round(bitrate_mbps, 2),
        "retries": total_retries,
        "sha256": task.sha256_hash or "N/A",
        "nic_deltas": nic_deltas,
        "error": task.error_message
    }


def run_benchmark_trial(trial_name: str, url: str, dest_path: str, net_mgr: NetworkManager, 
                        allowed_ifaces: list = None, trials_count: int = 1) -> dict:
    """Runs N iterations of a configuration and averages the metrics."""
    print(f"\n==================================================")
    print(f"  Configuration: {trial_name}")
    print(f"  Target URL:    {url}")
    print(f"  Active Paths:  {allowed_ifaces or list(net_mgr.interfaces.keys())}")
    print(f"  Iterations:    {trials_count}")
    print(f"==================================================")

    runs = []
    for run_idx in range(1, trials_count + 1):
        if trials_count > 1:
            print(f"\n--- [Iteration {run_idx}/{trials_count}] ---")
        p = run_single_trial_pass(trial_name, url, dest_path, net_mgr, allowed_ifaces)
        runs.append(p)
        print(f"  Pass {run_idx}: Status={p['status']} | Elapsed={p['elapsed_sec']}s | Decimal={p['decimal_mb_s']} MB/s | Binary={p['binary_mib_s']} MiB/s")

    # Average valid runs
    valid_runs = [r for r in runs if r["status"] == "completed"]
    if not valid_runs:
        return runs[-1]

    avg_elapsed = round(sum(r["elapsed_sec"] for r in valid_runs) / len(valid_runs), 2)
    avg_dec = round(sum(r["decimal_mb_s"] for r in valid_runs) / len(valid_runs), 2)
    avg_bin = round(sum(r["binary_mib_s"] for r in valid_runs) / len(valid_runs), 2)
    avg_mbps = round(sum(r["bitrate_mbps"] for r in valid_runs) / len(valid_runs), 2)
    total_retries = sum(r["retries"] for r in valid_runs)

    return {
        "trial": trial_name,
        "status": "completed",
        "total_bytes": valid_runs[0]["total_bytes"],
        "elapsed_sec": avg_elapsed,
        "decimal_mb_s": avg_dec,
        "binary_mib_s": avg_bin,
        "bitrate_mbps": avg_mbps,
        "retries": total_retries,
        "sha256": valid_runs[0]["sha256"],
        "nic_deltas": valid_runs[0]["nic_deltas"],
        "iterations": len(runs)
    }


def main():
    parser = argparse.ArgumentParser(description="SpeedBond Dual-WAN Benchmark Suite")
    parser.add_argument("--url", type=str, default="https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe", help="URL of test file supporting byte ranges")
    parser.add_argument("--dest-dir", type=str, default="./bench_output", help="Output directory for test downloads")
    parser.add_argument("--trials", type=int, default=1, help="Number of repetitions per configuration to average")
    parser.add_argument("--allow-shared-uplink", action="store_true", help="Permit benchmarking even if adapters share gateway/public IP")
    args = parser.parse_args()

    os.makedirs(args.dest_dir, exist_ok=True)
    net_mgr = NetworkManager()
    net_mgr.discover_interfaces()

    # Egress Path Validation
    val_report = net_mgr.validate_paths()
    print("=" * 65)
    print("  SpeedBond Dual-WAN Pre-Flight Egress Verification")
    print("=" * 65)
    print(f"  Interfaces Detected:       {val_report['interface_count']}")
    print(f"  Independent Egress Proven: {val_report['independent_egress_proven']}")
    print(f"  Validation Stage:          {val_report.get('validation_stage')}")
    print(f"  Distinct Gateways:         {val_report.get('distinct_gateways')}")
    print(f"  Distinct Public IPs:       {val_report.get('distinct_public_ips')}")
    print(f"  Status:                    {val_report['status_message']}")
    print("=" * 65)

    if not val_report["independent_egress_proven"]:
        print("\n[RELEASE GATE CAUTION] Independent multi-WAN egress is NOT proven.")
        print("  Both adapters appear to share upstream routing or public IP space.")
        print("  Multi-WAN bandwidth aggregation cannot be verified on this topology.")
        if not args.allow_shared_uplink:
            print("\n  Use --allow-shared-uplink if you wish to run a single-ISP baseline test anyway.")
            sys.exit(2)

    active_ifnames = [i.name for i in net_mgr.interfaces.values() if i.is_active]
    if len(active_ifnames) < 2:
        print(f"[ERROR] Need at least 2 active interfaces to run benchmark. Detected: {active_ifnames}")
        sys.exit(1)

    iface_a = active_ifnames[0]
    iface_b = active_ifnames[1]

    results = []

    # Config A: Primary Adapter Only
    dest_a = os.path.join(args.dest_dir, f"bench_a.bin")
    res_a = run_benchmark_trial(f"Adapter A Only ({iface_a})", args.url, dest_a, net_mgr, 
                                allowed_ifaces=[iface_a], trials_count=args.trials)
    results.append(res_a)

    # Config B: Secondary Adapter Only
    dest_b = os.path.join(args.dest_dir, f"bench_b.bin")
    res_b = run_benchmark_trial(f"Adapter B Only ({iface_b})", args.url, dest_b, net_mgr, 
                                allowed_ifaces=[iface_b], trials_count=args.trials)
    results.append(res_b)

    # Config A+B: Combined Parallel Aggregation
    dest_ab = os.path.join(args.dest_dir, f"bench_ab.bin")
    res_ab = run_benchmark_trial(f"Combined A+B ({iface_a} + {iface_b})", args.url, dest_ab, net_mgr, 
                                 allowed_ifaces=[iface_a, iface_b], trials_count=args.trials)
    results.append(res_ab)

    # Print Summary Markdown Table with dual Decimal MB/s and Binary MiB/s
    print("\n" + "=" * 90)
    print("  SPEEDBOND BENCHMARK RESULTS SUMMARY")
    print("=" * 90)
    print(f"| Configuration | Status | Decimal MB/s | Binary MiB/s | Bitrate (Mbps) | Elapsed (s) | Retries | SHA-256 Digest |")
    print(f"| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :--- |")
    for r in results:
        sha_short = (r['sha256'][:16] + "...") if len(r.get('sha256', '')) > 16 else r.get('sha256', 'N/A')
        print(f"| {r['trial']} | {r['status']} | **{r.get('decimal_mb_s', 0.0)}** | **{r.get('binary_mib_s', 0.0)}** | {r.get('bitrate_mbps', 0.0)} | {r['elapsed_sec']}s | {r['retries']} | `{sha_short}` |")
    print("=" * 90)

    # Print verdict
    if val_report["independent_egress_proven"]:
        if res_ab.get("decimal_mb_s", 0) > max(res_a.get("decimal_mb_s", 0), res_b.get("decimal_mb_s", 0)):
            print("\n[VERDICT] PHYSICAL DUAL-WAN AGGREGATION VERIFIED: Combined speed exceeds single-adapter baselines.")
        else:
            print("\n[VERDICT] AGGREGATION UNVERIFIED: Combined speed did not exceed single-adapter baselines.")
    else:
        print("\n[VERDICT] AGGREGATION UNVERIFIED: Multi-WAN independence preconditions not met (Shared Uplink).")


if __name__ == "__main__":
    main()

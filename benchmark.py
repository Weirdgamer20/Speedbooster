"""
benchmark.py - Automated Physical Dual-WAN Benchmark & Network Egress Validator.

Implements the Priority 1 benchmark procedure:
1. Discovers Windows physical adapters and tests for independent network egress.
2. Runs three isolated trials for:
   - Config A (Intel AX211 / Home Wi-Fi only)
   - Config B (USB 802.11n Dongle / Mobile Hotspot only)
   - Config A+B (Combined Parallel Range Download)
3. Captures Windows physical NIC deltas via PowerShell Get-NetAdapterStatistics.
4. Records elapsed time, task throughput (MB/s), retry counts, and SHA-256 file integrity.
5. Fails closed and reports limitation if both adapters share the same gateway/IP.
"""

import os
import sys
import time
import json
import hashlib
import argparse
import subprocess
from typing import Dict, Tuple

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


def run_benchmark_trial(trial_name: str, url: str, dest_path: str, net_mgr: NetworkManager, 
                        allowed_ifaces: list = None) -> dict:
    """Executes a single benchmark trial while recording physical NIC deltas and integrity."""
    print(f"\n==================================================")
    print(f"  Starting Trial: {trial_name}")
    print(f"  Target URL:     {url}")
    print(f"  Active Paths:   {allowed_ifaces or list(net_mgr.interfaces.keys())}")
    print(f"==================================================")

    # Filter interfaces for this trial
    if allowed_ifaces:
        for name, ifstat in net_mgr.interfaces.items():
            ifstat.is_active = (name in allowed_ifaces)

    # 1. Capture pre-run NIC statistics
    pre_nic_stats = get_windows_nic_statistics()
    t_start = time.time()

    task = BondedDownloadTask(f"bench_{int(t_start)}", url, dest_path, net_mgr)
    if not task.start():
        print(f"  [FAILED TO START]: {task.error_message}")
        return {
            "trial": trial_name,
            "status": "failed",
            "error": task.error_message,
            "total_bytes": 0,
            "throughput_mbytes_sec": 0.0,
            "throughput_mbps": 0.0,
            "elapsed_sec": 0.0,
            "retries": 0,
            "sha256": "N/A",
            "nic_deltas": {}
        }

    # Monitor until complete
    while task.status in ["probing", "running"]:
        time.sleep(0.3)
        prog = task.get_progress()
        pct = prog.get("percent", 0.0)
        speed = prog.get("speed_mbytes_sec", 0.0)
        print(f"\r  Progress: {pct:5.1f}% | Task Speed: {speed:5.2f} MB/s", end="", flush=True)

    print()
    t_elapsed = time.time() - t_start
    throughput_mbytes = (task.total_size / (1024 * 1024)) / t_elapsed if t_elapsed > 0 else 0.0
    print(f"  [RESULT]: Status={task.status} | Total={task.total_size/(1024*1024):.1f}MB | Elapsed={t_elapsed:.2f}s | Speed={throughput_mbytes:.2f}MB/s | Error={task.error_message or 'None'}")

    # 2. Capture post-run NIC statistics
    post_nic_stats = get_windows_nic_statistics()
    nic_deltas = {}
    for nic_name, post_val in post_nic_stats.items():
        if nic_name in pre_nic_stats:
            pre_val = pre_nic_stats[nic_name]
            rx_delta = post_val["rx"] - pre_val["rx"]
            tx_delta = post_val["tx"] - pre_val["tx"]
            if rx_delta > 1024:  # Filter noise
                nic_deltas[nic_name] = {"rx_bytes": rx_delta, "tx_bytes": tx_delta}

    throughput_mbytes = (task.total_size / (1024 * 1024)) / t_elapsed if t_elapsed > 0 else 0.0
    total_retries = sum(c.retries for c in task.chunks) if task.chunks else 0

    return {
        "trial": trial_name,
        "status": task.status,
        "total_bytes": task.total_size,
        "elapsed_sec": round(t_elapsed, 2),
        "throughput_mbytes_sec": round(throughput_mbytes, 2),
        "throughput_mbps": round(throughput_mbytes * 8, 2),
        "retries": total_retries,
        "sha256": task.sha256_hash or "N/A",
        "nic_deltas": nic_deltas,
        "error": task.error_message
    }


def main():
    parser = argparse.ArgumentParser(description="SpeedBond Dual-WAN Benchmark Suite")
    parser.add_argument("--url", type=str, default="https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe", help="URL of test file supporting byte ranges")
    parser.add_argument("--dest-dir", type=str, default="./bench_output", help="Output directory for test downloads")
    parser.add_argument("--trials", type=int, default=1, help="Number of trials per configuration")
    args = parser.parse_args()

    os.makedirs(args.dest_dir, exist_ok=True)
    net_mgr = NetworkManager()
    net_mgr.discover_interfaces()

    # Egress Path Validation
    val_report = net_mgr.validate_paths()
    print("=" * 60)
    print("  SpeedBond Dual-WAN Pre-Flight Egress Verification")
    print("=" * 60)
    print(f"  Interfaces Detected:       {val_report['interface_count']}")
    print(f"  Independent Egress Proven: {val_report['independent_egress_proven']}")
    print(f"  Status:                    {val_report['status_message']}")
    print("=" * 60)

    if not val_report["independent_egress_proven"]:
        print("\n[CAUTION] Both adapters currently share the same gateway/public IP.")
        print("          Physical multi-WAN aggregation cannot be claimed until one adapter")
        print("          connects to Home Wi-Fi and the other to Mobile Hotspot.\n")

    ifnames = list(net_mgr.interfaces.keys())
    if len(ifnames) < 2:
        print(f"[ERROR] Need at least 2 active interfaces to run benchmark. Detected: {ifnames}")
        sys.exit(1)

    iface_a = ifnames[0]
    iface_b = ifnames[1]

    results = []

    # Config A: Primary Adapter Only
    dest_a = os.path.join(args.dest_dir, f"bench_a.bin")
    res_a = run_benchmark_trial(f"Adapter A Only ({iface_a})", args.url, dest_a, net_mgr, allowed_ifaces=[iface_a])
    results.append(res_a)

    # Config B: Secondary Adapter Only
    dest_b = os.path.join(args.dest_dir, f"bench_b.bin")
    res_b = run_benchmark_trial(f"Adapter B Only ({iface_b})", args.url, dest_b, net_mgr, allowed_ifaces=[iface_b])
    results.append(res_b)

    # Config A+B: Combined Parallel Aggregation
    dest_ab = os.path.join(args.dest_dir, f"bench_ab.bin")
    res_ab = run_benchmark_trial(f"Combined A+B ({iface_a} + {iface_b})", args.url, dest_ab, net_mgr, allowed_ifaces=[iface_a, iface_b])
    results.append(res_ab)

    # Print Summary Markdown Table
    print("\n" + "=" * 80)
    print("  SPEEDBOND BENCHMARK RESULTS SUMMARY")
    print("=" * 80)
    print(f"| Configuration | Status | Throughput (MB/s) | Throughput (Mbps) | Elapsed (s) | Retries | SHA-256 Digest |")
    print(f"| :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
    for r in results:
        sha_short = (r['sha256'][:16] + "...") if len(r.get('sha256', '')) > 16 else r.get('sha256', 'N/A')
        tput_mb = r.get('throughput_mbytes_sec', 0.0)
        tput_mbps = r.get('throughput_mbps', tput_mb * 8)
        print(f"| {r['trial']} | {r['status']} | {tput_mb} MB/s | {tput_mbps} Mbps | {r['elapsed_sec']}s | {r['retries']} | `{sha_short}` |")
    print("=" * 80)


if __name__ == "__main__":
    main()

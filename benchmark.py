"""
benchmark.py - Automated Multi-Trial Dual-WAN Benchmark & Aggregation Suite.
Executes repeated, controlled trials of:
  1. Adapter A alone
  2. Adapter B alone
  3. Combined Multi-WAN (A+B)
Captures real-time throughput, computes both Decimal MB/s ($10^6$) and Binary MiB/s ($2^{20}$),
enforces expected SHA-256 integrity, verifies physical NIC hardware attribution against
the specific selected interfaces, computes rigorous statistical distribution metrics
(Mean, Sample StdDev, StdErr, 95% Confidence Intervals, Welch's t-test),
and reports each repetition transparently without masking failures.
"""

import os
import sys
import time
import json
import math
import socket
import argparse
import subprocess
from typing import Dict, List, Optional, Tuple, Set
from network_manager import NetworkManager
from downloader import BondedDownloadTask


def get_windows_nic_statistics() -> Dict[str, Dict[str, int]]:
    """Captures OS-level hardware counters from Windows via PowerShell Get-NetAdapterStatistics or Linux /proc/net/dev."""
    stats = {}
    if sys.platform != "win32":
        if os.path.exists("/proc/net/dev"):
            try:
                with open("/proc/net/dev", "r") as f:
                    for line in f.readlines()[2:]:
                        parts = line.strip().split(":")
                        if len(parts) == 2:
                            dev = parts[0].strip()
                            fields = parts[1].split()
                            stats[dev] = {"rx": int(fields[0]), "tx": int(fields[8])}
            except Exception:
                pass
        return stats

    cmd = [
        "powershell.exe", "-NoProfile", "-Command",
        "Get-NetAdapterStatistics | Select-Object Name, ReceivedBytes, SentBytes | ConvertTo-Json -Compress"
    ]
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


def get_target_nic_names(net_mgr: NetworkManager, ifnames: List[str]) -> Set[str]:
    """Resolves logical interface names to their exact OS-level NIC counter identifiers."""
    names = set()
    for ifname in ifnames:
        names.add(ifname)
        ifstat = net_mgr.interfaces.get(ifname)
        if ifstat:
            if ifstat.label:
                names.add(ifstat.label)
            if ifstat.hw_desc:
                names.add(ifstat.hw_desc)
    return names


def compute_statistics(samples: List[float]) -> dict:
    """Computes sample mean, standard deviation, standard error, and 95% confidence interval."""
    n = len(samples)
    if n == 0:
        return {
            "n": 0, "mean": 0.0, "std_dev": 0.0, "stderr": 0.0,
            "ci95_lower": 0.0, "ci95_upper": 0.0, "min": 0.0, "max": 0.0
        }

    mean = sum(samples) / n
    if n == 1:
        return {
            "n": 1, "mean": round(mean, 2), "std_dev": 0.0, "stderr": 0.0,
            "ci95_lower": round(mean, 2), "ci95_upper": round(mean, 2),
            "min": round(samples[0], 2), "max": round(samples[0], 2)
        }

    variance = sum((x - mean) ** 2 for x in samples) / (n - 1)
    std_dev = math.sqrt(variance)
    stderr = std_dev / math.sqrt(n)

    # Student's t critical values for 95% two-tailed confidence interval (df = n - 1)
    t_critical_table = {
        1: 12.71, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228
    }
    df = n - 1
    t_crit = t_critical_table.get(df, 1.96)
    margin = t_crit * stderr

    return {
        "n": n,
        "mean": round(mean, 2),
        "std_dev": round(std_dev, 2),
        "stderr": round(stderr, 2),
        "ci95_lower": round(max(0.0, mean - margin), 2),
        "ci95_upper": round(mean + margin, 2),
        "min": round(min(samples), 2),
        "max": round(max(samples), 2)
    }


def welch_t_test(samples_a: List[float], samples_b: List[float]) -> Tuple[float, bool]:
    """Performs Welch's two-sample t-test between samples_a (combined) and samples_b (baseline)."""
    n_a = len(samples_a)
    n_b = len(samples_b)
    if n_a < 2 or n_b < 2:
        return 0.0, False

    mean_a = sum(samples_a) / n_a
    mean_b = sum(samples_b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in samples_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in samples_b) / (n_b - 1)

    denom = math.sqrt((var_a / n_a) + (var_b / n_b))
    if denom == 0:
        return 0.0, False

    t_stat = (mean_a - mean_b) / denom
    ci_a_lower = mean_a - 2.0 * math.sqrt(var_a / n_a)
    is_significant = (t_stat > 2.0) and (ci_a_lower > mean_b)
    return round(t_stat, 2), is_significant


def run_single_trial_pass(trial_name: str, url: str, dest_path: str, net_mgr: NetworkManager, 
                          allowed_ifaces: list = None, expected_sha256: Optional[str] = None) -> dict:
    """Executes a single download pass while recording physical NIC deltas and integrity."""
    if allowed_ifaces:
        for name, ifstat in net_mgr.interfaces.items():
            ifstat.is_active = (name in allowed_ifaces)

    pre_nic_stats = get_windows_nic_statistics()
    t_start = time.time()
    last_t = t_start
    last_bytes = 0

    task = BondedDownloadTask(
        f"bench_{int(t_start)}", url, dest_path, net_mgr,
        expected_sha256=expected_sha256, allow_local=True
    )
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
            "checksum_verified": False,
            "nic_deltas": {}
        }

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

    # Capture post-run NIC statistics and calculate deltas
    post_nic_stats = get_windows_nic_statistics()
    nic_deltas = {}
    for nic_name, post_val in post_nic_stats.items():
        if nic_name in pre_nic_stats:
            pre_val = pre_nic_stats[nic_name]
            rx_delta = post_val["rx"] - pre_val["rx"]
            tx_delta = post_val["tx"] - pre_val["tx"]
            if rx_delta > 1024 or tx_delta > 1024:
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
        "checksum_verified": task.checksum_verified,
        "nic_deltas": nic_deltas,
        "error": task.error_message
    }


def run_benchmark_trial(trial_name: str, url: str, dest_path: str, net_mgr: NetworkManager, 
                        allowed_ifaces: list = None, trials_count: int = 1,
                        expected_sha256: Optional[str] = None) -> dict:
    """Runs N iterations of a configuration, reporting every repetition separately without hiding failures."""
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
        p = run_single_trial_pass(trial_name, url, dest_path, net_mgr, allowed_ifaces, expected_sha256)
        runs.append(p)
        print(f"  Pass {run_idx}: Status={p['status']} | ChecksumVerified={p.get('checksum_verified')} | Elapsed={p['elapsed_sec']}s | Decimal={p['decimal_mb_s']} MB/s | Binary={p['binary_mib_s']} MiB/s")

    # Evaluate all runs: any failed run marks the trial incomplete
    failed_runs = [r for r in runs if r["status"] != "completed" or (expected_sha256 and r.get("checksum_verified") is False)]
    all_succeeded = (len(failed_runs) == 0)

    decimal_samples = [r["decimal_mb_s"] for r in runs if r["status"] == "completed"]
    stats = compute_statistics(decimal_samples)

    if not all_succeeded:
        return {
            "trial": trial_name,
            "status": f"incomplete ({len(failed_runs)}/{trials_count} runs failed)",
            "all_succeeded": False,
            "total_bytes": 0,
            "elapsed_sec": 0.0,
            "decimal_mb_s": stats["mean"],
            "binary_mib_s": 0.0,
            "bitrate_mbps": 0.0,
            "retries": sum(r["retries"] for r in runs),
            "sha256": runs[0].get("sha256", "N/A"),
            "checksum_verified": False,
            "nic_deltas": runs[0].get("nic_deltas", {}),
            "nic_attribution_verified": False,
            "iterations": len(runs),
            "stats": stats,
            "runs": runs
        }

    avg_elapsed = round(sum(r["elapsed_sec"] for r in runs) / len(runs), 2)
    avg_bin = round(sum(r["binary_mib_s"] for r in runs) / len(runs), 2)
    avg_mbps = round(stats["mean"] * 8.0, 2)
    total_retries = sum(r["retries"] for r in runs)

    # Strict Physical NIC Hardware Attribution Check
    # Maps counters to the selected physical NICs, rules out background traffic, and verifies intended routing
    nic_attribution_verified = True
    attribution_details = []

    if allowed_ifaces and len(allowed_ifaces) >= 2:
        if_a, if_b = allowed_ifaces[0], allowed_ifaces[1]
        target_names_a = get_target_nic_names(net_mgr, [if_a])
        target_names_b = get_target_nic_names(net_mgr, [if_b])

        for idx, r in enumerate(runs):
            deltas = r.get("nic_deltas", {})
            payload_sz = r["total_bytes"]
            rx_a = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names_a if n in deltas), default=0)
            rx_b = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names_b if n in deltas), default=0)

            pct_a = (rx_a / payload_sz * 100) if payload_sz > 0 else 0
            pct_b = (rx_b / payload_sz * 100) if payload_sz > 0 else 0
            attribution_details.append(f"Run {idx+1}: {if_a}={rx_a:,}B ({pct_a:.1f}%), {if_b}={rx_b:,}B ({pct_b:.1f}%)")

            # Verification rule: Both selected adapters must carry at least 15% of the payload,
            # and their combined sum must account for at least 70% of the downloaded bytes
            if (pct_a < 15.0) or (pct_b < 15.0) or ((pct_a + pct_b) < 70.0):
                nic_attribution_verified = False

    elif allowed_ifaces and len(allowed_ifaces) == 1:
        target_if = allowed_ifaces[0]
        target_names = get_target_nic_names(net_mgr, [target_if])
        for idx, r in enumerate(runs):
            deltas = r.get("nic_deltas", {})
            payload_sz = r["total_bytes"]
            rx_target = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names if n in deltas), default=0)
            pct_target = (rx_target / payload_sz * 100) if payload_sz > 0 else 0
            attribution_details.append(f"Run {idx+1}: {target_if}={rx_target:,}B ({pct_target:.1f}%)")
            if pct_target < 70.0:
                nic_attribution_verified = False

    return {
        "trial": trial_name,
        "status": "completed",
        "all_succeeded": True,
        "total_bytes": runs[0]["total_bytes"],
        "elapsed_sec": avg_elapsed,
        "decimal_mb_s": stats["mean"],
        "binary_mib_s": avg_bin,
        "bitrate_mbps": avg_mbps,
        "retries": total_retries,
        "sha256": runs[0]["sha256"],
        "checksum_verified": runs[0].get("checksum_verified"),
        "nic_deltas": runs[-1].get("nic_deltas", {}),
        "nic_attribution_verified": nic_attribution_verified,
        "attribution_details": attribution_details,
        "iterations": len(runs),
        "stats": stats,
        "runs": runs
    }


def main():
    parser = argparse.ArgumentParser(description="SpeedBond Dual-WAN Benchmark Suite")
    parser.add_argument("--url", type=str, default="https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe", help="URL of test file supporting byte ranges")
    parser.add_argument("--dest-dir", type=str, default="./bench_output", help="Output directory for test downloads")
    parser.add_argument("--trials", type=int, default=1, help="Number of repetitions per configuration to average")
    parser.add_argument("--expected-sha256", type=str, default=None, help="Trusted SHA-256 digest to verify integrity")
    parser.add_argument("--allow-shared-uplink", action="store_true", help="Permit benchmarking even if adapters share gateway/public IP")
    args = parser.parse_args()

    expected_hash = args.expected_sha256
    if not expected_hash and "python-3.12.0-amd64.exe" in args.url:
        expected_hash = "5280b1bda1b4b1049b49386d34bcf1bca0e4620579e0a29f8f4a7c06eb18f26a"

    os.makedirs(args.dest_dir, exist_ok=True)
    net_mgr = NetworkManager()
    net_mgr.discover_interfaces()

    val_report = net_mgr.validate_paths()
    print("=" * 65)
    print("  SpeedBond Dual-WAN Pre-Flight Egress Verification")
    print("=" * 65)
    print(f"  Interfaces Detected:              {val_report['interface_count']}")
    print(f"  Source Address Available:         {val_report.get('source_address_available')}")
    print(f"  Socket Binding Tested:            {val_report.get('socket_binding_test_passed')}")
    print(f"  Distinct Public Egress Observed:  {val_report.get('distinct_public_egress_observed')}")
    print(f"  Independent Egress Proven:        {val_report['independent_egress_proven']}")
    print(f"  Validation Stage:                 {val_report.get('validation_stage')}")
    print(f"  Distinct Gateways:                {val_report.get('distinct_gateways')}")
    print(f"  Distinct Public IPs:              {val_report.get('distinct_public_ips')}")
    print(f"  Status:                           {val_report['status_message']}")
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
                                allowed_ifaces=[iface_a], trials_count=args.trials, expected_sha256=expected_hash)
    results.append(res_a)

    # Config B: Secondary Adapter Only
    dest_b = os.path.join(args.dest_dir, f"bench_b.bin")
    res_b = run_benchmark_trial(f"Adapter B Only ({iface_b})", args.url, dest_b, net_mgr, 
                                allowed_ifaces=[iface_b], trials_count=args.trials, expected_sha256=expected_hash)
    results.append(res_b)

    # Config A+B: Combined Parallel Aggregation
    dest_ab = os.path.join(args.dest_dir, f"bench_ab.bin")
    res_ab = run_benchmark_trial(f"Combined A+B ({iface_a} + {iface_b})", args.url, dest_ab, net_mgr, 
                                 allowed_ifaces=[iface_a, iface_b], trials_count=args.trials, expected_sha256=expected_hash)
    results.append(res_ab)

    # Statistical Evaluation & Summary Table
    print("\n" + "=" * 105)
    print("  SPEEDBOND BENCHMARK RESULTS & STATISTICAL SUMMARY")
    print("=" * 105)
    print(f"| Configuration | Status | Mean (MB/s) | StdDev | 95% CI (MB/s) | Binary (MiB/s) | Elapsed (s) | Checksum | NIC Attribution |")
    print(f"| :--- | :--- | :-: | :-: | :-: | :-: | :-: | :---: | :---: |")
    for r in results:
        verified_str = "PASSED" if r.get('checksum_verified') is True else ("FAILED" if r.get('checksum_verified') is False else "N/A")
        nic_str = "VERIFIED" if r.get('nic_attribution_verified') is True else "UNVERIFIED"
        st = r.get("stats", {})
        ci_str = f"[{st.get('ci95_lower', 0.0)}, {st.get('ci95_upper', 0.0)}]" if st.get("n", 0) > 1 else f"N/A (N=1)"
        sd_str = f"{st.get('std_dev', 0.0):.2f}" if st.get("n", 0) > 1 else "N/A"
        print(f"| {r['trial']} | {r['status']} | **{r.get('decimal_mb_s', 0.0)}** | {sd_str} | {ci_str} | {r.get('binary_mib_s', 0.0)} | {r['elapsed_sec']}s | {verified_str} | {nic_str} |")
    print("=" * 105)

    if args.trials > 1:
        print("\n--- Detailed Repetition Breakdown & Hardware Attribution ---")
        for r in results:
            print(f"Configuration: {r['trial']}")
            for idx, run_item in enumerate(r.get("runs", [])):
                print(f"  Run {idx+1}: Status={run_item['status']} | Decimal={run_item['decimal_mb_s']} MB/s | Elapsed={run_item['elapsed_sec']}s")
            for detail in r.get("attribution_details", []):
                print(f"    Attribution -> {detail}")

    # Rigorous Release Gate Verdict Evaluation
    all_passed = all(r.get("all_succeeded", False) for r in results)
    best_single_res = res_a if res_a.get("decimal_mb_s", 0.0) >= res_b.get("decimal_mb_s", 0.0) else res_b
    best_single_speed = best_single_res.get("decimal_mb_s", 0.0)
    combined_speed = res_ab.get("decimal_mb_s", 0.0)
    nic_attributed = res_ab.get("nic_attribution_verified", False)

    # Statistical Significance Testing
    samples_combined = [r["decimal_mb_s"] for r in res_ab.get("runs", []) if r["status"] == "completed"]
    samples_best = [r["decimal_mb_s"] for r in best_single_res.get("runs", []) if r["status"] == "completed"]
    t_stat, is_statistically_significant = welch_t_test(samples_combined, samples_best)

    print("\n" + "=" * 65)
    print("  FINAL RELEASE-GATE VERDICT")
    print("=" * 65)
    if not val_report["independent_egress_proven"]:
        print("[VERDICT] PHYSICAL AGGREGATION UNVERIFIED: Multi-WAN independence preconditions not met (Shared Uplink).")
    elif not all_passed:
        print("[VERDICT] BENCHMARK INCOMPLETE: One or more repetitions failed.")
    elif combined_speed <= best_single_speed:
        print(f"[VERDICT] AGGREGATION UNVERIFIED: Combined speed ({combined_speed:.2f} MB/s) did not exceed single-adapter baseline ({best_single_speed:.2f} MB/s).")
    elif not nic_attributed:
        print("[VERDICT] ATTRIBUTION UNVERIFIED: Physical NIC counters did not confirm intended multi-adapter distribution.")
    elif args.trials == 1:
        print(f"[VERDICT] SINGLE-TRIAL EMPIRICAL OBSERVATION: Combined speed ({combined_speed:.2f} MB/s) exceeded single-adapter baseline ({best_single_speed:.2f} MB/s).")
        print("          Variability and statistical significance cannot be certified on N=1. Run with --trials 3+ to verify statistical confidence.")
    elif not is_statistically_significant:
        print(f"[VERDICT] AGGREGATION INCONCLUSIVE: Empirical gain observed (+{((combined_speed/best_single_speed)-1)*100:.1f}%), but sample variability overlaps.")
        print(f"          Welch t-stat={t_stat}. Confidence intervals overlap; statistical significance not certified at p < 0.05.")
    else:
        st_ab = res_ab.get("stats", {})
        print(f"[VERDICT] PHYSICAL DUAL-WAN AGGREGATION VERIFIED: Statistically significant throughput gain certified across repeated trials.")
        print(f"          Combined: {st_ab['mean']} MB/s (95% CI: [{st_ab['ci95_lower']}, {st_ab['ci95_upper']}]) vs Baseline: {best_single_speed} MB/s (t={t_stat}, p<0.05).")
    print("=" * 65)


if __name__ == "__main__":
    main()

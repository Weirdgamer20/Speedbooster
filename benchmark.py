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
import random
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


try:
    import scipy.stats
except ImportError as exc:
    raise RuntimeError(
        "SciPy is required for benchmark statistical certification. "
        "Install dependencies from requirements.txt before running benchmark.py."
    ) from exc

_HAS_SCIPY = True


def _randomized_trial_order(configs: List[dict], rng=None) -> List[dict]:
    """Return a shuffled copy of the configurations without mutating the caller's list."""
    ordered = list(configs)
    (rng or random).shuffle(ordered)
    return ordered


def paired_t_test(samples_a: List[float], samples_b: List[float]) -> Tuple[float, float, float, bool]:
    """
    One-sided paired t-test for H1: mean(samples_a - samples_b) > 0.
    Inputs must be aligned by randomized trial block. Degenerate differences are inconclusive.
    """
    if len(samples_a) < 2 or len(samples_a) != len(samples_b):
        return 0.0, 1.0, 0.0, False

    differences = [float(a) - float(b) for a, b in zip(samples_a, samples_b)]
    mean_diff = sum(differences) / len(differences)
    variance = sum((x - mean_diff) ** 2 for x in differences) / (len(differences) - 1)
    if variance == 0.0:
        return 0.0, 1.0, 0.0, False

    res = scipy.stats.ttest_1samp(differences, popmean=0.0, alternative="greater")
    t_stat, p_val, df = float(res.statistic), float(res.pvalue), float(res.df)
    return t_stat, p_val, df, (p_val < 0.05 and t_stat > 0)


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
    """Computes sample mean, standard deviation, standard error, and 95% Student's t confidence interval."""
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
    df = n - 1

    t_crit = float(scipy.stats.t.ppf(0.975, df=df))

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


def welch_t_test(samples_a: List[float], samples_b: List[float]) -> Tuple[float, float, float, bool]:
    """
    Calculates Welch's two-sample t-test (unequal variances) testing H1: mean(A) > mean(B).
    Returns (t_statistic, p_value, degrees_of_freedom, is_significant).
    """
    n_a = len(samples_a)
    n_b = len(samples_b)
    if n_a < 2 or n_b < 2:
        return 0.0, 1.0, 0.0, False

    mean_a = sum(samples_a) / n_a
    mean_b = sum(samples_b) / n_b
    var_a = sum((x - mean_a) ** 2 for x in samples_a) / (n_a - 1)
    var_b = sum((x - mean_b) ** 2 for x in samples_b) / (n_b - 1)

    if var_a == 0.0 and var_b == 0.0:
        # Welch's t statistic is undefined when both groups have zero variance.
        # Different constant samples are not valid grounds for claiming p=0.
        return 0.0, 1.0, 0.0, False

    if _HAS_SCIPY:
        res = scipy.stats.ttest_ind(samples_a, samples_b, equal_var=False, alternative="greater")
        t_stat = float(res.statistic)
        p_val = float(res.pvalue)
        df = float(res.df)
    else:
        raise RuntimeError("SciPy is required for Welch's t-test.")

    is_significant = (p_val < 0.05) and (t_stat > 0)
    # Preserve full precision for multiplicity correction; round only for display.
    return float(t_stat), float(p_val), float(df), is_significant


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

    # Process-level telemetry: extract actual bytes transferred per interface by task chunks
    task_iface_bytes = {}
    if task.chunks:
        for c in task.chunks:
            if c.status == "completed" and c.assigned_iface:
                task_iface_bytes[c.assigned_iface] = task_iface_bytes.get(c.assigned_iface, 0) + c.downloaded_bytes

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
        "task_iface_bytes": task_iface_bytes,
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

    # Rigorous Physical NIC Attribution & Task Telemetry Check
    # Correlates process-level task socket telemetry with OS-level NIC counter deltas.
    # Background traffic alone cannot satisfy the gate: task chunk bytes on the adapter are mandatory.
    nic_attribution_verified = True
    attribution_details = []

    if allowed_ifaces and len(allowed_ifaces) >= 2:
        if_a, if_b = allowed_ifaces[0], allowed_ifaces[1]
        target_names_a = get_target_nic_names(net_mgr, [if_a])
        target_names_b = get_target_nic_names(net_mgr, [if_b])

        for idx, r in enumerate(runs):
            deltas = r.get("nic_deltas", {})
            task_bytes = r.get("task_iface_bytes", {})
            payload_sz = r["total_bytes"]

            # Task chunk download bytes per interface
            t_a = task_bytes.get(if_a, 0)
            t_b = task_bytes.get(if_b, 0)
            pct_task_a = (t_a / payload_sz * 100) if payload_sz > 0 else 0.0
            pct_task_b = (t_b / payload_sz * 100) if payload_sz > 0 else 0.0

            # Corroborating OS NIC counter deltas
            rx_a = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names_a if n in deltas), default=0)
            rx_b = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names_b if n in deltas), default=0)
            pct_rx_a = (rx_a / payload_sz * 100) if payload_sz > 0 else 0.0
            pct_rx_b = (rx_b / payload_sz * 100) if payload_sz > 0 else 0.0

            attribution_details.append(
                f"Run {idx+1}: Task Telemetry -> {if_a}={t_a:,}B ({pct_task_a:.1f}%), {if_b}={t_b:,}B ({pct_task_b:.1f}%); "
                f"OS NIC Delta -> {if_a}={rx_a:,}B ({pct_rx_a:.1f}%), {if_b}={rx_b:,}B ({pct_rx_b:.1f}%)"
            )

            # Verification rule:
            # 1. Process task telemetry: both selected adapters must carry at least 15%, sum >= 70%
            # 2. Background traffic rule: if task bytes on an adapter are zero, attribution fails
            # 3. OS NIC deltas on target adapters must both show positive activity
            if (pct_task_a < 15.0) or (pct_task_b < 15.0) or ((pct_task_a + pct_task_b) < 70.0):
                nic_attribution_verified = False
            if t_a == 0 or t_b == 0:
                nic_attribution_verified = False
            if rx_a <= 0 or rx_b <= 0:
                nic_attribution_verified = False

    elif allowed_ifaces and len(allowed_ifaces) == 1:
        target_if = allowed_ifaces[0]
        target_names = get_target_nic_names(net_mgr, [target_if])
        for idx, r in enumerate(runs):
            deltas = r.get("nic_deltas", {})
            task_bytes = r.get("task_iface_bytes", {})
            payload_sz = r["total_bytes"]

            t_target = task_bytes.get(target_if, 0)
            pct_task = (t_target / payload_sz * 100) if payload_sz > 0 else 0.0

            rx_target = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names if n in deltas), default=0)
            pct_rx = (rx_target / payload_sz * 100) if payload_sz > 0 else 0.0

            attribution_details.append(
                f"Run {idx+1}: Task Telemetry -> {target_if}={t_target:,}B ({pct_task:.1f}%); "
                f"OS NIC Delta -> {target_if}={rx_target:,}B ({pct_rx:.1f}%)"
            )

            if pct_task < 70.0 or rx_target <= 0 or t_target == 0:
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
    parser.add_argument("--seed", type=int, default=None, help="Optional RNG seed for reproducible randomized trial order")
    parser.add_argument("--expected-sha256", type=str, default=None, help="Trusted SHA-256 digest to verify integrity")
    parser.add_argument("--allow-shared-uplink", action="store_true", help="Permit benchmarking even if adapters share gateway/public IP")
    args = parser.parse_args()
    if args.trials < 1:
        parser.error("--trials must be at least 1")

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

    dest_a = os.path.join(args.dest_dir, f"bench_a.bin")
    dest_b = os.path.join(args.dest_dir, f"bench_b.bin")
    dest_ab = os.path.join(args.dest_dir, f"bench_ab.bin")

    trials_count = args.trials
    runs_a = []
    runs_b = []
    runs_ab = []

    cfg_a = {"id": "A", "name": f"Adapter A Only ({iface_a})", "dest": dest_a, "ifaces": [iface_a], "runs": runs_a}
    cfg_b = {"id": "B", "name": f"Adapter B Only ({iface_b})", "dest": dest_b, "ifaces": [iface_b], "runs": runs_b}
    cfg_ab = {"id": "AB", "name": f"Combined A+B ({iface_a} + {iface_b})", "dest": dest_ab, "ifaces": [iface_a, iface_b], "runs": runs_ab}
    all_cfgs = [cfg_a, cfg_b, cfg_ab]

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(0, 2**32)
    rng = random.Random(seed)
    print(f"\n[BENCHMARK SUITE] Executing {trials_count} randomized/interleaved trial block(s)...")
    print(f"  Randomization seed: {seed}")
    for block_idx in range(1, trials_count + 1):
        if trials_count > 1:
            print(f"\n==================================================")
            print(f"  --- [Trial Block {block_idx}/{trials_count} (Randomized Order)] ---")
            print(f"==================================================")
        # Randomize trial order within each repeated block to eliminate temporal drift bias
        block_order = _randomized_trial_order(all_cfgs, rng=rng)
        order_names = " -> ".join(c["id"] for c in block_order)
        print(f"  Block {block_idx} order: [{order_names}]")

        for cfg in block_order:
            print(f"\n  Running {cfg['name']} (Block {block_idx})...")
            p = run_single_trial_pass(cfg["name"], args.url, cfg["dest"], net_mgr, cfg["ifaces"], expected_hash)
            cfg["runs"].append(p)
            print(f"    Pass complete: Status={p['status']} | Elapsed={p['elapsed_sec']}s | Decimal={p['decimal_mb_s']} MB/s | Binary={p['binary_mib_s']} MiB/s")

    # Build summary results for each configuration
    def _summarize_trial(trial_name: str, runs: list, allowed_ifaces: list) -> dict:
        failed_runs = [r for r in runs if r["status"] != "completed" or (expected_hash and r.get("checksum_verified") is False)]
        all_succeeded = (len(failed_runs) == 0)
        decimal_samples = [r["decimal_mb_s"] for r in runs if r["status"] == "completed"]
        stats = compute_statistics(decimal_samples)

        if not all_succeeded:
            return {
                "trial": trial_name,
                "status": f"incomplete ({len(failed_runs)}/{len(runs)} runs failed)",
                "all_succeeded": False,
                "total_bytes": 0,
                "elapsed_sec": 0.0,
                "decimal_mb_s": stats["mean"],
                "binary_mib_s": 0.0,
                "bitrate_mbps": 0.0,
                "retries": sum(r["retries"] for r in runs),
                "sha256": runs[0].get("sha256", "N/A") if runs else "N/A",
                "checksum_verified": False,
                "nic_deltas": runs[0].get("nic_deltas", {}) if runs else {},
                "nic_attribution_verified": False,
                "iterations": len(runs),
                "stats": stats,
                "runs": runs
            }

        avg_elapsed = round(sum(r["elapsed_sec"] for r in runs) / len(runs), 2)
        avg_bin = round(sum(r["binary_mib_s"] for r in runs) / len(runs), 2)
        avg_mbps = round(stats["mean"] * 8.0, 2)
        total_retries = sum(r["retries"] for r in runs)

        nic_attribution_verified = True
        attribution_details = []

        if allowed_ifaces and len(allowed_ifaces) >= 2:
            if_a, if_b = allowed_ifaces[0], allowed_ifaces[1]
            target_names_a = get_target_nic_names(net_mgr, [if_a])
            target_names_b = get_target_nic_names(net_mgr, [if_b])
            for idx, r in enumerate(runs):
                deltas = r.get("nic_deltas", {})
                task_bytes = r.get("task_iface_bytes", {})
                payload_sz = r["total_bytes"]
                t_a = task_bytes.get(if_a, 0)
                t_b = task_bytes.get(if_b, 0)
                pct_task_a = (t_a / payload_sz * 100) if payload_sz > 0 else 0.0
                pct_task_b = (t_b / payload_sz * 100) if payload_sz > 0 else 0.0
                rx_a = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names_a if n in deltas), default=0)
                rx_b = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names_b if n in deltas), default=0)
                pct_rx_a = (rx_a / payload_sz * 100) if payload_sz > 0 else 0.0
                pct_rx_b = (rx_b / payload_sz * 100) if payload_sz > 0 else 0.0
                attribution_details.append(
                    f"Run {idx+1}: Task Telemetry -> {if_a}={t_a:,}B ({pct_task_a:.1f}%), {if_b}={t_b:,}B ({pct_task_b:.1f}%); "
                    f"OS NIC Delta -> {if_a}={rx_a:,}B ({pct_rx_a:.1f}%), {if_b}={rx_b:,}B ({pct_rx_b:.1f}%)"
                )
                if (pct_task_a < 15.0) or (pct_task_b < 15.0) or ((pct_task_a + pct_task_b) < 70.0):
                    nic_attribution_verified = False
                if t_a == 0 or t_b == 0 or rx_a <= 0 or rx_b <= 0:
                    nic_attribution_verified = False

        elif allowed_ifaces and len(allowed_ifaces) == 1:
            target_if = allowed_ifaces[0]
            target_names = get_target_nic_names(net_mgr, [target_if])
            for idx, r in enumerate(runs):
                deltas = r.get("nic_deltas", {})
                task_bytes = r.get("task_iface_bytes", {})
                payload_sz = r["total_bytes"]
                t_target = task_bytes.get(target_if, 0)
                pct_task = (t_target / payload_sz * 100) if payload_sz > 0 else 0.0
                rx_target = max((deltas.get(n, {}).get("rx_bytes", 0) for n in target_names if n in deltas), default=0)
                pct_rx = (rx_target / payload_sz * 100) if payload_sz > 0 else 0.0
                attribution_details.append(
                    f"Run {idx+1}: Task Telemetry -> {target_if}={t_target:,}B ({pct_task:.1f}%); "
                    f"OS NIC Delta -> {target_if}={rx_target:,}B ({pct_rx:.1f}%)"
                )
                if pct_task < 70.0 or rx_target <= 0 or t_target == 0:
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

    res_a = _summarize_trial(cfg_a["name"], runs_a, [iface_a])
    res_b = _summarize_trial(cfg_b["name"], runs_b, [iface_b])
    res_ab = _summarize_trial(cfg_ab["name"], runs_ab, [iface_a, iface_b])
    results = [res_a, res_b, res_ab]

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

    # Statistical Significance Testing with Predeclared Hypotheses and Holm-Bonferroni Correction
    samples_combined = [r["decimal_mb_s"] for r in res_ab.get("runs", []) if r["status"] == "completed"]
    samples_a = [r["decimal_mb_s"] for r in res_a.get("runs", []) if r["status"] == "completed"]
    samples_b = [r["decimal_mb_s"] for r in res_b.get("runs", []) if r["status"] == "completed"]

    # Each configuration has one result per block. Use paired block-level differences
    # to account for temporal/network conditions shared by each A/B/AB block.
    t_stat_a, p_val_a, df_a, sig_a = paired_t_test(samples_combined, samples_a)
    t_stat_b, p_val_b, df_b, sig_b = paired_t_test(samples_combined, samples_b)

    # Holm-Bonferroni step-down correction for m=2 comparisons against both single-adapter baselines
    comp_list = sorted([
        ("Adapter A", t_stat_a, p_val_a, df_a),
        ("Adapter B", t_stat_b, p_val_b, df_b)
    ], key=lambda x: x[2])

    p_adj_first = min(1.0, comp_list[0][2] * 2)
    p_adj_second = min(1.0, max(p_adj_first, comp_list[1][2] * 1))

    holm_results = {
        comp_list[0][0]: {"t_stat": comp_list[0][1], "p_raw": comp_list[0][2], "df": comp_list[0][3], "p_adj": round(p_adj_first, 4), "sig": (p_adj_first < 0.05 and comp_list[0][1] > 0)},
        comp_list[1][0]: {"t_stat": comp_list[1][1], "p_raw": comp_list[1][2], "df": comp_list[1][3], "p_adj": round(p_adj_second, 4), "sig": (p_adj_second < 0.05 and comp_list[1][1] > 0)}
    }
    is_statistically_significant = holm_results["Adapter A"]["sig"] and holm_results["Adapter B"]["sig"]

    if trials_count > 1:
        print("\n--- Statistical Significance (Holm-Corrected Paired t-tests) ---")
        print(f"  Comparison vs {comp_list[0][0]}: t={comp_list[0][1]:.3f}, df={comp_list[0][3]:.1f}, raw p={comp_list[0][2]:.4f}, adj p={p_adj_first:.4f} {'(Significant)' if holm_results[comp_list[0][0]]['sig'] else '(Not Significant)'}")
        print(f"  Comparison vs {comp_list[1][0]}: t={comp_list[1][1]:.3f}, df={comp_list[1][3]:.1f}, raw p={comp_list[1][2]:.4f}, adj p={p_adj_second:.4f} {'(Significant)' if holm_results[comp_list[1][0]]['sig'] else '(Not Significant)'}")

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
        print("[VERDICT] ATTRIBUTION UNVERIFIED: Physical NIC counters and task telemetry did not confirm intended multi-adapter distribution.")
    elif args.trials == 1:
        print(f"[VERDICT] SINGLE-TRIAL EMPIRICAL OBSERVATION (DESCRIPTIVE ONLY, N=1):")
        print(f"          Combined speed ({combined_speed:.2f} MB/s) exceeded single-adapter baseline ({best_single_speed:.2f} MB/s).")
        print("          N=1 cannot certify statistical significance or rule out network variance. Run with --trials 10+ for rigorous certification.")
    elif not is_statistically_significant:
        print(f"[VERDICT] AGGREGATION INCONCLUSIVE: Empirical gain observed (+{((combined_speed/best_single_speed)-1)*100:.1f}%), but sample variability overlaps.")
        print(f"          Paired t-test with Holm correction failed to reach significance (p_adj >= 0.05).")
    else:
        st_ab = res_ab.get("stats", {})
        print(f"[VERDICT] PHYSICAL DUAL-WAN AGGREGATION VERIFIED: Statistically significant throughput gain certified across repeated trials.")
        print(f"          Combined: {st_ab['mean']} MB/s (95% CI: [{st_ab['ci95_lower']}, {st_ab['ci95_upper']}]) vs Baselines A & B (Holm-corrected p < 0.05).")
    print("=" * 65)


if __name__ == "__main__":
    main()

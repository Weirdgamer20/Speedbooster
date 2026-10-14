"""
main.py - Entry point for SpeedBond.
Supports diagnostics (--diagnose), bounded parallel downloads (--download),
and the integrated dashboard & proxy services.
"""

import sys
import os
import ipaddress
import time
import argparse
import subprocess
from network_manager import NetworkManager
from proxy import MultiWANProxyServer
from web_server import SpeedBondServer
from downloader import BondedDownloadTask

def run_diagnostics(net_mgr: NetworkManager, debug: bool = False):
    print("=" * 70)
    print("  ⚡ SpeedBond Diagnostics & Network Egress Audit")
    print("=" * 70)

    print("\n[1] Windows Hardware Layer Discovery (via PowerShell Get-NetAdapter):")
    hw_map = net_mgr._get_hardware_map()
    if not hw_map:
        print("    [!] Could not retrieve Windows adapter facts via PowerShell interop.")
    else:
        for mac, info in hw_map.items():
            print(f"    - MAC: {mac} | Name: {info['name']} | Status: {info.get('status', 'N/A')} | Desc: {info['desc']}")

    print("\n[2] Active Linux Network Interfaces (WSL2 Mirrored Mode):")
    ifaces = net_mgr.discover_interfaces()
    if not ifaces:
        print("    [!] No active interfaces discovered with internet egress.")
    else:
        for i in ifaces:
            pub_ip = i.public_ip if debug else (f"{i.public_ip.split('.')[0]}.{i.public_ip.split('.')[1]}.*.*" if "." in i.public_ip else "***")
            print(f"    - {i.name}:")
            print(f"        Label:      {i.label}")
            print(f"        Hardware:   {i.hw_desc}")
            print(f"        MAC:        {i.mac}")
            print(f"        Local IP:   {i.ip}")
            print(f"        Gateway:    {i.gateway or 'None'}")
            print(f"        Public IP:  {pub_ip}")
            print(f"        Ping:       {i.latency_ms:.1f} ms")

    print("\n[3] Explicit Path Validation Layer (Dual-Path Egress Proof):")
    validation = net_mgr.validate_paths()
    print(f"    Active Path Count:          {validation['interface_count']}")
    print(f"    Independent Egress Proven:  {validation['independent_egress_proven']}")
    print(f"    Status:                     {validation['status_message']}")

    if not validation["independent_egress_proven"]:
        print("\n    [NOTICE] For true multi-provider aggregation:")
        print("             Connect Interface 1 to Home Wi-Fi/Fiber (e.g. Airtel)")
        print("             Connect Interface 2 to Mobile Hotspot/USB Dongle (e.g. Jio)")
    print("=" * 70)

def main():
    parser = argparse.ArgumentParser(description="SpeedBond - Multi-Interface Bandwidth Aggregator")
    parser.add_argument("--diagnose", action="store_true", help="Run comprehensive network diagnostics and path validation")
    parser.add_argument("--debug", action="store_true", help="Print unredacted public IP addresses in diagnostic output")
    parser.add_argument("--download", type=str, help="Download a file using verified multi-interface parallel range chunks")
    parser.add_argument("--output", type=str, help="Output destination for download")
    parser.add_argument("--host", type=str, default="127.0.0.1", help="Host address for services (default: 127.0.0.1)")
    parser.add_argument("--allow-remote-bind", action="store_true", help="Explicitly allow the unauthenticated dashboard and proxy to listen beyond loopback")
    parser.add_argument("--proxy-port", type=int, default=8080, help="Port for the Multi-WAN proxy (default: 8080)")
    parser.add_argument("--web-port", "--port", type=int, default=5000, help="Port for the Dashboard UI (default: 5000)")
    parser.add_argument("--no-proxy", action="store_true", help="Disable the local proxy server")
    args = parser.parse_args()

    if not args.diagnose and not args.download:
        try:
            loopback_bind = ipaddress.ip_address(args.host).is_loopback
        except ValueError:
            loopback_bind = args.host.lower() == "localhost"
        if not loopback_bind and not args.allow_remote_bind:
            parser.error(
                "--host must be localhost/loopback by default. Binding the unauthenticated dashboard/proxy "
                "to a LAN or public interface requires --allow-remote-bind."
            )
        if not loopback_bind and args.allow_remote_bind:
            print("[SECURITY WARNING] Remote bind enabled: the dashboard and proxy have no authentication. "
                  "Use only on a trusted network with firewall restrictions.")

    net_mgr = NetworkManager()

    if args.diagnose:
        run_diagnostics(net_mgr, debug=args.debug)
        sys.exit(0)

    print("=" * 65)
    print("  ⚡ SpeedBond - Multi-Interface Bandwidth Aggregator")
    print("=" * 65)

    print("[*] Discovering network adapters and validating paths...")
    ifaces = net_mgr.discover_interfaces()
    validation = net_mgr.validate_paths()

    if not ifaces:
        print("[!] Error: No active network interfaces available. Please check connections.")
        sys.exit(1)

    print(f"[*] Found {len(ifaces)} active network interface(s):")
    for i in ifaces:
        pub = i.public_ip if args.debug else (f"{i.public_ip.split('.')[0]}.{i.public_ip.split('.')[1]}.*.*" if "." in i.public_ip else "***")
        print(f"    - {i.label} ({i.name}): Local {i.ip} -> Public {pub} (Ping: {i.latency_ms:.1f}ms)")

    if validation["independent_egress_proven"]:
        print("[+] Independent dual-path egress verified.")
    else:
        print(f"[!] Warning: {validation['status_message']}")

    net_mgr.start_monitoring(interval_sec=0.5)

    # CLI Direct Download Mode
    if args.download:
        url = args.download
        out_name = args.output or os.path.basename(url.split("?")[0]) or "downloaded.bin"
        task = BondedDownloadTask("cli_task", url, out_name, net_mgr)
        print(f"\n[*] Target URL:    {url}")
        print(f"[*] Destination:   {task.dest_path}")
        print(f"[*] Slicing mode:  Parallel HTTP Range Chunks (2 MB)")

        started = task.start()
        if not started:
            print(f"\n[!] Download failed to start: {task.error_message}")
            sys.exit(1)

        try:
            while task.status in ["probing", "running"]:
                time.sleep(0.4)
                stats = net_mgr.get_stats(redact_ip=not args.debug)
                prog = task.get_progress()
                speed_str = f"Speed: {stats['total_speed_mbytes_sec']:.2f} MB/s ({stats['total_speed_mbps']:.1f} Mbps)"
                pct_str = f"Progress: {prog['progress_pct']}% ({prog['downloaded_bytes'] / (1024*1024):.1f}/{prog['total_bytes'] / (1024*1024):.1f} MB)"
                sys.stdout.write(f"\r  {speed_str} | {pct_str}  ")
                sys.stdout.flush()

            print(f"\n[*] Download finished with status: {task.status}")
            if task.status == "completed":
                print(f"[+] Output file: {task.dest_path}")
                print(f"[+] SHA-256:     {task.sha256_hash}")
                sys.exit(0)
            else:
                print(f"[!] Error: {task.error_message}")
                sys.exit(1)

        except KeyboardInterrupt:
            task.cancel()
            print("\n[*] Download cancelled by user.")
            sys.exit(130)

    # Full Service Mode (Proxy + Web UI)
    proxy_server = None
    if not args.no_proxy:
        proxy_server = MultiWANProxyServer(net_mgr, host=args.host, port=args.proxy_port)
        proxy_server.start()

    web_server = SpeedBondServer(net_mgr, proxy_server, host=args.host, port=args.web_port)

    print("\n" + "-" * 65)
    print(f"  ⚡ Web Dashboard:  http://{args.host}:{args.web_port}")
    if proxy_server:
        print(f"  🌐 Multi-WAN Proxy: http://{args.host}:{args.proxy_port}")
    print("-" * 65)
    print("Press Ctrl+C to stop services.")

    try:
        web_server.start()
    except KeyboardInterrupt:
        print("\n[*] Stopping SpeedBond services...")
        net_mgr.stop_monitoring()
        if proxy_server:
            proxy_server.stop()
        print("[*] Done.")

if __name__ == "__main__":
    main()

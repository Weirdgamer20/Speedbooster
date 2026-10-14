"""
network_manager.py - Interface Discovery, Hardware Mapping, Path Validation & Socket Factory.
Provides explicit path validation, SO_BINDTODEVICE / source-IP binding, thread-safe counters,
and clean separation of physical NIC stats versus SpeedBond task throughput.
"""

import os
import re
import sys
import json
import time
import socket
import ssl
import subprocess
import threading
from typing import List, Dict, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.connection import create_connection
import urllib.parse


def resolve_host(hostname: str, dns_servers: list = None) -> Optional[str]:
    """
    Resolves a hostname to an IPv4 address using a direct UDP DNS query,
    bypassing /etc/resolv.conf. Falls back to socket.getaddrinfo() if needed.
    In WSL2 mirrored mode, the stub resolver at 10.255.255.254 can be unreachable
    from source-IP-bound sockets, so we query 8.8.8.8 directly.
    """
    if not dns_servers:
        dns_servers = ["8.8.8.8", "1.1.1.1"]

    # Build a minimal DNS query for A record
    import struct
    import random as _random
    txid = _random.randint(0, 65535)
    flags = 0x0100  # recursion desired
    qdcount = 1
    header = struct.pack(">HHHHHH", txid, flags, qdcount, 0, 0, 0)
    # Encode QNAME
    qname = b""
    for part in hostname.rstrip(".").split("."):
        encoded = part.encode()
        qname += bytes([len(encoded)]) + encoded
    qname += b"\x00"
    qtype = 1   # A
    qclass = 1  # IN
    question = qname + struct.pack(">HH", qtype, qclass)
    query = header + question

    for dns_server in dns_servers:
        try:
            udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            udp.settimeout(3.0)
            udp.sendto(query, (dns_server, 53))
            response, _ = udp.recvfrom(512)
            udp.close()
            # Parse response: skip header (12) and question (len(question)) then iterate answers
            offset = 12 + len(question)
            ancount = struct.unpack(">H", response[6:8])[0]
            for _ in range(ancount):
                if offset >= len(response):
                    break
                # Skip name (may be pointer)
                while offset < len(response):
                    ll = response[offset]
                    if ll & 0xC0 == 0xC0:  # pointer
                        offset += 2
                        break
                    elif ll == 0:
                        offset += 1
                        break
                    else:
                        offset += 1 + ll
                if offset + 10 > len(response):
                    break
                rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", response[offset:offset+10])
                offset += 10
                if rtype == 1 and rdlen == 4:  # A record
                    ip = ".".join(str(b) for b in response[offset:offset+4])
                    return ip
                offset += rdlen
        except Exception:
            continue

    # Fallback: system resolver (may fail on bound sockets but try anyway)
    try:
        return socket.getaddrinfo(hostname, None, socket.AF_INET)[0][4][0]
    except Exception:
        return None

class InterfaceStats:
    def __init__(self, name: str, ip: str, mac: str = "", public_ip: str = "", label: str = "", hw_desc: str = "", gateway: str = "", if_index: int = 0):
        self.name = name
        self.ip = ip
        self.mac = mac
        self.public_ip = public_ip
        self.label = label
        self.hw_desc = hw_desc
        self.gateway = gateway
        self.if_index = if_index
        self.is_active = True
        self.latency_ms = 0.0

        # Application-attributed counters (thread-safe)
        self.bytes_downloaded = 0
        self.current_speed_bps = 0.0
        self._last_bytes = 0
        self._last_time = time.time()
        self._counter_lock = threading.Lock()

        # Physical NIC hardware counter baseline
        self.physical_nic_rx_bytes = 0
        self.physical_nic_tx_bytes = 0

    def update_bytes(self, num_bytes: int):
        with self._counter_lock:
            self.bytes_downloaded += num_bytes

    def tick_speed(self) -> float:
        now = time.time()
        with self._counter_lock:
            elapsed = now - self._last_time
            if elapsed >= 0.4:
                delta_bytes = self.bytes_downloaded - self._last_bytes
                self.current_speed_bps = delta_bytes / elapsed
                self._last_bytes = self.bytes_downloaded
                self._last_time = now
            return self.current_speed_bps

    @property
    def speed_mbps(self) -> float:
        return (self.current_speed_bps * 8) / (1024 * 1024)

    @property
    def speed_mbytes_per_sec(self) -> float:
        return self.current_speed_bps / (1024 * 1024)

    def to_dict(self, redact_ip: bool = True) -> dict:
        pub_ip = self.public_ip
        if redact_ip and pub_ip and "." in pub_ip:
            octets = pub_ip.split(".")
            if len(octets) == 4:
                pub_ip = f"{octets[0]}.{octets[1]}.*.*"

        return {
            "name": self.name,
            "ip": self.ip,
            "mac": self.mac,
            "public_ip": pub_ip,
            "gateway": self.gateway,
            "if_index": self.if_index,
            "label": self.label,
            "hw_desc": self.hw_desc,
            "is_active": self.is_active,
            "latency_ms": round(self.latency_ms, 1),
            "bytes_downloaded": self.bytes_downloaded,
            "speed_mbps": round(self.speed_mbps, 2),
            "speed_mbytes_sec": round(self.speed_mbytes_per_sec, 2),
        }


import logging

logger = logging.getLogger("speedbond.network")

class InterfaceBindingError(Exception):
    """Raised when an interface or source-IP binding operation fails."""
    pass

class BoundInterfaceHTTPAdapter(HTTPAdapter):
    """
    Custom requests HTTPAdapter that binds client sockets to a specific physical adapter.
    Enforces multi-layer egress binding:
    1. Linux/WSL: SO_BINDTODEVICE (socket.SOL_SOCKET, 25, ifname.encode() + b'\\0')
       Pins the socket directly to the physical/mirrored network interface at the link layer.
    2. Windows: IP_UNICAST_IF (socket.IPPROTO_IP, 31, struct.pack('!I', if_index))
       Forces Winsock to route outgoing unicast packets strictly via the adapter interface index.
    3. Both: source_address=(source_ip, 0)
       Binds the socket's local endpoint to the interface IP.
    """
    def __init__(self, ifname: str, source_ip: Optional[str] = None, if_index: int = 0, *args, **kwargs):
        self.ifname = ifname
        self.source_ip = source_ip
        self.if_index = if_index
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        sock_opts = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]

        # Linux/WSL: enforce strict SO_BINDTODEVICE (skip loopback interface)
        if (
            sys.platform.startswith("linux")
            and hasattr(socket, "SO_BINDTODEVICE")
            and self.ifname not in ["lo", "127.0.0.1", "loopback"]
        ):
            try:
                sock_opts.append((socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), self.ifname.encode() + b"\0"))
            except Exception as be:
                logger.warning("Could not set SO_BINDTODEVICE for %s: %s", self.ifname, be)
        elif sys.platform == "win32" and self.if_index > 0:
            # Windows: enforce IP_UNICAST_IF (31)
            import struct
            IP_UNICAST_IF = getattr(socket, "IP_UNICAST_IF", 31)
            try:
                sock_opts.append((socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", self.if_index)))
            except Exception as be:
                logger.warning("Could not set IP_UNICAST_IF for %s (index %d): %s", self.ifname, self.if_index, be)

        pool_kwargs["socket_options"] = sock_opts

        # Bind source IP so packets egress via the correct adapter
        if self.source_ip:
            pool_kwargs["source_address"] = (self.source_ip, 0)

        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


class NetworkManager:
    def __init__(self):
        self.interfaces: Dict[str, InterfaceStats] = {}
        self._lock = threading.Lock()
        self.ssl_context = ssl.create_default_context()
        self._stop_monitor = threading.Event()
        self._monitor_thread = None

    def _get_hardware_map(self) -> Dict[str, dict]:
        """Queries Windows hardware layer to map MAC addresses to physical adapter descriptions."""
        cmd = [
            'powershell.exe', '-NoProfile', '-Command', 
            'Get-NetAdapter | Select-Object Name, InterfaceDescription, MacAddress, Status | ConvertTo-Json -Compress'
        ]
        try:
            out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            res = {}
            for item in data:
                raw_mac = (item.get('MacAddress') or '').replace('-', ':').lower()
                if raw_mac:
                    res[raw_mac] = {
                        'name': item.get('Name'),
                        'desc': item.get('InterfaceDescription'),
                        'status': item.get('Status')
                    }
            return res
        except Exception:
            return {}

    def _discover_windows(self) -> List[dict]:
        """
        Windows-native interface discovery via PowerShell Get-NetIPConfiguration.
        Returns list of dicts with keys: name, ip, gateway, mac, desc.
        """
        ps_cmd = (
            "Get-NetIPConfiguration | Where-Object {$_.IPv4DefaultGateway -ne $null} | "
            "Select-Object -Property InterfaceAlias, InterfaceIndex, "
            "@{n='IP';e={$_.IPv4Address.IPAddress}}, "
            "@{n='Gateway';e={$_.IPv4DefaultGateway.NextHop}}, "
            "@{n='MAC';e={(Get-NetAdapter -InterfaceAlias $_.InterfaceAlias).MacAddress}}, "
            "@{n='Desc';e={(Get-NetAdapter -InterfaceAlias $_.InterfaceAlias).InterfaceDescription}} "
            "| ConvertTo-Json -Compress"
        )
        try:
            out = subprocess.check_output(
                ["powershell.exe", "-NoProfile", "-Command", ps_cmd],
                text=True, stderr=subprocess.DEVNULL, timeout=20
            ).strip()
            if not out:
                return []
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            results = []
            for item in data:
                name = item.get("InterfaceAlias", "")
                ip = item.get("IP", "")
                gw = item.get("Gateway", "")
                if_idx = int(item.get("InterfaceIndex") or 0)
                mac = (item.get("MAC") or "").replace("-", ":").lower()
                desc = item.get("Desc", "")
                if name and ip:
                    results.append({"name": name, "ip": ip, "gateway": gw, "mac": mac, "desc": desc, "if_index": if_idx})
            return results
        except Exception as e:
            logger.warning("Windows interface discovery failed: %s", e)
            return []

    def _discover_linux(self) -> List[dict]:
        """Linux/WSL interface discovery via ip commands."""
        gateway_map = {}
        try:
            route_out = subprocess.check_output(["ip", "route", "show"], text=True, timeout=5)
            for line in route_out.strip().split("\n"):
                if line.startswith("default via"):
                    parts = line.split()
                    if len(parts) >= 5:
                        gateway_map[parts[4]] = parts[2]
        except Exception:
            pass

        results = []
        try:
            out = subprocess.check_output(["ip", "-4", "-o", "addr", "show"], text=True, timeout=5)
            for line in out.strip().split("\n"):
                if not line:
                    continue
                parts = line.split()
                if len(parts) < 4:
                    continue
                ifname = parts[1]
                ip = parts[3].split("/")[0]
                if ifname in ["lo", "docker0"] or ip.startswith("127.") or ip.startswith("172.") or ip.startswith("169.254."):
                    continue
                mac = ""
                mac_path = f"/sys/class/net/{ifname}/address"
                if os.path.isfile(mac_path):
                    with open(mac_path) as mf:
                        mac = mf.read().strip().lower()
                if_idx = 0
                idx_path = f"/sys/class/net/{ifname}/ifindex"
                if os.path.isfile(idx_path):
                    try:
                        with open(idx_path) as idxf:
                            if_idx = int(idxf.read().strip())
                    except Exception:
                        pass
                results.append({"name": ifname, "ip": ip, "gateway": gateway_map.get(ifname, ""), "mac": mac, "desc": "", "if_index": if_idx})
            self._ensure_linux_policy_routing(results)
        except Exception as e:
            logger.warning("Linux interface discovery failed: %s", e)
        return results

    def _ensure_linux_policy_routing(self, interfaces: List[dict]):
        """
        Configures Linux source-based policy routing (tables 101, 102, ...)
        so sockets bound to specific interface IPs route strictly out their
        corresponding physical/mirrored interfaces even on shared subnets.
        """
        try:
            rule_out = subprocess.check_output(["ip", "rule", "show"], text=True, timeout=5)
        except Exception:
            rule_out = ""

        for idx, iface in enumerate(interfaces):
            ip = iface.get("ip")
            name = iface.get("name")
            gw = iface.get("gateway")
            if not ip or not name or not gw:
                continue
            table_id = 101 + idx
            if f"from {ip} lookup {table_id}" not in rule_out:
                try:
                    subprocess.run(["sudo", "ip", "rule", "add", "from", ip, "table", str(table_id)], check=False, timeout=5)
                    subprocess.run(["sudo", "ip", "route", "add", "default", "via", gw, "dev", name, "table", str(table_id)], check=False, timeout=5)
                    logger.info("Configured Linux policy routing: table %d for %s (IP %s via %s)", table_id, name, ip, gw)
                except Exception as e:
                    logger.warning("Could not set up policy routing for %s: %s", name, e)

    def discover_interfaces(self) -> List[InterfaceStats]:
        """
        Dynamically detects physical ports and USB dongles currently providing internet.
        Windows: uses PowerShell Get-NetIPConfiguration.
        Linux/WSL: uses 'ip route' and 'ip addr'.
        """
        hw_map = self._get_hardware_map()
        found = []

        if sys.platform == "win32":
            raw_ifaces = self._discover_windows()
        else:
            raw_ifaces = self._discover_linux()

        # Resolve hardware descriptions from hw_map for Linux entries
        for iface in raw_ifaces:
            mac = iface.get("mac", "")
            desc = iface.get("desc", "")
            name = iface.get("name", "")
            if_idx = iface.get("if_index", 0)

            if not desc and mac:
                hw_info = hw_map.get(mac, {})
                desc = hw_info.get("desc", "")
                if not name:
                    name = hw_info.get("name", name)

            # Exclude virtual adapters (Hyper-V, WSL internal vNICs, Speedify, Loopback)
            combined_check = f"{desc} {name}".lower()
            if any(k in combined_check for k in ["speedify", "virtual", "hyper-v", "container",
                                                   "loopback", "wsl", "bluetooth", "vpn"]):
                continue

            label = f"{name} ({desc})" if desc else name
            ip = iface.get("ip", "")
            gw = iface.get("gateway", "")

            if not ip or not name:
                continue

            # Test egress: on Windows, bind by source IP; on Linux, bind by device
            pub_ip, latency = self._test_interface_egress(name, source_ip=ip)
            if pub_ip:
                ifstat = InterfaceStats(
                    name=name,
                    ip=ip,
                    mac=mac,
                    public_ip=pub_ip,
                    label=label,
                    hw_desc=desc or label,
                    gateway=gw,
                    if_index=if_idx
                )
                ifstat.latency_ms = latency
                found.append(ifstat)
                logger.info(
                    "Discovered active interface: name='%s' ip='%s' gw='%s' pub='%s' latency=%.1fms if_index=%d",
                    name, ip, gw, pub_ip, latency, if_idx
                )
            else:
                logger.info("Interface '%s' (ip=%s) could not reach internet, skipping.", name, ip)

        with self._lock:
            current_names = [f.name for f in found]
            for ifstat in found:
                if ifstat.name not in self.interfaces:
                    self.interfaces[ifstat.name] = ifstat
                else:
                    self.interfaces[ifstat.name].public_ip = ifstat.public_ip
                    self.interfaces[ifstat.name].latency_ms = ifstat.latency_ms
                    self.interfaces[ifstat.name].label = ifstat.label
                    self.interfaces[ifstat.name].hw_desc = ifstat.hw_desc
                    self.interfaces[ifstat.name].gateway = ifstat.gateway
                    self.interfaces[ifstat.name].is_active = True
            for name in list(self.interfaces.keys()):
                if name not in current_names:
                    self.interfaces[name].is_active = False

        return list(self.interfaces.values())

    def validate_paths(self) -> Dict[str, any]:
        """
        Explicit Path Validation Layer (Release Gate Requirement):
        Proves whether active interfaces use distinct network egress paths.
        Returns a diagnostic report and validation status.
        """
        active = [i for i in self.interfaces.values() if i.is_active]
        report = {
            "interface_count": len(active),
            "paths": [],
            "independent_egress_proven": False,
            "status_message": "",
        }

        if len(active) < 2:
            report["status_message"] = f"Only {len(active)} active interface(s) available; multi-path bonding requires at least 2."
            return report

        distinct_public_ips = set()
        distinct_gateways = set()

        for ifstat in active:
            path_info = {
                "name": ifstat.name,
                "label": ifstat.label,
                "mac": ifstat.mac,
                "local_ip": ifstat.ip,
                "gateway": ifstat.gateway,
                "public_ip": ifstat.public_ip,
                "latency_ms": ifstat.latency_ms,
            }
            report["paths"].append(path_info)
            if ifstat.public_ip:
                distinct_public_ips.add(ifstat.public_ip)
            if ifstat.gateway:
                distinct_gateways.add(ifstat.gateway)

        if len(distinct_public_ips) >= 2 or len(distinct_gateways) >= 2:
            report["independent_egress_proven"] = True
            report["status_message"] = "Verified independent network egress paths across interfaces."
        else:
            report["independent_egress_proven"] = False
            report["status_message"] = (
                "WARNING: Independent egress cannot be proven. All interfaces share the same gateway and public IP. "
                "Ensure one adapter connects to Home Wi-Fi and the other to Mobile Hotspot."
            )

        return report

    def _test_interface_egress(self, ifname: str, source_ip: Optional[str] = None) -> Tuple[Optional[str], float]:
        """
        Tests whether an interface can route to the internet.
        Windows: binds by source IP address (the only supported method).
        Linux: additionally applies SO_BINDTODEVICE for strict interface pinning.
        Returns (public_ip, latency_ms), or (None, 0.0) on failure.
        """
        t0 = time.time()
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            # Linux: SO_BINDTODEVICE pins the socket to a physical NIC
            if sys.platform.startswith("linux") and hasattr(socket, "SO_BINDTODEVICE"):
                try:
                    s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), ifname.encode())
                except Exception as be:
                    logger.warning("SO_BINDTODEVICE failed for '%s': %s", ifname, be)

            # Windows & Linux: bind source IP so packets egress via the correct adapter
            if source_ip:
                try:
                    s.bind((source_ip, 0))
                except Exception as be:
                    s.close()
                    logger.error("Source IP bind failed for '%s' (ip=%s): %s", ifname, source_ip, be)
                    return None, 0.0

            s.settimeout(8.0)
            s.connect(("api.ipify.org", 80))
            latency = (time.time() - t0) * 1000
            s.sendall(b"GET / HTTP/1.1\r\nHost: api.ipify.org\r\nConnection: close\r\n\r\n")
            buf = b""
            while True:
                c = s.recv(512)
                if not c:
                    break
                buf += c
            s.close()
            parts = buf.split(b"\r\n\r\n")
            if len(parts) > 1:
                pub_ip = parts[-1].decode("ascii", errors="ignore").strip()
                if pub_ip:
                    logger.info("Egress verified: iface='%s' source='%s' public_ip='%s' latency=%.1fms",
                                ifname, source_ip, pub_ip, latency)
                    return pub_ip, latency
        except Exception as e:
            logger.info("Egress test failed for '%s' (ip=%s): %s", ifname, source_ip, e)
        return None, 0.0

    def create_requests_session(self, ifname: str) -> requests.Session:
        """Creates a requests.Session whose sockets are bound strictly to `ifname`."""
        session = requests.Session()
        ifstat = self.interfaces.get(ifname)
        source_ip = ifstat.ip if ifstat else None
        gateway = ifstat.gateway if ifstat else None
        if_index = ifstat.if_index if ifstat else 0

        logger.info(
            "Configuring requests session: iface='%s', source_ip='%s', gateway='%s', if_index=%d",
            ifname, source_ip, gateway, if_index
        )
        adapter = BoundInterfaceHTTPAdapter(ifname=ifname, source_ip=source_ip, if_index=if_index)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def create_bound_socket(self, ifname: str, host: str, port: int, timeout: float = 15.0, use_ssl: bool = False):
        """Creates a TCP socket bound strictly to the specified physical interface."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ifstat = self.interfaces.get(ifname)
        source_ip = ifstat.ip if ifstat else None
        gateway = ifstat.gateway if ifstat else None
        if_index = ifstat.if_index if ifstat else 0

        logger.info(
            "Binding raw socket: target=%s:%d, iface='%s', source_ip='%s', gateway='%s', if_index=%d",
            host, port, ifname, source_ip, gateway, if_index
        )

        if sys.platform.startswith("linux") and hasattr(socket, "SO_BINDTODEVICE") and ifname not in ["lo", "127.0.0.1", "loopback"]:
            try:
                sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), ifname.encode() + b"\0")
            except Exception as e:
                sock.close()
                raise InterfaceBindingError(f"Failed to set SO_BINDTODEVICE for interface '{ifname}': {e}") from e
        elif sys.platform == "win32" and if_index > 0:
            import struct
            IP_UNICAST_IF = getattr(socket, "IP_UNICAST_IF", 31)
            try:
                sock.setsockopt(socket.IPPROTO_IP, IP_UNICAST_IF, struct.pack("!I", if_index))
            except Exception as e:
                sock.close()
                raise InterfaceBindingError(f"Failed to set IP_UNICAST_IF for interface '{ifname}' (index {if_index}): {e}") from e

        if source_ip:
            try:
                sock.bind((source_ip, 0))
            except Exception as e:
                sock.close()
                raise InterfaceBindingError(f"Failed to bind source IP '{source_ip}' for interface '{ifname}': {e}") from e

        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1048576)
        except Exception:
            pass

        sock.settimeout(timeout)
        try:
            sock.connect((host, port))
            logger.info("Connected to %s:%d via iface='%s' (local=%s)", host, port, ifname, sock.getsockname())
        except Exception as e:
            sock.close()
            logger.error("Connection failed to %s:%d via iface='%s': %s", host, port, ifname, e)
            raise

        if use_ssl:
            wrapped = self.ssl_context.wrap_socket(sock, server_hostname=host)
            return wrapped
        return sock

    def start_monitoring(self, interval_sec: float = 0.5):
        """Starts real-time stats calculation."""
        if self._monitor_thread and self._monitor_thread.is_alive():
            return

        def _monitor():
            while not self._stop_monitor.is_set():
                with self._lock:
                    for ifstat in self.interfaces.values():
                        ifstat.tick_speed()
                time.sleep(interval_sec)

        self._stop_monitor.clear()
        self._monitor_thread = threading.Thread(target=_monitor, daemon=True)
        self._monitor_thread.start()

    def stop_monitoring(self):
        self._stop_monitor.set()

    def get_stats(self, redact_ip: bool = True) -> dict:
        with self._lock:
            ifaces = [ifstat.to_dict(redact_ip=redact_ip) for ifstat in self.interfaces.values() if ifstat.is_active]
            total_speed_mbps = sum(i["speed_mbps"] for i in ifaces)
            total_speed_mbytes = sum(i["speed_mbytes_sec"] for i in ifaces)
            total_bytes = sum(i["bytes_downloaded"] for i in ifaces)
            return {
                "interfaces": ifaces,
                "total_speed_mbps": round(total_speed_mbps, 2),
                "total_speed_mbytes_sec": round(total_speed_mbytes, 2),
                "total_bytes_downloaded": total_bytes,
            }

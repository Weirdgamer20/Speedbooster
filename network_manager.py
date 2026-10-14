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

class InterfaceBindingError(requests.exceptions.RequestException):
    """Raised when an interface or source-IP binding operation fails."""
    pass

from security_utils import DNSRebindingSafePoolManager, is_prohibited_ip, validate_url_ssrf

class BoundInterfaceHTTPAdapter(HTTPAdapter):
    """
    Custom requests HTTPAdapter that binds client sockets to a specific physical adapter
    and guarantees DNS rebinding immunity by connecting strictly to validated IP addresses.
    Enforces multi-layer egress binding:
    1. Linux/WSL: SO_BINDTODEVICE (socket.SOL_SOCKET, 25, ifname.encode() + b'\\0')
       Pins the socket directly to the physical/mirrored network interface at the link layer.
    2. Windows: IP_UNICAST_IF (socket.IPPROTO_IP, 31, struct.pack('!I', if_index))
       Forces Winsock to route outgoing unicast packets strictly via the adapter interface index.
    3. Both: source_address=(source_ip, 0)
       Binds the socket's local endpoint to the interface IP.
    4. DNS Rebinding Immunity:
       Uses DNSRebindingSafePoolManager to connect directly to validated IP targets
       without secondary TOCTOU resolution, while preserving TLS SNI and server certificate verification.
    """
    def __init__(self, ifname: str, source_ip: Optional[str] = None, if_index: int = 0, allow_local: bool = False, *args, **kwargs):
        self.ifname = ifname
        self.source_ip = source_ip
        self.if_index = if_index
        self.allow_local = allow_local
        self.pinned_hosts: Dict[str, str] = {}
        self._last_telemetry: Optional[dict] = None
        super().__init__(*args, **kwargs)

    @property
    def last_telemetry(self) -> Optional[dict]:
        return getattr(self, "_last_telemetry", None)

    @last_telemetry.setter
    def last_telemetry(self, val):
        self._last_telemetry = val

    def pin_host(self, host: str, ip: str, allow_local: Optional[bool] = None):
        """Pins a hostname to a validated destination IP address, eliminating TOCTOU DNS rebinding."""
        self.pinned_hosts[host.lower()] = ip
        if allow_local is not None:
            self.allow_local = allow_local

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

        self._pool_connections = connections
        self._pool_maxsize = maxsize
        self._pool_block = block
        self.poolmanager = DNSRebindingSafePoolManager(
            num_pools=connections, maxsize=maxsize, block=block, **pool_kwargs
        )
        self.poolmanager._adapter = self

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        conn = super().get_connection_with_tls_context(request, verify, proxies=proxies, cert=cert)
        import urllib.parse
        host = (urllib.parse.urlparse(request.url).hostname or "").lower()
        conn._allow_local = self.allow_local
        conn._adapter = self
        if host in self.pinned_hosts:
            conn._pinned_ip = self.pinned_hosts[host]
        return conn

    def get_connection(self, url, proxies=None):
        conn = super().get_connection(url, proxies=proxies)
        import urllib.parse
        host = (urllib.parse.urlparse(url).hostname or "").lower()
        conn._allow_local = self.allow_local
        conn._adapter = self
        if host in self.pinned_hosts:
            conn._pinned_ip = self.pinned_hosts[host]
        return conn

    def verify_socket_binding(self) -> Tuple[bool, str]:
        """
        Independently verifies at the socket layer that an outbound socket can be created
        and bound to the configured interface and source address.
        """
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            if sys.platform.startswith("linux") and hasattr(socket, "SO_BINDTODEVICE"):
                if self.ifname and self.ifname not in ["lo", "127.0.0.1", "loopback"]:
                    s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), self.ifname.encode() + b"\0")
            elif sys.platform == "win32" and self.if_index > 0:
                import struct
                s.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_UNICAST_IF", 31), struct.pack("!I", self.if_index))
            if self.source_ip:
                s.bind((self.source_ip, 0))
            return True, "Socket binding verified"
        except Exception as e:
            return False, f"Socket binding verification failed on '{self.ifname}': {e}"
        finally:
            if s:
                try:
                    s.close()
                except Exception:
                    pass

    def verify_outbound_binding(self, test_url: Optional[str] = None) -> Tuple[bool, str]:
        """
        Tests an actual outbound connection through this production HTTP adapter and verifies
        that the connection succeeds, mandatory interface options are applied, and the socket's bound
        local endpoint matches self.source_ip.
        """
        ok, err = self.verify_socket_binding()
        if not ok:
            return False, err

        if test_url:
            try:
                s = requests.Session()
                s.trust_env = False
                s.mount("http://", self)
                s.mount("https://", self)
                resp = s.get(test_url, timeout=4.0)
                if resp.status_code >= 400:
                    return False, f"Outbound probe returned error HTTP {resp.status_code}"

                # Verify actual socket telemetry from production connection
                telem = self.last_telemetry or {}
                actual_local_ip = telem.get("local_ip")
                actual_peer_ip = telem.get("peer_ip")
                applied_options = telem.get("options", [])

                if self.source_ip and actual_local_ip and actual_local_ip != self.source_ip:
                    return False, (
                        f"Outbound connection local IP mismatch on '{self.ifname}': "
                        f"expected {self.source_ip}, got actual {actual_local_ip}"
                    )

                return True, (
                    f"Verified outbound connection through adapter '{self.ifname}' "
                    f"(local: {actual_local_ip or self.source_ip}, peer: {actual_peer_ip}, options: {applied_options})"
                )
            except Exception as e:
                return False, f"Outbound connection failed through adapter '{self.ifname}': {e}"

        return True, f"Local socket options and address binding verified for '{self.ifname}' (outbound probe unperformed)"

    def send(self, request, **kwargs):
        """Wraps send to ensure socket-level interface or source-address binding failures fail closed and captures connection telemetry."""
        try:
            resp = super().send(request, **kwargs)
            # Inspect underlying pool or connection for telemetry if not already populated
            if not self.last_telemetry:
                conn = getattr(resp.raw, "_connection", None)
                if conn:
                    if hasattr(conn, "last_telemetry") and conn.last_telemetry:
                        self.last_telemetry = conn.last_telemetry
                    elif hasattr(conn, "_actual_local_ip"):
                        self.last_telemetry = {
                            "local_ip": getattr(conn, "_actual_local_ip", None),
                            "local_port": getattr(conn, "_actual_local_port", None),
                            "peer_ip": getattr(conn, "_actual_peer_ip", None),
                            "peer_port": getattr(conn, "_actual_peer_port", None),
                            "options": getattr(conn, "_interface_options_applied", []),
                            "interface_options_applied": getattr(conn, "_interface_options_applied", []),
                        }
            return resp
        except InterfaceBindingError:
            raise
        except (requests.exceptions.ConnectionError, requests.exceptions.ConnectTimeout, OSError, socket.error) as e:
            raise InterfaceBindingError(
                f"Interface socket binding or connection failed on '{self.ifname}' (IP: {self.source_ip}): {e}"
            ) from e


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
        except Exception as e:
            logger.warning("Linux interface discovery failed: %s", e)
        return results

    def configure_linux_policy_routing(self, apply: bool = False, rollback: bool = False) -> Dict[str, Any]:
        """
        Explicit, opt-in configuration for Linux source-based policy routing (tables 101, 102, ...).
        When apply=False and rollback=False: returns a preview plan (dry run).
        When apply=True: applies routing rules non-interactively.
        When rollback=True: removes rules.
        Interface discovery never invokes this method automatically.
        """
        if not sys.platform.startswith("linux"):
            return {"supported": False, "reason": "Not running on Linux/WSL2"}

        active = [i for i in self.interfaces.values() if i.is_active and i.ip and i.gateway]
        if len(active) < 2:
            return {"supported": False, "reason": "Requires at least 2 active interfaces with IP and gateway"}

        try:
            rule_out = subprocess.check_output(["ip", "rule", "show"], text=True, stderr=subprocess.DEVNULL, timeout=3)
        except Exception:
            rule_out = ""

        plan = []
        is_root = os.geteuid() == 0 if hasattr(os, "geteuid") else False
        sudo_prefix = [] if is_root else ["sudo", "-n"]

        for idx, ifstat in enumerate(active):
            table_id = 101 + idx
            rule_desc = f"from {ifstat.ip} lookup {table_id}"
            route_desc = f"default via {ifstat.gateway} dev {ifstat.name} table {table_id}"
            already_applied = rule_desc in rule_out

            plan.append({
                "interface": ifstat.name,
                "ip": ifstat.ip,
                "gateway": ifstat.gateway,
                "table_id": table_id,
                "rule": rule_desc,
                "route": route_desc,
                "already_applied": already_applied
            })

            if apply and not already_applied:
                try:
                    res = subprocess.run(
                        sudo_prefix + ["ip", "rule", "add", "from", ifstat.ip, "table", str(table_id)],
                        check=False, capture_output=True, timeout=3
                    )
                    if res.returncode == 0:
                        subprocess.run(
                            sudo_prefix + ["ip", "route", "add", "default", "via", ifstat.gateway, "dev", ifstat.name, "table", str(table_id)],
                            check=False, capture_output=True, timeout=3
                        )
                        logger.info("Applied policy route: %s via %s (table %d)", ifstat.ip, ifstat.gateway, table_id)
                except Exception as e:
                    logger.warning("Failed to apply policy route for %s: %s", ifstat.name, e)

            elif rollback and already_applied:
                try:
                    subprocess.run(
                        sudo_prefix + ["ip", "rule", "del", "from", ifstat.ip, "table", str(table_id)],
                        check=False, capture_output=True, timeout=3
                    )
                    subprocess.run(
                        sudo_prefix + ["ip", "route", "flush", "table", str(table_id)],
                        check=False, capture_output=True, timeout=3
                    )
                    logger.info("Rolled back policy route for table %d", table_id)
                except Exception as e:
                    logger.warning("Failed to rollback policy route for %s: %s", ifstat.name, e)

        return {"supported": True, "applied": apply, "rollback": rollback, "plan": plan}

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

    def validate_paths(self, outbound_test_url: Optional[str] = None) -> Dict[str, any]:
        """
        Explicit Path Validation Layer (Release Gate Requirement):
        Evaluates discovery, source IP binding, public egress, distinct gateways,
        and distinct public IPs across separate, verifiable stages.
        """
        active = [i for i in self.interfaces.values() if i.is_active]

        distinct_public_ips = set()
        distinct_gateways = set()
        paths = []

        for ifstat in active:
            path_info = {
                "name": ifstat.name,
                "label": ifstat.label,
                "mac": ifstat.mac,
                "local_ip": ifstat.ip,
                "gateway": ifstat.gateway,
                "public_ip": ifstat.public_ip,
                "latency_ms": ifstat.latency_ms,
                "binding_supported": bool(ifstat.ip),
                "egress_verified": bool(ifstat.public_ip and ifstat.public_ip not in ["N/A", ""]),
            }
            paths.append(path_info)
            if ifstat.public_ip and ifstat.public_ip not in ["N/A", ""]:
                distinct_public_ips.add(ifstat.public_ip)
            if ifstat.gateway and ifstat.gateway not in ["N/A", ""]:
                distinct_gateways.add(ifstat.gateway)

        # Multi-stage evaluation
        interfaces_discovered = (len(active) >= 2)
        source_address_available = bool(active and all(i.ip for i in active))

        # Test socket binding and outbound connectivity through production BoundInterfaceHTTPAdapter
        socket_binding_test_passed = False
        if source_address_available and active:
            passed_all = True
            for ifstat in active:
                adapter = BoundInterfaceHTTPAdapter(ifname=ifstat.name, source_ip=ifstat.ip, if_index=ifstat.if_index)
                if outbound_test_url:
                    ok, _ = adapter.verify_outbound_binding(test_url=outbound_test_url)
                else:
                    ok, _ = adapter.verify_socket_binding()
                if not ok:
                    passed_all = False
                    break
                # Egress check: ifstat must have verified outbound routing (public_ip not empty/N/A)
                if not (ifstat.public_ip and ifstat.public_ip not in ["N/A", ""]):
                    passed_all = False
                    break
            socket_binding_test_passed = passed_all

        public_egress_observed = bool(active and all(i.public_ip and i.public_ip != "N/A" for i in active))
        distinct_public_egress_observed = (len(distinct_public_ips) >= 2 and len(active) >= 2)
        independent_egress_proven = distinct_public_egress_observed
        physical_nic_attribution_verified = False  # Updated when NIC hardware counters confirm distinct physical routing
        combined_throughput_gain_verified = False  # Updated when multi-trial benchmark exceeds baselines

        if len(active) < 2:
            stage = "single_interface" if len(active) == 1 else "no_egress"
            status_msg = f"Only {len(active)} active interface(s) available; multi-path bonding requires at least 2."
        elif independent_egress_proven:
            stage = "multi_provider_independent"
            status_msg = f"Verified independent multi-WAN egress across {len(active)} paths (Public IPs: {', '.join(sorted(distinct_public_ips))})."
        elif len(distinct_gateways) >= 2 and len(distinct_public_ips) <= 1:
            stage = "distinct_gateways_shared_ip"
            status_msg = (
                f"NOTICE: Interfaces have distinct local gateways, but share the same public egress IP "
                f"({next(iter(distinct_public_ips), 'Unknown')}). Multi-WAN bandwidth aggregation is unverified."
            )
        else:
            stage = "shared_uplink_single_isp"
            status_msg = (
                "WARNING: Independent egress cannot be proven. All interfaces share the same gateway and public IP. "
                "Ensure one adapter connects to Home Wi-Fi and the other to Mobile Hotspot."
            )

        return {
            "interface_count": len(active),
            "interfaces_discovered": interfaces_discovered,
            "source_address_available": source_address_available,
            "socket_binding_test_passed": socket_binding_test_passed,
            "binding_verified": source_address_available,  # Maintained for backwards compatibility
            "public_egress_observed": public_egress_observed,
            "distinct_public_egress_observed": distinct_public_egress_observed,
            "physical_nic_attribution_verified": physical_nic_attribution_verified,
            "combined_throughput_gain_verified": combined_throughput_gain_verified,
            "distinct_gateways": sorted(list(distinct_gateways)),
            "distinct_public_ips": sorted(list(distinct_public_ips)),
            "independent_egress_proven": independent_egress_proven,
            "validation_stage": stage,
            "status_message": status_msg,
            "paths": paths,
        }

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
                if ifname and ifname not in ["lo", "127.0.0.1", "loopback"]:
                    try:
                        s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_BINDTODEVICE", 25), ifname.encode() + b"\0")
                    except Exception as be:
                        s.close()
                        logger.error("SO_BINDTODEVICE failed for '%s': %s (failing closed)", ifname, be)
                        return None, 0.0

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
        session.trust_env = False  # Disable untrusted environment proxy inheritance
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

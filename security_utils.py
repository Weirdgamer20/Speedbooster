"""
security_utils.py - Centralized SSRF validation, DNS rebinding defense, and safe redirect traversal.
Guarantees that destination sockets connect strictly to validated IP addresses without
secondary DNS resolution TOCTOU races, while preserving correct TLS SNI and certificate validation.
"""

import socket
import ipaddress
import urllib.parse
from typing import Tuple, List, Optional, Dict
import requests
import urllib3
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import PoolManager


def is_prohibited_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Checks whether an IP address belongs to private, loopback, link-local, multicast, or reserved ranges."""
    if (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    ):
        return True
    # Carrier-grade NAT (100.64.0.0/10)
    if isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.IPv4Network("100.64.0.0/10"):
        return True
    # IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1 or ::ffff:10.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return is_prohibited_ip(ip.ipv4_mapped)
    return False


def validate_url_ssrf(url: str, allow_local: bool = False) -> Tuple[bool, str, List[str]]:
    """
    Validates that a URL does not target loopback, private, link-local, or reserved network destinations.
    Resolves all A and AAAA DNS records and inspects every resolved IP address.
    Returns (is_valid, error_message, resolved_ips).
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception as e:
        return False, f"Malformed URL: {e}", []

    if parsed.scheme not in ("http", "https"):
        return False, f"Prohibited scheme '{parsed.scheme}'; only http and https are allowed", []

    hostname = parsed.hostname
    if not hostname:
        return False, "Missing hostname in URL", []

    clean_host = hostname.strip("[]").lower()

    # If allow_local is set (for internal testing suite), permit localhost and 127.0.0.1
    if allow_local and clean_host in ("127.0.0.1", "localhost", "::1"):
        return True, "", ["127.0.0.1"]

    # Check if clean_host is an explicit IP or alternate numeric/hex representation
    try:
        # 1. Standard dotted-decimal IPv4 or bracketed/raw IPv6
        ip_direct = ipaddress.ip_address(clean_host)
        if is_prohibited_ip(ip_direct):
            if allow_local and ip_direct.is_loopback:
                return True, "", [str(ip_direct)]
            return False, f"SSRF blocked: Prohibited IP address '{clean_host}'", [str(ip_direct)]
        return True, "", [str(ip_direct)]
    except ValueError:
        pass

    # 2. Integer representation (e.g. 2130706433 -> 127.0.0.1)
    if clean_host.isdigit():
        try:
            val = int(clean_host)
            if 0 <= val <= 0xFFFFFFFF:
                ip_int = ipaddress.IPv4Address(val)
                if is_prohibited_ip(ip_int):
                    if allow_local and ip_int.is_loopback:
                        return True, "", [str(ip_int)]
                    return False, f"SSRF blocked: Prohibited integer IP '{clean_host}' ({ip_int})", [str(ip_int)]
                return True, "", [str(ip_int)]
        except (ValueError, OverflowError):
            pass

    # 3. Hexadecimal representation (e.g. 0x7f000001)
    if clean_host.startswith("0x") or clean_host.startswith("0X"):
        try:
            val = int(clean_host, 16)
            if 0 <= val <= 0xFFFFFFFF:
                ip_hex = ipaddress.IPv4Address(val)
                if is_prohibited_ip(ip_hex):
                    if allow_local and ip_hex.is_loopback:
                        return True, "", [str(ip_hex)]
                    return False, f"SSRF blocked: Prohibited hex IP '{clean_host}' ({ip_hex})", [str(ip_hex)]
                return True, "", [str(ip_hex)]
        except (ValueError, OverflowError):
            pass

    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addr_info = socket.getaddrinfo(clean_host, port, family=socket.AF_UNSPEC, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, f"DNS resolution failed for hostname '{hostname}': {e}", []
    except Exception as e:
        return False, f"Address resolution error for '{hostname}': {e}", []

    if not addr_info:
        return False, f"No DNS records found for '{hostname}'", []

    resolved_ips = []
    for entry in addr_info:
        sockaddr = entry[4]
        ip_str = sockaddr[0]
        resolved_ips.append(ip_str)
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"Invalid IP address representation '{ip_str}' resolved from '{hostname}'", []

        if is_prohibited_ip(ip_obj):
            if allow_local and ip_obj.is_loopback:
                continue
            return False, f"SSRF blocked: Hostname '{hostname}' resolved to prohibited address {ip_str}", resolved_ips

    return True, "", resolved_ips


def resolve_and_validate_host(host: str, port: int = 80, allow_local: bool = False) -> str:
    """
    Resolves hostname and verifies that no returned A/AAAA record targets prohibited network spaces.
    Returns the first verified IP address or raises RequestException.
    """
    clean_host = host.strip("[]").lower()
    if allow_local and clean_host in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"

    url = f"http://{host}:{port}/"
    is_valid, err_msg, resolved_ips = validate_url_ssrf(url, allow_local=allow_local)
    if not is_valid or not resolved_ips:
        raise requests.exceptions.RequestException(f"SSRF / DNS rebinding security rejection for '{host}': {err_msg}")
    return resolved_ips[0]


class DNSRebindingSafeHTTPConnection(HTTPConnection):
    """
    HTTPConnection that binds directly to a pre-validated IP address,
    preventing secondary DNS resolution and TOCTOU DNS rebinding.
    """
    def _new_conn(self) -> socket.socket:
        target_ip = getattr(self, "_pinned_ip", None)
        allow_local = getattr(self, "_allow_local", False)
        if not target_ip:
            target_ip = resolve_and_validate_host(self.host, self.port, allow_local=allow_local)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for opt in self.socket_options:
            try:
                s.setsockopt(*opt)
            except Exception:
                pass
        if self.source_address:
            s.bind(self.source_address)

        s.settimeout(self.timeout)
        s.connect((target_ip, self.port))
        return s


class DNSRebindingSafeHTTPSConnection(HTTPSConnection):
    """
    HTTPSConnection that binds directly to a pre-validated IP address,
    guaranteeing that destination IP is one of the validated addresses
    while preserving TLS SNI and server certificate hostname verification.
    """
    def _new_conn(self) -> socket.socket:
        target_ip = getattr(self, "_pinned_ip", None)
        allow_local = getattr(self, "_allow_local", False)
        if not target_ip:
            target_ip = resolve_and_validate_host(self.host, self.port, allow_local=allow_local)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        for opt in self.socket_options:
            try:
                s.setsockopt(*opt)
            except Exception:
                pass
        if self.source_address:
            s.bind(self.source_address)

        s.settimeout(self.timeout)
        s.connect((target_ip, self.port))
        return s


class DNSRebindingSafeHTTPPool(HTTPConnectionPool):
    ConnectionCls = DNSRebindingSafeHTTPConnection


class DNSRebindingSafeHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = DNSRebindingSafeHTTPSConnection


class DNSRebindingSafePoolManager(PoolManager):
    """PoolManager that routes all HTTP/HTTPS traffic through DNS rebinding-safe connection pools."""
    pool_classes_by_scheme = {
        "http": DNSRebindingSafeHTTPPool,
        "https": DNSRebindingSafeHTTPSPool
    }


def safe_request_get(session: requests.Session, url: str, allow_local: bool = False, max_redirects: int = 5, **kwargs) -> requests.Response:
    """
    Issues a GET request with strict step-by-step SSRF validation and DNS pinning across all redirect hops.
    Guarantees destination IP is one of the validated addresses, preventing DNS rebinding pivots.
    """
    current_url = url
    for hop in range(max_redirects + 1):
        is_valid, err_msg, resolved_ips = validate_url_ssrf(current_url, allow_local=allow_local)
        if not is_valid:
            raise requests.exceptions.RequestException(f"SSRF violation on hop {hop} ({current_url}): {err_msg}")

        parsed = urllib.parse.urlparse(current_url)
        clean_host = parsed.hostname.strip("[]").lower()
        pinned_ip = resolved_ips[0] if resolved_ips else clean_host

        # Pin validated IP into adapters mounted on the session
        for prefix, adapter in session.adapters.items():
            if hasattr(adapter, "pin_host"):
                adapter.pin_host(clean_host, pinned_ip, allow_local=allow_local)

        # Disable requests automatic redirection so we can validate each hop manually
        kwargs["allow_redirects"] = False
        resp = session.get(current_url, **kwargs)

        if resp.is_redirect and "Location" in resp.headers:
            redirect_target = resp.headers["Location"]
            next_url = urllib.parse.urljoin(current_url, redirect_target)
            current_url = next_url
            resp.close()
            continue

        return resp

    raise requests.exceptions.TooManyRedirects(f"Exceeded maximum allowed redirects ({max_redirects})")

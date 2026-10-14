"""
security_utils.py - Centralized SSRF validation, DNS rebinding defense, and safe redirect traversal.
Guarantees that destination sockets connect strictly to validated IP addresses without
secondary DNS resolution TOCTOU races, while preserving correct TLS SNI and certificate validation.
"""

import socket
import ipaddress
import urllib.parse
from typing import Tuple, List, Optional, Dict, Union
import requests
import urllib3
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.poolmanager import PoolManager


def is_prohibited_ip(ip: Union[str, ipaddress.IPv4Address, ipaddress.IPv6Address]) -> bool:
    """Checks whether an IP address belongs to private, loopback, link-local, multicast, or reserved ranges."""
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip)
        except ValueError:
            return True
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

    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as e:
        return False, f"Invalid URL port: {e}", []

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


import logging
logger = logging.getLogger("speedbond.security")


class InterfaceBindingError(requests.exceptions.RequestException):
    """Raised when an interface socket binding or connection cannot be fulfilled."""
    pass


def resolve_and_validate_host(host: str, port: int = 80, allow_local: bool = False) -> str:
    """
    Resolves hostname and verifies that no returned A/AAAA record targets prohibited network spaces.
    Returns the first verified IP address or raises RequestException.
    """
    clean_host = host.strip("[]").lower()
    if allow_local and clean_host in ("127.0.0.1", "localhost", "::1"):
        return "127.0.0.1"

    # Handle IP literals before URL construction; bare IPv6 literals must be bracketed
    # inside URLs, but socket connection APIs expect the unbracketed address.
    try:
        literal_ip = ipaddress.ip_address(clean_host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if is_prohibited_ip(literal_ip) and not (allow_local and literal_ip.is_loopback):
            raise requests.exceptions.RequestException(
                f"SSRF / DNS rebinding security rejection for '{host}': prohibited IP address"
            )
        return str(literal_ip)

    url_host = f"[{clean_host}]" if ":" in clean_host else clean_host
    url = f"http://{url_host}:{port}/"
    is_valid, err_msg, resolved_ips = validate_url_ssrf(url, allow_local=allow_local)
    if not is_valid or not resolved_ips:
        raise requests.exceptions.RequestException(f"SSRF / DNS rebinding security rejection for '{host}': {err_msg}")
    return resolved_ips[0]


def _build_and_bind_safe_socket(conn_instance) -> socket.socket:
    """
    Builds, configures, and connects an AF_INET or AF_INET6 socket to the validated destination IP.
    Enforces fail-closed semantics: mandatory interface options (SO_BINDTODEVICE, IP_UNICAST_IF)
    and source-address binding must succeed, or the socket is closed and InterfaceBindingError is raised
    before connect() is attempted.
    """
    target_ip = getattr(conn_instance, "_pinned_ip", None)
    allow_local = getattr(conn_instance, "_allow_local", False)
    if not target_ip:
        target_ip = resolve_and_validate_host(conn_instance.host, conn_instance.port, allow_local=allow_local)
    elif not allow_local and is_prohibited_ip(target_ip):
        raise requests.exceptions.RequestException(
            f"SSRF violation: destination IP {target_ip} is in prohibited address space"
        )

    ip_obj = ipaddress.ip_address(target_ip)
    if ip_obj.version == 6:
        family = socket.AF_INET6
        sockaddr = (target_ip, conn_instance.port, 0, 0)
    else:
        family = socket.AF_INET
        sockaddr = (target_ip, conn_instance.port)

    s = socket.socket(family, socket.SOCK_STREAM)
    applied_mandatory_opts = []

    SO_BINDTODEVICE = getattr(socket, "SO_BINDTODEVICE", 25)
    IP_UNICAST_IF = getattr(socket, "IP_UNICAST_IF", 31)

    for opt in conn_instance.socket_options:
        level, optname, val = opt[0], opt[1], opt[2]
        is_mandatory = (optname == SO_BINDTODEVICE or optname == IP_UNICAST_IF)
        try:
            s.setsockopt(*opt)
            if is_mandatory:
                applied_mandatory_opts.append(optname)
        except Exception as e:
            if is_mandatory:
                s.close()
                raise InterfaceBindingError(
                    f"Mandatory interface-binding socket option failed ({optname}): {e}"
                ) from e
            # Optional socket options (e.g. TCP_NODELAY) are non-fatal
            logger.debug("Optional socket option (%d) failed: %s", optname, e)

    if conn_instance.source_address:
        src_ip_str = conn_instance.source_address[0]
        src_port = conn_instance.source_address[1] if len(conn_instance.source_address) > 1 else 0
        if src_ip_str:
            try:
                src_ip_obj = ipaddress.ip_address(src_ip_str)
                if src_ip_obj.version != ip_obj.version:
                    s.close()
                    raise InterfaceBindingError(
                        f"Mandatory source-address binding family mismatch: source {src_ip_str} "
                        f"(IPv{src_ip_obj.version}) vs destination {target_ip} (IPv{ip_obj.version})"
                    )
                if ip_obj.version == 6:
                    s.bind((src_ip_str, src_port, 0, 0))
                else:
                    s.bind((src_ip_str, src_port))
                applied_mandatory_opts.append("source_address")
            except Exception as e:
                s.close()
                if isinstance(e, InterfaceBindingError):
                    raise
                raise InterfaceBindingError(
                    f"Mandatory source-address binding failed on {conn_instance.source_address}: {e}"
                ) from e

    s.settimeout(conn_instance.timeout)
    try:
        s.connect(sockaddr)
    except Exception as e:
        s.close()
        raise InterfaceBindingError(
            f"Failed to connect to destination {sockaddr}: {e}"
        ) from e

    # Record actual telemetry on connection instance
    try:
        sock_name = s.getsockname()
        if sock_name and len(sock_name) >= 2:
            conn_instance._actual_local_ip, conn_instance._actual_local_port = sock_name[0], sock_name[1]
        else:
            conn_instance._actual_local_ip, conn_instance._actual_local_port = None, None
    except Exception:
        conn_instance._actual_local_ip, conn_instance._actual_local_port = None, None

    try:
        peer_name = s.getpeername()
        if peer_name and len(peer_name) >= 2:
            conn_instance._actual_peer_ip, conn_instance._actual_peer_port = peer_name[0], peer_name[1]
        else:
            conn_instance._actual_peer_ip, conn_instance._actual_peer_port = None, None
    except Exception:
        conn_instance._actual_peer_ip, conn_instance._actual_peer_port = None, None

    conn_instance._interface_options_applied = applied_mandatory_opts
    conn_instance._actual_socket = s

    # Propagate telemetry to pool and adapter
    pool = getattr(conn_instance, "_pool", None)
    if pool is not None:
        telem = {
            "local_ip": conn_instance._actual_local_ip,
            "local_port": conn_instance._actual_local_port,
            "peer_ip": conn_instance._actual_peer_ip,
            "peer_port": conn_instance._actual_peer_port,
            "options": conn_instance._interface_options_applied,
            "interface_options_applied": conn_instance._interface_options_applied,
        }
        pool.last_telemetry = telem
        adapter = getattr(pool, "_adapter", None)
        if adapter is not None:
            adapter.last_telemetry = telem

    return s


class DNSRebindingSafeHTTPConnection(HTTPConnection):
    """
    HTTPConnection that binds directly to a pre-validated IP address (IPv4 or IPv6),
    preventing secondary DNS resolution and TOCTOU DNS rebinding.
    """
    def _new_conn(self) -> socket.socket:
        return _build_and_bind_safe_socket(self)


class DNSRebindingSafeHTTPSConnection(HTTPSConnection):
    """
    HTTPSConnection that binds directly to a pre-validated IP address (IPv4 or IPv6),
    guaranteeing destination IP is pre-validated while preserving TLS SNI and server
    certificate hostname verification.
    """
    def _new_conn(self) -> socket.socket:
        return _build_and_bind_safe_socket(self)


class DNSRebindingSafeHTTPPool(HTTPConnectionPool):
    ConnectionCls = DNSRebindingSafeHTTPConnection

    def _new_conn(self):
        conn = super()._new_conn()
        conn._pinned_ip = getattr(self, "_pinned_ip", None)
        conn._allow_local = getattr(self, "_allow_local", False)
        conn._pool = self
        return conn


class DNSRebindingSafeHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = DNSRebindingSafeHTTPSConnection

    def _new_conn(self):
        conn = super()._new_conn()
        conn._pinned_ip = getattr(self, "_pinned_ip", None)
        conn._allow_local = getattr(self, "_allow_local", False)
        conn._pool = self
        return conn


class DNSRebindingSafePoolManager(PoolManager):
    """PoolManager that routes all HTTP/HTTPS traffic through DNS rebinding-safe connection pools."""
    pool_classes_by_scheme = {
        "http": DNSRebindingSafeHTTPPool,
        "https": DNSRebindingSafeHTTPSPool
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pool_classes_by_scheme = {
            "http": DNSRebindingSafeHTTPPool,
            "https": DNSRebindingSafeHTTPSPool
        }

    def _new_pool(self, scheme, host, port, request_context=None):
        pool = super()._new_pool(scheme, host, port, request_context=request_context)
        pool._adapter = getattr(self, "_adapter", None)
        return pool


def safe_request_get(
    session: requests.Session,
    url: str,
    allow_local: bool = False,
    max_redirects: int = 5,
    trust_env: bool = False,
    **kwargs
) -> requests.Response:
    """
    Issues a GET request with strict step-by-step SSRF validation and DNS pinning across all redirect hops.
    Guarantees destination IP is one of the validated addresses, preventing DNS rebinding pivots.
    Disables environment proxy inheritance (trust_env=False) by default to prevent proxy hijacking.
    """
    # Enforce safe proxy policy: disable unverified environment proxy inheritance
    session.trust_env = trust_env

    # If explicit proxies are supplied in kwargs or session, validate proxy destination against SSRF
    proxies = kwargs.get("proxies") or session.proxies or {}
    for scheme, proxy_url in proxies.items():
        if proxy_url:
            p_valid, p_err, _ = validate_url_ssrf(proxy_url, allow_local=False)
            if not p_valid:
                raise requests.exceptions.RequestException(
                    f"SSRF violation: Prohibited proxy configuration for {scheme} ({proxy_url}): {p_err}"
                )

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

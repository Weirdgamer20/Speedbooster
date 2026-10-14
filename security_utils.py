"""
security_utils.py - Centralized SSRF validation and safe redirect-handling utilities.
Validates all IPv4/IPv6 addresses against loopback, private, link-local, multicast,
and reserved ranges via DNS resolution, and enforces safe redirect traversal.
"""

import socket
import ipaddress
import urllib.parse
from typing import Tuple, List, Optional
import requests

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


def safe_request_get(session: requests.Session, url: str, allow_local: bool = False, max_redirects: int = 5, **kwargs) -> requests.Response:
    """
    Issues a GET request with strict step-by-step SSRF validation across all redirect hops.
    Rejects any redirect attempting to pivot to private/loopback/link-local networks.
    """
    current_url = url
    for hop in range(max_redirects + 1):
        is_valid, err_msg, _ = validate_url_ssrf(current_url, allow_local=allow_local)
        if not is_valid:
            raise requests.exceptions.RequestException(f"SSRF violation on hop {hop} ({current_url}): {err_msg}")

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

"""
proxy.py - High-Performance Multi-WAN Load Balancing HTTP & HTTPS CONNECT Proxy.
Provides per-connection round-robin balancing across validated active physical interfaces.
NOTE: Each independent client TCP connection routes through one selected interface.
It does not stripe packets of a single TCP connection across multiple links without a remote relay.
"""

import socket
import threading
from urllib.parse import urlsplit
from typing import Optional, Tuple
from network_manager import NetworkManager

BUFFER_SIZE = 65536  # 64KB per read
SOCKET_BUFFER = 1048576  # 1MB kernel socket buffer

class MultiWANProxyServer:
    def __init__(self, net_mgr: NetworkManager, host: str = "127.0.0.1", port: int = 8080):
        self.net_mgr = net_mgr
        self.host = host
        self.port = port
        self.server_sock = None
        self._running = False
        self._thread = None
        self._iface_idx = 0
        self._lock = threading.Lock()

    def _next_interface(self) -> Optional[str]:
        """Picks the next active physical interface in round-robin order."""
        with self._lock:
            active = [i.name for i in self.net_mgr.interfaces.values() if i.is_active]
            if not active:
                return None
            self._iface_idx = (self._iface_idx + 1) % len(active)
            return active[self._iface_idx]

    def start(self):
        self._running = True
        self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.server_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except Exception:
            pass
        self.server_sock.bind((self.host, self.port))
        self.server_sock.listen(256)

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        print(f"[Proxy] Multi-WAN Proxy listening on {self.host}:{self.port} (Per-Connection Distribution)")

    def stop(self):
        self._running = False
        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass

    def _accept_loop(self):
        while self._running:
            try:
                client_sock, _ = self.server_sock.accept()
                t = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
                t.start()
            except Exception:
                if not self._running:
                    break

    def _optimize_socket(self, s: socket.socket):
        try:
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER)
        except Exception:
            pass

    def _read_headers(self, sock: socket.socket, max_bytes: int = 32768) -> Tuple[bytes, bytes]:
        """Reads until CRLF-CRLF delimiter across multiple TCP packets with a bounded buffer."""
        buf = b""
        sock.settimeout(10.0)
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(2048)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                raise ValueError("Request headers exceeded maximum allowed buffer")

        if b"\r\n\r\n" in buf:
            headers, remainder = buf.split(b"\r\n\r\n", 1)
            return headers, remainder
        return buf, b""

    def _parse_host_port(self, target: str, default_port: int = 80) -> Tuple[str, int]:
        """IPv6-safe host and port parsing."""
        if target.startswith("[") and "]" in target:
            host = target[1:target.index("]")]
            rest = target[target.index("]") + 1:]
            port = int(rest.lstrip(":")) if ":" in rest else default_port
            return host, port
        elif ":" in target:
            parts = target.rsplit(":", 1)
            return parts[0], int(parts[1])
        else:
            return target, default_port

    def _handle_client(self, client_sock: socket.socket):
        self._optimize_socket(client_sock)
        try:
            raw_headers, body_remainder = self._read_headers(client_sock)
            if not raw_headers:
                client_sock.close()
                return

            first_line = raw_headers.split(b"\r\n")[0].decode("iso-8859-1", errors="ignore")
            parts = first_line.split()
            if len(parts) < 2:
                client_sock.close()
                return

            method, target = parts[0], parts[1]
            iface = self._next_interface()
            if not iface:
                client_sock.sendall(b"HTTP/1.1 503 Service Unavailable\r\n\r\nNo active physical network interfaces available.\r\n")
                client_sock.close()
                return

            ifstat = self.net_mgr.interfaces.get(iface)

            if method.upper() == "CONNECT":
                # HTTPS Tunneling
                host, port = self._parse_host_port(target, default_port=443)
                try:
                    remote_sock = self.net_mgr.create_bound_socket(iface, host, port, timeout=12.0, use_ssl=False)
                    self._optimize_socket(remote_sock)
                    client_sock.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                    if body_remainder:
                        remote_sock.sendall(body_remainder)
                    self._bi_directional_pipe(client_sock, remote_sock, ifstat)
                except Exception as e:
                    try:
                        client_sock.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\nConnection to {host}:{port} failed via {iface}: {e}\r\n".encode())
                    except Exception:
                        pass
                    client_sock.close()
            else:
                # Plain HTTP Proxying
                parsed = urlsplit(target)
                host = parsed.hostname
                port = parsed.port or 80

                if not host:
                    # Look in Host header
                    for line in raw_headers.split(b"\r\n")[1:]:
                        if line.lower().startswith(b"host:"):
                            host_hdr = line.split(b":", 1)[1].strip().decode("iso-8859-1")
                            host, port = self._parse_host_port(host_hdr, default_port=80)
                            break

                if not host:
                    client_sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\nMissing Host header\r\n")
                    client_sock.close()
                    return

                try:
                    remote_sock = self.net_mgr.create_bound_socket(iface, host, port, timeout=12.0, use_ssl=False)
                    self._optimize_socket(remote_sock)

                    # Reconstruct request: convert absolute URI to origin-form path
                    path = parsed.path or "/"
                    if parsed.query:
                        path += "?" + parsed.query

                    # Rebuild sanitized headers
                    lines = raw_headers.split(b"\r\n")
                    protocol = parts[2] if len(parts) >= 3 else "HTTP/1.1"
                    req_lines = [f"{method} {path} {protocol}".encode()]

                    for l in lines[1:]:
                        if not l:
                            continue
                        # Strip proxy-specific headers
                        lower = l.lower()
                        if lower.startswith(b"proxy-connection:") or lower.startswith(b"proxy-authorization:"):
                            continue
                        req_lines.append(l)

                    forward_req = b"\r\n".join(req_lines) + b"\r\n\r\n" + body_remainder
                    remote_sock.sendall(forward_req)
                    self._bi_directional_pipe(client_sock, remote_sock, ifstat)
                except Exception as e:
                    try:
                        client_sock.sendall(f"HTTP/1.1 502 Bad Gateway\r\n\r\nForwarding to {host}:{port} failed: {e}\r\n".encode())
                    except Exception:
                        pass
                    client_sock.close()

        except Exception:
            try:
                client_sock.close()
            except Exception:
                pass

    def _bi_directional_pipe(self, client_sock: socket.socket, remote_sock: socket.socket, ifstat):
        client_sock.settimeout(None)
        remote_sock.settimeout(None)

        def forward(src: socket.socket, dst: socket.socket, is_download: bool):
            try:
                while self._running:
                    data = src.recv(BUFFER_SIZE)
                    if not data:
                        break
                    dst.sendall(data)
                    if is_download and ifstat:
                        ifstat.update_bytes(len(data))
            except Exception:
                pass
            finally:
                try:
                    dst.shutdown(socket.SHUT_WR)
                except Exception:
                    pass

        t1 = threading.Thread(target=forward, args=(client_sock, remote_sock, False), daemon=True)
        t2 = threading.Thread(target=forward, args=(remote_sock, client_sock, True), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        try:
            client_sock.close()
        except Exception:
            pass
        try:
            remote_sock.close()
        except Exception:
            pass

"""
web_server.py - Safe REST API and Dashboard Web Server.
Binds to 127.0.0.1 by default, enforces strict path traversal and URL validation,
handles proxy=None gracefully, and avoids NameErrors during interface refresh.
"""

import os
import json
import uuid
import socket
import ipaddress
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse
from typing import Dict, Optional, Tuple, List
from network_manager import NetworkManager
from downloader import BondedDownloadTask
from proxy import MultiWANProxyServer
from security_utils import validate_url_ssrf, is_prohibited_ip

class SpeedBondServer:
    def __init__(self, net_mgr: NetworkManager, proxy: Optional[MultiWANProxyServer] = None, host: str = "127.0.0.1", port: int = 5000):
        self.net_mgr = net_mgr
        self.proxy = proxy
        self.host = host
        self.port = port
        self.downloads: Dict[str, BondedDownloadTask] = {}
        self.static_dir = os.path.join(os.path.dirname(__file__), "static")

        # Dedicated safe download directory
        self.download_dir = os.path.abspath(
            os.environ.get("SPEEDBOND_DOWNLOAD_DIR", 
                           "/mnt/c/Users/thega/Downloads" if os.path.isdir("/mnt/c/Users/thega/Downloads") else "./downloads")
        )
        os.makedirs(self.download_dir, exist_ok=True)

    def create_handler(sb_self):
        class SpeedBondRequestHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=sb_self.static_dir, **kwargs)

            def do_GET(self):
                parsed = urlparse(self.path)
                if parsed.path == "/api/stats":
                    self._send_json(sb_self.net_mgr.get_stats())
                elif parsed.path == "/api/validate":
                    self._send_json(sb_self.net_mgr.validate_paths())
                elif parsed.path == "/api/downloads":
                    res = [d.get_progress() for d in sb_self.downloads.values()]
                    self._send_json(res)
                elif parsed.path == "/api/proxy":
                    if sb_self.proxy:
                        self._send_json({"running": sb_self.proxy._running, "port": sb_self.proxy.port})
                    else:
                        self._send_json({"running": False, "port": None})
                else:
                    if parsed.path in ["", "/", "/index.html"]:
                        self.path = "/index.html"
                    return super().do_GET()

            def do_POST(self):
                parsed = urlparse(self.path)
                try:
                    content_len = int(self.headers.get("Content-Length", 0))
                    if content_len > 65536:
                        self._send_json({"error": "Payload too large"}, status=413)
                        return
                    body = self.rfile.read(content_len).decode("utf-8") if content_len > 0 else "{}"
                    data = json.loads(body) if body else {}
                except Exception as e:
                    self._send_json({"error": f"Invalid JSON payload: {e}"}, status=400)
                    return

                if parsed.path == "/api/download":
                    # Check concurrency limit (max 3 active tasks)
                    active_tasks = [d for d in sb_self.downloads.values() if d.status in ("probing", "running")]
                    if len(active_tasks) >= 3:
                        self._send_json({"error": "Concurrent download limit reached (maximum 3 active downloads)"}, status=429)
                        return

                    url = (data.get("url") or "").strip()
                    if not url:
                        self._send_json({"error": "URL is required"}, status=400)
                        return

                    # 1. Comprehensive SSRF protection: resolves all A/AAAA records and inspects IP targets
                    allow_local = os.environ.get("ALLOW_LOCAL_TEST_DOWNLOADS") == "1"
                    is_valid, ssrf_err, _ = validate_url_ssrf(url, allow_local=allow_local)
                    if not is_valid:
                        self._send_json({"error": ssrf_err}, status=403)
                        return

                    # 2. Prevent path traversal in target filename using canonical path resolution
                    parsed_url = urlparse(url)
                    raw_filename = (data.get("filename") or os.path.basename(parsed_url.path) or "downloaded_file.bin").strip()
                    candidate_path = os.path.join(sb_self.download_dir, raw_filename)
                    resolved_dest = os.path.realpath(candidate_path)
                    resolved_dir = os.path.realpath(sb_self.download_dir)

                    # Ensure resolved canonical path is strictly inside download_dir
                    if not (resolved_dest == resolved_dir or resolved_dest.startswith(resolved_dir + os.sep)):
                        self._send_json({"error": "Path traversal detected: destination outside download directory"}, status=403)
                        return

                    task_id = str(uuid.uuid4())[:8]
                    task = BondedDownloadTask(task_id, url, resolved_dest, sb_self.net_mgr, allow_local=allow_local)
                    sb_self.downloads[task_id] = task
                    success = task.start()
                    self._send_json({"success": success, "task": task.get_progress()})

                elif parsed.path == "/api/stop":
                    task_id = data.get("task_id")
                    if task_id in sb_self.downloads:
                        sb_self.downloads[task_id].cancel()
                        self._send_json({"success": True})
                    else:
                        self._send_json({"error": "Task not found"}, status=404)

                elif parsed.path == "/api/refresh":
                    # Correctly launch background thread without NameError
                    t = threading.Thread(target=sb_self.net_mgr.discover_interfaces, daemon=True)
                    t.start()
                    self._send_json({"success": True, "message": "Interface discovery initiated"})

                else:
                    self._send_json({"error": "Not Found"}, status=404)

            def _send_json(self, payload, status=200):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                pass  # Suppress console log spam

        return SpeedBondRequestHandler

    def start_server(self):
        self.httpd = ThreadingHTTPServer((self.host, self.port), self.create_handler())
        self.actual_port = self.httpd.server_port
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        return self.actual_port

    def stop_server(self):
        if hasattr(self, "httpd"):
            self.httpd.shutdown()
            try:
                self.httpd.server_close()
            except Exception:
                pass
        for d in self.downloads.values():
            try:
                d.cancel()
            except Exception:
                pass

    def start(self):
        self.httpd = ThreadingHTTPServer((self.host, self.port), self.create_handler())
        self.actual_port = self.httpd.server_port
        print(f"[Dashboard] SpeedBond Server listening on http://{self.host}:{self.actual_port}")
        self.httpd.serve_forever()

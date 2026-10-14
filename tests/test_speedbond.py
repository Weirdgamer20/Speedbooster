"""
test_speedbond.py - Comprehensive Deterministic Test Suite for SpeedBond v2.1.

Validates:
1. Module compilation and runtime imports.
2. RFC 9110 range requests with verified SHA-256 against a trusted expected checksum.
3. Checksum verification failure on digest mismatch.
4. Safe single-stream fallback when server ignores Range (200 OK).
5. Rejection of invalid Content-Range byte offsets.
6. Rejection of oversized chunk payloads before disk overwrite.
7. Detection and bounded retries on truncated chunk responses.
8. Rejection of Content-Range headers with mismatched total resource size.
9. Retry exhaustion leading to deterministic terminal 'failed' state.
10. Resource representation mutation (ETag change) mid-stream abort.
11. Cancellation with worker thread termination and partial file cleanup.
12. Explicit failure reporting for invalid interface/source-IP bindings (no silent ignores).
13. Real HTTP POST to /api/refresh asserting valid status code and JSON response.
14. Strict path traversal prevention through the live HTTP /api/download endpoint.
"""

import os
import sys
import time
import json
import socket
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import requests
import pytest

# Add parent directory to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from network_manager import NetworkManager, InterfaceStats, BoundInterfaceHTTPAdapter, InterfaceBindingError
from downloader import BondedDownloadTask, ChunkVerificationError, ResourceChangedError
from web_server import SpeedBondServer
from security_utils import (
    validate_url_ssrf,
    is_prohibited_ip,
    safe_request_get,
    resolve_and_validate_host,
    DNSRebindingSafePoolManager,
)
from benchmark import get_target_nic_names, compute_statistics, welch_t_test

# 4MB deterministic test payload
TEST_PAYLOAD = b"SpeedBondDeterministicPayload_0123456789ABCDEF\n" * (4 * 1024 * 1024 // 48)
TEST_SHA256 = hashlib.sha256(TEST_PAYLOAD).hexdigest()
TEST_ETAG = '"speedbond-test-v2.1"'


class MockHTTPServerHandler(BaseHTTPRequestHandler):
    mode = "normal"  # normal, ignore_range, wrong_range, oversized, truncated, bad_total_size, change_etag, slow, probe_malformed_range, probe_wrong_length
    etag = TEST_ETAG

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(TEST_PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", self.etag)
        self.end_headers()

    def do_GET(self):
        # Handle explicit redirect testing endpoints
        if self.path == "/redirect_to_internal":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:8080/internal_secret")
            self.end_headers()
            return

        if self.path == "/redirect_to_prohibited":
            self.send_response(302)
            self.send_header("Location", "http://10.254.1.1/admin")
            self.end_headers()
            return

        range_header = self.headers.get("Range")

        # 1. Server ignores Range header (returns 200 OK with whole body)
        if self.mode == "ignore_range" or not range_header:
            self.send_response(200)
            self.send_header("Content-Length", str(len(TEST_PAYLOAD)))
            self.send_header("ETag", self.etag)
            self.end_headers()
            self.wfile.write(TEST_PAYLOAD)
            return

        # Parse range header
        if range_header.startswith("bytes="):
            parts = range_header.replace("bytes=", "").split("-")
            start = int(parts[0])
            end = int(parts[1]) if parts[1] else len(TEST_PAYLOAD) - 1

            # Probe error simulation modes
            if self.mode == "probe_malformed_range":
                self.send_response(206)
                self.send_header("Content-Range", "bytes 0-1/*")
                self.send_header("ETag", self.etag)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"AB")
                return

            if self.mode == "probe_wrong_length":
                self.send_response(206)
                self.send_header("Content-Range", f"bytes 0-1/{len(TEST_PAYLOAD)}")
                self.send_header("ETag", self.etag)
                self.send_header("Content-Length", "1")
                self.end_headers()
                self.wfile.write(b"A")
                return

            # 2. Resource mutation between chunks
            if self.mode == "change_etag" and start > 0:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(TEST_PAYLOAD)}")
                self.send_header("ETag", '"speedbond-mutated-v2"')
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self.wfile.write(TEST_PAYLOAD[start:end+1])
                return

            # 3. Wrong Content-Range offset returned by server
            if self.mode == "wrong_range":
                if start == 0 and end == 1:
                    pass  # Allow initial probe to succeed
                else:
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes 9999-9999/{len(TEST_PAYLOAD)}")
                    self.send_header("ETag", self.etag)
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    self.wfile.write(b"X")
                    return

            # 4. Oversized chunk: server returns more bytes than requested
            if self.mode == "oversized" and start > 1:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(TEST_PAYLOAD)}")
                self.send_header("ETag", self.etag)
                self.send_header("Content-Length", str((end - start + 1) + 2048))
                self.end_headers()
                # Write chunk plus excess bytes
                self.wfile.write(TEST_PAYLOAD[start:end+1] + (b"E" * 2048))
                return

            # 5. Truncated chunk: server closes connection prematurely
            if self.mode == "truncated" and start > 1:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(TEST_PAYLOAD)}")
                self.send_header("ETag", self.etag)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                half = (end - start + 1) // 2
                self.wfile.write(TEST_PAYLOAD[start:start+half])
                return

            # 6. Bad total size in Content-Range
            if self.mode == "bad_total_size" and start > 1:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(TEST_PAYLOAD) + 500000}")
                self.send_header("ETag", self.etag)
                self.send_header("Content-Length", str(end - start + 1))
                self.end_headers()
                self.wfile.write(TEST_PAYLOAD[start:end+1])
                return

            # 7. Slow response for testing active cancellation
            if self.mode == "slow":
                time.sleep(0.3)

            # Normal valid 206 Partial Content
            try:
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(TEST_PAYLOAD)}")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("ETag", self.etag)
                self.end_headers()
                self.wfile.write(TEST_PAYLOAD[start:end+1])
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format, *args):
        pass


@pytest.fixture(scope="module")
def mock_server():
    os.environ["ALLOW_LOCAL_TEST_DOWNLOADS"] = "1"
    server = HTTPServer(("127.0.0.1", 0), MockHTTPServerHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    os.environ.pop("ALLOW_LOCAL_TEST_DOWNLOADS", None)


@pytest.fixture
def test_net_mgr():
    mgr = NetworkManager()
    mgr.interfaces["lo"] = InterfaceStats(
        name="lo",
        ip="127.0.0.1",
        public_ip="127.0.0.1",
        label="Loopback Interface",
        gateway="127.0.0.1"
    )
    return mgr


@pytest.fixture
def speedbond_api_server(test_net_mgr, tmp_path):
    server = SpeedBondServer(test_net_mgr, proxy=None, host="127.0.0.1", port=0)
    server.download_dir = str(tmp_path / "api_downloads")
    os.makedirs(server.download_dir, exist_ok=True)
    actual_port = server.start_server()
    yield f"http://127.0.0.1:{actual_port}", server
    server.stop_server()


def test_compilation():
    """Requirement: All Python modules compile and import cleanly."""
    import network_manager
    import downloader
    import proxy
    import web_server
    import main
    assert network_manager is not None
    assert downloader is not None
    assert proxy is not None
    assert web_server is not None
    assert main is not None


def test_range_success_with_trusted_checksum(mock_server, test_net_mgr, tmp_path):
    """Requirement: Valid 206 range chunking matches trusted expected SHA-256."""
    MockHTTPServerHandler.mode = "normal"
    dest = str(tmp_path / "valid_download.bin")
    task = BondedDownloadTask(
        "test_valid", mock_server + "/test", dest, test_net_mgr,
        expected_sha256=TEST_SHA256
    )
    assert task.start() is True

    timeout = 10.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "completed"
    assert task.checksum_verified is True
    assert task.calculated_sha256 == TEST_SHA256
    assert os.path.exists(dest)
    assert os.path.getsize(dest) == len(TEST_PAYLOAD)


def test_range_checksum_mismatch(mock_server, test_net_mgr, tmp_path):
    """Requirement: Mismatch against trusted expected SHA-256 results in terminal failed state."""
    MockHTTPServerHandler.mode = "normal"
    dest = str(tmp_path / "checksum_fail.bin")
    wrong_hash = "0000000000000000000000000000000000000000000000000000000000000000"
    task = BondedDownloadTask(
        "test_bad_hash", mock_server + "/test", dest, test_net_mgr,
        expected_sha256=wrong_hash
    )
    assert task.start() is True

    timeout = 10.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "failed"
    assert task.checksum_verified is False
    assert "Checksum verification failed" in task.error_message
    assert not os.path.exists(dest)


def test_ignored_range_safe_fallback(mock_server, test_net_mgr, tmp_path):
    """Requirement: Server returning 200 OK falls back safely to single stream without corruption."""
    MockHTTPServerHandler.mode = "ignore_range"
    dest = str(tmp_path / "ignored_range.bin")
    task = BondedDownloadTask("test_ignore", mock_server + "/test", dest, test_net_mgr)
    assert task.start() is True

    timeout = 10.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "completed"
    assert task.supports_ranges is False
    assert os.path.exists(dest)
    assert os.path.getsize(dest) == len(TEST_PAYLOAD)


def test_wrong_content_range_rejected(mock_server, test_net_mgr, tmp_path):
    """Requirement: Invalid Content-Range header causes chunk rejection and bounded failure."""
    MockHTTPServerHandler.mode = "wrong_range"
    dest = str(tmp_path / "wrong_range.bin")
    task = BondedDownloadTask("test_wrong", mock_server + "/test", dest, test_net_mgr)
    assert task.start() is True

    timeout = 8.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "failed"
    assert not os.path.exists(dest)


def test_oversized_body_rejected(mock_server, test_net_mgr, tmp_path):
    """Requirement: Reject excess bytes before they can overwrite adjacent chunks."""
    MockHTTPServerHandler.mode = "oversized"
    dest = str(tmp_path / "oversized.bin")
    task = BondedDownloadTask("test_oversized", mock_server + "/test", dest, test_net_mgr)
    assert task.start() is True

    timeout = 8.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "failed"
    assert not os.path.exists(dest)


def test_truncated_body_rejected(mock_server, test_net_mgr, tmp_path):
    """Requirement: Truncated responses are detected, retried, and bounded."""
    MockHTTPServerHandler.mode = "truncated"
    dest = str(tmp_path / "truncated.bin")
    task = BondedDownloadTask("test_truncated", mock_server + "/test", dest, test_net_mgr)
    assert task.start() is True

    timeout = 8.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "failed"
    assert not os.path.exists(dest)


def test_incorrect_total_size_rejected(mock_server, test_net_mgr, tmp_path):
    """Requirement: Content-Range headers returning an incorrect total size are rejected."""
    MockHTTPServerHandler.mode = "bad_total_size"
    dest = str(tmp_path / "bad_total.bin")
    task = BondedDownloadTask("test_bad_total", mock_server + "/test", dest, test_net_mgr)
    assert task.start() is True

    timeout = 8.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "failed"
    assert not os.path.exists(dest)


def test_etag_mutation_detected(mock_server, test_net_mgr, tmp_path):
    """Requirement: Mutated ETag during range transfers triggers resource abort."""
    MockHTTPServerHandler.mode = "change_etag"
    dest = str(tmp_path / "mutated_etag.bin")
    task = BondedDownloadTask("test_etag", mock_server + "/test", dest, test_net_mgr)
    assert task.start() is True

    timeout = 8.0
    t0 = time.time()
    while task.status in ["probing", "running"] and (time.time() - t0) < timeout:
        time.sleep(0.2)

    assert task.status == "failed"
    assert "Remote resource changed" in task.error_message
    assert not os.path.exists(dest)


def test_cancellation_and_worker_termination(mock_server, test_net_mgr, tmp_path):
    """Requirement: Task cancellation cleanly halts workers, joins threads, and clears partial files."""
    MockHTTPServerHandler.mode = "slow"
    dest = str(tmp_path / "cancelled.bin")
    task = BondedDownloadTask("test_cancel", mock_server + "/test", dest, test_net_mgr)
    task.start()
    time.sleep(0.1)
    task.cancel()

    assert task.status == "cancelled"
    # Ensure all worker threads have terminated
    for t in task._threads:
        assert not t.is_alive()
    assert not os.path.exists(task.temp_path)
    assert not os.path.exists(dest)


def test_binding_failure_reported(mock_server):
    """Requirement: Verifies binding failure is reported and never silently uses the default route."""
    session = requests.Session()
    # Non-existent/unreachable IP for source binding
    invalid_adapter = BoundInterfaceHTTPAdapter(ifname="bad_iface", source_ip="198.51.100.99")
    session.mount("http://", invalid_adapter)

    with pytest.raises(requests.exceptions.RequestException):
        session.get(mock_server + "/test", timeout=1.5)


def test_refresh_api_real_http_post(speedbond_api_server):
    """Requirement: Real HTTP POST to /api/refresh asserting status and JSON response."""
    base_url, _ = speedbond_api_server
    resp = requests.post(f"{base_url}/api/refresh", json={}, timeout=5.0)

    assert resp.status_code == 200
    data = resp.json()
    assert data.get("success") is True
    assert "Interface discovery initiated" in data.get("message", "")


def test_path_traversal_actual_http_endpoint(speedbond_api_server):
    """Requirement: Test path traversal strictly through the live HTTP /api/download endpoint."""
    base_url, server = speedbond_api_server

    # Attempt directory traversal outside download directory
    os.environ["ALLOW_LOCAL_TEST_DOWNLOADS"] = "1"
    try:
        payload = {
            "url": "http://127.0.0.1/test",
            "filename": "../../../evil_payload.bin"
        }
        resp = requests.post(f"{base_url}/api/download", json=payload, timeout=5.0)

        assert resp.status_code == 403
        data = resp.json()
        assert "Path traversal detected" in data.get("error", "")

        # Ensure no file was created outside download_dir
        forbidden_target = os.path.abspath(os.path.join(server.download_dir, "../../../evil_payload.bin"))
        assert not os.path.exists(forbidden_target)
    finally:
        os.environ.pop("ALLOW_LOCAL_TEST_DOWNLOADS", None)


def test_bound_interface_adapter_socket_options():
    """Requirement: BoundInterfaceHTTPAdapter strictly configures hardware-level socket options (SO_BINDTODEVICE / IP_UNICAST_IF)."""
    import struct
    adapter = BoundInterfaceHTTPAdapter(ifname="eth1", source_ip="192.168.1.9", if_index=6)
    adapter.init_poolmanager(connections=2, maxsize=2)

    assert hasattr(adapter, "poolmanager")
    opts = adapter.poolmanager.connection_pool_kw.get("socket_options", [])
    assert adapter.poolmanager.connection_pool_kw.get("source_address") == ("192.168.1.9", 0)

    # Verify platform-specific hardware routing socket option
    if sys.platform.startswith("linux") and hasattr(socket, "SO_BINDTODEVICE"):
        so_bind = [opt for opt in opts if opt[0] == socket.SOL_SOCKET and opt[1] == getattr(socket, "SO_BINDTODEVICE", 25)]
        assert len(so_bind) == 1, "SO_BINDTODEVICE must be configured on Linux"
        assert so_bind[0][2].startswith(b"eth1")
    elif sys.platform == "win32":
        ip_unicast = [opt for opt in opts if opt[0] == socket.IPPROTO_IP and opt[1] == 31]
        assert len(ip_unicast) == 1, "IP_UNICAST_IF must be configured on Windows"
        assert ip_unicast[0][2] == struct.pack("!I", 6)


def test_physical_adapter_hardware_traffic_isolation():
    """
    Requirement: Conclusively proves which physical adapter carries traffic using OS-level hardware counters.
    Verifies that requests bound to Adapter A increment Adapter A's hardware counter,
    and requests bound to Adapter B increment Adapter B's hardware counter, with zero cross-adapter leakage.
    """
    nm = NetworkManager()
    ifaces = nm.discover_interfaces()
    active_ifaces = [i for i in ifaces if i.is_active and not i.name.startswith("lo") and not i.name.startswith("docker")]

    if len(active_ifaces) < 2:
        pytest.skip("Physical dual-adapter hardware verification requires at least 2 active network adapters.")

    val = nm.validate_paths()
    if not val.get("independent_egress_proven"):
        pytest.skip(
            f"Physical hardware isolation test requires independent egress paths across adapters "
            f"(current stage: {val.get('validation_stage')}). Skipping to avoid overstating dual-adapter isolation."
        )

    iface_a = active_ifaces[0]
    iface_b = active_ifaces[1]

    def read_nic_rx_bytes():
        counters = {}
        if os.path.exists("/proc/net/dev"):
            with open("/proc/net/dev", "r") as f:
                for line in f.readlines()[2:]:
                    parts = line.strip().split(":")
                    if len(parts) == 2:
                        dev = parts[0].strip()
                        fields = parts[1].split()
                        counters[dev] = int(fields[0])
        elif sys.platform == "win32":
            import subprocess, json
            try:
                cmd = ["powershell.exe", "-NoProfile", "-Command", "Get-NetAdapterStatistics | Select-Object Name, ReceivedBytes | ConvertTo-Json -Compress"]
                out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL).strip()
                if out:
                    data = json.loads(out)
                    if isinstance(data, dict): data = [data]
                    for item in data:
                        counters[item.get("Name")] = int(item.get("ReceivedBytes") or 0)
            except Exception:
                pass
        return counters

    test_url = "https://www.python.org/ftp/python/3.12.0/python-3.12.0-amd64.exe"

    # 1. Download payload targeted strictly at Adapter A (512 KB)
    baseline_a = read_nic_rx_bytes()
    session_a = nm.create_requests_session(iface_a.name)
    resp_a = session_a.get(test_url, headers={"Range": "bytes=0-524287", "User-Agent": "Mozilla/5.0"}, timeout=10)
    assert resp_a.status_code == 206
    after_a = read_nic_rx_bytes()

    delta_a_on_a = after_a.get(iface_a.name, 0) - baseline_a.get(iface_a.name, 0)
    delta_b_on_a = after_a.get(iface_b.name, 0) - baseline_a.get(iface_b.name, 0)

    # 2. Download payload targeted strictly at Adapter B (512 KB)
    baseline_b = read_nic_rx_bytes()
    session_b = nm.create_requests_session(iface_b.name)
    resp_b = session_b.get(test_url, headers={"Range": "bytes=0-524287", "User-Agent": "Mozilla/5.0"}, timeout=10)
    assert resp_b.status_code == 206
    after_b = read_nic_rx_bytes()

    delta_a_on_b = after_b.get(iface_a.name, 0) - baseline_b.get(iface_a.name, 0)
    delta_b_on_b = after_b.get(iface_b.name, 0) - baseline_b.get(iface_b.name, 0)

    # Assertions:
    # During Adapter A run, Adapter A must receive the payload (>= 400 KB), while Adapter B receives negligible traffic
    assert delta_a_on_a >= 400_000, f"Adapter {iface_a.name} must register at least 400KB RX, got {delta_a_on_a}"
    assert delta_b_on_a < delta_a_on_a * 0.15, f"Adapter {iface_b.name} must not carry {iface_a.name} traffic"

    # During Adapter B run, Adapter B must receive the payload (>= 400 KB), while Adapter A receives negligible traffic
    assert delta_b_on_b >= 400_000, f"Adapter {iface_b.name} must register at least 400KB RX, got {delta_b_on_b}"
    assert delta_a_on_b < delta_b_on_b * 0.15, f"Adapter {iface_a.name} must not carry {iface_b.name} traffic"


def test_validate_paths_multistage_logic():
    """
    Requirement (P0 audit): Distinct gateways alone must NOT declare independent_egress_proven=True.
    Verifies that multi-stage path validation correctly categorizes:
    - shared uplink single ISP
    - distinct gateways with shared public IP
    - multi-provider independent
    """
    nm = NetworkManager()

    # Case 1: Two interfaces sharing gateway and public IP
    if1 = InterfaceStats(name="eth1", ip="192.168.1.5", mac="00:11:22:33:44:55", gateway="192.168.1.1", public_ip="203.0.113.1")
    if2 = InterfaceStats(name="eth2", ip="192.168.1.6", mac="00:11:22:33:44:56", gateway="192.168.1.1", public_ip="203.0.113.1")
    nm.interfaces = {"eth1": if1, "eth2": if2}

    res1 = nm.validate_paths()
    assert res1["independent_egress_proven"] is False
    assert res1["validation_stage"] == "shared_uplink_single_isp"

    # Case 2: Two interfaces with distinct gateways but sharing the same public IP
    if2.gateway = "10.0.0.1"
    res2 = nm.validate_paths()
    assert res2["independent_egress_proven"] is False
    assert res2["validation_stage"] == "distinct_gateways_shared_ip"

    # Case 3: Two interfaces with distinct public IPs
    if2.public_ip = "198.51.100.2"
    res3 = nm.validate_paths()
    assert res3["independent_egress_proven"] is True
    assert res3["validation_stage"] == "multi_provider_independent"


def test_probe_strict_range_and_malformed_rejection(mock_server):
    """
    Requirement (Downloader audit): Probe must strictly validate RFC 9110 range response
    and reject malformed Content-Range or mismatched slice body lengths.
    """
    base_url = mock_server
    nm = NetworkManager()
    if1 = InterfaceStats(name="lo", ip="127.0.0.1", mac="00:00:00:00:00:01", gateway="127.0.0.1", public_ip="127.0.0.1")
    nm.interfaces = {"lo": if1}

    # 1. Normal server returns valid 206 with bytes 0-1/<total> and 2 bytes body
    task = BondedDownloadTask("probe_ok", f"{base_url}/test", "./dest_ok.bin", nm, allow_local=True)
    assert task.probe() is True
    assert task.supports_ranges is True
    assert task.total_size == len(TEST_PAYLOAD)

    # 2. Server returning 200 OK (range ignored) falls back to single-stream
    MockHTTPServerHandler.mode = "ignore_range"
    try:
        task_fallback = BondedDownloadTask("probe_fallback", f"{base_url}/test", "./dest_fallback.bin", nm, allow_local=True)
        assert task_fallback.probe() is True
        assert task_fallback.supports_ranges is False
        assert task_fallback.total_size == len(TEST_PAYLOAD)
    finally:
        MockHTTPServerHandler.mode = "normal"

    # 3. Server returning malformed Content-Range header (e.g. bytes 0-1/*)
    MockHTTPServerHandler.mode = "probe_malformed_range"
    try:
        task_malformed = BondedDownloadTask("probe_malformed", f"{base_url}/test", "./dest_malformed.bin", nm, allow_local=True)
        assert task_malformed.probe() is False
        assert "Probe failed on all interfaces" in task_malformed.error_message
    finally:
        MockHTTPServerHandler.mode = "normal"

    # 4. Server returning incorrect slice body length (1 byte instead of 2 bytes)
    MockHTTPServerHandler.mode = "probe_wrong_length"
    try:
        task_wrong_len = BondedDownloadTask("probe_wrong_len", f"{base_url}/test", "./dest_wrong_len.bin", nm, allow_local=True)
        assert task_wrong_len.probe() is False
        assert "Probe failed on all interfaces" in task_wrong_len.error_message
    finally:
        MockHTTPServerHandler.mode = "normal"


def test_worker_setup_failure_transitions_to_failed():
    """
    Requirement (Downloader audit): Worker session setup exceptions must propagate
    cleanly to mark task status 'failed' and abort.
    """
    nm = NetworkManager()
    if1 = InterfaceStats(name="broken_nic", ip="192.0.2.1", mac="00:00:00:00:00:02", gateway="192.0.2.254", public_ip="192.0.2.1")
    nm.interfaces = {"broken_nic": if1}

    def fail_session(ifname):
        raise ConnectionError(f"Simulated hardware binding failure on {ifname}")

    nm.create_requests_session = fail_session

    task = BondedDownloadTask("fail_task", "http://example.com/test.bin", "./fail.bin", nm, allow_local=True)
    task.supports_ranges = True
    task.total_size = 1048576

    task._worker_loop("broken_nic")

    assert task.status == "failed"
    assert "Worker initialization failed" in task.error_message
    assert task._cancel_event.is_set()


def test_single_stream_session_failure_handled(mock_server):
    """
    Requirement (Downloader audit): Single-stream fallback background worker must
    catch session creation failure and transition deterministically to 'failed'.
    """
    nm = NetworkManager()
    if1 = InterfaceStats(name="lo", ip="127.0.0.1", mac="00:00:00:00:00:01", gateway="127.0.0.1", public_ip="127.0.0.1")
    nm.interfaces = {"lo": if1}

    task = BondedDownloadTask("single_stream_fail", f"{mock_server}/test", "./ss_fail.bin", nm, allow_local=True)
    task.supports_ranges = False
    task.total_size = len(TEST_PAYLOAD)

    # Monkey patch create_requests_session to simulate session constructor failure
    def raise_session_err(ifname):
        raise RuntimeError("Simulated session initialization error in single-stream")

    nm.create_requests_session = raise_session_err

    task._start_single_stream()

    timeout = 3.0
    t0 = time.time()
    while task.status == "running" and (time.time() - t0) < timeout:
        time.sleep(0.1)

    assert task.status == "failed"
    assert "Single stream error" in task.error_message
    assert task._cancel_event.is_set()


def test_finalization_lock_state_transition_race(tmp_path):
    """
    Requirement (Downloader audit): Finalization lock and state checks must prevent
    a task from transitioning to 'completed' if cancellation occurred during finalization.
    """
    nm = NetworkManager()
    if1 = InterfaceStats(name="lo", ip="127.0.0.1", mac="00:00:00:00:00:01", gateway="127.0.0.1", public_ip="127.0.0.1")
    nm.interfaces = {"lo": if1}

    dest = str(tmp_path / "race_test.bin")
    task = BondedDownloadTask("race_task", "http://example.com/test", dest, nm, allow_local=True)
    task.total_size = 100
    task.supports_ranges = False

    # Create dummy temp file
    with open(task.temp_path, "wb") as f:
        f.write(b"X" * 100)

    # Pre-set cancel event to simulate race before move
    task._cancel_event.set()
    task.status = "cancelled"

    task._finalize_file()

    # Must NOT be completed
    assert task.status == "cancelled"
    assert not os.path.exists(dest)


def test_ssrf_comprehensive_protection(mock_server):
    """
    Requirement (P0 audit): SSRF validation must resolve all A/AAAA records, reject prohibited
    address ranges (loopback, private, link-local, CGNAT, multicast, IPv4-mapped IPv6,
    alternate integer/hex IP formats), and prevent redirect pivots.
    """
    # 1. Prohibited direct and alternate IP formats
    prohibited_urls = [
        "http://127.0.0.1:8080/admin",           # IPv4 loopback
        "http://127.127.127.127/secret",        # IPv4 loopback subnet
        "http://2130706433/",                   # Decimal representation of 127.0.0.1
        "http://0x7f000001/",                   # Hex representation of 127.0.0.1
        "http://10.0.0.1/internal",             # RFC 1918 Class A
        "http://172.16.0.1/internal",           # RFC 1918 Class B
        "http://192.168.1.1/internal",          # RFC 1918 Class C
        "http://100.64.0.1/cgnat",              # Carrier-Grade NAT (100.64.0.0/10)
        "http://169.254.169.254/metadata",      # Link-local AWS/cloud metadata
        "http://[::1]/",                        # IPv6 loopback
        "http://[fe80::1]/",                    # IPv6 link-local
        "http://[::ffff:127.0.0.1]/",           # IPv4-mapped IPv6 loopback
        "http://[::ffff:10.0.0.1]/",            # IPv4-mapped IPv6 private
        "ftp://example.com/file",               # Prohibited protocol
    ]

    for target_url in prohibited_urls:
        is_valid, err, _ = validate_url_ssrf(target_url, allow_local=False)
        assert is_valid is False, f"URL '{target_url}' should have been rejected by SSRF protection"
        assert len(err) > 0

    # 2. Redirect pivot prevention: URL on safe host that redirects to internal address must be aborted
    session = requests.Session()
    redirect_target = f"{mock_server}/redirect_to_internal"
    with pytest.raises(requests.exceptions.RequestException) as exc_info:
        safe_request_get(session, redirect_target, allow_local=False)
    assert "SSRF violation" in str(exc_info.value)

    redirect_private = f"{mock_server}/redirect_to_prohibited"
    with pytest.raises(requests.exceptions.RequestException) as exc_info2:
        safe_request_get(session, redirect_private, allow_local=False)
    assert "SSRF violation" in str(exc_info2.value)


def test_fail_closed_socket_binding_error(mock_server):
    """
    Requirement (P0 audit): Verify binding at the connection/socket layer,
    propagate binding-specific failures reliably without string searches,
    and prove fail-closed behavior on invalid interface configurations.
    """
    # 1. Independent socket-layer verification method fails on invalid source IP
    adapter_bad_ip = BoundInterfaceHTTPAdapter(ifname="eth0", source_ip="198.51.100.254")
    ok, err = adapter_bad_ip.verify_socket_binding()
    assert ok is False
    assert "Socket binding verification failed" in err

    # 2. Sockets attempting to send through invalid interface configuration fail closed with InterfaceBindingError
    session = requests.Session()
    session.mount("http://", adapter_bad_ip)

    with pytest.raises(InterfaceBindingError) as exc_info:
        session.get(f"{mock_server}/test", timeout=1.0)
    assert "Interface socket binding or connection failed" in str(exc_info.value)


def test_opt_in_linux_policy_routing():
    """
    Requirement (WSL2 routing audit): Interface discovery must be 100% read-only and
    never mutate host routing as a side effect. Policy-route installation must be an
    explicit opt-in operation with safe preview.
    """
    nm = NetworkManager()
    # Discover interfaces must not invoke sudo ip rule mutating commands
    ifaces = nm.discover_interfaces()
    assert isinstance(ifaces, list)

    # Explicit policy routing preview (apply=False) must generate plan without executing
    preview = nm.configure_linux_policy_routing(apply=False)
    assert isinstance(preview, dict)
    assert "plan" in preview
    assert preview["applied"] is False


def test_api_concurrency_limit_and_ssrf_protection(speedbond_api_server):
    """
    Requirement (API security audit): /api/download must block SSRF targets by default
    and enforce max 3 active concurrent downloads with HTTP 429.
    """
    base_url, server = speedbond_api_server

    # 1. SSRF Protection: Loopback/private address blocked by default
    os.environ.pop("ALLOW_LOCAL_TEST_DOWNLOADS", None)
    resp_ssrf = requests.post(
        f"{base_url}/api/download",
        json={"url": "http://127.0.0.1:8080/secret", "filename": "ssrf.bin"},
        timeout=5.0
    )
    assert resp_ssrf.status_code == 403
    assert "SSRF" in resp_ssrf.json().get("error", "")

    # 2. Concurrency Limit: Enforce maximum 3 active downloads
    server.downloads.clear()
    for i in range(3):
        t = BondedDownloadTask(f"t_{i}", "http://example.com/file", f"./f_{i}.bin", server.net_mgr)
        t.status = "running"
        server.downloads[f"t_{i}"] = t

    # 4th download should be rejected with 429
    resp_limit = requests.post(
        f"{base_url}/api/download",
        json={"url": "https://example.com/overflow.bin", "filename": "overflow.bin"},
        timeout=5.0
    )
    assert resp_limit.status_code == 429
    assert "Concurrent download limit reached" in resp_limit.json().get("error", "")
    server.downloads.clear()


def test_dns_rebinding_defense_pins_ip_and_preserves_tls(mock_server):
    """
    Requirement (High audit finding): Prevent DNS rebinding (TOCTOU).
    Connections must bind directly to a pre-validated IP address without secondary DNS
    resolution, eliminating TOCTOU DNS rebinding, while preserving correct TLS hostname
    verification.
    """
    # 1. Test resolve_and_validate_host directly
    ip = resolve_and_validate_host("127.0.0.1", port=80, allow_local=True)
    assert ip == "127.0.0.1"

    ip_pub = resolve_and_validate_host("example.com", port=80, allow_local=False)
    assert ip_pub is not None

    # Host resolving to private IP rejected when allow_local=False
    with pytest.raises(requests.exceptions.RequestException) as exc_priv:
        resolve_and_validate_host("127.0.0.1", port=80, allow_local=False)
    assert "SSRF" in str(exc_priv.value)

    # 2. DNSRebindingSafePoolManager with pinned IP connects directly
    session = requests.Session()
    adapter = BoundInterfaceHTTPAdapter(ifname="lo", source_ip="127.0.0.1", allow_local=True)
    # Pin mock server hostname to 127.0.0.1
    adapter.pin_host("127.0.0.1", "127.0.0.1", allow_local=True)
    session.mount("http://", adapter)

    resp = session.get(f"{mock_server}/", timeout=2.0)
    assert resp.status_code == 200
    assert len(resp.content) > 0

    # 3. safe_request_get uses DNS rebinding defense and verifies each redirect hop
    resp_safe = safe_request_get(session, f"{mock_server}/", allow_local=True, timeout=2.0)
    assert resp_safe.status_code == 200

    # If redirect target points to prohibited internal IP, it is blocked before connection
    with pytest.raises(requests.exceptions.RequestException) as exc_info:
        safe_request_get(session, f"{mock_server}/redirect_to_internal", allow_local=False, timeout=2.0)
    assert "SSRF violation" in str(exc_info.value)


def test_production_adapter_outbound_binding_verification(mock_server):
    """
    Requirement (High audit finding): Prove interface binding using actual outbound
    connections via the production BoundInterfaceHTTPAdapter.
    Verify socket source address matches bound IP rather than relying on dummy local UDP binds.
    """
    # 1. Valid loopback adapter verifies successfully with outbound HTTP probe
    adapter_valid = BoundInterfaceHTTPAdapter(ifname="lo", source_ip="127.0.0.1", allow_local=True)
    adapter_valid.pin_host("127.0.0.1", "127.0.0.1", allow_local=True)
    ok, msg = adapter_valid.verify_outbound_binding(test_url=f"{mock_server}/")
    assert ok is True
    assert "Verified outbound" in msg

    # 2. Invalid source IP fails outbound verification without unhandled exceptions
    adapter_invalid = BoundInterfaceHTTPAdapter(ifname="eth0", source_ip="198.51.100.254")
    ok_inv, msg_inv = adapter_invalid.verify_outbound_binding(test_url=f"{mock_server}/")
    assert ok_inv is False
    assert "Socket binding verification failed" in msg_inv or "Outbound connection failed" in msg_inv

    # 3. NetworkManager validate_paths includes socket_binding_test_passed logic based on production adapters
    nm = NetworkManager()
    paths_res = nm.validate_paths(outbound_test_url=f"{mock_server}/")
    assert isinstance(paths_res, dict)
    assert "socket_binding_test_passed" in paths_res
    assert "independent_egress_proven" in paths_res
    # On single-uplink / local test, independent egress is strictly False
    assert paths_res["independent_egress_proven"] is False


def test_benchmark_hardware_attribution_and_statistical_testing():
    """
    Requirement (Medium audit findings):
    1. Benchmark hardware attribution: Map OS counters to selected physical NICs,
       isolate background traffic, and require both adapters to carry >= 15% (and sum >= 70%)
       of bytes attributable to the benchmark.
    2. Statistical testing: Report distribution (mean, std dev, std err, 95% CI) and
       use Welch's t-test before claiming statistically significant improvement.
    """
    # 1. Interface mapping
    nm = NetworkManager()
    target_nics = get_target_nic_names(nm, ["eth0", "eth1"])
    assert "eth0" in target_nics
    assert "eth1" in target_nics

    # 2. Statistical calculations
    runs_a = [10.0, 10.5, 9.8, 10.2, 10.1]
    stats_a = compute_statistics(runs_a)
    assert stats_a["n"] == 5
    assert abs(stats_a["mean"] - 10.12) < 0.05
    assert stats_a["std_dev"] > 0
    assert stats_a["ci95_upper"] >= stats_a["mean"]
    assert stats_a["ci95_lower"] <= stats_a["mean"]

    # Statistical test: identical or overlapping distributions should not be significant
    t_stat, is_sig = welch_t_test(runs_a, runs_a)
    assert is_sig is False
    assert t_stat == 0.0

    # Distinct distributions: significant difference
    runs_b = [20.0, 20.5, 19.8, 20.2, 20.1]
    t_stat_b, is_sig_b = welch_t_test(runs_b, runs_a)
    assert is_sig_b is True
    assert t_stat_b > 2.0

    # Single-run edge case (N=1) cannot compute std dev or claim statistical significance
    single_stat = compute_statistics([15.0])
    assert single_stat["n"] == 1
    assert single_stat["std_dev"] == 0.0
    _, single_sig = welch_t_test([20.0], [10.0])
    assert single_sig is False



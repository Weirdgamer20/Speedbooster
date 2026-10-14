import os
import sys
import time
import json
import hashlib
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

PAYLOAD_SIZE = 16 * 1024 * 1024  # 16 MB
PAYLOAD = (b"SpeedBondControlledBenchmarkPayload_0123456789\n" * (PAYLOAD_SIZE // 47))[:PAYLOAD_SIZE]
PAYLOAD_SHA256 = hashlib.sha256(PAYLOAD).hexdigest()

class RangeServer(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", '"speedbond-bench-v1"')
        self.end_headers()

    def do_GET(self):
        range_h = self.headers.get("Range")
        if not range_h or not range_h.startswith("bytes="):
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD)
            return

        parts = range_h.replace("bytes=", "").split("-")
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else len(PAYLOAD) - 1
        self.send_response(206)
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(PAYLOAD)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("ETag", '"speedbond-bench-v1"')
        self.end_headers()
        self.wfile.write(PAYLOAD[start:end+1])

    def log_message(self, *args):
        pass

def main():
    server = HTTPServer(("127.0.0.1", 0), RangeServer)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()

    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))
    from network_manager import NetworkManager, InterfaceStats
    from downloader import BondedDownloadTask

    mgr = NetworkManager()
    mgr.interfaces["lo"] = InterfaceStats(name="lo", ip="127.0.0.1", public_ip="127.0.0.1")

    # Trial 1: Single stream worker
    t0 = time.time()
    dest1 = "/tmp/bench_single.bin"
    task1 = BondedDownloadTask("t_single", f"http://127.0.0.1:{port}/bench", dest1, mgr, workers_per_iface=1)
    task1.start()
    while task1.status in ["probing", "running"]:
        time.sleep(0.05)
    el1 = time.time() - t0
    spd1 = (PAYLOAD_SIZE / (1024 * 1024)) / el1 if el1 > 0 else 0

    # Trial 2: Parallel 4-worker chunking
    t0 = time.time()
    dest2 = "/tmp/bench_parallel.bin"
    task2 = BondedDownloadTask("t_parallel", f"http://127.0.0.1:{port}/bench", dest2, mgr, workers_per_iface=4, expected_sha256=PAYLOAD_SHA256)
    task2.start()
    while task2.status in ["probing", "running"]:
        time.sleep(0.05)
    el2 = time.time() - t0
    spd2 = (PAYLOAD_SIZE / (1024 * 1024)) / el2 if el2 > 0 else 0

    res = {
        "benchmark_timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload_bytes": PAYLOAD_SIZE,
        "payload_mbytes": round(PAYLOAD_SIZE / (1024 * 1024), 2),
        "expected_sha256": PAYLOAD_SHA256,
        "single_stream_trial": {
            "elapsed_sec": round(el1, 3),
            "throughput_mbytes_sec": round(spd1, 2),
            "throughput_mbps": round(spd1 * 8, 2),
            "status": task1.status,
            "calculated_sha256": task1.calculated_sha256,
            "integrity_match": (task1.calculated_sha256 == PAYLOAD_SHA256)
        },
        "parallel_chunked_trial": {
            "elapsed_sec": round(el2, 3),
            "throughput_mbytes_sec": round(spd2, 2),
            "throughput_mbps": round(spd2 * 8, 2),
            "status": task2.status,
            "calculated_sha256": task2.calculated_sha256,
            "checksum_verified": task2.checksum_verified,
            "integrity_match": (task2.calculated_sha256 == PAYLOAD_SHA256)
        }
    }

    out_path = os.path.join(os.path.dirname(__file__), "controlled_local_benchmark.json")
    with open(out_path, "w") as f:
        json.dump(res, f, indent=2)

    print("CONTROLLED_BENCHMARK_SUCCESS")
    print(json.dumps(res, indent=2))
    server.shutdown()

if __name__ == "__main__":
    main()

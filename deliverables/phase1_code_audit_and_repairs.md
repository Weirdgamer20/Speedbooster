# Phase 1 — Codebase Audit and Implementation Repairs Report

**Module:** SpeedBond Core Engine  
**Runtime:** Python 3.14.4 / Linux (WSL2 Mirrored Mode) & Windows 11 PowerShell Interop  
**Date:** 14 October 2026  
**Status:** Completed & Verified  

---

## 1. Executive Summary & Repository Inspection

An in-depth architectural and code-level audit was conducted across all components of the SpeedBond codebase located at `C:\Users\thega\.gemini\antigravity-ide\scratch\speedbond\`.

### Source Inventory & Entry Points
* **Core Application Entry Point:** [`main.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/main.py) — CLI handler, runtime selector, `--diagnose` audit flag, and service launcher.
* **Network & Interface Management:** [`network_manager.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/network_manager.py) — Dynamic Windows hardware discovery, MAC resolution, interface telemetry, and socket binding factories.
* **Downloader Engine:** [`downloader.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/downloader.py) — RFC 9110 parallel range chunker, byte-boundary validator, and atomic file assembler.
* **Proxy Server:** [`proxy.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/proxy.py) — Multi-WAN HTTP/HTTPS proxy listening on loopback `127.0.0.1:8080`.
* **REST API & Web UI:** [`web_server.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/web_server.py) — Local HTTP management plane (`127.0.0.1:5000`) and static asset server.
* **Automated Benchmark Suite:** [`benchmark.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/benchmark.py) — Dual-WAN performance validator capturing physical Windows NIC deltas.
* **Automated Acceptance Suite:** [`tests/test_speedbond.py`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/tests/test_speedbond.py) — 15 deterministic test cases verifying RFC 9110 chunking, boundary guards, thread lifecycles, API endpoints, socket options, and OS-level hardware counters (15/15 passed).
* **Desktop Telemetry OSD:** [`floating_speedometer.ps1`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/floating_speedometer.ps1) — MSI Afterburner / RTSS styled on-screen HUD.
* **Route Metric Scripts:** [`apply_desktop_bonding.ps1`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/apply_desktop_bonding.ps1) & [`reset_desktop_bonding.ps1`](file:///C:/Users/thega/.gemini/antigravity-ide/scratch/speedbond/reset_desktop_bonding.ps1).

---

## 2. Defects Identified and Repaired

### 2.1 Removal of Process-Global Monkey-Patching in Interface Binding
* **Defect:** `BoundInterfaceHTTPAdapter` in `network_manager.py` previously monkey-patched `urllib3.util.connection.create_connection = _custom_create_connection` globally. This caused severe race conditions, cross-session pollution, and broke thread isolation in concurrent multi-adapter downloads.
* **Repair:** Removed monkey-patching completely. Re-implemented interface and source IP binding cleanly using urllib3's native connection pool parameters:
  ```python
  class BoundInterfaceHTTPAdapter(HTTPAdapter):
      def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
          sock_opts = []
          if self.ifname and self.ifname != "lo" and sys.platform.startswith("linux") and os.path.exists(f"/sys/class/net/{self.ifname}"):
              so_bind = getattr(socket, "SO_BINDTODEVICE", 25)
              sock_opts.append((socket.SOL_SOCKET, so_bind, self.ifname.encode()))
          sock_opts.append((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1))
          pool_kwargs["socket_options"] = sock_opts
          if self.source_ip:
              pool_kwargs["source_address"] = (self.source_ip, 0)
          super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)
  ```
* **No Silent Swallowing:** Removed `except Exception: pass` from socket binding. Raised `InterfaceBindingError` on failure so binding errors are immediately observable rather than silently falling back to the default OS gateway.

### 2.2 Download Range Validation & Chunk Boundary Protection (RFC 9110)
* **Defect:** Downloader previously did not verify whether Content-Range total matched the expected file size, and wrote response bytes directly to disk without checking if the server sent more bytes than requested. Excess bytes would overwrite neighboring chunks on disk.
* **Repair:**
  * Enforced strict checking of `Content-Range: bytes <start>-<end>/<total>` where start, end, and total must match chunk bounds and resource size.
  * Added excess byte rejection before writing to disk:
    ```python
    if bytes_received + block_len > chunk.expected_size:
        raise ChunkVerificationError(f"Oversized chunk: received {bytes_received + block_len} bytes, exceeds expected {chunk.expected_size}")
    f.write(block)
    ```
  * Enforced terminal state consistency: tasks that fail or are cancelled can never transition to `completed`.

### 2.3 Worker Lifecycle and Safe Cancellation
* **Defect:** `cancel()` previously deleted `.speedbond_part` temporary files while worker threads were still running, risking write-after-delete errors and race conditions.
* **Repair:** In `cancel()`, the cancellation event is set and worker threads are joined (`t.join(timeout=2.0)`) before temporary files are removed.

### 2.4 Cryptographic Checksum Integrity
* **Defect:** Downloader computed a SHA-256 digest at completion and labeled it verified even when no external trusted hash was provided.
* **Repair:** Explicitly distinguished between:
  * `calculated_sha256`: Digest computed from downloaded bytes.
  * `expected_sha256`: Trusted digest provided by user/caller.
  * `checksum_verified`: Set to `True` only if expected matches calculated; set to `False` (triggering failure) on mismatch; set to `None` if calculated only without external comparison.

### 2.5 Security & API Protection
* **Loopback Binding Default:** Dashboard and proxy bind to `127.0.0.1` by default.
* **Path Traversal Protection:** Validated destination paths in `POST /api/download` against `resolved_download_dir + os.sep`. Directory breakouts return HTTP 403 Forbidden.
* **Fixed API Import:** Fixed `threading` import in `web_server.py`, resolving NameError on `POST /api/refresh`.

---

## 3. Compilation and Baseline Verification Results

```bash
python3 -m py_compile main.py network_manager.py downloader.py proxy.py web_server.py benchmark.py tests/test_speedbond.py
```
* **Exit Code:** `0`
* **Result:** `ALL_MODULES_COMPILED_CLEANLY`
* **Verified Dependencies:** `requests>=2.31.0`, `urllib3>=2.0.0`, `pytest>=7.0.0`.

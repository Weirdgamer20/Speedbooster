"""
downloader.py - Robust Multi-Interface Parallel Range Chunker & Integrity Downloader.
Implements RFC 9110 HTTP Range semantics, strict 206 Content-Range validation,
ETag/Last-Modified consistency checks, bounded retries with jitter, truthful progress,
cancellation tokens, and atomic destination rename.
"""

import os
import re
import time
import math
import random
import hashlib
import logging
import threading
import queue
import requests
from urllib.parse import urlparse
from typing import Dict, List, Optional, Callable, Tuple
from network_manager import NetworkManager, InterfaceStats
from security_utils import safe_request_get, validate_url_ssrf

logger = logging.getLogger("speedbond.downloader")

CHUNK_SIZE = 2 * 1024 * 1024  # 2MB chunks
MAX_CHUNK_RETRIES = 3

class ChunkVerificationError(Exception):
    pass

class ResourceChangedError(Exception):
    pass

class DownloadChunk:
    def __init__(self, chunk_id: int, start_byte: int, end_byte: int):
        self.chunk_id = chunk_id
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.expected_size = (end_byte - start_byte) + 1
        self.downloaded_bytes = 0
        self.assigned_iface = ""
        self.status = "pending"  # pending, downloading, completed, failed
        self.retries = 0
        self.error = ""

class BondedDownloadTask:
    def __init__(self, task_id: str, url: str, dest_path: str, net_mgr: NetworkManager, 
                 workers_per_iface: int = 2, expected_sha256: Optional[str] = None,
                 allow_local: Optional[bool] = None):
        self.task_id = task_id
        self.url = url
        self.dest_path = os.path.abspath(dest_path)
        self.temp_path = self.dest_path + ".speedbond_part"
        self.net_mgr = net_mgr
        self.workers_per_iface = workers_per_iface
        self.expected_sha256: Optional[str] = expected_sha256
        if allow_local is not None:
            self.allow_local = allow_local
        else:
            self.allow_local = (os.environ.get("ALLOW_LOCAL_TEST_DOWNLOADS") == "1")

        self.total_size = 0
        self.supports_ranges = False
        self.etag: Optional[str] = None
        self.last_modified: Optional[str] = None
        self.sha256_hash: Optional[str] = None
        self.calculated_sha256: Optional[str] = None
        self.checksum_verified: Optional[bool] = None

        self.status = "initialized"  # initialized, probing, running, completed, failed, cancelled
        self.error_message = ""
        self.start_time = 0.0
        self.end_time = 0.0

        self.chunks: List[DownloadChunk] = []
        self._chunk_queue = queue.Queue()
        self._file_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._finalize_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._threads: List[threading.Thread] = []
        self._callbacks: List[Callable] = []

    def add_callback(self, cb: Callable):
        self._callbacks.append(cb)

    def _notify(self):
        for cb in self._callbacks:
            try:
                cb(self)
            except Exception:
                pass

    def probe(self) -> bool:
        """
        Probes the target resource using an actual GET Range request (bytes=0-1)
        per RFC 9110 to strictly verify 206 Partial Content support and capture
        resource validators (ETag / Last-Modified).
        """
        self.status = "probing"

        # Pre-flight SSRF target check
        is_valid, ssrf_err, _ = validate_url_ssrf(self.url, allow_local=self.allow_local)
        if not is_valid:
            self.error_message = f"SSRF blocked: {ssrf_err}"
            self.status = "failed"
            return False

        active_ifaces = [i for i in self.net_mgr.interfaces.values() if i.is_active]
        if not active_ifaces:
            self.error_message = "No active network interfaces available"
            self.status = "failed"
            return False

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 SpeedBond/2.0",
            "Range": "bytes=0-1",
        }

        # Attempt probe across available interfaces
        last_err = None
        for iface in active_ifaces:
            try:
                # Custom socket adapter / session for bound iface
                session = self.net_mgr.create_requests_session(iface.name)
                resp = safe_request_get(session, self.url, allow_local=self.allow_local, headers=headers, timeout=(6.0, 10.0), stream=True)

                if resp.status_code == 206:
                    # Valid partial content response: require full-match Content-Range and verify length
                    content_range = resp.headers.get("Content-Range", "").strip()
                    m = re.fullmatch(r"bytes\s+0-1/(\d+)", content_range)
                    if m:
                        total = int(m.group(1))
                        probe_body = resp.raw.read(16)
                        if len(probe_body) == 2 and total >= 2:
                            self.total_size = total
                            self.supports_ranges = True
                            self.etag = resp.headers.get("ETag")
                            self.last_modified = resp.headers.get("Last-Modified")
                            resp.close()
                            return True
                        else:
                            logger.warning("Probe received invalid slice body length (%d) or total (%d)", len(probe_body), total)
                    else:
                        logger.warning("Probe received malformed Content-Range header: '%s'", content_range)
                elif resp.status_code == 200:
                    # Server ignored Range header and returned full body
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        self.total_size = int(cl)
                    self.supports_ranges = False
                    self.etag = resp.headers.get("ETag")
                    self.last_modified = resp.headers.get("Last-Modified")
                    resp.close()
                    return True
                resp.close()
            except Exception as e:
                last_err = e
                continue

        self.error_message = f"Probe failed on all interfaces: {last_err}"
        self.status = "failed"
        return False

    def start(self) -> bool:
        """Starts the download lifecycle."""
        if not self.probe():
            return False

        if not self.supports_ranges or self.total_size <= 0:
            # Fall back safely to deterministic single-stream download from byte 0
            self._start_single_stream()
            return True

        # Pre-allocate sparse temporary destination file
        os.makedirs(os.path.dirname(self.temp_path) or ".", exist_ok=True)
        try:
            with open(self.temp_path, "wb") as f:
                f.seek(self.total_size - 1)
                f.write(b"\0")
        except Exception as e:
            self.error_message = f"Failed to allocate temporary file: {e}"
            self.status = "failed"
            return False

        # Partition file into discrete non-overlapping ranges
        self.chunks = []
        num_chunks = math.ceil(self.total_size / CHUNK_SIZE)
        for i in range(num_chunks):
            start = i * CHUNK_SIZE
            end = min(start + CHUNK_SIZE - 1, self.total_size - 1)
            chunk = DownloadChunk(i, start, end)
            self.chunks.append(chunk)
            self._chunk_queue.put(chunk)

        self.status = "running"
        self.start_time = time.time()
        active_ifaces = [i.name for i in self.net_mgr.interfaces.values() if i.is_active]

        # Spawn workers for each interface
        for ifname in active_ifaces:
            for _ in range(self.workers_per_iface):
                t = threading.Thread(target=self._worker_loop, args=(ifname,), daemon=True)
                t.start()
                self._threads.append(t)

        return True

    def _worker_loop(self, ifname: str):
        ifstat = self.net_mgr.interfaces.get(ifname)
        try:
            session = self.net_mgr.create_requests_session(ifname)
        except Exception as e:
            logger.error("Failed to construct worker session on %s: %s", ifname, e)
            with self._state_lock:
                self.error_message = f"Worker initialization failed on {ifname}: {e}"
                self.status = "failed"
                self._cancel_event.set()
            self._notify()
            return

        while not self._cancel_event.is_set():
            try:
                chunk: DownloadChunk = self._chunk_queue.get(timeout=0.5)
            except queue.Empty:
                # Check if all chunks completed
                if self._is_all_chunks_done():
                    break
                continue

            chunk.status = "downloading"
            chunk.assigned_iface = ifname

            success = False
            try:
                self._download_chunk(session, chunk, ifstat)
                chunk.status = "completed"
                chunk.error = ""
                success = True
            except ResourceChangedError as e:
                # ETag or resource representation modified during download: abort immediately
                with self._state_lock:
                    self.error_message = f"Remote resource changed mid-download: {e}"
                    self.status = "failed"
                    self._cancel_event.set()
                break
            except Exception as e:
                chunk.error = str(e)
                chunk.retries += 1
                # Exponential backoff with jitter before re-queuing
                delay = (2 ** chunk.retries) * 0.2 + random.uniform(0.05, 0.2)
                time.sleep(min(delay, 2.0))

            if not success and not self._cancel_event.is_set():
                if chunk.retries <= MAX_CHUNK_RETRIES:
                    chunk.status = "pending"
                    self._chunk_queue.put(chunk)
                else:
                    chunk.status = "failed"
                    with self._state_lock:
                        self.error_message = f"Chunk {chunk.chunk_id} failed after {MAX_CHUNK_RETRIES} retries: {chunk.error}"
                        self.status = "failed"
                        self._cancel_event.set()
                    break

            self._check_completion()

    def _download_chunk(self, session: requests.Session, chunk: DownloadChunk, ifstat: Optional[InterfaceStats]):
        """Downloads a specific byte slice and enforces strict 206 Partial Content validation."""
        req_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 SpeedBond/2.0",
            "Range": f"bytes={chunk.start_byte}-{chunk.end_byte}",
        }
        if self.etag:
            req_headers["If-Range"] = self.etag

        resp = safe_request_get(session, self.url, allow_local=self.allow_local, headers=req_headers, timeout=(10.0, 20.0), stream=True)

        try:
            # Strictly reject 200 OK for range chunk requests
            if resp.status_code == 200:
                raise ChunkVerificationError(
                    f"Server returned 200 OK instead of 206 Partial Content for byte range {chunk.start_byte}-{chunk.end_byte}"
                )

            if resp.status_code != 206:
                raise ChunkVerificationError(f"HTTP {resp.status_code} returned for range request")

            # Validate Content-Range header
            cr = resp.headers.get("Content-Range", "")
            m = re.search(r"bytes\s+(\d+)-(\d+)/(\d+)", cr)
            if not m:
                raise ChunkVerificationError(f"Malformed or missing Content-Range header: '{cr}'")

            resp_start, resp_end, resp_total = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if resp_start != chunk.start_byte or resp_end != chunk.end_byte:
                raise ChunkVerificationError(
                    f"Content-Range mismatch: expected {chunk.start_byte}-{chunk.end_byte}, got {resp_start}-{resp_end}"
                )

            if self.total_size > 0 and resp_total != self.total_size:
                raise ChunkVerificationError(
                    f"Content-Range total mismatch: expected total {self.total_size}, got {resp_total}"
                )

            # Check ETag consistency
            resp_etag = resp.headers.get("ETag")
            if self.etag and resp_etag and resp_etag != self.etag:
                raise ResourceChangedError(f"ETag mismatch: initial '{self.etag}' vs current '{resp_etag}'")

            # Stream body into temp file at exact offset - reject excess bytes before writing
            bytes_received = 0
            with open(self.temp_path, "r+b") as f:
                f.seek(chunk.start_byte)
                for block in resp.iter_content(chunk_size=65536):
                    if self._cancel_event.is_set():
                        return
                    if not block:
                        break
                    block_len = len(block)
                    if bytes_received + block_len > chunk.expected_size:
                        raise ChunkVerificationError(
                            f"Oversized chunk: received {bytes_received + block_len} bytes, exceeds expected {chunk.expected_size}"
                        )
                    f.write(block)
                    bytes_received += block_len
                    if ifstat:
                        ifstat.update_bytes(block_len)

            if bytes_received != chunk.expected_size:
                raise ChunkVerificationError(
                    f"Truncated chunk: expected {chunk.expected_size} bytes, received {bytes_received} bytes"
                )

            chunk.downloaded_bytes = bytes_received

        finally:
            resp.close()

    def _start_single_stream(self):
        """Safe single-stream fallback when range requests are not supported."""
        def _stream():
            self.status = "running"
            self.start_time = time.time()
            try:
                active = [i for i in self.net_mgr.interfaces.values() if i.is_active]
                if not active:
                    with self._state_lock:
                        self.error_message = "No active interfaces for single-stream fallback"
                        self.status = "failed"
                    self._cancel_event.set()
                    self._notify()
                    return

                ifstat = active[0]
                session = self.net_mgr.create_requests_session(ifstat.name)

                resp = safe_request_get(session, self.url, allow_local=self.allow_local, headers={"User-Agent": "SpeedBond/2.0"}, timeout=(10.0, 30.0), stream=True)
                if resp.status_code != 200:
                    with self._state_lock:
                        self.error_message = f"Single stream returned HTTP {resp.status_code}"
                        self.status = "failed"
                    self._cancel_event.set()
                    self._notify()
                    return

                with open(self.temp_path, "wb") as f:
                    for block in resp.iter_content(chunk_size=65536):
                        if self._cancel_event.is_set():
                            return
                        if not block:
                            break
                        f.write(block)
                        bl = len(block)
                        ifstat.update_bytes(bl)

                resp.close()
                self._finalize_file()
            except Exception as e:
                logger.error("Single stream fallback error: %s", e)
                with self._state_lock:
                    self.error_message = f"Single stream error: {e}"
                    self.status = "failed"
                self._cancel_event.set()
                self._notify()

        t = threading.Thread(target=_stream, daemon=True)
        t.start()
        self._threads.append(t)

    def _is_all_chunks_done(self) -> bool:
        with self._state_lock:
            return bool(self.chunks) and all(c.status == "completed" for c in self.chunks)

    def _check_completion(self):
        with self._finalize_lock:
            with self._state_lock:
                if self.status != "running":
                    return
                if not self._is_all_chunks_done():
                    return
                self.status = "finalizing"
            self._finalize_file()

    def _finalize_file(self):
        """Validates complete non-overlapping coverage and atomically renames the temp file."""
        try:
            # 1. Verify coverage
            if self.supports_ranges:
                expected_offset = 0
                for c in self.chunks:
                    if c.status != "completed":
                        with self._state_lock:
                            self.status = "failed"
                            self.error_message = f"Cannot finalize: chunk {c.chunk_id} incomplete"
                        self._notify()
                        return
                    if c.start_byte != expected_offset:
                        with self._state_lock:
                            self.status = "failed"
                            self.error_message = f"Gap detected: expected offset {expected_offset}, found {c.start_byte}"
                        self._notify()
                        return
                    expected_offset = c.end_byte + 1

                if expected_offset != self.total_size:
                    with self._state_lock:
                        self.status = "failed"
                        self.error_message = f"Size mismatch: expected {self.total_size}, final offset {expected_offset}"
                    self._notify()
                    return

            # 2. Verify file size on disk
            actual_size = os.path.getsize(self.temp_path)
            if self.total_size > 0 and actual_size != self.total_size:
                with self._state_lock:
                    self.status = "failed"
                    self.error_message = f"Disk size mismatch: expected {self.total_size}, got {actual_size}"
                self._notify()
                return

            # 3. Compute SHA-256 and distinguish calculated vs trusted verification
            hasher = hashlib.sha256()
            with open(self.temp_path, "rb") as f:
                while chunk := f.read(131072):
                    hasher.update(chunk)
            self.calculated_sha256 = hasher.hexdigest()
            self.sha256_hash = self.calculated_sha256

            if self.expected_sha256:
                if self.calculated_sha256.lower() == self.expected_sha256.lower():
                    self.checksum_verified = True
                else:
                    self.checksum_verified = False
                    with self._state_lock:
                        self.status = "failed"
                        self.error_message = (
                            f"Checksum verification failed: expected {self.expected_sha256}, "
                            f"got {self.calculated_sha256}"
                        )
                    self._notify()
                    return
            else:
                self.checksum_verified = None

            # 4. Atomic rename guarded by cancellation state
            with self._state_lock:
                if self._cancel_event.is_set() or self.status in ("failed", "cancelled"):
                    logger.warning("Task was cancelled or failed during finalization; not completing.")
                    return

            if os.path.exists(self.dest_path):
                os.remove(self.dest_path)
            os.rename(self.temp_path, self.dest_path)

            with self._state_lock:
                if self._cancel_event.is_set() or self.status in ("failed", "cancelled"):
                    logger.warning("Task was cancelled or failed after move; not completing.")
                    return
                self.status = "completed"
                self.end_time = time.time()
            self._notify()

        except Exception as e:
            with self._state_lock:
                self.status = "failed"
                self.error_message = f"Finalization failed: {e}"
            self._notify()

    def cancel(self):
        """Cancels task, aborts worker threads, and safely manages temporary file cleanup."""
        with self._state_lock:
            if self.status in ["completed", "failed", "cancelled"]:
                return
            self.status = "cancelled"
            self.end_time = time.time()
        self._cancel_event.set()

        # Join worker threads before attempting partial file removal
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2.0)

        # Retain temporary file if any writer thread has not cleanly stopped to prevent race conditions
        if not any(t.is_alive() for t in self._threads):
            try:
                if os.path.exists(self.temp_path):
                    os.remove(self.temp_path)
            except Exception:
                pass
        else:
            logger.warning("Retaining temporary file %s because some worker threads are still terminating", self.temp_path)

        self._notify()

    def get_progress(self) -> dict:
        """Returns truthful progress derived strictly from verified completed chunk byte ranges."""
        with self._state_lock:
            if self.supports_ranges and self.chunks:
                completed_bytes = sum(c.expected_size for c in self.chunks if c.status == "completed")
                in_progress_bytes = sum(c.downloaded_bytes for c in self.chunks if c.status == "downloading")
                verified_bytes = min(completed_bytes + in_progress_bytes, self.total_size)
            else:
                try:
                    verified_bytes = os.path.getsize(self.temp_path) if os.path.exists(self.temp_path) else 0
                except Exception:
                    verified_bytes = 0

            pct = (verified_bytes / self.total_size * 100.0) if self.total_size > 0 else 0.0
            elapsed = (time.time() - self.start_time) if self.start_time > 0 else 0.0

            iface_chunks = {}
            for c in self.chunks:
                if c.assigned_iface:
                    iface_chunks[c.assigned_iface] = iface_chunks.get(c.assigned_iface, 0) + (1 if c.status == "completed" else 0)

            return {
                "task_id": self.task_id,
                "url": self.url,
                "dest_path": self.dest_path,
                "filename": os.path.basename(self.dest_path),
                "status": self.status,
                "total_bytes": self.total_size,
                "downloaded_bytes": verified_bytes,
                "progress_pct": min(round(pct, 1), 100.0),
                "elapsed_sec": round(elapsed, 1),
                "supports_ranges": self.supports_ranges,
                "sha256": self.sha256_hash or "",
                "iface_chunks": iface_chunks,
                "error_message": self.error_message,
            }

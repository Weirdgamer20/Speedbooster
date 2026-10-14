// app.js - Real-time SpeedBond Dashboard Controller with Live Tachometer & Floating HUD

const API_BASE = "";

// State
let pipWindowInstance = null;

// DOM Elements
const totalSpeedEl = document.getElementById("total-speed");
const totalMbpsEl = document.getElementById("total-mbps");
const totalDownloadedEl = document.getElementById("total-downloaded");
const tachoNeedleGroup = document.getElementById("tacho-needle-group");
const tachoActiveArc = document.getElementById("tacho-active-arc");
const tachoLiveVal = document.getElementById("tacho-live-val");
const adaptersGrid = document.getElementById("adapters-grid");
const downloadForm = document.getElementById("download-form");
const downloadUrlInput = document.getElementById("download-url");
const activeDownloadsContainer = document.getElementById("active-downloads");
const refreshBtn = document.getElementById("refresh-btn");
const floatHudBtn = document.getElementById("float-hud-btn");

// Arc circumference for path M 30,130 A 70,70 0 1,1 170,130 ≈ 293
const TACHO_ARC_LENGTH = 293;
const MAX_SPEEDOMETER_SCALE = 50.0; // 0 to 50 MB/s scale

async function fetchStats() {
  try {
    const res = await fetch(`${API_BASE}/api/stats`);
    const data = await res.json();
    renderStats(data);
  } catch (err) {
    console.error("Failed to fetch stats", err);
  }
}

async function fetchDownloads() {
  try {
    const res = await fetch(`${API_BASE}/api/downloads`);
    const downloads = await res.json();
    renderDownloads(downloads);
  } catch (err) {
    console.error("Failed to fetch downloads", err);
  }
}

function renderStats(data) {
  const speedMB = data.total_speed_mbytes_sec || 0;
  const speedMbps = data.total_speed_mbps || 0;
  const totalMB = ((data.total_bytes_downloaded || 0) / (1024 * 1024)).toFixed(2);
  const formattedDigits = speedMB.toFixed(2);

  // Big hero digital readout
  totalSpeedEl.textContent = formattedDigits;
  totalMbpsEl.textContent = `${speedMbps.toFixed(2)} Mbps`;
  totalDownloadedEl.textContent = `${totalMB} MB`;

  // Real-Time Speedometer Dial Animation
  if (tachoNeedleGroup && tachoActiveArc && tachoLiveVal) {
    tachoLiveVal.textContent = formattedDigits;

    // Angle: -120deg (0 MB/s) to +120deg (50 MB/s)
    const ratio = Math.min(speedMB / MAX_SPEEDOMETER_SCALE, 1.0);
    const needleAngle = -120 + (ratio * 240);
    tachoNeedleGroup.setAttribute("transform", `rotate(${needleAngle.toFixed(1)} 100 130)`);

    // Arc fill
    const arcOffset = TACHO_ARC_LENGTH - (ratio * TACHO_ARC_LENGTH);
    tachoActiveArc.style.strokeDashoffset = arcOffset.toFixed(1);
  }

  // Update Floating Picture-in-Picture window if open
  if (pipWindowInstance && !pipWindowInstance.closed) {
    const pSpeed = pipWindowInstance.document.getElementById("pip-speed");
    const pBreakdown = pipWindowInstance.document.getElementById("pip-breakdown");
    if (pSpeed) pSpeed.textContent = formattedDigits;
    if (pBreakdown && data.interfaces && data.interfaces.length >= 2) {
      const s1 = data.interfaces[0].speed_mbytes_sec.toFixed(1);
      const s2 = data.interfaces[1].speed_mbytes_sec.toFixed(1);
      pBreakdown.textContent = `WiFi: ${s1} MB/s | Dongle: ${s2} MB/s`;
    }
  }

  // Render Adapters Grid
  if (!data.interfaces || data.interfaces.length === 0) {
    adaptersGrid.innerHTML = `<div class="loading-placeholder">No active network interfaces detected.</div>`;
    return;
  }

  let html = "";
  data.interfaces.forEach((iface, idx) => {
    const isPrimary = idx === 0;
    const cardClass = isPrimary ? "iface-wifi1" : "iface-wifi2";
    const badgeClass = isPrimary ? "badge-cyan" : "badge-emerald";
    const icon = isPrimary ? "📶" : "📡";

    html += `
      <div class="adapter-card ${cardClass}">
        <div class="card-top">
          <div class="adapter-title-group">
            <span class="adapter-icon">${icon}</span>
            <div>
              <div class="adapter-name">${escapeHtml(iface.label || iface.name)}</div>
              <div class="adapter-device">${escapeHtml(iface.name)} • ${escapeHtml(iface.ip)}</div>
            </div>
          </div>
          <span class="adapter-badge ${badgeClass}">ACTIVE</span>
        </div>

        <div class="adapter-stats-row">
          <div>
            <span class="adapter-speed-val">${iface.speed_mbytes_sec.toFixed(2)}</span>
            <span class="adapter-speed-unit">MB/s</span>
          </div>
          <div style="font-size: 13px; color: #94a3b8; font-family: monospace;">
            ${iface.speed_mbps.toFixed(1)} Mbps
          </div>
        </div>

        <div class="adapter-details-grid">
          <div><span class="detail-label">Public IP:</span> <span class="detail-val">${escapeHtml(iface.public_ip || 'Discovering...')}</span></div>
          <div><span class="detail-label">Ping:</span> <span class="detail-val">${iface.latency_ms} ms</span></div>
          <div><span class="detail-label">Transferred:</span> <span class="detail-val">${(iface.bytes_downloaded / (1024 * 1024)).toFixed(1)} MB</span></div>
          <div><span class="detail-label">Status:</span> <span class="detail-val" style="color:#34d399;">Connected</span></div>
        </div>
      </div>
    `;
  });

  adaptersGrid.innerHTML = html;
}

function renderDownloads(downloads) {
  if (!downloads || downloads.length === 0) {
    activeDownloadsContainer.innerHTML = `<div style="font-size: 13px; color: #64748b; text-align: center; padding: 10px;">No downloads in progress. Paste a link above to test combined speed!</div>`;
    return;
  }

  let html = "";
  downloads.forEach(dl => {
    const totalMB = (dl.total_bytes / (1024 * 1024)).toFixed(1);
    const dldMB = (dl.downloaded_bytes / (1024 * 1024)).toFixed(1);

    let chunksInfo = "";
    if (dl.iface_chunks && Object.keys(dl.iface_chunks).length > 0) {
      chunksInfo = "Slices: " + Object.entries(dl.iface_chunks).map(([iface, cnt]) => `${iface}: ${cnt}`).join(" | ");
    }

    html += `
      <div class="download-item">
        <div class="dl-header">
          <span class="dl-name" title="${escapeHtml(dl.filename)}">${escapeHtml(dl.filename)}</span>
          <span class="dl-pct">${dl.progress_pct}%</span>
        </div>
        <div class="progress-track">
          <div class="progress-bar" style="width: ${dl.progress_pct}%;"></div>
        </div>
        <div class="dl-footer">
          <span>${dldMB} / ${totalMB} MB</span>
          <span>${chunksInfo || dl.status.toUpperCase()}</span>
          <span>${dl.elapsed_sec}s</span>
        </div>
      </div>
    `;
  });

  activeDownloadsContainer.innerHTML = html;
}

// Float HUD Button Click
floatHudBtn.addEventListener("click", async () => {
  // Try modern Document Picture-in-Picture (stays floating on top of Windows desktop)
  if ('documentPictureInPicture' in window) {
    try {
      const pip = await window.documentPictureInPicture.requestWindow({
        width: 290,
        height: 110,
      });

      pip.document.head.innerHTML = `
        <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@700;800&family=JetBrains+Mono:wght@700;800&display=swap" rel="stylesheet">
      `;

      pip.document.body.innerHTML = `
        <div style="background:#090d16; color:#fff; font-family:'Plus Jakarta Sans',sans-serif; padding:12px 16px; height:100vh; box-sizing:border-box; display:flex; flex-direction:column; justify-content:center; border:1.5px solid rgba(168,85,247,0.5); border-radius:12px; overflow:hidden;">
          <div style="font-size:10px; font-weight:800; color:#a855f7; letter-spacing:1px; display:flex; justify-content:space-between; align-items:center;">
            <span>⚡ SPEEDBOND HUD</span>
            <span style="width:7px; height:7px; background:#34d399; border-radius:50%; box-shadow:0 0 8px #34d399;"></span>
          </div>
          <div style="display:flex; align-items:baseline; gap:6px; margin:2px 0;">
            <span id="pip-speed" style="font-family:'JetBrains Mono',monospace; font-size:36px; font-weight:800; color:#fff; text-shadow:0 0 14px rgba(56,189,248,0.5);">${totalSpeedEl.textContent}</span>
            <span style="font-size:14px; font-weight:700; color:#94a3b8;">MB/s</span>
          </div>
          <div id="pip-breakdown" style="font-size:11px; color:#64748b; font-family:'JetBrains Mono',monospace;">WiFi: 0.0 MB/s | Dongle: 0.0 MB/s</div>
        </div>
      `;

      pipWindowInstance = pip;
      return;
    } catch (e) {
      console.log("Picture-in-Picture error, opening popup window instead", e);
    }
  }

  // Fallback: Open popup window
  window.open('/hud.html', 'SpeedometerHUD', 'width=310,height=130,menubar=no,toolbar=no,location=no,status=no,resizable=yes');
});

// Quick samples
document.querySelectorAll(".sample-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    downloadUrlInput.value = btn.getAttribute("data-url");
    downloadUrlInput.setAttribute("data-filename", btn.getAttribute("data-name") || "");
  });
});

// Start download
downloadForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const url = downloadUrlInput.value.trim();
  const filename = downloadUrlInput.getAttribute("data-filename") || "";
  if (!url) return;

  const btn = document.getElementById("start-dl-btn");
  btn.disabled = true;
  btn.textContent = "Starting...";

  try {
    const res = await fetch(`${API_BASE}/api/download`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, filename }),
    });
    const result = await res.json();
    if (result.success) {
      downloadUrlInput.value = "";
      downloadUrlInput.removeAttribute("data-filename");
      fetchDownloads();
    } else {
      alert("Download failed to start: " + (result.error || "Unknown error"));
    }
  } catch (err) {
    alert("Error starting download: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Start Bonded Download";
  }
});

// Refresh button
refreshBtn.addEventListener("click", async () => {
  refreshBtn.style.transform = "rotate(360deg)";
  try {
    await fetch(`${API_BASE}/api/refresh`, { method: "POST" });
    setTimeout(fetchStats, 1000);
  } catch (err) {
    console.error(err);
  }
});

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// Initial loops
fetchStats();
fetchDownloads();
setInterval(fetchStats, 500); // 500ms for smooth real-time needle updates
setInterval(fetchDownloads, 1000);

import asyncio
import json
import logging
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from drivers.gt521s_driver import GT521SDriver
from drivers.bardbox_env_node_v1_driver import SensorDriver as EnvDriver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

# =========================
# CONFIG
# =========================

PORT = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0"
BAUD = 9600

ENV_PORT = "/dev/serial/by-id/usb-Arduino__www.arduino.cc__0043_03536383236351C09231-if00"
ENV_BAUD = 115200

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast?latitude=41.93&longitude=-73.91&current_weather=true"

UID = "bb-0001"

DEFAULT_LOCATION_LABEL = "GoLab"
DEFAULT_LOCATION_ID = 1
DEFAULT_SAMPLE_TIME_S = 10
DEFAULT_HOLD_TIME_S = 50
DEFAULT_SAMPLES = 480

# Unit conversions for UI only
FT3_TO_M3 = 35.3147
PMS_0P1L_TO_M3 = 10000

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

_TIME_STATUS_CACHE: dict | None = None
_TIME_STATUS_CACHE_TS: float = 0.0
_TIME_STATUS_CACHE_TTL_S = 5.0

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def system_time_sane() -> bool:
    # Reject obviously bad time like 1970.
    return utc_now().year >= 2025

def ntp_synced() -> bool:
    try:
        result = subprocess.run(
            ["chronyc", "tracking"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0:
            return False
        return any(
            "Leap status" in line and "Normal" in line
            for line in result.stdout.splitlines()
        )
    except Exception:
        return False

def _compute_time_status() -> dict:
    sane = system_time_sane()
    synced = ntp_synced()

    if sane and synced:
        return {
            "valid": True,
            "sane": True,
            "ntp_synced": True,
            "source": "ntp",
        }

    if sane and not synced:
        # Accept sane RTC-backed/offline holdover time.
        return {
            "valid": True,
            "sane": True,
            "ntp_synced": False,
            "source": "rtc_holdover",
        }

    return {
        "valid": False,
        "sane": False,
        "ntp_synced": False,
        "source": "invalid",
    }

def time_status(force_refresh: bool = False) -> dict:
    global _TIME_STATUS_CACHE, _TIME_STATUS_CACHE_TS

    now_ts = time.time()
    if (
        not force_refresh
        and _TIME_STATUS_CACHE is not None
        and (now_ts - _TIME_STATUS_CACHE_TS) < _TIME_STATUS_CACHE_TTL_S
    ):
        return _TIME_STATUS_CACHE

    status = _compute_time_status()
    _TIME_STATUS_CACHE = status
    _TIME_STATUS_CACHE_TS = now_ts
    return status

def system_time_valid() -> bool:
    return time_status()["valid"]

# =========================
# CLEANROOM STANDARDS
# =========================

_STANDARDS_PATH = Path(__file__).parent / "config" / "cleanroom_standards.json"
try:
    with open(_STANDARDS_PATH) as _f:
        CLEANROOM_STANDARDS = json.load(_f)
    log.info("Cleanroom standards loaded from %s", _STANDARDS_PATH)
except Exception:
    CLEANROOM_STANDARDS = {}
    log.warning("Could not load cleanroom_standards.json — presets disabled")

# =========================
# THRESHOLD SETTINGS
# =========================

class ThresholdSettings(BaseModel):
    threshold_c03: int = Field(default=1000, ge=1, le=999999)
    threshold_c50: int = Field(default=500, ge=1, le=999999)

thresholds = ThresholdSettings()
thresholds_lock = threading.Lock()

# =========================
# RUN SETTINGS MODEL
# =========================

class RunSettings(BaseModel):
    sample_time_s: int = Field(default=DEFAULT_SAMPLE_TIME_S, ge=1, le=9999)
    hold_time_s: int = Field(default=DEFAULT_HOLD_TIME_S, ge=0, le=9999)
    samples: int = Field(default=DEFAULT_SAMPLES, ge=1, le=999)

current_settings = RunSettings()

# =========================
# SESSION DATA
# =========================

class SessionDataPoint(BaseModel):
    ts: str
    c03: int
    c50: int
    exceeded_c03: bool = False
    exceeded_c50: bool = False


class SessionManager:
    def __init__(self):
        self.lock = threading.Lock()
        self._session: Dict[str, Any] = self._empty_session()

    @staticmethod
    def _empty_session() -> Dict[str, Any]:
        return {
            "uid": UID,
            "session_id": None,
            "status": "idle",
            "start_time": None,
            "end_time": None,
            "metadata": {},
            "summary": {},
            "data": [],
        }

    def start(self, metadata: Dict[str, Any]) -> str:
        with self.lock:
            session_id = str(uuid.uuid4())
            self._session = {
                "uid": UID,
                "session_id": session_id,
                "status": "running",
                "start_time": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_time": None,
                "metadata": metadata,
                "summary": {},
                "data": [],
            }
            return session_id

    def append(self, point: SessionDataPoint) -> None:
        with self.lock:
            self._session["data"].append(point)

    def complete(self) -> None:
        with self.lock:
            self._session["status"] = "complete"
            self._session["end_time"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            self._session["summary"] = {
                "total_samples": len(self._session["data"]),
            }

    def error(self, reason: str) -> None:
        with self.lock:
            self._session["status"] = "error"
            self._session["end_time"] = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            self._session["summary"] = {"error": reason}

    def get_data(self) -> List[SessionDataPoint]:
        with self.lock:
            return list(self._session["data"])

    def get_session(self) -> Dict[str, Any]:
        with self.lock:
            s = dict(self._session)
            s["data"] = [dp.dict() for dp in s["data"]]
            return s

    def clear(self) -> None:
        with self.lock:
            self._session = self._empty_session()


session_manager = SessionManager()

# =========================
# GT-521S DRIVER
# =========================

gt = GT521SDriver(uid=UID, port=PORT, baud=BAUD)

time.sleep(3.0)  # let GT settle before opening Arduino port

# =========================
# ENV NODE DRIVER
# =========================

env = EnvDriver(port=ENV_PORT, baud=ENV_BAUD)
try:
    env.connect()
    log.info("ENV: connected")
except Exception:
    log.exception("ENV: failed to connect")

# =========================
# DASHBOARD UI
# =========================

@app.get("/", response_class=HTMLResponse)
def dashboard():
    s = current_settings
    iso_defaults = CLEANROOM_STANDARDS.get("iso_14644_1", {}).get("ISO_3", {})
    default_c03 = iso_defaults.get("0.3", 1000)
    default_c50 = iso_defaults.get("5.0", 500)
    return f"""
    <html>
    <head>
        <title>GoLab Monitor</title>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
        <style>
            :root {{
                --bg: #000;
                --panel: #111;
                --panel-border: #333;
                --text: #fff;
                --muted: #aaa;
                --accent: #4da3ff;
                --accent-hover: #2f85e0;
                --ok: #38d39f;
                --bad: #ff6b6b;
                --safe-bg: #0f2a1f;
                --safe-text: #9ff0c7;
                --exceeded-bg: #3a1212;
                --exceeded-text: #ffb3b3;
                --grid: #333;
            }}

            body {{ font-family: system-ui; padding: 30px; max-width: 1600px; margin: 0 auto; background: var(--bg); color: var(--text); }}
            h1 {{ margin-bottom: 30px; color: var(--text); }}
            h3, h4, label, .graph-title {{ color: var(--text); }}

            .header-row {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:30px; }}
            .header-left {{ display:flex; align-items:center; gap:20px; }}
            .header-title {{ margin:0; font-size:28px; line-height:1; color:var(--text); }}
            .header-clock {{ font-size:18px; line-height:1; color:rgba(255,255,255,0.85); white-space:nowrap; }}

            .controls-row {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 30px; margin-bottom: 40px; }}
            @media (max-width: 900px) {{ .controls-row {{ grid-template-columns: 1fr; }} }}

            .gt-card {{ padding: 20px; border: 1px solid var(--panel-border); border-radius: 10px; background: var(--panel); margin-bottom: 30px; }}
            .gt-card-inner {{ display: grid; grid-template-columns: 320px 1fr 1fr; gap: 30px; }}
            @media (max-width: 1100px) {{ .gt-card-inner {{ grid-template-columns: 1fr; }} }}

            label {{ display:block; margin-top: 12px; font-weight: 600; }}
            input {{ font-size: 16px; padding: 8px; width: 100%; background: var(--panel); color: var(--text); border: 1px solid var(--panel-border); border-radius: 6px; }}
            .card {{ padding: 20px; border: 1px solid var(--panel-border); border-radius: 8px; background: var(--panel); box-shadow: none; }}
            button {{ font-size: 18px; padding: 10px 16px; margin-right: 10px; cursor: pointer; background: var(--accent); color: white; border: none; border-radius: 6px; }}
            button:hover {{ background: var(--accent-hover); }}
            button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
            .muted {{ color: var(--muted); }}
            .small {{ font-size: 13px; }}
            .ok {{ color: var(--ok); font-weight: 700; }}
            .bad {{ color: var(--bad); font-weight: 700; }}

            .graph-card {{ padding: 20px; border: 1px solid var(--panel-border); border-radius: 10px; background: var(--panel); }}
            .graph-title {{ font-size: 18px; font-weight: 700; margin-bottom: 15px; }}
            .graph-container {{ position: relative; height: 400px; margin-bottom: 15px; }}

            .threshold-status {{ display: inline-block; padding: 6px 12px; border-radius: 4px; font-size: 13px; font-weight: 600; margin-top: 10px; }}
            .threshold-status.safe {{ background: var(--safe-bg); color: var(--safe-text); }}
            .threshold-status.exceeded {{ background: var(--exceeded-bg); color: var(--exceeded-text); }}

            .env-grid {{ display:grid; grid-template-columns: repeat(5, 1fr); gap:20px; }}
            @media (max-width: 900px) {{ .env-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
        </style>
    </head>
    <body>
        <div class="header-row">
          <div class="header-left">
            <img src="/static/Bard-Web-Logos/bard-logo-red.png" style="height:60px;"/>
            <h1 class="header-title">Gravitational-wave Optics Lab Environmental Monitor</h1>
          </div>
          <div style="text-align:right;">
            <div id="header-clock" class="header-clock"></div>
            <div id="time-warning" class="small muted" style="display:none; margin-top:6px;"></div>
          </div>
        </div>

        <div class="gt-card">
          <h3 style="margin-top:0; margin-bottom:20px;">GT-521S Particle Counter</h3>
          <div class="gt-card-inner">

            <div>
              <h4 style="margin-top:0;">Run settings</h4>

              <label>Sample Time (seconds)</label>
              <input id="sample_time_s" type="number" min="1" max="9999" value="{s.sample_time_s}"/>

              <label>Hold Time (seconds)</label>
              <input id="hold_time_s" type="number" min="0" max="9999" value="{s.hold_time_s}"/>

              <label>Samples (1–999)</label>
              <input id="samples" type="number" min="1" max="999" value="{s.samples}"/>

              <h4 style="margin-top: 20px; margin-bottom: 15px; border-top: 1px solid #333; padding-top: 15px;">Threshold Settings</h4>

              <label>Preset</label>
              <select id="threshold_preset" style="font-size:16px;padding:8px;width:100%;background:var(--panel);color:var(--text);border:1px solid var(--panel-border);border-radius:6px;">
                <option value="ISO_1">ISO 1</option>
                <option value="ISO_2">ISO 2</option>
                <option value="ISO_3" selected>ISO 3</option>
                <option value="ISO_4">ISO 4</option>
                <option value="ISO_5">ISO 5</option>
                <option value="ISO_6">ISO 6</option>
                <option value="ISO_7">ISO 7</option>
                <option value="ISO_8">ISO 8</option>
                <option value="ISO_9">ISO 9</option>
              </select>

              <input id="threshold_c03" type="hidden" value="{default_c03}"/>
              <input id="threshold_c50" type="hidden" value="{default_c50}"/>

              <p class="muted small" style="margin-top:12px;">
                Start applies settings to the GT, then begins sampling.
              </p>

              <p>
                <button id="start-button" onclick="startRun()">Start</button>
                <button onclick="stopRun()">Stop</button>
              </p>

              <div id="confirm" class="small muted">No action yet.</div>
            </div>

            <div>
              <div class="graph-title">0.3µm Particles</div>
              <div style="font-size: 28px; font-weight: 700; color: var(--accent); margin-bottom: 15px;">
                <span id="current_c03">—</span> <span style="font-size: 16px; color: var(--muted);">/m³</span>
              </div>
              <div class="graph-container">
                <canvas id="chart-c03"></canvas>
              </div>
              <div id="status-c03" class="threshold-status safe">✓ Below Threshold</div>
            </div>

            <div>
              <div class="graph-title">5.0µm Particles</div>
              <div style="font-size: 28px; font-weight: 700; color: var(--accent); margin-bottom: 15px;">
                <span id="current_c50">—</span> <span style="font-size: 16px; color: var(--muted);">/m³</span>
              </div>
              <div class="graph-container">
                <canvas id="chart-c50"></canvas>
              </div>
              <div id="status-c50" class="threshold-status safe">✓ Below Threshold</div>
            </div>

          </div>
        </div>

        <div class="card" style="margin-top: 20px;">
          <h3>Environment Node (trend only)</h3>
          <div class="env-grid">
            <div><div class="small muted">&gt;0.3µm /m³</div><div id="env_c03" style="font-size:28px;font-weight:700;">—</div></div>
            <div><div class="small muted">&gt;0.5µm /m³</div><div id="env_c05" style="font-size:28px;font-weight:700;">—</div></div>
            <div><div class="small muted">&gt;1.0µm /m³</div><div id="env_c10" style="font-size:28px;font-weight:700;">—</div></div>
            <div><div class="small muted">Temp (°C)</div><div id="env_temp" style="font-size:28px;font-weight:700;">—</div></div>
            <div><div class="small muted">RH (%)</div><div id="env_rh" style="font-size:28px;font-weight:700;">—</div></div>
          </div>
        </div>

        <script>
            let chartC03 = null;
            let chartC50 = null;
            let pollInterval = null;
            let wasRunning = false;

            const FT3_TO_M3 = {FT3_TO_M3};
            const PMS_0P1L_TO_M3 = {PMS_0P1L_TO_M3};

            function gtFt3ToM3(value) {{
              if (value === null || value === undefined) return null;
              return Math.round(value * FT3_TO_M3);
            }}

            function pmsCountToM3(value) {{
              if (value === null || value === undefined) return null;
              return value * PMS_0P1L_TO_M3;
            }}

            function initializeCharts() {{
              const s = getSettings();
              const sessionDurationSeconds = (s.sample_time_s + s.hold_time_s) * s.samples;
              const tC03 = parseInt(document.getElementById("threshold_c03").value);
              const tC50 = parseInt(document.getElementById("threshold_c50").value);

              createOrUpdateChart("chart-c03", [], tC03, sessionDurationSeconds);
              createOrUpdateChart("chart-c50", [], tC50, sessionDurationSeconds);
            }}

            function getSettings() {{
              return {{
                sample_time_s: parseInt(document.getElementById("sample_time_s").value || "10"),
                hold_time_s: parseInt(document.getElementById("hold_time_s").value || "50"),
                samples: parseInt(document.getElementById("samples").value || "480"),
              }};
            }}

            function getThresholds() {{
              return {{
                threshold_c03: parseInt(document.getElementById("threshold_c03").value),
                threshold_c50: parseInt(document.getElementById("threshold_c50").value),
              }};
            }}

            function updateComputed() {{
              initializeCharts();
            }}

            ["sample_time_s","hold_time_s","samples"].forEach(id => {{
              document.getElementById(id).addEventListener("input", updateComputed);
            }});
            updateComputed();

            async function startRun() {{
              const c = document.getElementById("confirm");
              c.className = "small muted";
              c.textContent = "Applying settings...";

              try {{
                const settings = getSettings();
                const r = await fetch("/gt/start", {{
                  method: "POST",
                  headers: {{ "Content-Type": "application/json" }},
                  body: JSON.stringify(settings),
                }});

                const j = await r.json();

                if (!r.ok) {{
                  c.className = "small bad";
                  c.textContent = j.error || `Start failed (HTTP ${{r.status}})`;
                  return;
                }}

                if (j.ok) {{
                  c.className = "small ok";
                  c.textContent = `Applied @ ${{j.applied_at}}`;
                  startGraphPolling();
                }} else {{
                  c.className = "small bad";
                  c.textContent = j.error || `Start failed @ ${{j.applied_at}}`;
                }}
              }} catch (e) {{
                c.className = "small bad";
                c.textContent = "Start error";
                console.error(e);
              }}
            }}

            async function stopRun() {{
              const c = document.getElementById("confirm");
              c.className = "small muted";
              c.textContent = "Stopping...";

              try {{
                const r = await fetch("/gt/stop", {{ method: "POST" }});
                const j = await r.json();
                c.className = j.ok ? "small ok" : "small bad";
                c.textContent = j.ok ? `Stopped @ ${{j.at}}` : `Stop failed`;
                stopGraphPolling();
              }} catch (e) {{
                c.className = "small bad";
                c.textContent = "Stop error";
              }}
            }}

            async function pollLatest() {{
              try {{
                const r = await fetch("/gt/latest");
                const j = await r.json();
                if (j && j.latest) {{
                  const c03m3 = gtFt3ToM3(j.latest.data?.c03);
                  const c50m3 = gtFt3ToM3(j.latest.data?.c50);
                  document.getElementById("current_c03").textContent = (c03m3 ?? "—").toString();
                  document.getElementById("current_c50").textContent = (c50m3 ?? "—").toString();
                }}
              }} catch (e) {{}}
            }}

            async function pollEnv() {{
              try {{
                const r = await fetch("/env/latest");
                const j = await r.json();

                if (j && j.latest) {{
                  const d = j.latest.data || {{}};
                  const x = j.latest.extended || {{}};

                  document.getElementById("env_c03").textContent = (pmsCountToM3(d.c03) ?? "—").toString();
                  document.getElementById("env_c05").textContent = (pmsCountToM3(x.c05) ?? "—").toString();
                  document.getElementById("env_c10").textContent = (pmsCountToM3(x.c10) ?? "—").toString();
                  document.getElementById("env_temp").textContent = (d.temp_c ?? "—").toString();
                  document.getElementById("env_rh").textContent = (x.rh_pct ?? "—").toString();
                }}
              }} catch (e) {{}}
            }}

            async function fetchSessionData() {{
              try {{
                const r = await fetch("/gt/session-data");
                const j = await r.json();
                return j.data || [];
              }} catch (e) {{
                console.error("Failed to fetch session data:", e);
                return [];
              }}
            }}

            let sessionStartTime = null;
            function getElapsedSeconds(timestamp) {{
              if (!sessionStartTime) {{
                sessionStartTime = new Date(timestamp).getTime();
              }}
              const currentTime = new Date(timestamp).getTime();
              return Math.floor((currentTime - sessionStartTime) / 1000);
            }}

            function createOrUpdateChart(canvasId, data, thresholdM3, sessionDurationSeconds) {{
              const ctx = document.getElementById(canvasId).getContext("2d");
              const dataPoints = [];

              data.forEach(d => {{
                const elapsed = getElapsedSeconds(d.ts);
                const countFt3 = canvasId === "chart-c03" ? d.c03 : d.c50;
                const exceeded = canvasId === "chart-c03" ? d.exceeded_c03 : d.exceeded_c50;
                const countM3 = gtFt3ToM3(countFt3);

                if (countM3 !== undefined && countM3 !== null && elapsed <= sessionDurationSeconds) {{
                  dataPoints.push({{
                    x: elapsed,
                    y: Math.max(countM3, 1),
                    color: exceeded ? "#ff6b6b" : "#4da3ff"
                  }});
                }}
              }});

              const chartId = canvasId === "chart-c03" ? 0 : 1;
              const existingChart = chartId === 0 ? chartC03 : chartC50;

              const chartConfig = {{
                type: "scatter",
                data: {{
                  datasets: (() => {{
                    const datasets = [
                      {{
                        label: "Particle Count",
                        data: dataPoints.map(p => ({{ x: p.x, y: p.y }})),
                        backgroundColor: dataPoints.map(p => p.color),
                        borderWidth: 0,
                        pointRadius: 4,
                        pointBorderColor: dataPoints.map(p => p.color),
                        pointBorderWidth: 1,
                        showLine: true,
                        borderColor: "#4da3ff",
                        fill: false,
                        tension: 0.2,
                      }}
                    ];
                    if (thresholdM3 !== null && thresholdM3 !== undefined && !Number.isNaN(thresholdM3)) {{
                      datasets.push({{
                        label: "Threshold",
                        data: [
                          {{ x: 0, y: thresholdM3 }},
                          {{ x: sessionDurationSeconds, y: thresholdM3 }}
                        ],
                        borderColor: "#ff6b6b",
                        borderDash: [5, 5],
                        borderWidth: 2,
                        pointRadius: 0,
                        showLine: true,
                        fill: false,
                      }});
                    }}
                    return datasets;
                  }})(),
                }},
                options: {{
                  responsive: true,
                  maintainAspectRatio: false,
                  animation: false,
                  plugins: {{
                    legend: {{ display: true, position: "top", labels: {{ color: "#fff" }} }},
                    tooltip: {{ enabled: true }}
                  }},
                  scales: {{
                    y: {{
                      type: "logarithmic",
                      title: {{ display: true, text: "count/m³ (log scale)", color: "#fff" }},
                      ticks: {{ color: "#fff" }},
                      grid: {{ color: "#333" }},
                      min: 1,
                      max: 110000000,
                    }},
                    x: {{
                      type: "linear",
                      min: 0,
                      max: sessionDurationSeconds,
                      title: {{ display: true, text: "Elapsed Time (HH:MM:SS)", color: "#fff" }},
                      ticks: {{
                        color: "#fff",
                        callback: function(value) {{
                          const h = Math.floor(value / 3600).toString().padStart(2, '0');
                          const m = Math.floor((value % 3600) / 60).toString().padStart(2, '0');
                          const s = (value % 60).toString().padStart(2, '0');
                          return h + ':' + m + ':' + s;
                        }}
                      }},
                      grid: {{ color: "#333" }}
                    }},
                  }},
                }},
              }};

              if (existingChart) {{
                existingChart.destroy();
              }}

              const newChart = new Chart(ctx, chartConfig);
              if (chartId === 0) {{
                chartC03 = newChart;
              }} else {{
                chartC50 = newChart;
              }}

              return newChart;
            }}

            function startGraphPolling() {{
              if (pollInterval) clearInterval(pollInterval);
              sessionStartTime = null;

              pollInterval = setInterval(async () => {{
                const data = await fetchSessionData();
                if (data.length === 0) return;

                const s = getSettings();
                const sessionDurationSeconds = (s.sample_time_s + s.hold_time_s) * s.samples;
                const tC03 = parseInt(document.getElementById("threshold_c03").value);
                const tC50 = parseInt(document.getElementById("threshold_c50").value);

                createOrUpdateChart("chart-c03", data, tC03, sessionDurationSeconds);
                createOrUpdateChart("chart-c50", data, tC50, sessionDurationSeconds);

                const last = data[data.length - 1];
                const sC03 = document.getElementById("status-c03");
                const sC50 = document.getElementById("status-c50");

                sC03.className = last.exceeded_c03 ? "threshold-status exceeded" : "threshold-status safe";
                sC03.textContent = last.exceeded_c03 ? "⚠ EXCEEDED" : "✓ Below Threshold";

                sC50.className = last.exceeded_c50 ? "threshold-status exceeded" : "threshold-status safe";
                sC50.textContent = last.exceeded_c50 ? "⚠ EXCEEDED" : "✓ Below Threshold";

                document.getElementById("current_c03").textContent = (gtFt3ToM3(last.c03) ?? "—").toString();
                document.getElementById("current_c50").textContent = (gtFt3ToM3(last.c50) ?? "—").toString();
              }}, 1000);
            }}

            function stopGraphPolling() {{
              if (pollInterval) {{
                clearInterval(pollInterval);
                pollInterval = null;
              }}
            }}

            async function pollState() {{
              try {{
                const r = await fetch("/state");
                const j = await r.json();

                const editingIds = ["sample_time_s","hold_time_s","samples"];
                const userEditing = editingIds.includes(document.activeElement?.id);
                if (!userEditing) {{
                  document.getElementById("sample_time_s").value = j.settings.sample_time_s;
                  document.getElementById("hold_time_s").value = j.settings.hold_time_s;
                  document.getElementById("samples").value = j.settings.samples;
                }}


                const c = document.getElementById("confirm");
                if (j.run_active) {{
                  c.className = "small ok";
                  c.textContent = `Running — ${{j.received_samples}} / ${{j.target_samples}} samples`;
                  if (!wasRunning) {{
                    sessionStartTime = null;
                    startGraphPolling();
                  }}
                }} else {{
                  if (wasRunning) {{
                    c.className = "small muted";
                    c.textContent = "Run complete.";
                    stopGraphPolling();
                  }}
                }}

                wasRunning = j.run_active;
              }} catch (e) {{}}
            }}

            // =========================
            // CLEANROOM PRESET LOGIC
            // =========================

            const CLEANROOM_PRESETS = {json.dumps(CLEANROOM_STANDARDS.get("iso_14644_1", {}))};

            function applyPreset(presetKey) {{
              const preset = CLEANROOM_PRESETS[presetKey];
              if (!preset) return;

              const v03 = (preset["0.3"] !== null && preset["0.3"] !== undefined) ? preset["0.3"] : null;
              const v50 = (preset["5.0"] !== null && preset["5.0"] !== undefined) ? preset["5.0"] : null;

              document.getElementById("threshold_c03").value = v03 !== null ? v03 : "";
              document.getElementById("threshold_c50").value = v50 !== null ? v50 : "";

              const payload = {{
                threshold_c03: v03 !== null ? v03 : 999999,
                threshold_c50: v50 !== null ? v50 : 999999,
              }};

              fetch("/gt/thresholds", {{
                method: "POST",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify(payload),
              }})
                .then(async () => {{
                  const data = await fetchSessionData();
                  const s = getSettings();
                  const sessionDurationSeconds = (s.sample_time_s + s.hold_time_s) * s.samples;
                  createOrUpdateChart("chart-c03", data, v03, sessionDurationSeconds);
                  createOrUpdateChart("chart-c50", data, v50, sessionDurationSeconds);
                }})
                .catch(e => console.error("Failed to apply preset thresholds:", e));
            }}

            document.getElementById("threshold_preset").addEventListener("change", function() {{
              applyPreset(this.value);
            }});
            
            applyPreset("ISO_3");
            setInterval(pollLatest, 1000);
            setInterval(pollEnv, 1000);
            setInterval(pollState, 2000);
            pollState();
            pollLatest();

            async function updateHeaderClock() {{
              const clockEl = document.getElementById("header-clock");
              const warnEl = document.getElementById("time-warning");
              const startBtn = document.getElementById("start-button");
              if (!clockEl) return;

              try {{
                const r = await fetch("/time");
                const j = await r.json();

                clockEl.textContent = j.local || "—";

                if (j.source === "ntp") {{
                  if (warnEl) {{
                    warnEl.style.display = "none";
                    warnEl.textContent = "";
                    warnEl.className = "small muted";
                  }}
                  if (startBtn) startBtn.disabled = false;
                }} else if (j.source === "rtc_holdover") {{
                  if (warnEl) {{
                    warnEl.style.display = "block";
                    warnEl.className = "small muted";
                    warnEl.textContent = "TIME OK — RTC holdover (NTP not currently synced)";
                  }}
                  if (startBtn) startBtn.disabled = false;
                }} else {{
                  if (warnEl) {{
                    warnEl.style.display = "block";
                    warnEl.className = "small bad";
                    warnEl.textContent = "TIME INVALID — RTC/NTP sync required before logging";
                  }}
                  if (startBtn) startBtn.disabled = true;
                }}
              }} catch (e) {{
                if (warnEl) {{
                  warnEl.style.display = "block";
                  warnEl.className = "small bad";
                  warnEl.textContent = "TIME STATUS UNKNOWN — backend time check failed";
                }}
                if (startBtn) startBtn.disabled = true;
              }}
            }}
            setInterval(updateHeaderClock, 1000);
            updateHeaderClock();
        </script>
    </body>
    </html>
    """

# =========================
# CONTROL ENDPOINTS
# =========================

@app.post("/gt/start")
def start(settings: RunSettings):
    global current_settings
    if not system_time_valid():
        return JSONResponse({
            "ok": False,
            "error": "System time invalid; RTC/NTP sync required before logging."
        }, status_code=503)

    current_settings = settings
    applied_at = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")

    settings_dict = {
        "sample_time_s": settings.sample_time_s,
        "hold_time_s": settings.hold_time_s,
        "samples": settings.samples,
    }

    def on_sample(parsed: Dict[str, Any]) -> None:
        c03_m3 = round(parsed.get("c03", 0) * FT3_TO_M3)
        c50_m3 = round(parsed.get("c50", 0) * FT3_TO_M3)
        with thresholds_lock:
            exceeded_c03 = c03_m3 > thresholds.threshold_c03
            exceeded_c50 = c50_m3 > thresholds.threshold_c50
        dp = SessionDataPoint(
            ts=utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            c03=parsed.get("c03", 0),
            c50=parsed.get("c50", 0),
            exceeded_c03=exceeded_c03,
            exceeded_c50=exceeded_c50,
        )
        session_manager.append(dp)

    try:
        result = gt.start_session(settings_dict, on_sample=on_sample)
        session_id = session_manager.start(metadata=settings_dict)
        log.info("GT: session started — id=%s", session_id)
        return JSONResponse({
            "ok": True,
            "session_id": session_id,
            "applied_at": applied_at,
            "requested": settings_dict,
            "applied": result.get("applied", {}),
            "mismatch": result.get("mismatch", {}),
            "readback_ok": not bool(result.get("mismatch")),
            "op_status": result.get("op_status"),
            "expected_cycle_s": settings.sample_time_s + settings.hold_time_s,
            "expected_duration_s": (settings.sample_time_s + settings.hold_time_s) * settings.samples,
        })
    except Exception as e:
        log.exception("GT: start_session failed")
        session_manager.error(str(e))
        return JSONResponse({
            "ok": False,
            "applied_at": applied_at,
            "requested": settings_dict,
            "error": str(e),
        }, status_code=500)

@app.post("/gt/stop")
def stop():
    at = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    result = gt.stop()
    session_manager.complete()
    log.info("GT: session stopped — stopped=%s op=%s", result.get("stopped"), result.get("op_status"))
    return JSONResponse({"ok": result.get("stopped", False), "at": at})

@app.get("/gt/latest")
def get_latest():
    return JSONResponse({"latest": gt.get_reading()})

@app.get("/env/latest")
def get_env_latest():
    try:
        return JSONResponse({"latest": env.get_reading()})
    except Exception as e:
        return JSONResponse({"latest": None, "error": str(e)})

@app.get("/gt/session-data")
def get_session_data():
    data = session_manager.get_data()
    return JSONResponse({"data": [dp.dict() for dp in data]})

@app.get("/gt/thresholds")
def get_thresholds():
    with thresholds_lock:
        return JSONResponse({
            "threshold_c03": thresholds.threshold_c03,
            "threshold_c50": thresholds.threshold_c50,
        })

@app.post("/gt/thresholds")
def set_thresholds(settings: ThresholdSettings):
    global thresholds
    with thresholds_lock:
        thresholds = settings
    return JSONResponse({"ok": True, "thresholds": settings.dict()})

@app.get("/presets")
def get_presets():
    iso = CLEANROOM_STANDARDS.get("iso_14644_1", {})
    result = {}
    for key, val in iso.items():
        if key == "units":
            continue
        result[key] = {
            "0.3": val.get("0.3"),
            "5.0": val.get("5.0"),
        }
    return JSONResponse({"iso_14644_1": result, "units": iso.get("units", "particles/m3")})

@app.get("/gt/status")
def status():
    return JSONResponse(gt.get_state())

@app.get("/state")
def get_state():
    with thresholds_lock:
        t = {
            "threshold_c03": thresholds.threshold_c03,
            "threshold_c50": thresholds.threshold_c50,
        }
    status = time_status()
    state = gt.get_state()
    state.update({
        "settings": current_settings.dict(),
        "thresholds": t,
        "last_update": time.time(),
        "time": {
            "utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valid": status["valid"],
            "sane": status["sane"],
            "ntp_synced": status["ntp_synced"],
            "source": status["source"],
        },
    })
    return JSONResponse(state)

@app.get("/time")
def get_time():
    now_utc = utc_now()
    now_local = now_utc.astimezone()
    status = time_status()
    return JSONResponse({
        "utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "local": now_local.strftime("%a %b %-d, %-I:%M:%S %p"),
        "valid": status["valid"],
        "sane": status["sane"],
        "ntp_synced": status["ntp_synced"],
        "source": status["source"],
    })

# =========================
# OUTDOOR TEMP WEBSOCKET
# =========================

async def fetch_outdoor_temp_c() -> float | None:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(OPEN_METEO_URL)
            r.raise_for_status()
            data = r.json()
            return float(data["current_weather"]["temperature"])
    except Exception:
        return None

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    while True:
        temp = await fetch_outdoor_temp_c()
        await websocket.send_json({"temperature": temp})
        await asyncio.sleep(10)

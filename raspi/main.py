import asyncio
import logging
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
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
                "start_time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
            self._session["end_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self._session["summary"] = {
                "total_samples": len(self._session["data"]),
            }

    def error(self, reason: str) -> None:
        with self.lock:
            self._session["status"] = "error"
            self._session["end_time"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    return f"""
    <html>
    <head>
        <title>GoLab Monitor</title>
        <script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/3.9.1/chart.min.js"></script>
        <style>
            body {{ font-family: system-ui; padding: 30px; max-width: 1600px; margin: 0 auto; background: #f5f5f5; }}
            h1 {{ margin-bottom: 30px; }}

            .controls-row {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 30px; margin-bottom: 40px; }}
            @media (max-width: 900px) {{ .controls-row {{ grid-template-columns: 1fr; }} }}

            label {{ display:block; margin-top: 12px; font-weight: 600; }}
            input {{ font-size: 16px; padding: 8px; width: 100%; }}
            .card {{ padding: 20px; border: 1px solid #ddd; border-radius: 8px; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            button {{ font-size: 18px; padding: 10px 16px; margin-right: 10px; cursor: pointer; background: #0071e3; color: white; border: none; border-radius: 6px; }}
            button:hover {{ background: #0062cc; }}
            .muted {{ color:#666; }}
            .small {{ font-size: 13px; }}
            .ok {{ color: #0a7; font-weight: 700; }}
            .bad {{ color: #c22; font-weight: 700; }}

            .graph-card {{ padding: 20px; border: 1px solid #ddd; border-radius: 10px; background: white; }}
            .graph-title {{ font-size: 18px; font-weight: 700; margin-bottom: 15px; }}
            .graph-container {{ position: relative; height: 400px; margin-bottom: 15px; }}
            .threshold-status {{ display: inline-block; padding: 6px 12px; border-radius: 4px; font-size: 13px; font-weight: 600; margin-top: 10px; }}
            .threshold-status.safe {{ background: #d4edda; color: #155724; }}
            .threshold-status.exceeded {{ background: #f8d7da; color: #721c24; }}
            .env-grid {{ display:grid; grid-template-columns: repeat(5, 1fr); gap:20px; }}
            @media (max-width: 900px) {{ .env-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
        </style>
    </head>
    <body>
        <h1>GT-521S Particle Monitor</h1>

        <div class="controls-row">
          <div class="card">
            <h3>Run settings</h3>

            <label>Sample Time (seconds)</label>
            <input id="sample_time_s" type="number" min="1" max="9999" value="{s.sample_time_s}"/>

            <label>Hold Time (seconds)</label>
            <input id="hold_time_s" type="number" min="0" max="9999" value="{s.hold_time_s}"/>

            <label>Samples (1–999)</label>
            <input id="samples" type="number" min="1" max="999" value="{s.samples}"/>

            <h4 style="margin-top: 20px; margin-bottom: 15px; border-top: 1px solid #ddd; padding-top: 15px;">Threshold Settings</h4>

            <label>0.3µm Threshold (count/ft³)</label>
            <input id="threshold_c03" type="number" value="1000" min="1" max="999999"/>

            <label>5.0µm Threshold (count/ft³)</label>
            <input id="threshold_c50" type="number" value="500" min="1" max="999999"/>

            <p class="muted small" style="margin-top:12px;">
              Start applies settings to the GT, then begins sampling.
            </p>

            <p>
              <button onclick="startRun()">Start</button>
              <button onclick="stopRun()">Stop</button>
            </p>

            <div id="confirm" class="small muted">No action yet.</div>
            <div id="last_update" class="small muted" style="margin-top:6px;"></div>
          </div>

          <div class="graph-card">
            <div class="graph-title">0.3µm Particles</div>
            <div style="font-size: 28px; font-weight: 700; color: #0071e3; margin-bottom: 15px;">
              <span id="current_c03">—</span> <span style="font-size: 16px; color: #666;">/m³</span>
            </div>
            <div class="graph-container">
              <canvas id="chart-c03"></canvas>
            </div>
            <div id="status-c03" class="threshold-status safe">✓ Below Threshold</div>
          </div>

          <div class="graph-card">
            <div class="graph-title">5.0µm Particles</div>
            <div style="font-size: 28px; font-weight: 700; color: #0071e3; margin-bottom: 15px;">
              <span id="current_c50">—</span> <span style="font-size: 16px; color: #666;">/m³</span>
            </div>
            <div class="graph-container">
              <canvas id="chart-c50"></canvas>
            </div>
            <div id="status-c50" class="threshold-status safe">✓ Below Threshold</div>
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
              if (!pollInterval) {{
                initializeCharts();
              }}
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

                if (!r.ok) {{
                  throw new Error(`HTTP ${{r.status}}`);
                }}

                const j = await r.json();

                if (j.ok) {{
                  c.className = "small ok";
                  c.textContent = `Applied @ ${{j.applied_at}}`;
                  startGraphPolling();
                }} else {{
                  c.className = "small bad";
                  c.textContent = `Start failed @ ${{j.applied_at}}`;
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

            function createOrUpdateChart(canvasId, data, thresholdFt3, sessionDurationSeconds) {{
              const ctx = document.getElementById(canvasId).getContext("2d");
              const thresholdM3 = thresholdFt3 * FT3_TO_M3;
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
                    color: exceeded ? "#c22" : "#0071e3"
                  }});
                }}
              }});

              const chartId = canvasId === "chart-c03" ? 0 : 1;
              const existingChart = chartId === 0 ? chartC03 : chartC50;

              const thresholdData = [
                {{ x: 0, y: thresholdM3 }},
                {{ x: sessionDurationSeconds, y: thresholdM3 }}
              ];

              const chartConfig = {{
                type: "scatter",
                data: {{
                  datasets: [
                    {{
                      label: "Particle Count",
                      data: dataPoints.map(p => ({{ x: p.x, y: p.y }})),
                      backgroundColor: dataPoints.map(p => p.color),
                      borderWidth: 0,
                      pointRadius: 4,
                      pointBorderColor: dataPoints.map(p => p.color),
                      pointBorderWidth: 1,
                      showLine: true,
                      borderColor: "#0071e3",
                      fill: false,
                      borderWidth: 2,
                      tension: 0.2,
                    }},
                    {{
                      label: "Threshold",
                      data: thresholdData,
                      borderColor: "#c22",
                      borderDash: [5, 5],
                      borderWidth: 2,
                      pointRadius: 0,
                      showLine: true,
                      fill: false,
                    }},
                  ],
                }},
                options: {{
                  responsive: true,
                  maintainAspectRatio: false,
                  animation: false,
                  plugins: {{
                    legend: {{ display: true, position: "top" }},
                    tooltip: {{ enabled: true }}
                  }},
                  scales: {{
                    y: {{
                      type: "logarithmic",
                      title: {{ display: true, text: "count/m³ (log scale)" }},
                      min: 1,
                      max: 110000000,
                    }},
                    x: {{
                      type: "linear",
                      min: 0,
                      max: sessionDurationSeconds,
                      title: {{ display: true, text: "Elapsed Time (HH:MM:SS)" }},
                      ticks: {{
                        callback: function(value) {{
                          const h = Math.floor(value / 3600).toString().padStart(2, '0');
                          const m = Math.floor((value % 3600) / 60).toString().padStart(2, '0');
                          const s = (value % 60).toString().padStart(2, '0');
                          return h + ':' + m + ':' + s;
                        }}
                      }}
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

                const editingIds = ["sample_time_s","hold_time_s","samples","threshold_c03","threshold_c50"];
                const userEditing = editingIds.includes(document.activeElement?.id);
                if (!userEditing) {{
                  document.getElementById("sample_time_s").value = j.settings.sample_time_s;
                  document.getElementById("hold_time_s").value = j.settings.hold_time_s;
                  document.getElementById("samples").value = j.settings.samples;
                  document.getElementById("threshold_c03").value = j.thresholds.threshold_c03;
                  document.getElementById("threshold_c50").value = j.thresholds.threshold_c50;
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
                const ts = new Date(j.last_update * 1000).toLocaleTimeString();
                document.getElementById("last_update").textContent = `State as of ${{ts}}`;
              }} catch (e) {{}}
            }}

            initializeCharts();
            setInterval(pollLatest, 1000);
            setInterval(pollEnv, 1000);
            setInterval(pollState, 2000);
            pollState();
            pollLatest();
            pollEnv();
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
    current_settings = settings
    applied_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    settings_dict = {
        "sample_time_s": settings.sample_time_s,
        "hold_time_s": settings.hold_time_s,
        "samples": settings.samples,
    }

    def on_sample(parsed: Dict[str, Any]) -> None:
        with thresholds_lock:
            exceeded_c03 = parsed.get("c03", 0) > thresholds.threshold_c03
            exceeded_c50 = parsed.get("c50", 0) > thresholds.threshold_c50
        dp = SessionDataPoint(
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
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
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
    state = gt.get_state()
    state.update({
        "settings": current_settings.dict(),
        "thresholds": t,
        "last_update": time.time(),
    })
    return JSONResponse(state)

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
import asyncio
import time
import threading
import re
import traceback
from typing import Optional, Dict, Any, Tuple, List

import httpx
import serial
from fastapi import FastAPI, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# =========================
# CONFIG
# =========================

PORT = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0"
BAUD = 9600

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast?latitude=41.93&longitude=-73.91&current_weather=true"

DEFAULT_LOCATION_LABEL = "GoLab"
DEFAULT_LOCATION_ID = 1
DEFAULT_SAMPLE_TIME_S = 10
DEFAULT_HOLD_TIME_S = 50
DEFAULT_SAMPLES = 480

app = FastAPI()

# =========================
# THRESHOLD SETTINGS
# =========================

class ThresholdSettings(BaseModel):
    threshold_0p3: int = Field(default=1000, ge=1, le=999999)
    threshold_5p0: int = Field(default=500, ge=1, le=999999)

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
    count_0p3: int
    count_5p0: int
    exceeded_0p3: bool = False
    exceeded_5p0: bool = False

session_data: List[SessionDataPoint] = []
session_lock = threading.Lock()

# =========================
# LATEST READING (in-memory)
# =========================

latest_lock = threading.Lock()
latest_reading: Optional[Dict[str, Any]] = None

# =========================
# GT-521S CONTROLLER (ORIGINAL - UNCHANGED)
# =========================

class GT521:
    def __init__(self):
        self.ser: Optional[serial.Serial] = None
        self.lock = threading.Lock()

        self.reader_thread: Optional[threading.Thread] = None
        self.reader_stop = threading.Event()
        self.reader_running = False

        self.run_active = False
        self.target_samples = 0
        self.received_samples = 0

    def open(self):
        if self.ser and self.ser.is_open:
            return

        self.ser = serial.Serial(
            PORT,
            BAUD,
            timeout=0,   # non-blocking
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

        try:
            self.ser.dtr = True
            self.ser.rts = True
        except Exception:
            pass

        time.sleep(1.0)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

    def _read_for(self, seconds=1.0) -> bytes:
        end = time.time() + seconds
        out = b""
        while time.time() < end:
            n = self.ser.in_waiting if self.ser else 0
            if n:
                out += self.ser.read(n)
            else:
                time.sleep(0.02)
        return out

    def _poke_until_star(self) -> bytes:
        seen = b""
        if not self.ser:
            return seen
        for _ in range(12):
            self.ser.write(b"\r")
            self.ser.flush()
            time.sleep(0.08)
            chunk = self._read_for(0.6)
            if chunk:
                seen += chunk
                if b"*" in chunk:
                    return seen
        return seen

    def send_line(self, line: bytes, read_seconds: float = 1.2) -> Tuple[bool, bytes]:
        """
        Send command (CR terminated) and collect response.
        ok=True means we saw a '*' prompt (device responsive).
        IMPORTANT: This holds self.lock for the whole transaction so the reader
        cannot consume replies mid-command.
        """
        with self.lock:
            if not self.ser:
                return False, b"(serial not open)"

            all_seen = b""
            for _ in range(3):
                all_seen += self._poke_until_star()

                try:
                    self.ser.reset_input_buffer()
                except Exception:
                    pass

                self.ser.write(line + b"\r")
                self.ser.flush()
                time.sleep(0.12)

                resp = self._read_for(read_seconds)
                all_seen += resp

                if b"*" in resp:
                    return True, all_seen

                all_seen += self._poke_until_star()

            return (b"*" in all_seen), all_seen

    # ---- basic ----
    def start(self): return self.send_line(b"S", read_seconds=0.9)
    def stop(self):  return self.send_line(b"E", read_seconds=0.9)
    def op_status(self): return self.send_line(b"OP", read_seconds=0.9)

    # ---- settings ----
    def set_location_id(self, loc_id: int): return self.send_line(f"ID {loc_id:03d}".encode(), read_seconds=0.9)
    def set_sample_time(self, sec: int):    return self.send_line(f"ST {sec:04d}".encode(), read_seconds=0.9)
    def set_hold_time(self, sec: int):      return self.send_line(f"SH {sec:04d}".encode(), read_seconds=0.9)
    def set_samples(self, n: int):          return self.send_line(f"SN {n:03d}".encode(), read_seconds=0.9)
    def set_report_csv(self):               return self.send_line(b"SR 1", read_seconds=0.9)

    def read_settings_report(self) -> Tuple[bool, str]:
        ok, raw = self.send_line(b"1", read_seconds=2.0)
        return ok, raw.decode(errors="replace")

    @staticmethod
    def _parse_measurement_line(line: str):
        line = line.strip()
        if not line:
            return None

        line = line.lstrip("*").strip()

        # Expect timestamp prefix
        if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},", line):
            return None

        # Strip checksum suffix "*xxxxx"
        if "*" in line:
            line = line.split("*", 1)[0].strip().rstrip(",")

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            return None

        ts = parts[0]
        try:
            size1 = float(parts[1]); cnt1 = int(parts[2])
            size2 = float(parts[3]); cnt2 = int(parts[4])
        except Exception:
            return None

        out = {"ts": ts}

        if abs(size1 - 0.3) < 0.11:
            out["count_0p3"] = cnt1
        if abs(size1 - 5.0) < 0.11:
            out["count_5p0"] = cnt1
        if abs(size2 - 0.3) < 0.11:
            out["count_0p3"] = cnt2
        if abs(size2 - 5.0) < 0.11:
            out["count_5p0"] = cnt2

        return out

    # =========================
    # Reader thread
    # =========================
    def _reader_loop(self):
        buf = b""
        self.reader_running = True
        try:
            while not self.reader_stop.is_set():
                if not self.ser:
                    time.sleep(0.1)
                    continue

                # Non-blocking read; lock held only for the read itself.
                chunk = b""
                with self.lock:
                    n = self.ser.in_waiting if self.ser else 0
                    if n:
                        chunk = self.ser.read(n)

                if chunk:
                    buf += chunk
                    buf = buf.replace(b"\r", b"\n")

                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        s = line.decode(errors="replace").strip()
                        parsed = self._parse_measurement_line(s)
                        if parsed:
                            self.received_samples += 1
                            with latest_lock:
                                global latest_reading
                                latest_reading = parsed

                            # Append to session data with threshold check
                            with session_lock:
                                with thresholds_lock:
                                    exceeded_0p3 = parsed.get("count_0p3", 0) > thresholds.threshold_0p3
                                    exceeded_5p0 = parsed.get("count_5p0", 0) > thresholds.threshold_5p0
                                
                                dp = SessionDataPoint(
                                    ts=parsed["ts"],
                                    count_0p3=parsed.get("count_0p3", 0),
                                    count_5p0=parsed.get("count_5p0", 0),
                                    exceeded_0p3=exceeded_0p3,
                                    exceeded_5p0=exceeded_5p0,
                                )
                                session_data.append(dp)

                            if (
                                self.run_active
                                and self.target_samples > 0
                                and self.received_samples >= self.target_samples
                            ):
                                # End run bookkeeping; stop just the reader.
                                self.run_active = False
                                self.stop_reader()
                else:
                    time.sleep(0.05)
        finally:
            self.reader_running = False

    def ensure_reader(self):
        # If running reader exists, do nothing
        if self.reader_thread and self.reader_thread.is_alive() and not self.reader_stop.is_set():
            return

        # If a previous reader is still alive but stop was set, give it a moment to exit
        if self.reader_thread and self.reader_thread.is_alive() and self.reader_stop.is_set():
            self.reader_thread.join(timeout=1.0)
            if self.reader_thread.is_alive():
                # Don't start a second reader
                return

        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def stop_reader(self):
        self.reader_stop.set()

gt = GT521()

# =========================
# Helpers: parse settings report
# =========================

def parse_settings_report(text: str) -> dict:
    def pick_int(label: str):
        m = re.search(rf"^\s*{re.escape(label)}\s*,\s*(\d+)", text, flags=re.MULTILINE)
        return int(m.group(1)) if m else None

    return {
        "sample_time_s": pick_int("Sample Time"),
        "hold_time_s": pick_int("Hold Time"),
        "samples": pick_int("Samples"),
    }

# =========================
# DASHBOARD UI (ORIGINAL + ENHANCED)
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
            .current-reading {{ font-size: 32px; font-weight: 700; color: #0071e3; margin-bottom: 15px; }}
            .current-reading-unit {{ font-size: 13px; color: #666; }}
            .graph-container {{ position: relative; height: 400px; margin-bottom: 15px; }}
            .threshold-status {{ display: inline-block; padding: 6px 12px; border-radius: 4px; font-size: 13px; font-weight: 600; margin-top: 10px; }}
            .threshold-status.safe {{ background: #d4edda; color: #155724; }}
            .threshold-status.exceeded {{ background: #f8d7da; color: #721c24; }}
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

            <label>0.3µm Threshold (particles/cm³)</label>
            <input id="threshold_0p3" type="number" value="1000" min="1" max="999999"/>

            <label>5.0µm Threshold (particles/cm³)</label>
            <input id="threshold_5p0" type="number" value="500" min="1" max="999999"/>

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
              <span id="current_0p3">—</span> <span style="font-size: 16px; color: #666;">particles/cm³</span>
            </div>
            <div class="graph-container">
              <canvas id="chart-0p3"></canvas>
            </div>
            <div id="status-0p3" class="threshold-status safe">✓ Below Threshold</div>
          </div>

          <div class="graph-card">
            <div class="graph-title">5.0µm Particles</div>
            <div style="font-size: 28px; font-weight: 700; color: #0071e3; margin-bottom: 15px;">
              <span id="current_5p0">—</span> <span style="font-size: 16px; color: #666;">particles/cm³</span>
            </div>
            <div class="graph-container">
              <canvas id="chart-5p0"></canvas>
            </div>
            <div id="status-5p0" class="threshold-status safe">✓ Below Threshold</div>
          </div>
        </div>

        <script>
            let chart0p3 = null;
            let chart5p0 = null;
            let pollInterval = null;
            let wasRunning = false;

            function initializeCharts() {{
              const s = getSettings();
              const sessionDurationSeconds = (s.sample_time_s + s.hold_time_s) * s.samples;
              const t0p3 = parseInt(document.getElementById("threshold_0p3").value);
              const t5p0 = parseInt(document.getElementById("threshold_5p0").value);
              
              createOrUpdateChart("chart-0p3", [], t0p3, sessionDurationSeconds);
              createOrUpdateChart("chart-5p0", [], t5p0, sessionDurationSeconds);
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
                threshold_0p3: parseInt(document.getElementById("threshold_0p3").value),
                threshold_5p0: parseInt(document.getElementById("threshold_5p0").value),
              }};
            }}

            function fmtDuration(totalSeconds) {{
              const s = Math.max(0, Math.floor(totalSeconds));
              const h = Math.floor(s / 3600);
              const m = Math.floor((s % 3600) / 60);
              const r = s % 60;
              return `${{h}}h ${{m}}m ${{r}}s`;
            }}

            function updateComputed() {{
              const s = getSettings();
              if (!pollInterval) {{
                initializeCharts();
              }}
            }}

            ["sample_time_s","hold_time_s","samples"].forEach(id => {{
              document.getElementById(id).addEventListener("input", updateComputed);
            }});
            updateComputed();

            async function loadThresholds() {{
              try {{
                const r = await fetch("/gt/thresholds");
                const j = await r.json();
                if (j.threshold_0p3) document.getElementById("threshold_0p3").value = j.threshold_0p3;
                if (j.threshold_5p0) document.getElementById("threshold_5p0").value = j.threshold_5p0;
              }} catch (e) {{
                console.error("Failed to load thresholds:", e);
              }}
            }}

            async function saveThresholds() {{
              try {{
                const thresholds = getThresholds();
                const r = await fetch("/gt/thresholds", {{
                  method: "POST",
                  headers: {{ "Content-Type": "application/json" }},
                  body: JSON.stringify(thresholds),
                }});
                const j = await r.json();
              }} catch (e) {{
                console.error("Error saving thresholds:", e);
              }}
            }}

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
                  const txt = await r.text().catch(() => "");
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
                  document.getElementById("current_0p3").textContent = (j.latest.count_0p3 ?? "—").toString();
                  document.getElementById("current_5p0").textContent = (j.latest.count_5p0 ?? "—").toString();
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

            function generateTimeLabels(sessionDurationSeconds) {{
              const labels = [];
              const interval = Math.max(1, Math.floor(sessionDurationSeconds / 20));
              for (let i = 0; i <= sessionDurationSeconds; i += interval) {{
                const h = Math.floor(i / 3600).toString().padStart(2, '0');
                const m = Math.floor((i % 3600) / 60).toString().padStart(2, '0');
                const s = (i % 60).toString().padStart(2, '0');
                labels.push(`${{h}}:${{m}}:${{s}}`);
              }}
              return labels;
            }}

            let sessionStartTime = null;
            function getElapsedSeconds(timestamp) {{
              if (!sessionStartTime) {{
                sessionStartTime = new Date(timestamp).getTime();
              }}
              const currentTime = new Date(timestamp).getTime();
              return Math.floor((currentTime - sessionStartTime) / 1000);
            }}

            function createOrUpdateChart(canvasId, data, threshold, sessionDurationSeconds) {{
              const ctx = document.getElementById(canvasId).getContext("2d");
              const dataPoints = [];
              data.forEach(d => {{
                const elapsed = getElapsedSeconds(d.ts);
                const count = canvasId === "chart-0p3" ? d.count_0p3 : d.count_5p0;
                const exceeded = canvasId === "chart-0p3" ? d.exceeded_0p3 : d.exceeded_5p0;
                
                if (count !== undefined && count !== null && elapsed <= sessionDurationSeconds) {{
                  dataPoints.push({{
                    x: elapsed,
                    y: Math.max(count, 1),
                    color: exceeded ? "#c22" : "#0071e3"
                  }});
                }}
              }});

              const chartId = canvasId === "chart-0p3" ? 0 : 1;
              const existingChart = chartId === 0 ? chart0p3 : chart5p0;

              const thresholdData = [
                {{ x: 0, y: threshold }},
                {{ x: sessionDurationSeconds, y: threshold }}
              ];

              const chartConfig = {{
                type: "scatter",
                data: {{
                  datasets: [
                    {{
                      label: "Particle Count",
                      data: dataPoints.map(p => ({{ x: p.x, y: p.y }})),
                      borderColor: "#0071e3",
                      backgroundColor: dataPoints.map(p => p.color),
                      borderWidth: 0,
                      pointRadius: 4,
                      pointBorderColor: dataPoints.map(p => p.color),
                      pointBorderWidth: 1,
                      showLine: true,
                      fill: false,
                      borderColor: "#0071e3",
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
                      title: {{ display: true, text: "Particles/cm³ (log scale)" }},
                      min: 1,
                      max: 3000000,
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
                chart0p3 = newChart;
              }} else {{
                chart5p0 = newChart;
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
                const t0p3 = parseInt(document.getElementById("threshold_0p3").value);
                const t5p0 = parseInt(document.getElementById("threshold_5p0").value);

                createOrUpdateChart("chart-0p3", data, t0p3, sessionDurationSeconds);
                createOrUpdateChart("chart-5p0", data, t5p0, sessionDurationSeconds);

                const last = data[data.length - 1];
                const s0p3 = document.getElementById("status-0p3");
                const s5p0 = document.getElementById("status-5p0");

                s0p3.className = last.exceeded_0p3 ? "threshold-status exceeded" : "threshold-status safe";
                s0p3.textContent = last.exceeded_0p3 ? "⚠ EXCEEDED" : "✓ Below Threshold";

                s5p0.className = last.exceeded_5p0 ? "threshold-status exceeded" : "threshold-status safe";
                s5p0.textContent = last.exceeded_5p0 ? "⚠ EXCEEDED" : "✓ Below Threshold";
                
                document.getElementById("current_0p3").textContent = (last.count_0p3 ?? "—").toString();
                document.getElementById("current_5p0").textContent = (last.count_5p0 ?? "—").toString();
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

                const editingIds = ["sample_time_s","hold_time_s","samples","threshold_0p3","threshold_5p0"];
                const userEditing = editingIds.includes(document.activeElement?.id);
                if (!userEditing) {{
                  document.getElementById("sample_time_s").value = j.settings.sample_time_s;
                  document.getElementById("hold_time_s").value = j.settings.hold_time_s;
                  document.getElementById("samples").value = j.settings.samples;
                  document.getElementById("threshold_0p3").value = j.thresholds.threshold_0p3;
                  document.getElementById("threshold_5p0").value = j.thresholds.threshold_5p0;
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
                console.debug("[state]", ts, j);
              }} catch (e) {{}}
            }}

            initializeCharts();
            setInterval(pollLatest, 1000);
            setInterval(pollState, 2000);
            pollState();
            pollLatest();
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
    applied_at = time.strftime("%Y-%m-%d %H:%M:%S")

    with session_lock:
        global session_data
        session_data.clear()

    wanted = {
        "sample_time_s": settings.sample_time_s,
        "hold_time_s": settings.hold_time_s,
        "samples": settings.samples,
    }

    try:
        gt.open()
        gt.stop_reader()
        if gt.reader_thread and gt.reader_thread.is_alive():
            gt.reader_thread.join(timeout=1.0)

        gt.stop()
        time.sleep(0.2)

        gt.set_location_id(1)
        gt.set_sample_time(settings.sample_time_s)
        gt.set_hold_time(settings.hold_time_s)
        gt.set_samples(settings.samples)

        gt.set_report_csv()

        readback_ok, report = gt.read_settings_report()
        applied = parse_settings_report(report) or {}

        mismatch = {
            k: {"wanted": wanted[k], "got": applied.get(k)}
            for k in wanted
            if applied.get(k) != wanted[k]
        }

        gt.start()

        gt.target_samples = settings.samples
        gt.received_samples = 0
        gt.run_active = True
        gt.ensure_reader()

        ok = True

        return JSONResponse({
            "ok": ok,
            "applied_at": applied_at,
            "requested": wanted,
            "applied": applied,
            "mismatch": mismatch,
            "readback_ok": bool(readback_ok),
            "expected_cycle_s": settings.sample_time_s + settings.hold_time_s,
            "expected_duration_s": (settings.sample_time_s + settings.hold_time_s) * settings.samples,
        })
    except Exception as e:
        gt.run_active = False
        gt.target_samples = 0
        gt.received_samples = 0

        return JSONResponse({
            "ok": False,
            "applied_at": applied_at,
            "requested": wanted,
            "mismatch": {},
            "error": str(e),
            "trace": traceback.format_exc(limit=3),
        }, status_code=200)

@app.post("/gt/stop")
def stop():
    at = time.strftime("%Y-%m-%d %H:%M:%S")

    gt.open()

    gt.run_active = False
    gt.target_samples = 0
    gt.received_samples = 0
    gt.stop_reader()
    if gt.reader_thread and gt.reader_thread.is_alive():
        gt.reader_thread.join(timeout=1.0)

    gt.stop()
    time.sleep(0.15)

    stopped = False
    for _ in range(6):
        _, op_raw = gt.op_status()
        op_text = op_raw.decode(errors="replace")
        if ("OP S" in op_text) or ("OP STOP" in op_text):
            stopped = True
            break
        time.sleep(0.3)

    return JSONResponse({"ok": stopped, "at": at})

@app.get("/gt/latest")
def get_latest():
    with latest_lock:
        return JSONResponse({"latest": latest_reading})

@app.get("/gt/session-data")
def get_session_data():
    with session_lock:
        data = [dp.dict() for dp in session_data]
    return JSONResponse({"data": data})

@app.get("/gt/thresholds")
def get_thresholds():
    with thresholds_lock:
        return JSONResponse({
            "threshold_0p3": thresholds.threshold_0p3,
            "threshold_5p0": thresholds.threshold_5p0,
        })

@app.post("/gt/thresholds")
def set_thresholds(settings: ThresholdSettings):
    global thresholds
    with thresholds_lock:
        thresholds = settings
    return JSONResponse({"ok": True, "thresholds": settings.dict()})

@app.get("/gt/status")
def status():
    return JSONResponse({
        "run_active": gt.run_active,
        "received_samples": gt.received_samples,
        "target_samples": gt.target_samples,
        "reader_running": gt.reader_running,
    })

@app.get("/state")
def get_state():
    with thresholds_lock:
        t = {"threshold_0p3": thresholds.threshold_0p3, "threshold_5p0": thresholds.threshold_5p0}
    return JSONResponse({
        "run_active": gt.run_active,
        "received_samples": gt.received_samples,
        "target_samples": gt.target_samples,
        "settings": current_settings.dict(),
        "thresholds": t,
        "last_update": time.time(),
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
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

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

ENV1_UID = "bb-0001"
GT_UID = "bb-0002"

DEFAULT_LOCATION_LABEL = "GoLab"
DEFAULT_LOCATION_ID = 1
DEFAULT_SAMPLE_TIME_S = 10
DEFAULT_HOLD_TIME_S = 50
DEFAULT_SAMPLES = 480

DATA_DIR = Path.home() / "golab-monitor" / "data"
GT_SESSIONS_DIR = DATA_DIR / "sessions"
ENV_DAILY_AVERAGES_PATH = DATA_DIR / "env_daily_averages.jsonl"
MEDIA_BASE_DIR = Path("/media/golab")

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

def time_metadata(status: Optional[Dict[str, Any]] = None, utc: Optional[str] = None) -> Dict[str, Any]:
    if status is None:
        status = time_status()
    return {
        "utc": utc or utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": status["source"],
        "ntp_synced": status["ntp_synced"],
        "sane": status["sane"],
    }

def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    ensure_data_dirs()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), default=str))
        f.write("\n")

def get_env_reading() -> Dict[str, Any]:
    reading = env.get_reading()
    reading["uid"] = ENV1_UID
    return reading

def local_date_str(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = utc_now()
    return dt.astimezone().date().isoformat()

def safe_timestamp_for_filename(ts_utc: str) -> str:
    return ts_utc.replace(":", "-")

def get_storage_targets() -> List[Dict[str, str]]:
    if not MEDIA_BASE_DIR.exists():
        return []
    out = []
    for p in sorted(MEDIA_BASE_DIR.iterdir()):
        if p.is_dir() and os.access(p, os.W_OK):
            out.append({"name": p.name, "path": str(p)})
    return out

def get_storage_target(path: str) -> Optional[Dict[str, str]]:
    try:
        requested = Path(path).resolve()
    except Exception:
        return None
    for target in get_storage_targets():
        if Path(target["path"]).resolve() == requested:
            return target
    return None

class EnvDailyAccumulator:
    def __init__(self, output_path: Path):
        self.output_path = output_path
        self.lock = threading.Lock()
        self._nodes: Dict[str, Dict[str, Any]] = {}

    def update(self, reading: Optional[Dict[str, Any]]) -> None:
        if not reading:
            return

        uid = reading.get("uid") or "env1"
        now_date = local_date_str()

        with self.lock:
            node = self._nodes.get(uid)
            if node is None:
                node = self._new_node(now_date)
                self._nodes[uid] = node
            elif node["date_local"] != now_date:
                self._write_summary_locked(uid, node)
                node = self._new_node(now_date)
                self._nodes[uid] = node

            node["latest"] = reading
            reading_ts = reading.get("timestamp")
            if reading_ts and reading_ts == node.get("last_timestamp"):
                return

            values = self._numeric_values(reading)
            if not values:
                return

            for key, value in values.items():
                node["sums"][key] = node["sums"].get(key, 0.0) + value
                node["counts"][key] = node["counts"].get(key, 0) + 1
            node["samples"] += 1
            node["last_timestamp"] = reading_ts

    def latest(self, uid: str = ENV1_UID) -> Optional[Dict[str, Any]]:
        with self.lock:
            node = self._nodes.get(uid)
            if node is None:
                return None
            return node.get("latest")

    def current_averages(self, uid: str = ENV1_UID) -> Dict[str, float]:
        key_map = {
            "temp_c_avg": "temp_c",
            "rh_pct_avg": "rh_pct",
            "c03_avg": "c03",
            "c05_avg": "c05",
            "c10_avg": "c10",
        }
        with self.lock:
            node = self._nodes.get(uid)
            if node is None:
                return {}
            averages = {}
            for avg_key, api_key in key_map.items():
                count = node["counts"].get(avg_key, 0)
                if count:
                    averages[api_key] = node["sums"][avg_key] / count
            return averages

    def _new_node(self, date_local: str) -> Dict[str, Any]:
        return {
            "date_local": date_local,
            "samples": 0,
            "sums": {},
            "counts": {},
            "latest": None,
            "last_timestamp": None,
        }

    def _numeric_values(self, reading: Dict[str, Any]) -> Dict[str, float]:
        data = reading.get("data") or {}
        extended = reading.get("extended") or {}
        values: Dict[str, float] = {}
        for source_key, avg_key in (
            ("temp_c", "temp_c_avg"),
            ("rh_pct", "rh_pct_avg"),
            ("c03", "c03_avg"),
            ("c05", "c05_avg"),
            ("c10", "c10_avg"),
        ):
            value = data.get(source_key)
            if value is None:
                value = extended.get(source_key)
            if isinstance(value, (int, float)):
                values[avg_key] = float(value)
        return values

    def _write_summary_locked(self, uid: str, node: Dict[str, Any]) -> None:
        if node["samples"] <= 0:
            return

        averaged = {}
        for key, total in node["sums"].items():
            count = node["counts"].get(key, 0)
            if count:
                averaged[key] = total / count

        latest = node.get("latest") or {}
        append_jsonl(self.output_path, {
            "date_local": node["date_local"],
            "generated_at_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "uid": uid,
            "status": latest.get("status", "unknown"),
            "samples": node["samples"],
            "data": averaged,
            "extended": {},
            "raw": None,
        })


class GTSessionWriter:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self.lock = threading.Lock()
        self.active_path: Optional[Path] = None

    def start(self, start_utc: str, sessions_dir: Optional[Path] = None) -> Path:
        ensure_data_dirs()
        if sessions_dir is not None:
            sessions_dir.mkdir(parents=True, exist_ok=True)
        safe_ts = safe_timestamp_for_filename(start_utc)
        target_dir = sessions_dir or self.sessions_dir
        path = target_dir / f"gt_session_{safe_ts}.jsonl"
        with self.lock:
            self.active_path = path
        path.touch(exist_ok=True)
        log.info("GT: session writer started — path=%s", path)
        return path

    def stop(self) -> None:
        with self.lock:
            if self.active_path is not None:
                log.info("GT: session writer stopped — path=%s", self.active_path)
            self.active_path = None

    def append_sample(
        self,
        session_ts_utc: str,
        time_info: Dict[str, Any],
        gt_reading: Optional[Dict[str, Any]],
        env_reading: Optional[Dict[str, Any]],
    ) -> None:
        with self.lock:
            path = self.active_path
            if path is None:
                log.warning("GT: append_sample skipped because session writer has no active path")
                return

            append_jsonl(path, {
                "session_ts_utc": session_ts_utc,
                "time": time_info,
                "devices": {
                    "gt521": gt_reading,
                    "env1": env_reading,
                },
            })
            log.info("GT: appended session sample — path=%s ts=%s", path, session_ts_utc)


ensure_data_dirs()
env_daily_accumulator = EnvDailyAccumulator(ENV_DAILY_AVERAGES_PATH)
gt_session_writer = GTSessionWriter(GT_SESSIONS_DIR)

session_save_lock = threading.Lock()
session_save_mode = "local"
session_save_root = GT_SESSIONS_DIR
session_save_display = "Local"
session_save_mount: Optional[Path] = None

def get_current_session_target() -> Dict[str, str]:
    with session_save_lock:
        selectable_path = str(session_save_mount) if session_save_mount is not None else str(session_save_root)
        return {
            "mode": session_save_mode,
            "path": selectable_path,
            "resolved_path": str(session_save_root),
            "label": session_save_display,
        }

def set_session_target_local() -> Dict[str, str]:
    global session_save_mode, session_save_root, session_save_display, session_save_mount
    ensure_data_dirs()
    with session_save_lock:
        session_save_mode = "local"
        session_save_root = GT_SESSIONS_DIR
        session_save_display = "Local"
        session_save_mount = None
    return get_current_session_target()

def set_session_target_usb(target: Dict[str, str]) -> Dict[str, str]:
    global session_save_mode, session_save_root, session_save_display, session_save_mount
    mount = Path(target["path"])
    root = mount / "golab-monitor" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    with session_save_lock:
        session_save_mode = "usb"
        session_save_root = root
        session_save_display = target["name"]
        session_save_mount = mount
    return get_current_session_target()

def resolve_current_session_dir() -> tuple[Optional[Path], Optional[str]]:
    with session_save_lock:
        mode = session_save_mode
        root = session_save_root
        mount = session_save_mount
    if mode == "local":
        ensure_data_dirs()
        return root, None
    if mode == "usb" and mount is not None:
        if get_storage_target(str(mount)) is None:
            return None, "Selected USB session target is no longer mounted or writable."
        root.mkdir(parents=True, exist_ok=True)
        return root, None
    return None, "Session save target is invalid."

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

class SessionTargetRequest(BaseModel):
    mode: str
    path: Optional[str] = None

class ExportDailyRequest(BaseModel):
    path: str

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
            "uid": GT_UID,
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
            start_time = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
            self._session = {
                "uid": GT_UID,
                "session_id": session_id,
                "status": "running",
                "start_time": start_time,
                "end_time": None,
                "metadata": metadata,
                "summary": {},
                "data": [],
            }
            log.info("GT: session manager started — id=%s start_time=%s metadata=%s", session_id, start_time, metadata)
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

    def status(self) -> str:
        with self.lock:
            return str(self._session.get("status", "idle"))

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

GT_ERROR_END_REASONS = {"stopped_early", "timeout", "serial_fault", "time_invalid"}
gt_lifecycle_lock = threading.Lock()
gt_starting = False

def set_gt_starting(value: bool) -> None:
    global gt_starting
    with gt_lifecycle_lock:
        gt_starting = value
    log.info("GT: lifecycle starting=%s", value)

def is_gt_starting() -> bool:
    with gt_lifecycle_lock:
        return gt_starting

def finalize_gt_session_from_state(state: Dict[str, Any]) -> None:
    if is_gt_starting():
        log.info(
            "GT: skipping backend finalization while new run is starting — stale_reason=%s received=%s target=%s",
            state.get("run_end_reason"),
            state.get("received_samples"),
            state.get("target_samples"),
        )
        return

    if state.get("run_active"):
        return

    reason = state.get("run_end_reason")
    if reason not in {"completed", "manual_stop", *GT_ERROR_END_REASONS}:
        return

    if session_manager.status() != "running":
        gt_session_writer.stop()
        return

    log.info(
        "GT: backend finalization running — reason=%s received=%s target=%s",
        reason,
        state.get("received_samples"),
        state.get("target_samples"),
    )
    gt_session_writer.stop()
    if reason == "completed":
        session_manager.complete()
    elif reason in GT_ERROR_END_REASONS:
        session_manager.error(reason)
    elif reason == "manual_stop":
        session_manager.error("manual_stop")
    log.info("GT: backend session finalized — reason=%s", reason)

gt_monitor_stop = threading.Event()

def monitor_gt_session_state() -> None:
    while not gt_monitor_stop.is_set():
        try:
            state = gt.get_state()
            if state.get("run_active"):
                status = time_status(force_refresh=True)
                if not status["valid"]:
                    log.error("GT: aborting active run because system time became invalid")
                    gt.abort_run("time_invalid")
                    state = gt.get_state()
            finalize_gt_session_from_state(state)
        except Exception:
            log.exception("GT: session monitor failed")
        gt_monitor_stop.wait(1.0)

# =========================
# GT-521S DRIVER
# =========================

gt = GT521SDriver(uid=GT_UID, port=PORT, baud=BAUD)

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

gt_monitor_thread = threading.Thread(target=monitor_gt_session_state, daemon=True)
gt_monitor_thread.start()

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
              <div id="run-diagnostics" class="small muted" style="margin-top:6px;"></div>
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
            <div><div class="small muted">&gt;0.3µm /m³</div><div id="env_c03" style="font-size:28px;font-weight:700;">—</div><div id="env_c03_avg" class="small muted">Avg: —</div></div>
            <div><div class="small muted">&gt;0.5µm /m³</div><div id="env_c05" style="font-size:28px;font-weight:700;">—</div><div id="env_c05_avg" class="small muted">Avg: —</div></div>
            <div><div class="small muted">&gt;1.0µm /m³</div><div id="env_c10" style="font-size:28px;font-weight:700;">—</div><div id="env_c10_avg" class="small muted">Avg: —</div></div>
            <div><div class="small muted">Temp (°C)</div><div id="env_temp" style="font-size:28px;font-weight:700;">—</div><div id="env_temp_avg" class="small muted">Avg: —</div></div>
            <div><div class="small muted">RH (%)</div><div id="env_rh" style="font-size:28px;font-weight:700;">—</div><div id="env_rh_avg" class="small muted">Avg: —</div></div>
          </div>
        </div>

        <div class="card" style="margin-top: 20px;">
          <h3>Data & Export</h3>
          <div class="small muted" style="margin-bottom:12px;">Current Session Target: <span id="session-target-current">Local</span></div>
          <div style="max-width:720px;">
            <label style="margin-top:0;">File Target</label>
            <select id="file-target-select" onchange="applyFileTarget()" style="font-size:16px;padding:8px;width:100%;background:var(--panel);color:var(--text);border:1px solid var(--panel-border);border-radius:6px;"></select>
            <div style="display:flex; gap:16px; flex-wrap:wrap; margin-top:18px; margin-bottom:12px;">
              <button id="export-daily-button" onclick="exportDailyAverages()">Export Daily Averages</button>
              <button onclick="loadStorageTargets()">Refresh Drives</button>
            </div>
          </div>
          <div id="session-target-status" class="small muted"></div>
          <div id="export-status" class="small muted" style="margin-top:6px;"></div>
        </div>

        <script>
            let chartC03 = null;
            let chartC50 = null;
            let pollInterval = null;
            let wasRunning = false;
            let runBusy = false;
            let settingsInitialized = false;
            let storageTargets = [];

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

            function avgText(value) {{
              return value === null || value === undefined ? "Avg: —" : `Avg: ${{Number(value).toFixed(2)}}`;
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
              const startBtn = document.getElementById("start-button");
              runBusy = true;
              if (startBtn) startBtn.disabled = true;
              c.className = "small muted";
              c.textContent = "Starting...";

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
                  runBusy = false;
                  return;
                }}

                if (j.ok) {{
                  c.className = "small ok";
                  c.textContent = `Applied @ ${{j.applied_at}}`;
                  startGraphPolling();
                }} else {{
                  c.className = "small bad";
                  c.textContent = j.error || `Start failed @ ${{j.applied_at}}`;
                  runBusy = false;
                }}
              }} catch (e) {{
                c.className = "small bad";
                c.textContent = "Start error";
                runBusy = false;
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
                  const avg = j.averages || {{}};

                  document.getElementById("env_c03").textContent = (pmsCountToM3(d.c03) ?? "—").toString();
                  document.getElementById("env_c05").textContent = (pmsCountToM3(x.c05) ?? "—").toString();
                  document.getElementById("env_c10").textContent = (pmsCountToM3(x.c10) ?? "—").toString();
                  document.getElementById("env_temp").textContent = (d.temp_c ?? "—").toString();
                  document.getElementById("env_rh").textContent = (x.rh_pct ?? "—").toString();

                  document.getElementById("env_c03_avg").textContent = avgText(pmsCountToM3(avg.c03));
                  document.getElementById("env_c05_avg").textContent = avgText(pmsCountToM3(avg.c05));
                  document.getElementById("env_c10_avg").textContent = avgText(pmsCountToM3(avg.c10));
                  document.getElementById("env_temp_avg").textContent = avgText(avg.temp_c);
                  document.getElementById("env_rh_avg").textContent = avgText(avg.rh_pct);
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

            function setStorageStatus(id, text, ok = true) {{
              const el = document.getElementById(id);
              if (!el) return;
              el.className = ok ? "small ok" : "small bad";
              el.textContent = text;
            }}

            function optionExists(selectEl, value) {{
              return Array.from(selectEl.options).some(opt => opt.value === value);
            }}

            function populateStorageTargets(payload) {{
              storageTargets = payload.targets || [];
              const fileSelect = document.getElementById("file-target-select");
              const currentEl = document.getElementById("session-target-current");
              if (!fileSelect) return;

              const previousValue = fileSelect.value;

              fileSelect.innerHTML = "";
              const localOption = document.createElement("option");
              localOption.value = "__local__";
              localOption.textContent = "Local — /home/golab/golab-monitor/data/sessions";
              fileSelect.appendChild(localOption);

              storageTargets.forEach(target => {{
                const option = document.createElement("option");
                option.value = target.path;
                option.textContent = `${{target.name}} — ${{target.path}}`;
                fileSelect.appendChild(option);
              }});

              const current = payload.current || {{ mode: "local", label: "Local", path: "" }};
              const currentValue = current.mode === "usb" ? current.path : "__local__";
              if (optionExists(fileSelect, previousValue)) {{
                fileSelect.value = previousValue;
              }} else if (optionExists(fileSelect, currentValue)) {{
                fileSelect.value = currentValue;
              }} else {{
                fileSelect.value = "__local__";
              }}

              if (currentEl) currentEl.textContent = `${{current.label}} (${{current.resolved_path || current.path}})`;
              const exportButton = document.getElementById("export-daily-button");
              if (exportButton) exportButton.disabled = storageTargets.length === 0;
              if (storageTargets.length === 0) {{
                setStorageStatus("export-status", "No mounted USB targets found under /media/golab.", false);
              }}
            }}

            async function loadStorageTargets() {{
              try {{
                const r = await fetch("/storage/targets");
                const j = await r.json();
                populateStorageTargets(j);
                setStorageStatus("session-target-status", "Drive list refreshed.");
              }} catch (e) {{
                setStorageStatus("session-target-status", "Could not refresh drive list.", false);
              }}
            }}

            async function applyFileTarget() {{
              const fileEl = document.getElementById("file-target-select");
              if (!fileEl) return;
              const payload = fileEl.value === "__local__"
                ? {{ mode: "local" }}
                : {{ mode: "usb", path: fileEl.value }};

              try {{
                const r = await fetch("/storage/session-target", {{
                  method: "POST",
                  headers: {{ "Content-Type": "application/json" }},
                  body: JSON.stringify(payload),
                }});
                const j = await r.json();
                if (!r.ok || !j.ok) {{
                  setStorageStatus("session-target-status", j.error || `Target update failed (HTTP ${{r.status}})`, false);
                  return;
                }}
                const currentEl = document.getElementById("session-target-current");
                if (currentEl) currentEl.textContent = `${{j.current.label}} (${{j.current.resolved_path || j.current.path}})`;
                setStorageStatus("session-target-status", `Session target set to ${{j.current.label}}.`);
              }} catch (e) {{
                setStorageStatus("session-target-status", "Session target update failed.", false);
              }}
            }}

            async function exportDailyAverages() {{
              const selectEl = document.getElementById("file-target-select");
              if (!selectEl || !selectEl.value || selectEl.value === "__local__") {{
                setStorageStatus("export-status", "Choose a mounted USB target first.", false);
                return;
              }}

              try {{
                const r = await fetch("/export/env-daily", {{
                  method: "POST",
                  headers: {{ "Content-Type": "application/json" }},
                  body: JSON.stringify({{ path: selectEl.value }}),
                }});
                const j = await r.json();
                if (!r.ok || !j.ok) {{
                  setStorageStatus("export-status", j.error || `Export failed (HTTP ${{r.status}})`, false);
                  return;
                }}
                setStorageStatus("export-status", `Exported to ${{j.destination}}.`);
              }} catch (e) {{
                setStorageStatus("export-status", "Daily averages export failed.", false);
              }}
            }}

            async function pollState() {{
              try {{
                const r = await fetch("/state");
                const j = await r.json();

                if (!settingsInitialized) {{
                  document.getElementById("sample_time_s").value = j.settings.sample_time_s;
                  document.getElementById("hold_time_s").value = j.settings.hold_time_s;
                  document.getElementById("samples").value = j.settings.samples;
                  settingsInitialized = true;
                }}

                const currentTargetEl = document.getElementById("session-target-current");
                const sessionTarget = j.storage?.session_save;
                if (currentTargetEl && sessionTarget) {{
                  currentTargetEl.textContent = `${{sessionTarget.label}} (${{sessionTarget.resolved_path || sessionTarget.path}})`;
                }}
                const fileSelect = document.getElementById("file-target-select");
                if (fileSelect) {{
                  fileSelect.disabled = !!j.run_active || !!j.gt_starting;
                }}
                const startBtn = document.getElementById("start-button");
                runBusy = !!j.run_active || !!j.gt_starting;
                if (startBtn) startBtn.disabled = runBusy;

                const c = document.getElementById("confirm");
                const diag = document.getElementById("run-diagnostics");
                const received = j.received_samples ?? 0;
                const target = j.target_samples ?? 0;
                const reason = j.run_end_reason;
                if (j.gt_starting) {{
                  c.className = "small muted";
                  c.textContent = "Starting...";
                }} else if (j.run_active) {{
                  c.className = "small ok";
                  c.textContent = `Running — ${{received}} / ${{target}} samples`;
                  if (!wasRunning) {{
                    sessionStartTime = null;
                    startGraphPolling();
                  }}
                }} else {{
                  if (reason === "completed") {{
                    c.className = "small ok";
                    c.textContent = "Run complete.";
                  }} else if (reason === "stopped_early") {{
                    c.className = "small bad";
                    c.textContent = `Stopped early — ${{received}} / ${{target}} samples received`;
                  }} else if (reason === "serial_fault") {{
                    c.className = "small bad";
                    c.textContent = `Run fault — ${{received}} / ${{target}} samples received`;
                  }} else if (reason === "timeout") {{
                    c.className = "small bad";
                    c.textContent = `Run timeout — ${{received}} / ${{target}} samples received`;
                  }} else if (reason === "manual_stop") {{
                    c.className = "small muted";
                    c.textContent = "Stopped.";
                  }}
                  if (wasRunning) {{
                    stopGraphPolling();
                  }}
                }}

                if (diag) {{
                  const bits = [];
                  if (j.last_gt_sample_timestamp) bits.push(`Last GT ts: ${{j.last_gt_sample_timestamp}}`);
                  if (j.last_op_status) bits.push(`OP: ${{j.last_op_status}}`);
                  if ((j.suspected_missed_samples ?? 0) > 0) {{
                    bits.push(`Suspected missed: ${{j.suspected_missed_samples}}`);
                  }}
                  diag.textContent = bits.join(" · ");
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
            loadStorageTargets();
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

                const d = new Date(j.utc);
                const timeStr = d.toLocaleTimeString([], {{
                  hour: "2-digit",
                  minute: "2-digit",
                  second: "2-digit",
                  hour12: false
                }});
                clockEl.textContent = timeStr;

                if (j.source === "ntp") {{
                  if (warnEl) {{
                    warnEl.style.display = "none";
                    warnEl.textContent = "";
                    warnEl.className = "small muted";
                  }}
                  if (startBtn) startBtn.disabled = runBusy;
                }} else if (j.source === "rtc_holdover") {{
                  if (warnEl) {{
                    warnEl.style.display = "block";
                    warnEl.className = "small muted";
                    warnEl.textContent = "TIME OK — RTC holdover (NTP not currently synced)";
                  }}
                  if (startBtn) startBtn.disabled = runBusy;
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
    if is_gt_starting():
        return JSONResponse({
            "ok": False,
            "error": "GT run is already starting."
        }, status_code=409)
    current_gt_state = gt.get_state()
    if current_gt_state.get("run_active"):
        return JSONResponse({
            "ok": False,
            "error": "GT run is already active."
        }, status_code=409)

    current_settings = settings
    applied_at = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")

    settings_dict = {
        "sample_time_s": settings.sample_time_s,
        "hold_time_s": settings.hold_time_s,
        "samples": settings.samples,
    }

    def on_sample(parsed: Dict[str, Any]) -> None:
        session_ts_utc = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
        log.info("GT: on_sample started — ts=%s parsed=%s", session_ts_utc, parsed)
        sample_time_status = time_status(force_refresh=True)
        if not sample_time_status["valid"]:
            log.error("GT: dropping sample and finalizing session because system time is invalid")
            gt_session_writer.stop()
            if session_manager.status() == "running":
                session_manager.error("time_invalid")
            return

        c03_m3 = round(parsed.get("c03", 0) * FT3_TO_M3)
        c50_m3 = round(parsed.get("c50", 0) * FT3_TO_M3)
        with thresholds_lock:
            exceeded_c03 = c03_m3 > thresholds.threshold_c03
            exceeded_c50 = c50_m3 > thresholds.threshold_c50
        dp = SessionDataPoint(
            ts=session_ts_utc,
            c03=parsed.get("c03", 0),
            c50=parsed.get("c50", 0),
            exceeded_c03=exceeded_c03,
            exceeded_c50=exceeded_c50,
        )
        session_manager.append(dp)

        try:
            env_reading = get_env_reading()
            env_daily_accumulator.update(env_reading)
        except Exception:
            log.exception("ENV: failed to read latest sample for GT session record")
            env_reading = env_daily_accumulator.latest()

        gt_session_writer.append_sample(
            session_ts_utc=session_ts_utc,
            time_info=time_metadata(sample_time_status, utc=session_ts_utc),
            gt_reading=gt.get_reading(),
            env_reading=env_reading,
        )
        log.info("GT: on_sample completed — ts=%s", session_ts_utc)

    session_dir, session_dir_error = resolve_current_session_dir()
    if session_dir_error:
        return JSONResponse({
            "ok": False,
            "applied_at": applied_at,
            "requested": settings_dict,
            "error": session_dir_error,
        }, status_code=400)

    session_id: Optional[str] = None
    set_gt_starting(True)
    try:
        session_id = session_manager.start(metadata=settings_dict)
        session_path = gt_session_writer.start(applied_at, sessions_dir=session_dir)
        log.info("GT: calling driver start_session — id=%s settings=%s", session_id, settings_dict)
        result = gt.start_session(settings_dict, on_sample=on_sample)
        log.info("GT: driver start_session returned — id=%s result=%s state=%s", session_id, result, gt.get_state())
        log.info("GT: session started — id=%s", session_id)
        log.info("GT: local session file — %s", session_path)
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
        gt_session_writer.stop()
        if session_id is not None:
            session_manager.error(str(e))
        return JSONResponse({
            "ok": False,
            "applied_at": applied_at,
            "requested": settings_dict,
            "error": str(e),
        }, status_code=500)
    finally:
        set_gt_starting(False)

@app.post("/gt/stop")
def stop():
    at = utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")
    result = gt.stop()
    state = gt.get_state()
    finalize_gt_session_from_state(state)
    if not result.get("stopped", False) and state.get("run_end_reason") == "manual_stop":
        session_manager.error("stop_failed")
    if session_manager.status() == "running":
        reason = state.get("run_end_reason") or ("manual_stop" if result.get("stopped") else "stop_failed")
        gt_session_writer.stop()
        session_manager.error(reason)
    log.info("GT: session stopped — stopped=%s op=%s", result.get("stopped"), result.get("op_status"))
    return JSONResponse({
        "ok": result.get("stopped", False),
        "at": at,
        "run_end_reason": state.get("run_end_reason"),
    })

@app.get("/gt/latest")
def get_latest():
    return JSONResponse({"latest": gt.get_reading()})

@app.get("/env/latest")
def get_env_latest():
    try:
        reading = get_env_reading()
        env_daily_accumulator.update(reading)
        return JSONResponse({
            "latest": reading,
            "averages": env_daily_accumulator.current_averages(),
        })
    except Exception as e:
        return JSONResponse({
            "latest": None,
            "averages": env_daily_accumulator.current_averages(),
            "error": str(e),
        })

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
    state = gt.get_state()
    finalize_gt_session_from_state(state)
    return JSONResponse(state)

@app.get("/storage/targets")
def storage_targets():
    return JSONResponse({
        "current": get_current_session_target(),
        "targets": get_storage_targets(),
    })

@app.post("/storage/session-target")
def set_storage_session_target(req: SessionTargetRequest):
    if gt.get_state().get("run_active"):
        return JSONResponse({
            "ok": False,
            "error": "Cannot change session save target while a run is active.",
        }, status_code=409)

    if req.mode == "local":
        current = set_session_target_local()
        return JSONResponse({"ok": True, "current": current})

    if req.mode == "usb":
        if not req.path:
            return JSONResponse({"ok": False, "error": "USB target path is required."}, status_code=400)
        target = get_storage_target(req.path)
        if target is None:
            return JSONResponse({"ok": False, "error": "USB target is not mounted or writable."}, status_code=400)
        current = set_session_target_usb(target)
        return JSONResponse({"ok": True, "current": current})

    return JSONResponse({"ok": False, "error": "Unknown session target mode."}, status_code=400)

@app.post("/export/env-daily")
def export_env_daily(req: ExportDailyRequest):
    target = get_storage_target(req.path)
    if target is None:
        return JSONResponse({"ok": False, "error": "USB target is not mounted or writable."}, status_code=400)
    if not ENV_DAILY_AVERAGES_PATH.exists():
        return JSONResponse({
            "ok": False,
            "error": f"Daily averages file not found: {ENV_DAILY_AVERAGES_PATH}",
        }, status_code=404)

    export_dir = Path(target["path"]) / "golab-monitor" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now().astimezone().strftime("%Y-%m-%d_%H%M")
    dest = export_dir / f"env_daily_averages_export_{stamp}.jsonl"
    shutil.copy2(ENV_DAILY_AVERAGES_PATH, dest)
    return JSONResponse({
        "ok": True,
        "destination": str(dest),
        "filename": dest.name,
    })

@app.get("/state")
def get_state():
    with thresholds_lock:
        t = {
            "threshold_c03": thresholds.threshold_c03,
            "threshold_c50": thresholds.threshold_c50,
        }
    status = time_status()
    state = gt.get_state()
    finalize_gt_session_from_state(state)
    state.update({
        "gt_starting": is_gt_starting(),
        "settings": current_settings.dict(),
        "thresholds": t,
        "last_update": time.time(),
        "storage": {
            "session_save": get_current_session_target(),
        },
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

import asyncio
import csv
import json
import logging
import os
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
from drivers.env_driver_factory import build_environment_driver

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

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast?latitude=41.93&longitude=-73.91&current_weather=true"

GT_UID = "bb-0002"
GT_STORAGE_UID = "bb-golab-gt521-001"
ENV_STORAGE_UIDS = {
    "bb-golab-air-001": "bb-golab-air-001",
    "bb-golab-air-002": "bb-golab-air-002",
}

ENV_NODES = [
    {
        "uid": "bb-golab-air-001",
        "label": "GoLab Air 1",
        "driver": "web_node",
        "server_url": "https://bard-box.org",
        "source_uid": "bb-golab-air-001",
        "pms_sensor": "pms_a",
        "poll_interval_s": 5,
    },
    {
        "uid": "bb-golab-air-002",
        "label": "GoLab Air 2",
        "driver": "web_node",
        "server_url": "https://bard-box.org",
        "source_uid": "bb-golab-air-002",
        "pms_sensor": "pms_a",
        "poll_interval_s": 5,
    },
]

DEFAULT_LOCATION_LABEL = "GoLab"
DEFAULT_LOCATION_ID = 1
DEFAULT_SAMPLE_TIME_S = 10
DEFAULT_HOLD_TIME_S = 50
DEFAULT_SAMPLES = 480
ENV_NODE_STALE_AFTER_S = float(os.environ.get("GOLAB_ENV_NODE_STALE_AFTER_S", "30"))

DATA_DIR = Path.home() / "golab-monitor" / "data"
GT521_DATA_DIR = DATA_DIR / "gt521" / GT_STORAGE_UID
GT_SESSIONS_DIR = GT521_DATA_DIR / "sessions"
ENV_DATA_DIR = DATA_DIR / "env"
BACKUP_STATUS_PATH = Path(os.environ.get("GOLAB_BACKUP_STATUS", "/var/lib/golab-backup/status.json"))
BACKUP_REMOTE = os.environ.get("GOLAB_BACKUP_REMOTE", "bardbox")
BACKUP_REMOTE_ROOT = os.environ.get("GOLAB_BACKUP_REMOTE_ROOT", "sensor_data/golab-monitor")
BACKUP_DESTINATION = f"{BACKUP_REMOTE}:{BACKUP_REMOTE_ROOT.strip('/')}"
BACKUP_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "backup_to_drive.sh"

# Unit conversions for UI only
FT3_TO_M3 = 35.3147
PMS_0P1L_TO_M3 = 10000

def gtFt3ToM3(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return round(float(value) * FT3_TO_M3)
    except (TypeError, ValueError):
        return None

def pmsCountToM3(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value) * PMS_0P1L_TO_M3
    except (TypeError, ValueError):
        return None

app = FastAPI()
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

@app.on_event("startup")
def startup_storage_and_backup() -> None:
    ensure_data_dirs()
    launch_startup_backup()

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

ENV_DAILY_CSV_FIELDS = [
    "date_local",
    "generated_at_utc",
    "node_uid",
    "samples",
    "temp_c_avg",
    "rh_pct_avg",
    "press_pa_avg",
    "pm1_std_avg",
    "pm25_std_avg",
    "pm10_std_avg",
    "pm1_env_avg",
    "pm25_env_avg",
    "pm10_env_avg",
    "c03_avg_m3",
    "c05_avg_m3",
    "c10_avg_m3",
    "c25_avg_m3",
    "c50_avg_m3",
    "c100_avg_m3",
    "status",
]

GT_SESSION_CSV_FIELDS = [
    "timestamp_utc",
    "session_id",
    "device_uid",
    "c03",
    "c50",
    "c03_m3",
    "c50_m3",
    "exceeded_c03",
    "exceeded_c50",
    "env1_temp_c",
    "env1_rh_pct",
    "env1_press_pa",
    "env1_c03_m3",
    "env1_c05_m3",
    "env1_c10_m3",
    "env2_temp_c",
    "env2_rh_pct",
    "env2_press_pa",
    "env2_c03_m3",
    "env2_c05_m3",
    "env2_c10_m3",
    "time_source",
    "ntp_synced",
]

def storage_env_uid(uid: str) -> str:
    return ENV_STORAGE_UIDS.get(uid, uid)

def env_daily_csv_path(uid: str, date_local: Optional[str] = None) -> Path:
    return ENV_DATA_DIR / storage_env_uid(uid) / f"{date_local or local_date_str()}.csv"

def ensure_csv_header(path: Path, fieldnames: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > 0:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fieldnames).writeheader()

def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GT_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    ENV_DATA_DIR.mkdir(parents=True, exist_ok=True)
    for node in ENV_NODES:
        ensure_csv_header(env_daily_csv_path(str(node["uid"])), ENV_DAILY_CSV_FIELDS)

def append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    ensure_data_dirs()
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, separators=(",", ":"), default=str))
        f.write("\n")

env_drivers: Dict[str, Dict[str, Any]] = {}

def configured_env_uid(index: int = 0) -> Optional[str]:
    if not ENV_NODES:
        return None
    if index < len(ENV_NODES):
        return str(ENV_NODES[index]["uid"])
    return str(ENV_NODES[0]["uid"])

def env_device_key(uid: str) -> str:
    for idx, node in enumerate(ENV_NODES, start=1):
        if node["uid"] == uid:
            return f"env{idx}"
    return uid

def normalize_env_reading(uid: str, reading: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if reading is None:
        return None
    out = dict(reading)
    if not out.get("uid") or out.get("uid") == "unknown":
        out["uid"] = uid
    return out

def env_last_seen(reading: Optional[Dict[str, Any]]) -> Optional[str]:
    if not reading:
        return None
    extended = reading.get("extended") or {}
    return extended.get("last_seen") or reading.get("timestamp")

def env_unavailable_reading(
    uid: str,
    message: str = "Node unavailable",
    last_seen: Optional[str] = None,
    status: str = "offline",
) -> Dict[str, Any]:
    return {
        "uid": uid,
        "timestamp": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "data": {
            "temp_c": None,
            "pm1_std": None,
            "pm25_std": None,
            "pm10_std": None,
            "c03": None,
        },
        "extended": {
            "rh_pct": None,
            "press_pa": None,
            "pm1_env": None,
            "pm25_env": None,
            "pm10_env": None,
            "c05": None,
            "c10": None,
            "c25": None,
            "c50": None,
            "c100": None,
            "sample_idx": None,
            "message": message,
            "last_seen": last_seen,
            "stale_after_s": ENV_NODE_STALE_AFTER_S,
        },
        "raw": None,
        "error": message,
    }

def env_response_for_uid(uid: str) -> Dict[str, Any]:
    try:
        reading = get_env_reading(uid)
    except Exception as exc:
        log.exception("ENV: failed to read %s", uid)
        previous = env_daily_accumulator.latest(uid)
        return {
            "latest": env_unavailable_reading(
                uid,
                "No node found",
                previous.get("timestamp") if previous else None,
            ),
            "averages": env_daily_accumulator.current_averages(uid),
            "last_seen": previous.get("timestamp") if previous else None,
            "error": str(exc),
        }

    if not reading.get("error") and any(value is not None for value in (reading.get("data") or {}).values()):
        env_daily_accumulator.update(reading)
    averages_uid = reading.get("uid") or uid
    return {
        "latest": reading,
        "averages": env_daily_accumulator.current_averages(averages_uid),
        "last_seen": reading.get("timestamp"),
    }

def get_env_reading(uid: str) -> Dict[str, Any]:
    entry = env_drivers.get(uid)
    if entry is None:
        raise KeyError(f"Unknown env node uid: {uid}")
    reading = entry["driver"].get_reading()
    normalized = normalize_env_reading(uid, reading)
    if normalized is None:
        raise RuntimeError(f"No reading returned for env node {uid}")
    return normalized

def get_all_env_readings() -> Dict[str, Optional[Dict[str, Any]]]:
    readings: Dict[str, Optional[Dict[str, Any]]] = {}
    for uid in env_drivers:
        try:
            readings[uid] = get_env_reading(uid)
        except Exception:
            log.exception("ENV: failed to read %s", uid)
            readings[uid] = None
    return readings

def local_date_str(dt: Optional[datetime] = None) -> str:
    if dt is None:
        dt = utc_now()
    return dt.astimezone().date().isoformat()

def safe_timestamp_for_filename(ts_utc: str) -> str:
    return ts_utc.replace("T", "_").replace(":", "-").replace("Z", "")

def get_backup_status() -> Dict[str, Any]:
    status = {
        "destination": BACKUP_DESTINATION,
        "local_data_root": str(DATA_DIR),
        "status": "unknown",
        "last_attempt": None,
        "last_success": None,
        "last_error": None,
        "files_uploaded": None,
    }
    try:
        if BACKUP_STATUS_PATH.exists():
            with BACKUP_STATUS_PATH.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if isinstance(loaded, dict):
                status.update(loaded)
    except Exception:
        log.exception("Could not read backup status")
        status["status"] = "error"
        status["last_error"] = f"Could not read {BACKUP_STATUS_PATH}"
    status["destination"] = BACKUP_DESTINATION
    status["local_data_root"] = str(DATA_DIR)
    return status

def launch_backup(args: Optional[List[str]] = None) -> None:
    if not BACKUP_SCRIPT_PATH.exists():
        log.warning("Backup script not found: %s", BACKUP_SCRIPT_PATH)
        return
    cmd = ["bash", str(BACKUP_SCRIPT_PATH), *(args or [])]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("Backup launched: %s", " ".join(cmd))
    except Exception:
        log.exception("Could not launch backup")

def launch_startup_backup() -> None:
    launch_backup(["--reason", "startup"])

def launch_gt_session_backup(path: Path) -> None:
    launch_backup(["--file", str(path), "gt521/bb-golab-gt521-001/sessions", "--reason", "gt_session_complete"])

class EnvDailyAccumulator:
    def __init__(self):
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
            self._write_summary_locked(uid, node)

    def latest(self, uid: Optional[str] = None) -> Optional[Dict[str, Any]]:
        uid = uid or configured_env_uid() or ENV1_UID
        with self.lock:
            node = self._nodes.get(uid)
            if node is None:
                return None
            return node.get("latest")

    def current_averages(self, uid: Optional[str] = None) -> Dict[str, float]:
        uid = uid or configured_env_uid() or ENV1_UID
        key_map = {
            "temp_c_avg": "temp_c",
            "rh_pct_avg": "rh_pct",
            "press_pa_avg": "press_pa",
            "c03_avg_m3": "c03",
            "c05_avg_m3": "c05",
            "c10_avg_m3": "c10",
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
            ("press_pa", "press_pa_avg"),
            ("pm1_std", "pm1_std_avg"),
            ("pm25_std", "pm25_std_avg"),
            ("pm10_std", "pm10_std_avg"),
            ("pm1_env", "pm1_env_avg"),
            ("pm25_env", "pm25_env_avg"),
            ("pm10_env", "pm10_env_avg"),
            ("c03", "c03_avg_m3"),
            ("c05", "c05_avg_m3"),
            ("c10", "c10_avg_m3"),
            ("c25", "c25_avg_m3"),
            ("c50", "c50_avg_m3"),
            ("c100", "c100_avg_m3"),
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

        averaged: Dict[str, float] = {}
        for key, total in node["sums"].items():
            count = node["counts"].get(key, 0)
            if count:
                averaged[key] = total / count

        latest = node.get("latest") or {}
        path = env_daily_csv_path(uid, node["date_local"])
        ensure_csv_header(path, ENV_DAILY_CSV_FIELDS)
        row = {
            "date_local": node["date_local"],
            "generated_at_utc": utc_now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "node_uid": storage_env_uid(uid),
            "samples": node["samples"],
            "status": latest.get("status", "unknown"),
        }
        row.update(averaged)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=ENV_DAILY_CSV_FIELDS)
            writer.writeheader()
            writer.writerow({key: row.get(key, "") for key in ENV_DAILY_CSV_FIELDS})


class GTSessionWriter:
    def __init__(self, sessions_dir: Path):
        self.sessions_dir = sessions_dir
        self.lock = threading.Lock()
        self.active_path: Optional[Path] = None
        self.active_session_id: Optional[str] = None

    def start(self, start_utc: str, session_id: str, sessions_dir: Optional[Path] = None) -> Path:
        ensure_data_dirs()
        if sessions_dir is not None:
            sessions_dir.mkdir(parents=True, exist_ok=True)
        safe_ts = safe_timestamp_for_filename(start_utc)
        target_dir = sessions_dir or self.sessions_dir
        path = target_dir / f"gt_session_{safe_ts}.csv"
        with self.lock:
            self.active_path = path
            self.active_session_id = session_id
        ensure_csv_header(path, GT_SESSION_CSV_FIELDS)
        log.info("GT: session writer started — path=%s", path)
        return path

    def stop(self) -> Optional[Path]:
        with self.lock:
            path = self.active_path
            if self.active_path is not None:
                log.info("GT: session writer stopped — path=%s", self.active_path)
            self.active_path = None
            self.active_session_id = None
            return path

    def append_sample(
        self,
        session_ts_utc: str,
        time_info: Dict[str, Any],
        gt_reading: Optional[Dict[str, Any]],
        env_readings: Optional[Dict[str, Optional[Dict[str, Any]]]],
    ) -> None:
        with self.lock:
            path = self.active_path
            session_id = self.active_session_id
            if path is None:
                log.warning("GT: append_sample skipped because session writer has no active path")
                return

            devices: Dict[str, Any] = {"gt521": gt_reading}
            if env_readings:
                for uid, reading in env_readings.items():
                    devices[env_device_key(uid)] = reading

            gt_data = gt_reading or {}
            row: Dict[str, Any] = {
                "timestamp_utc": session_ts_utc,
                "session_id": session_id,
                "device_uid": GT_STORAGE_UID,
                "c03": gt_data.get("c03"),
                "c50": gt_data.get("c50"),
                "c03_m3": gtFt3ToM3(gt_data.get("c03")),
                "c50_m3": gtFt3ToM3(gt_data.get("c50")),
                "exceeded_c03": gt_data.get("exceeded_c03"),
                "exceeded_c50": gt_data.get("exceeded_c50"),
                "time_source": time_info.get("source"),
                "ntp_synced": time_info.get("ntp_synced"),
            }
            for key, reading in devices.items():
                if not key.startswith("env"):
                    continue
                env_data = (reading or {}).get("data") or {}
                env_extended = (reading or {}).get("extended") or {}
                row[f"{key}_temp_c"] = env_data.get("temp_c")
                row[f"{key}_rh_pct"] = env_extended.get("rh_pct")
                row[f"{key}_press_pa"] = env_extended.get("press_pa") or env_data.get("press_pa")
                row[f"{key}_c03_m3"] = pmsCountToM3(env_data.get("c03"))
                row[f"{key}_c05_m3"] = pmsCountToM3(env_extended.get("c05"))
                row[f"{key}_c10_m3"] = pmsCountToM3(env_extended.get("c10"))
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=GT_SESSION_CSV_FIELDS)
                writer.writerow({key: row.get(key, "") for key in GT_SESSION_CSV_FIELDS})
            log.info("GT: appended session sample — path=%s ts=%s", path, session_ts_utc)


ensure_data_dirs()
env_daily_accumulator = EnvDailyAccumulator()
gt_session_writer = GTSessionWriter(GT_SESSIONS_DIR)

def get_current_session_target() -> Dict[str, str]:
    ensure_data_dirs()
    return {
        "mode": "local",
        "path": str(GT_SESSIONS_DIR),
        "resolved_path": str(GT_SESSIONS_DIR),
        "label": "Local",
    }

def resolve_current_session_dir() -> tuple[Optional[Path], Optional[str]]:
    ensure_data_dirs()
    return GT_SESSIONS_DIR, None

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
GT_TERMINAL_END_REASONS = {"completed", "manual_stop", *GT_ERROR_END_REASONS}
GT_TERMINAL_STATE_DISPLAY_S = 5.0
gt_lifecycle_lock = threading.Lock()
gt_starting = False
gt_terminal_state_until = 0.0

def set_gt_starting(value: bool) -> None:
    global gt_starting, gt_terminal_state_until
    with gt_lifecycle_lock:
        gt_starting = value
        if value:
            gt_terminal_state_until = 0.0
    log.info("GT: lifecycle starting=%s", value)

def is_gt_starting() -> bool:
    with gt_lifecycle_lock:
        return gt_starting

def mark_gt_terminal_state_visible() -> None:
    global gt_terminal_state_until
    with gt_lifecycle_lock:
        gt_terminal_state_until = time.time() + GT_TERMINAL_STATE_DISPLAY_S

def gt_terminal_state_visible() -> bool:
    with gt_lifecycle_lock:
        return time.time() < gt_terminal_state_until

def api_gt_state(raw_state: Dict[str, Any]) -> Dict[str, Any]:
    state = dict(raw_state)
    starting = is_gt_starting()
    session_status = session_manager.status()
    idle_ready = (
        not starting
        and not state.get("run_active")
        and session_status != "running"
        and not gt_terminal_state_visible()
    )
    state["gt_starting"] = starting
    state["gt_idle_ready"] = idle_ready
    if idle_ready:
        state["run_end_reason"] = None
        state["last_op_status"] = None
        state["last_gt_sample_timestamp"] = None
        state["suspected_missed_samples"] = 0
    return state

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
    if reason not in GT_TERMINAL_END_REASONS:
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
    completed_session_path = gt_session_writer.stop()
    if reason == "completed":
        session_manager.complete()
        if completed_session_path is not None:
            launch_gt_session_backup(completed_session_path)
    elif reason in GT_ERROR_END_REASONS:
        session_manager.error(reason)
    elif reason == "manual_stop":
        session_manager.error("manual_stop")
    mark_gt_terminal_state_visible()
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

for node in ENV_NODES:
    uid = str(node["uid"])
    driver_type = str(node.get("driver", "serial_env_node"))
    driver = build_environment_driver(node, ENV_NODE_STALE_AFTER_S)
    env_drivers[uid] = {
        "config": node,
        "driver": driver,
    }
    try:
        driver.connect()
        info = driver.get_info()
        if info.get("uid") and info.get("uid") != "unknown":
            log.info("ENV: connected uid=%s label=%s driver=%s", info.get("uid"), node["label"], driver_type)
        else:
            log.warning("ENV: not connected yet uid=%s label=%s driver=%s", uid, node["label"], driver_type)
    except Exception:
        log.exception("ENV: failed to connect uid=%s label=%s driver=%s", uid, node["label"], driver_type)

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
    env_node_config_json = json.dumps([
        {
            "uid": str(node["uid"]),
            "label": str(node.get("label") or node["uid"]),
        }
        for node in ENV_NODES
    ])
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

            * {{ box-sizing: border-box; }}
            html {{ width: 100%; overflow-x: hidden; }}
            body {{ width: 100%; min-width: 0; margin: 0; overflow-x: hidden; font-family: system-ui; background: var(--bg); color: var(--text); }}
            img, canvas {{ max-width: 100%; }}
            .dashboard-shell {{ width: 100%; max-width: 1280px; margin: 0 auto; padding: 24px 20px 40px; overflow-x: hidden; }}
            .dashboard-shell > * {{ min-width: 0; }}
            h1 {{ margin-bottom: 30px; color: var(--text); }}
            h3, h4, label, .graph-title {{ color: var(--text); }}

            .header-row {{ display:flex; justify-content:space-between; align-items:center; gap:16px; margin-bottom:30px; min-width:0; }}
            .header-left {{ display:flex; align-items:center; gap:20px; min-width:0; }}
            .header-left img {{ flex:0 0 auto; height:60px; width:auto; }}
            .header-title {{ margin:0; font-size:28px; line-height:1; color:var(--text); }}
            .header-clock {{ font-size:18px; line-height:1; color:rgba(255,255,255,0.85); white-space:nowrap; }}

            .controls-row {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 30px; margin-bottom: 40px; }}
            @media (max-width: 900px) {{ .controls-row {{ grid-template-columns: 1fr; }} }}

            .gt-card {{ min-width:0; padding: 20px; border: 1px solid var(--panel-border); border-radius: 10px; background: var(--panel); margin-bottom: 30px; overflow:hidden; }}
            .gt-card-inner {{ display: grid; grid-template-columns: minmax(260px, 320px) minmax(0, 1fr); gap: 18px; align-items:start; min-width:0; }}
            .gt-settings-card {{ min-width:0; }}
            .gt-graphs-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:16px; min-width:0; }}
            @media (max-width: 1100px) {{ .gt-card-inner {{ grid-template-columns: 1fr; }} }}
            @media (max-width: 900px) {{ .gt-graphs-grid {{ grid-template-columns: 1fr; }} }}

            label {{ display:block; margin-top: 12px; font-weight: 600; }}
            input {{ font-size: 16px; padding: 8px; width: 100%; background: var(--panel); color: var(--text); border: 1px solid var(--panel-border); border-radius: 6px; }}
            .card {{ min-width:0; padding: 20px; border: 1px solid var(--panel-border); border-radius: 8px; background: var(--panel); box-shadow: none; overflow:hidden; }}
            button {{ font-size: 18px; padding: 10px 16px; margin: 0 10px 10px 0; cursor: pointer; background: var(--accent); color: white; border: none; border-radius: 6px; }}
            button:hover {{ background: var(--accent-hover); }}
            button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
            .muted {{ color: var(--muted); }}
            .small {{ font-size: 13px; }}
            .ok {{ color: var(--ok); font-weight: 700; }}
            .bad {{ color: var(--bad); font-weight: 700; }}

            .graph-card {{ min-width:0; max-width:100%; padding: 16px; border: 1px solid var(--panel-border); border-radius: 8px; background: #0b0b0b; overflow:hidden; }}
            .graph-title {{ font-size: 18px; font-weight: 700; margin-bottom: 12px; }}
            .graph-reading {{ font-size: 26px; font-weight: 700; color: var(--accent); margin-bottom: 12px; }}
            .graph-container {{ position: relative; width:100%; max-width:100%; height: clamp(240px, 30vw, 340px); margin-bottom: 12px; overflow:hidden; }}
            .graph-container canvas {{ display:block; width:100% !important; max-width:100% !important; height:100% !important; }}

            .threshold-status {{ display: inline-block; padding: 6px 12px; border-radius: 4px; font-size: 13px; font-weight: 600; margin-top: 10px; }}
            .threshold-status.safe {{ background: var(--safe-bg); color: var(--safe-text); }}
            .threshold-status.exceeded {{ background: var(--exceeded-bg); color: var(--exceeded-text); }}

            .env-nodes-grid {{ display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); align-items:start; gap:14px; min-width:0; }}
            .env-node-card {{ min-width:0; border:1px solid var(--panel-border); border-radius:8px; padding:16px; background:#0b0b0b; overflow:hidden; }}
            .env-node-card.error {{ border-color: var(--bad); }}
            .env-node-head {{ display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:0; }}
            .env-node-title-block {{ min-width:0; }}
            .env-node-title {{ overflow-wrap:anywhere; font-size:17px; font-weight:700; color:var(--text); line-height:1.2; }}
            .env-node-subtitle {{ margin-top:3px; color:var(--muted); font-size:12px; line-height:1.3; }}
            .env-node-uid {{ margin-top:3px; color:var(--muted); font-size:12px; font-weight:700; line-height:1.3; }}
            .env-status {{ flex:0 0 auto; border-radius:999px; border:1px solid currentColor; padding:4px 8px; font-size:12px; font-weight:800; line-height:1; color:var(--ok); }}
            .env-status.catchup {{ color:var(--warn); }}
            .env-status.offline,
            .env-status.error {{ color:var(--bad); }}
            .env-node-note {{ margin-top:10px; color:var(--muted); font-size:12px; line-height:1.3; }}
            .env-grid {{ display:grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap:10px; min-width:0; }}
            .env-metric {{ min-width:0; border:1px solid var(--panel-border); border-radius:8px; padding:10px; background:var(--panel); }}
            .env-value {{ min-height:27px; overflow-wrap:anywhere; font-size:24px; font-weight:750; line-height:1.1; }}
            .env-node-card .env-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); gap:8px; margin-top:12px; }}
            .env-node-card .env-metric {{ padding:7px 8px; }}
            .env-node-card .env-metric-label {{ margin-bottom:5px; color:var(--muted); font-size:11px; line-height:1.15; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
            .env-node-card .env-value {{ min-height:20px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-size:18px; font-weight:750; line-height:1.1; font-variant-numeric: tabular-nums; letter-spacing:0; }}
            @media (max-width: 900px) {{ .env-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
            @media (max-width: 980px) {{ .env-nodes-grid {{ grid-template-columns: minmax(0, 1fr); }} }}
            @media (max-width: 760px) {{ .env-node-card .env-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
            @media (max-width: 700px) {{
              .dashboard-shell {{ padding: 18px 14px 28px; }}
              .header-row {{ align-items:flex-start; }}
              .header-left img {{ height:44px; }}
              .header-title {{ font-size:22px; line-height:1.1; }}
              .header-clock {{ font-size:14px; }}
              .gt-card {{ padding:14px; }}
              .graph-card {{ padding:14px; }}
              .graph-container {{ height:260px; }}
              .env-nodes-grid {{ grid-template-columns: minmax(0, 1fr); }}
              .env-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }}
            }}
            @media (max-width: 380px) {{ .env-node-card .env-grid {{ grid-template-columns: 1fr; }} }}
        </style>
    </head>
    <body>
      <main class="dashboard-shell">
        <div class="header-row">
          <div class="header-left">
            <img src="/static/Bard-Web-Logos/bard-logo-red.png"/>
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

            <div class="gt-settings-card">
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

            <div class="gt-graphs-grid">
              <div class="graph-card">
                <div class="graph-title">0.3µm Particles</div>
                <div class="graph-reading">
                  <span id="current_c03">—</span> <span style="font-size: 16px; color: var(--muted);">/m³</span>
                </div>
                <div class="graph-container">
                  <canvas id="chart-c03"></canvas>
                </div>
                <div id="status-c03" class="threshold-status safe">✓ Below Threshold</div>
              </div>

              <div class="graph-card">
                <div class="graph-title">5.0µm Particles</div>
                <div class="graph-reading">
                  <span id="current_c50">—</span> <span style="font-size: 16px; color: var(--muted);">/m³</span>
                </div>
                <div class="graph-container">
                  <canvas id="chart-c50"></canvas>
                </div>
                <div id="status-c50" class="threshold-status safe">✓ Below Threshold</div>
              </div>
            </div>

          </div>
        </div>

        <div class="card" style="margin-top: 20px;">
          <h3>Environmental Nodes</h3>
          <div id="env-nodes-grid" class="env-nodes-grid"></div>
        </div>

        <div class="card" style="margin-top: 20px;">
          <h3>Backup</h3>
          <div class="small muted" style="margin-bottom:12px;">Local Data Root: <span id="backup-local-root">/home/golab/golab-monitor/data</span></div>
          <div class="small muted" style="margin-bottom:12px;">Session files are always written locally. Google Drive is a redundant backup only.</div>
          <div class="env-grid" style="max-width:920px;">
            <div class="env-metric"><div class="small muted">Destination</div><div id="backup-destination" class="env-value" style="font-size:16px;">—</div></div>
            <div class="env-metric"><div class="small muted">Status</div><div id="backup-status" class="env-value" style="font-size:20px;">—</div></div>
            <div class="env-metric"><div class="small muted">Last Attempt</div><div id="backup-last-attempt" class="env-value" style="font-size:16px;">—</div></div>
            <div class="env-metric"><div class="small muted">Last Success</div><div id="backup-last-success" class="env-value" style="font-size:16px;">—</div></div>
            <div class="env-metric"><div class="small muted">Files Uploaded</div><div id="backup-files-uploaded" class="env-value" style="font-size:20px;">—</div></div>
            <div class="env-metric"><div class="small muted">Last Error</div><div id="backup-last-error" class="env-value" style="font-size:16px;">—</div></div>
          </div>
        </div>
      </main>

        <script>
            let chartC03 = null;
            let chartC50 = null;
            let pollInterval = null;
            let wasRunning = false;
            let runBusy = false;
            let settingsInitialized = false;
            const ENV_NODES = {env_node_config_json};
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

            function setText(id, value) {{
              const el = document.getElementById(id);
              if (!el) return;
              el.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
            }}

            function envPrefix(uid) {{
              return `env_${{String(uid).replace(/[^a-zA-Z0-9]+/g, "_")}}`;
            }}

            function formatTimestamp(value) {{
              if (!value) return "—";
              const parsed = new Date(value);
              if (Number.isNaN(parsed.getTime())) return value;
              return parsed.toLocaleTimeString([], {{ hour: "2-digit", minute: "2-digit", second: "2-digit" }});
            }}

            function setEnvNodeStatus(prefix, status, message, lastSeen, hasData) {{
              const statusEl = document.getElementById(`${{prefix}}_status`);
              const noteEl = document.getElementById(`${{prefix}}_note`);
              const panel = statusEl?.closest(".env-node-card");
              const displayStatus = status || "offline";
              const ok = hasData && !["offline", "error"].includes(displayStatus);
              if (statusEl) {{
                statusEl.textContent = displayStatus.toUpperCase();
                statusEl.classList.toggle("error", !ok);
                statusEl.classList.toggle("offline", displayStatus === "offline");
                statusEl.classList.toggle("catchup", displayStatus === "catchup");
              }}
              panel?.classList.toggle("error", !ok);
              if (noteEl) {{
                noteEl.textContent = ok
                  ? `Last reading: ${{formatTimestamp(lastSeen)}}`
                  : `${{message || "Node unavailable"}}. Last seen: ${{formatTimestamp(lastSeen)}}`;
              }}
            }}

            function resetEnvNode(prefix) {{
              ["c03", "c05", "c10", "temp", "rh", "press"].forEach(metric => {{
                setText(`${{prefix}}_${{metric}}`, "—");
                setText(`${{prefix}}_${{metric}}_avg`, "Avg: —");
              }});
              setEnvNodeStatus(prefix, "offline", "Node unavailable", null, false);
            }}

            function updateEnvNode(uid, prefix, node) {{
              const latest = node?.latest;
              const avg = node?.averages || {{}};
              if (!latest) {{
                resetEnvNode(prefix);
                return;
              }}

              const d = latest.data || {{}};
              const hasData = Object.values(d).some(value => value !== null && value !== undefined);
              const lastSeen = node?.last_seen || latest.extended?.last_seen || latest.timestamp;
              if (!hasData) {{
                resetEnvNode(prefix);
                setEnvNodeStatus(prefix, latest.status, latest.extended?.message || latest.error || "Node unavailable", lastSeen, false);
                return;
              }}

              const x = latest.extended || {{}};
              setText(`${{prefix}}_c03`, (pmsCountToM3(d.c03) ?? "—").toString());
              setText(`${{prefix}}_c05`, (pmsCountToM3(x.c05) ?? "—").toString());
              setText(`${{prefix}}_c10`, (pmsCountToM3(x.c10) ?? "—").toString());
              setText(`${{prefix}}_temp`, (d.temp_c ?? "—").toString());
              setText(`${{prefix}}_rh`, (x.rh_pct ?? "—").toString());
              setText(`${{prefix}}_press`, (x.press_pa ?? d.press_pa ?? "—").toString());

              setText(`${{prefix}}_c03_avg`, avgText(pmsCountToM3(avg.c03)));
              setText(`${{prefix}}_c05_avg`, avgText(pmsCountToM3(avg.c05)));
              setText(`${{prefix}}_c10_avg`, avgText(pmsCountToM3(avg.c10)));
              setText(`${{prefix}}_temp_avg`, avgText(avg.temp_c));
              setText(`${{prefix}}_rh_avg`, avgText(avg.rh_pct));
              setEnvNodeStatus(prefix, latest.status, latest.extended?.message || latest.error, lastSeen, true);
            }}

            function renderEnvNodes(nodesByUid = {{}}) {{
              const grid = document.getElementById("env-nodes-grid");
              if (!grid) return;
              const configs = ENV_NODES.map(config => nodesByUid[config.uid]?.config || config);
              grid.innerHTML = configs.map(config => {{
                const uid = config.uid;
                const prefix = envPrefix(uid);
                const label = config.label || uid;
                return `
                  <div class="env-node-card" data-env-uid="${{uid}}">
                    <div class="env-node-head">
                      <div class="env-node-title-block">
                        <div class="env-node-title">${{label}}</div>
                        <div class="env-node-subtitle">Environmental monitor</div>
                        <div class="env-node-uid">UID ${{uid}}</div>
                      </div>
                      <div id="${{prefix}}_status" class="env-status">OFFLINE</div>
                    </div>
                    <div class="env-grid">
                      <div class="env-metric"><div class="env-metric-label">&gt;0.3µm /m³</div><div id="${{prefix}}_c03" class="env-value">—</div></div>
                      <div class="env-metric"><div class="env-metric-label">&gt;0.5µm /m³</div><div id="${{prefix}}_c05" class="env-value">—</div></div>
                      <div class="env-metric"><div class="env-metric-label">&gt;1.0µm /m³</div><div id="${{prefix}}_c10" class="env-value">—</div></div>
                      <div class="env-metric"><div class="env-metric-label">Temp (°C)</div><div id="${{prefix}}_temp" class="env-value">—</div></div>
                      <div class="env-metric"><div class="env-metric-label">RH (%)</div><div id="${{prefix}}_rh" class="env-value">—</div></div>
                      <div class="env-metric"><div class="env-metric-label">Pressure (Pa)</div><div id="${{prefix}}_press" class="env-value">—</div></div>
                    </div>
                    <div id="${{prefix}}_note" class="env-node-note">Waiting for live reading.</div>
                  </div>
                `;
              }}).join("");
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
                const r = await fetch("/env/all");
                const j = await r.json();
                const nodes = j?.nodes || {{}};
                renderEnvNodes(nodes);
                ENV_NODES.forEach(config => {{
                  const uid = config.uid;
                  updateEnvNode(uid, envPrefix(uid), nodes[uid]);
                }});
              }} catch (e) {{
                renderEnvNodes();
                ENV_NODES.forEach(config => resetEnvNode(envPrefix(config.uid)));
              }}
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

            function updateBackupStatus(backup) {{
              if (!backup) return;
              setText("backup-local-root", backup.local_data_root);
              setText("backup-destination", backup.destination);
              setText("backup-status", backup.status);
              setText("backup-last-attempt", backup.last_attempt);
              setText("backup-last-success", backup.last_success);
              setText("backup-files-uploaded", backup.files_uploaded);
              setText("backup-last-error", backup.last_error);
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

                updateBackupStatus(j.backup);
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
                }} else if (j.gt_idle_ready) {{
                  c.className = "small muted";
                  c.textContent = "No action yet.";
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
            renderEnvNodes();
            setInterval(pollLatest, 1000);
            setInterval(pollEnv, 1000);
            setInterval(pollState, 2000);
            pollState();
            pollLatest();
            pollEnv();

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

        env_readings = get_all_env_readings()
        for uid, env_reading in env_readings.items():
            if env_reading is None:
                previous = env_daily_accumulator.latest(uid)
                env_readings[uid] = env_unavailable_reading(
                    uid,
                    "No node found",
                    previous.get("timestamp") if previous else None,
                )
                continue

            if not env_reading.get("error") and any(value is not None for value in (env_reading.get("data") or {}).values()):
                env_daily_accumulator.update(env_reading)

        gt_session_writer.append_sample(
            session_ts_utc=session_ts_utc,
            time_info=time_metadata(sample_time_status, utc=session_ts_utc),
            gt_reading={
                **parsed,
                "exceeded_c03": exceeded_c03,
                "exceeded_c50": exceeded_c50,
            },
            env_readings=env_readings,
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
        session_path = gt_session_writer.start(applied_at, session_id=session_id, sessions_dir=session_dir)
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
def get_env_latest(uid: Optional[str] = None):
    selected_uid = uid or configured_env_uid()
    if selected_uid not in env_drivers:
        first_uid = configured_env_uid()
        if uid is None and first_uid:
            selected_uid = first_uid
        else:
            return JSONResponse({
                "latest": None,
                "averages": {},
                "error": f"Unknown env node uid: {selected_uid}",
            }, status_code=404)

    return JSONResponse(env_response_for_uid(selected_uid))

@app.get("/env/all")
def get_env_all():
    nodes: Dict[str, Dict[str, Any]] = {}
    for uid, entry in env_drivers.items():
        node = env_response_for_uid(uid)
        config = entry.get("config") or {}
        node["config"] = {
            "uid": uid,
            "label": str(config.get("label") or uid),
        }
        nodes[uid] = node
    return JSONResponse({"nodes": nodes})

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
    state = api_gt_state(state)
    state.update({
        "settings": current_settings.dict(),
        "thresholds": t,
        "last_update": time.time(),
        "storage": {
            "session_save": get_current_session_target(),
        },
        "backup": get_backup_status(),
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

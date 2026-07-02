"""
web_node_driver.py

BardBox web-node driver for GoLab environmental particle nodes.

The driver fetches the BardBox dashboard API, selects exactly one configured
source UID, and normalizes one configured PMS channel into the same reading
shape used by GoLab's serial environmental node driver.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

DEFAULT_SERVER_URL = "https://bard-box.org"
DEFAULT_PMS_SENSOR = "pms_a"
DEFAULT_TIMEOUT_S = 10.0
DEFAULT_POLL_INTERVAL_S = 5.0
DEFAULT_STALE_AFTER_S = 60.0

DATA_CHANNELS = ["temp_c", "pm1_std", "pm25_std", "pm10_std", "c03"]
EXTENDED_CHANNELS = [
    "rh_pct",
    "press_pa",
    "pm1_env",
    "pm25_env",
    "pm10_env",
    "c05",
    "c10",
    "c25",
    "c50",
    "c100",
    "sample_idx",
]
ENVIRONMENTAL_FIELDS = [
    "temp_c",
    "humidity_percent",
    "pressure_hpa",
    "bme680_gas_resistance_ohms",
    "rssi_dbm",
    "timestamp",
    "status",
    "read_count",
    "sample_interval_ms",
    "age_seconds",
    "stale_after_s",
]
PMS_ALIASES = {
    "pm1_std": ("pm1_std", "pm1_0_std", "pm1_0_standard", "pm1"),
    "pm25_std": ("pm25_std", "pm2_5_std", "pm2_5_standard", "pm25"),
    "pm10_std": ("pm10_std", "pm10_standard", "pm10"),
    "pm1_env": ("pm1_env", "pm1_0_env", "pm1_0_atm"),
    "pm25_env": ("pm25_env", "pm2_5_env", "pm2_5_atm"),
    "pm10_env": ("pm10_env", "pm10_atm"),
    "c03": ("c03", "particles_03um", "particles_0_3um", "count_0_3um"),
    "c05": ("c05", "particles_05um", "particles_0_5um", "count_0_5um"),
    "c10": ("c10", "particles_10um", "particles_1_0um", "count_1_0um"),
    "c25": ("c25", "particles_25um", "particles_2_5um", "count_2_5um"),
    "c50": ("c50", "particles_50um", "particles_5_0um", "count_5_0um"),
    "c100": ("c100", "particles_100um", "particles_10_0um", "count_10_0um"),
}


class WebNodeDriver:
    """BardBox HTTP dashboard API driver for one configured web node."""

    def __init__(
        self,
        server_url: str = DEFAULT_SERVER_URL,
        source_uid: str = "",
        pms_sensor: str = DEFAULT_PMS_SENSOR,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
    ):
        if pms_sensor not in {"pms_a", "pms_b"}:
            raise ValueError("pms_sensor must be 'pms_a' or 'pms_b'")
        self.server_url = server_url.rstrip("/")
        self.source_uid = source_uid
        self.pms_sensor = pms_sensor
        self.poll_interval_s = float(poll_interval_s)
        self.timeout_s = float(timeout_s)
        self.stale_after_s = float(stale_after_s)
        self._client: Optional[httpx.Client] = None
        self._latest: Optional[Dict[str, Any]] = None
        self._latest_fetch_monotonic: Optional[float] = None
        self._last_error: Optional[str] = None

    def connect(self, retries: int = 1, retry_delay: float = 0.0) -> None:
        """Create the HTTP client and try an initial poll without raising."""
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)

        for attempt in range(1, retries + 1):
            reading = self._poll()
            if reading.get("status") == "ok":
                return
            if attempt < retries and retry_delay > 0:
                time.sleep(retry_delay)
        log.warning("WebNode: initial poll did not produce ok reading uid=%s error=%s", self.source_uid, self._last_error)

    def get_info(self) -> Dict[str, Any]:
        return {
            "uid": self.source_uid or "unknown",
            "source_type": "bardbox_web_node",
            "transport": "http",
            "protocol": "bardbox_dashboard_api",
            "firmware": None,
            "info_raw": {
                "server_url": self.server_url,
                "pms_sensor": self.pms_sensor,
            },
        }

    def get_capabilities(self) -> Dict[str, Any]:
        return {
            "channels": {
                "temp_c": {"label": "Temperature", "unit": "deg C"},
                "pm1_std": {"label": "PM1.0 Std", "unit": "ug/m3"},
                "pm25_std": {"label": "PM2.5 Std", "unit": "ug/m3"},
                "pm10_std": {"label": "PM10 Std", "unit": "ug/m3"},
                "c03": {"label": "Particles >0.3um", "unit": "count/0.1L"},
            },
            "raw_available": True,
        }

    def get_reading(self) -> Dict[str, Any]:
        if self._should_poll():
            return self._poll()
        if self._latest is not None:
            return dict(self._latest)
        return self._unavailable("no valid reading received yet", "error")

    def stop(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _should_poll(self) -> bool:
        if self._latest_fetch_monotonic is None:
            return True
        return (time.monotonic() - self._latest_fetch_monotonic) >= self.poll_interval_s

    def _poll(self) -> Dict[str, Any]:
        try:
            payload = self._fetch_payload()
            node = self._find_node(payload)
            if node is None:
                return self._set_unavailable(f"UID not found: {self.source_uid}", "node_unavailable")

            channel = self._pms_channel(node)
            if not isinstance(channel, dict):
                return self._set_unavailable(f"PMS channel not found: {self.pms_sensor}", "node_unavailable", node)

            reading = self._normalize_node(node, channel)
            self._latest = reading
            self._latest_fetch_monotonic = time.monotonic()
            self._last_error = None if reading.get("status") == "ok" else reading.get("error")
            return dict(reading)
        except httpx.TimeoutException as exc:
            return self._set_unavailable(f"timeout fetching BardBox dashboard API: {exc}", "node_unavailable")
        except httpx.HTTPError as exc:
            return self._set_unavailable(f"server failure fetching BardBox dashboard API: {exc}", "node_unavailable")
        except (TypeError, ValueError) as exc:
            return self._set_unavailable(f"bad dashboard payload: {exc}", "error")

    def _fetch_payload(self) -> Dict[str, Any]:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s)
        url = urljoin(f"{self.server_url}/", "api/v1/dashboard/latest")
        response = self._client.get(url)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("dashboard payload must be an object")
        return payload

    def _find_node(self, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for node in self._iter_nodes(payload):
            uid = node.get("uid") or node.get("source_uid") or node.get("id")
            if uid == self.source_uid:
                return node
        return None

    def _iter_nodes(self, payload: Dict[str, Any]):
        candidates = []
        for key in ("nodes", "sources", "readings", "latest"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
            elif isinstance(value, dict):
                candidates.extend(item for item in value.values() if isinstance(item, dict))
        if isinstance(payload.get("data"), dict):
            data = payload["data"]
            for key in ("nodes", "sources", "readings"):
                value = data.get(key)
                if isinstance(value, list):
                    candidates.extend(item for item in value if isinstance(item, dict))
                elif isinstance(value, dict):
                    candidates.extend(item for item in value.values() if isinstance(item, dict))
        if payload.get("uid") or payload.get("source_uid"):
            candidates.append(payload)
        return candidates

    def _pms_channel(self, node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for container_key in ("pms", "pms_sensors", "particle_sensors"):
            container = node.get(container_key)
            if isinstance(container, dict) and isinstance(container.get(self.pms_sensor), dict):
                return container[self.pms_sensor]
        channel = node.get(self.pms_sensor)
        if isinstance(channel, dict):
            return channel
        data = node.get("data")
        if isinstance(data, dict):
            channel = data.get(self.pms_sensor)
            if isinstance(channel, dict):
                return channel
            prefix = f"{self.pms_sensor}_"
            if any(str(key).startswith(prefix) for key in data):
                return data
        return None

    def _normalize_node(self, node: Dict[str, Any], channel: Dict[str, Any]) -> Dict[str, Any]:
        source_ts = self._first_value(node, "timestamp", "updated_at", "last_seen")
        timestamp = source_ts if isinstance(source_ts, str) and source_ts else self._now_iso()
        age_seconds = self._numeric(self._first_value(node, "age_seconds"))
        stale_after_s = self._numeric(self._first_value(node, "stale_after_s")) or self.stale_after_s
        node_status = str(self._first_value(node, "status") or "ok")
        is_stale = self._first_value(node, "is_stale")

        stale = bool(is_stale) or (age_seconds is not None and stale_after_s is not None and age_seconds > stale_after_s)
        if node_status not in {"ok", "live"}:
            return self._unavailable("Node unavailable", "node_unavailable", node, timestamp)
        if stale:
            return self._unavailable("Node unavailable", "node_unavailable", node, timestamp)
        valid_key = f"{self.pms_sensor}_valid"
        if valid_key in channel and channel.get(valid_key) is not True:
            return self._unavailable(f"PMS channel not valid: {self.pms_sensor}", "node_unavailable", node, timestamp)

        data = {
            "temp_c": self._numeric(self._first_value(node, "temp_c")),
            "pm1_std": self._numeric(self._channel_value(channel, "pm1_std")),
            "pm25_std": self._numeric(self._channel_value(channel, "pm25_std")),
            "pm10_std": self._numeric(self._channel_value(channel, "pm10_std")),
            "c03": self._numeric(self._channel_value(channel, "c03")),
        }
        extended = {
            "rh_pct": self._numeric(self._first_value(node, "rh_pct", "humidity_percent")),
            "press_pa": self._pressure_pa(node),
            "pm1_env": self._numeric(self._channel_value(channel, "pm1_env")),
            "pm25_env": self._numeric(self._channel_value(channel, "pm25_env")),
            "pm10_env": self._numeric(self._channel_value(channel, "pm10_env")),
            "c05": self._numeric(self._channel_value(channel, "c05")),
            "c10": self._numeric(self._channel_value(channel, "c10")),
            "c25": self._numeric(self._channel_value(channel, "c25")),
            "c50": self._numeric(self._channel_value(channel, "c50")),
            "c100": self._numeric(self._channel_value(channel, "c100")),
            "sample_idx": self._numeric(self._first_value(node, "read_count")),
            "last_seen": timestamp,
            "source_uid": self.source_uid,
            "pms_sensor": self.pms_sensor,
        }
        for field in ENVIRONMENTAL_FIELDS:
            value = self._first_value(node, field)
            if value is not None:
                extended[field] = value
        return {
            "uid": self.source_uid,
            "timestamp": timestamp,
            "status": "ok",
            "data": data,
            "extended": extended,
            "raw": self._bounded_raw(node),
        }

    def _pressure_pa(self, node: Dict[str, Any]) -> Optional[float]:
        press_pa = self._numeric(self._first_value(node, "press_pa"))
        if press_pa is not None:
            return press_pa
        pressure_hpa = self._numeric(self._first_value(node, "pressure_hpa"))
        if pressure_hpa is None:
            return None
        return pressure_hpa * 100.0

    def _first_value(self, node: Dict[str, Any], *keys: str) -> Any:
        for key in keys:
            for source in (node, node.get("data"), node.get("extended"), node.get("environment")):
                if isinstance(source, dict) and key in source:
                    return source[key]
        return None

    def _channel_value(self, channel: Dict[str, Any], canonical_key: str) -> Any:
        for key in PMS_ALIASES[canonical_key]:
            if key in channel:
                return channel[key]
            prefixed_key = f"{self.pms_sensor}_{key}"
            if prefixed_key in channel:
                return channel[prefixed_key]
        data = channel.get("data")
        if isinstance(data, dict):
            for key in PMS_ALIASES[canonical_key]:
                if key in data:
                    return data[key]
                prefixed_key = f"{self.pms_sensor}_{key}"
                if prefixed_key in data:
                    return data[prefixed_key]
        return None

    def _numeric(self, value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed.is_integer():
            return int(parsed)
        return parsed

    def _set_unavailable(
        self,
        message: str,
        status: str,
        node: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        reading = self._unavailable(message, status, node)
        self._latest = reading
        self._latest_fetch_monotonic = time.monotonic()
        self._last_error = message
        return dict(reading)

    def _unavailable(
        self,
        message: str,
        status: str = "node_unavailable",
        node: Optional[Dict[str, Any]] = None,
        timestamp: Optional[str] = None,
    ) -> Dict[str, Any]:
        last_seen = timestamp or self._first_value(node or {}, "timestamp", "last_seen")
        extended = {
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
            "source_uid": self.source_uid,
            "pms_sensor": self.pms_sensor,
            "stale_after_s": self.stale_after_s,
        }
        return {
            "uid": self.source_uid or "unknown",
            "timestamp": self._now_iso(),
            "status": status,
            "data": {key: None for key in DATA_CHANNELS},
            "extended": extended,
            "raw": self._bounded_raw(node) if node else None,
            "error": message,
        }

    def _bounded_raw(self, node: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if not isinstance(node, dict):
            return None
        raw = {
            "uid": node.get("uid") or node.get("source_uid") or node.get("id"),
            "timestamp": self._first_value(node, "timestamp"),
            "status": self._first_value(node, "status"),
        }
        return {key: value for key, value in raw.items() if value is not None}

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SensorDriver = WebNodeDriver

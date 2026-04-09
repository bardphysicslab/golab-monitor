"""
bardbox_env_node_v1_driver.py

Bard Box driver for the Arduino Bard Box serial protocol v1 sensor node.
Supports devices with PMS5003 + BME280 (or similar) reporting over USB serial.

Bard Box protocol v1 messages:
  OK INFO uid=bb-0001 fw=1.0 sensors=PMS,BME280
  HDR,v1,sample_idx,temp_c,rh_pct,press_pa,pm1_std,pm25_std,pm10_std,pm1_env,pm25_env,pm10_env,c03,c05,c10,c25,c50,c100
  DAT,1,26.43,9.96,102873,1,1,1,1,1,1,294,92,7,0,0,0

Newline-terminated commands:
  INFO\\n  START\\n  STOP\\n  PING\\n  HEADER\\n  STATUS\\n
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import serial

log = logging.getLogger(__name__)

STALE_THRESHOLD_S = 60

# v1 normalized data channels (exactly 5)
V1_DATA_CHANNELS = ["temp_c", "pm1_std", "pm25_std", "pm10_std", "c03"]

# v1 extended channels (all other useful parsed values)
V1_EXTENDED_CHANNELS = [
    "rh_pct", "press_pa",
    "pm1_env", "pm25_env", "pm10_env",
    "c05", "c10", "c25", "c50", "c100",
    "sample_idx",
]

# Expected HDR field list (after HDR,v1)
EXPECTED_HDR_FIELDS = [
    "sample_idx", "temp_c", "rh_pct", "press_pa",
    "pm1_std", "pm25_std", "pm10_std",
    "pm1_env", "pm25_env", "pm10_env",
    "c03", "c05", "c10", "c25", "c50", "c100",
]

# Fields parsed as float
_FLOAT_FIELDS = {"temp_c", "rh_pct"}

# Fields parsed as int
_INT_FIELDS = {
    "sample_idx", "press_pa",
    "pm1_std", "pm25_std", "pm10_std",
    "pm1_env", "pm25_env", "pm10_env",
    "c03", "c05", "c10", "c25", "c50", "c100",
}


class BardboxEnvNodeV1Driver:
    """
    Driver for Bard Box serial protocol v1 Arduino sensor node.

    Connection sequence:
      1. Open serial port
      2. Send INFO\\n, parse uid/firmware/sensors
      3. Send START\\n
      4. Read and validate HDR,v1,...
      5. Accept DAT,... lines in background thread
    """

    def __init__(self, port: str = "/dev/ttyUSB0", baud: int = 115200):
        self.port = port
        self.baud = baud
        self._ser: Optional[serial.Serial] = None
        self._info: dict = {}
        self._hdr_fields: Optional[list] = None
        self._latest: Optional[dict] = None
        self._latest_time: Optional[float] = None
        self._latest_raw: Optional[str] = None
        self._lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Open serial port and complete the full connection sequence."""
        self._open()
        self._send_info()
        self._send_start()
        self._read_hdr()
        self._start_reader()
        log.info("BardboxEnvNode: connected uid=%s", self._info.get("uid"))

    def get_info(self) -> dict:
        return {
            "uid": self._info.get("uid", "unknown"),
            "source_type": "bardbox_env_node_v1",
            "transport": "serial",
            "protocol": "bardbox",
            "firmware": self._info.get("firmware"),
            "info_raw": {
                "sensors": self._info.get("sensors"),
            },
        }

    def get_capabilities(self) -> dict:
        return {
            "channels": {
                "temp_c":   {"label": "Temperature",      "unit": "°C"},
                "pm1_std":  {"label": "PM1.0 Std",        "unit": "µg/m³"},
                "pm25_std": {"label": "PM2.5 Std",        "unit": "µg/m³"},
                "pm10_std": {"label": "PM10 Std",         "unit": "µg/m³"},
                "c03":      {"label": "Particles >0.3µm", "unit": "count/0.1L"},
            },
            "raw_available": True,
        }

    def get_reading(self) -> dict:
        uid = self._info.get("uid", "unknown")
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        with self._lock:
            latest = self._latest
            latest_time = self._latest_time
            latest_raw = self._latest_raw

        if latest is None:
            return {
                "uid": uid,
                "timestamp": now_iso,
                "status": "error",
                "data": {
                    "temp_c": None,
                    "pm1_std": None,
                    "pm25_std": None,
                    "pm10_std": None,
                    "c03": None,
                },
                "extended": {},
                "raw": None,
                "error": "no valid reading received yet",
            }

        age = time.monotonic() - latest_time
        status = "ok" if age <= STALE_THRESHOLD_S else "stale"

        data = {k: latest.get(k) for k in V1_DATA_CHANNELS}
        extended = {k: latest.get(k) for k in V1_EXTENDED_CHANNELS}

        return {
            "uid": uid,
            "timestamp": latest["_timestamp"],
            "status": status,
            "data": data,
            "extended": extended,
            "raw": latest_raw,
        }

    def stop(self) -> None:
        """Stop the background reader and close the serial port."""
        self._stop_event.set()
        if self._reader_thread and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=3)
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(b"STOP\n")
                self._ser.flush()
            except Exception:
                pass
            self._ser.close()
        log.info("BardboxEnvNode: stopped")

    # ------------------------------------------------------------------
    # Connection sequence
    # ------------------------------------------------------------------

    def _open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        log.info("BardboxEnvNode: opening %s at %d baud", self.port, self.baud)
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            timeout=5,
        )
        time.sleep(2.5)
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()

    def _send_info(self) -> None:
        """Send INFO\\n and parse the OK INFO response."""
        log.info("BardboxEnvNode: sending INFO")
        self._ser.write(b"INFO\n")
        self._ser.flush()

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            line = self._readline()
            if line is None:
                continue
            if line.startswith("OK INFO"):
                self._parse_info_line(line)
                log.info(
                    "BardboxEnvNode: INFO parsed uid=%s fw=%s",
                    self._info.get("uid"), self._info.get("firmware"),
                )
                return

        raise RuntimeError("BardboxEnvNode: timed out waiting for OK INFO response")

    def _parse_info_line(self, line: str) -> None:
        """Parse: OK INFO uid=bb-0001 fw=1.0 sensors=PMS,BME280"""
        parts = line.split()
        kv = {}
        for part in parts[2:]:  # skip "OK INFO"
            if "=" in part:
                key, _, val = part.partition("=")
                kv[key] = val
        self._info = {
            "uid": kv.get("uid", "unknown"),
            "firmware": kv.get("fw"),
            "sensors": kv.get("sensors"),
        }

    def _send_start(self) -> None:
        """Send START\\n once."""
        log.info("BardboxEnvNode: sending START")
        self._ser.write(b"START\n")
        self._ser.flush()

    def _read_hdr(self) -> None:
        """Wait for HDR,v1,... and validate it."""
        log.info("BardboxEnvNode: waiting for HDR")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            line = self._readline()
            if line is None:
                continue
            if line.startswith("HDR,"):
                self._parse_hdr(line)
                log.info(
                    "BardboxEnvNode: HDR validated, %d fields", len(self._hdr_fields)
                )
                return

        raise RuntimeError("BardboxEnvNode: timed out waiting for HDR,v1,...")

    def _parse_hdr(self, line: str) -> None:
        """Parse and validate HDR,v1,field1,field2,..."""
        parts = line.split(",")
        if len(parts) < 3:
            raise ValueError(f"BardboxEnvNode: malformed HDR line: {line!r}")
        version = parts[1]
        if version != "v1":
            raise ValueError(
                f"BardboxEnvNode: unsupported protocol version: {version!r}"
            )
        fields = parts[2:]
        if fields != EXPECTED_HDR_FIELDS:
            raise ValueError(
                f"BardboxEnvNode: unexpected HDR fields.\n"
                f"  Expected: {EXPECTED_HDR_FIELDS}\n"
                f"  Got:      {fields}"
            )
        self._hdr_fields = fields

    # ------------------------------------------------------------------
    # Background reader
    # ------------------------------------------------------------------

    def _start_reader(self) -> None:
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="bardbox-env-node-reader",
            daemon=True,
        )
        self._reader_thread.start()
        log.info("BardboxEnvNode: reader thread started")

    def _reader_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                line = self._readline()
                if line is None:
                    continue
                if line.startswith("DAT,"):
                    self._handle_dat(line)
                # Ignore all other lines (OK START, OK STOP, PONG, etc.)
            except Exception:
                log.exception("BardboxEnvNode: error in reader loop")
                time.sleep(0.1)

    def _handle_dat(self, line: str) -> None:
        """Parse a DAT,... line and update the latest reading."""
        if self._hdr_fields is None:
            log.warning("BardboxEnvNode: received DAT before HDR — ignoring")
            return

        parts = line.split(",")
        if len(parts) < 2 or parts[0] != "DAT":
            return

        values = parts[1:]
        if len(values) != len(self._hdr_fields):
            log.warning(
                "BardboxEnvNode: DAT field count mismatch — expected %d, got %d: %r",
                len(self._hdr_fields), len(values), line,
            )
            return

        try:
            parsed: dict = {}
            for field, raw_val in zip(self._hdr_fields, values):
                if field in _FLOAT_FIELDS:
                    parsed[field] = float(raw_val)
                elif field in _INT_FIELDS:
                    parsed[field] = int(raw_val)
                else:
                    parsed[field] = raw_val
            parsed["_timestamp"] = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (ValueError, TypeError) as e:
            log.warning("BardboxEnvNode: failed to parse DAT line %r: %s", line, e)
            return

        with self._lock:
            self._latest = parsed
            self._latest_time = time.monotonic()
            self._latest_raw = line

        log.debug(
            "BardboxEnvNode: sample received temp_c=%s c03=%s",
            parsed.get("temp_c"), parsed.get("c03"),
        )

    # ------------------------------------------------------------------
    # Serial helper
    # ------------------------------------------------------------------

    def _readline(self) -> Optional[str]:
        """Read one line from serial. Returns None on timeout or empty."""
        try:
            raw = self._ser.readline()
            if not raw:
                return None
            line = raw.decode("ascii", errors="replace").strip()
            return line if line else None
        except serial.SerialException as e:
            log.error("BardboxEnvNode: serial error: %s", e)
            return None


# ------------------------------------------------------------------
# Standalone test mode
# ------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        stream=sys.stdout,
    )

    port = sys.argv[1] if len(sys.argv) > 1 else "/dev/ttyUSB0"
    print(f"Connecting to {port}...")

    driver = BardboxEnvNodeV1Driver(port=port)
    driver.connect()

    print("\n--- get_info() ---")
    print(json.dumps(driver.get_info(), indent=2))

    print("\n--- get_capabilities() ---")
    print(json.dumps(driver.get_capabilities(), indent=2, ensure_ascii=False))

    print("\n--- Polling get_reading() (Ctrl+C to stop) ---")
    try:
        while True:
            reading = driver.get_reading()
            print(json.dumps(reading, indent=2))
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopping...")
        driver.stop()

# Contract test alias
SensorDriver = BardboxEnvNodeV1Driver

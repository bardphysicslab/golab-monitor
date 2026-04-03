"""
gt521s_driver.py
Bard Box driver for the GT-521S optical particle counter.

Channels : c03 (≥ 0.3 µm), c05 (second configured channel)
Interface : USB serial via CP2102 (Silicon Labs)
Baud rate : 9600
Count units: count/ft³ (CU 0)

Implements the Bard Box driver interface defined in
docs/pi-driver-instructions.md:
  get_info()         -> device metadata
  get_capabilities() -> channel and sampling description
  get_reading()      -> normalized Bard Box reading object

All serial handshake and parsing logic is internal to this driver.
The GT-521S streams CSV lines during a session; the driver buffers the
latest parsed line and exposes it atomically via get_reading().

NOTE — Channel naming:
  The parser checks for particle sizes 0.3 µm (c03) and 5.0 µm (c05).
  Per channel-names.md, 5.0 µm is canonically c50. The mapping to c05
  follows the GoLab refactor specification. Confirm the actual sizes
  configured on the device to verify the correct canonical channel name.
"""

import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import serial


DEFAULT_UID = "bb-0001"
DEFAULT_PORT = (
    "/dev/serial/by-id/"
    "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0"
)
DEFAULT_BAUD = 9600


class GT521SDriver:
    """
    Bard Box driver for the GT-521S optical particle counter.

    Usage
    -----
    driver = GT521SDriver()
    driver.open()
    driver.set_sample_time(10)
    driver.set_hold_time(50)
    driver.set_samples(480)
    driver.set_report_csv()
    driver.start()
    driver.ensure_reader()

    reading = driver.get_reading()   # normalized Bard Box reading or None
    driver.stop()
    driver.stop_reader()
    """

    def __init__(
        self,
        uid: str = DEFAULT_UID,
        port: str = DEFAULT_PORT,
        baud: int = DEFAULT_BAUD,
    ):
        self._uid = uid
        self._port = port
        self._baud = baud

        self.ser: Optional[serial.Serial] = None
        self.lock = threading.Lock()

        self.reader_thread: Optional[threading.Thread] = None
        self.reader_stop = threading.Event()
        self.reader_running = False

        self.run_active = False
        self.target_samples = 0
        self.received_samples = 0

        self._latest: Optional[Dict[str, Any]] = None
        self._latest_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Bard Box driver interface
    # ------------------------------------------------------------------

    def get_info(self) -> Dict[str, Any]:
        """Return stable device metadata."""
        return {
            "uid": self._uid,
            "source_type": "gt521s",
            "transport": "serial",
            "protocol": "vendor",
            "firmware": None,
        }

    def get_capabilities(self) -> Dict[str, Any]:
        """Return device capabilities per capabilities-schema.md."""
        return {
            "device": {
                "device_type": "particle_counter",
                "source_type": "gt521s",
                "transport": "serial",
            },
            "channels": {
                "c03": {"label": "0.3 µm", "unit": "count/ft³"},
                "c05": {"label": "0.5 µm", "unit": "count/ft³"},
            },
            "sampling": {
                "mode": "session",
                "supports_live": True,
            },
            "controls": {
                "start": True,
                "stop": True,
                "configure": True,
            },
        }

    def get_reading(self) -> Optional[Dict[str, Any]]:
        """
        Return the most recent normalized reading, or None if no data yet.

        The GT-521S streams CSV lines during a session. This driver buffers
        the latest parsed line and returns it atomically. The streaming
        behavior is not exposed to the caller.
        """
        with self._latest_lock:
            if self._latest is None:
                return None
            return {
                "uid": self._uid,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "ok",
                "data": {
                    "c03": self._latest.get("c03"),
                    "c05": self._latest.get("c05"),
                },
                "extended": {
                    "device_ts": self._latest.get("_device_ts"),
                },
                "raw": self._latest.get("_raw_line"),
            }

    # ------------------------------------------------------------------
    # Serial port
    # ------------------------------------------------------------------

    def open(self):
        if self.ser and self.ser.is_open:
            return

        self.ser = serial.Serial(
            self._port,
            self._baud,
            timeout=0,
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

    # ------------------------------------------------------------------
    # Command helpers (serial handshake logic preserved from GoLab GT521)
    # ------------------------------------------------------------------

    def _read_for(self, seconds: float = 1.0) -> bytes:
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
        Send command (CR-terminated) and collect response.
        ok=True means a '*' prompt was seen (device is responsive).
        Holds self.lock for the whole transaction so the reader thread
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

    # ---- basic commands ----
    def start(self):     return self.send_line(b"S", read_seconds=0.9)
    def stop(self):      return self.send_line(b"E", read_seconds=0.9)
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

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_line(line: str) -> Optional[Dict[str, Any]]:
        """
        Parse one CSV line from the GT-521S into a normalized channel dict.

        Returns dict with keys c03, c05, _device_ts, _raw_line, or None.
        """
        line = line.strip()
        if not line:
            return None

        line = line.lstrip("*").strip()

        if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},", line):
            return None

        raw_line = line
        if "*" in line:
            line = line.split("*", 1)[0].strip().rstrip(",")

        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            return None

        device_ts = parts[0]
        try:
            size1 = float(parts[1]); cnt1 = int(parts[2])
            size2 = float(parts[3]); cnt2 = int(parts[4])
        except Exception:
            return None

        out: Dict[str, Any] = {
            "_device_ts": device_ts,
            "_raw_line": raw_line,
        }

        if abs(size1 - 0.3) < 0.11:
            out["c03"] = cnt1
        if abs(size1 - 5.0) < 0.11:
            out["c05"] = cnt1
        if abs(size2 - 0.3) < 0.11:
            out["c03"] = cnt2
        if abs(size2 - 5.0) < 0.11:
            out["c05"] = cnt2

        if "c03" not in out and "c05" not in out:
            return None

        return out

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self):
        buf = b""
        self.reader_running = True
        try:
            while not self.reader_stop.is_set():
                if not self.ser:
                    time.sleep(0.1)
                    continue

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
                        parsed = self._parse_line(s)
                        if parsed:
                            self.received_samples += 1
                            with self._latest_lock:
                                self._latest = parsed

                            if (
                                self.run_active
                                and self.target_samples > 0
                                and self.received_samples >= self.target_samples
                            ):
                                self.run_active = False
                                self.stop_reader()
                else:
                    time.sleep(0.05)
        finally:
            self.reader_running = False

    def ensure_reader(self):
        if self.reader_thread and self.reader_thread.is_alive() and not self.reader_stop.is_set():
            return

        if self.reader_thread and self.reader_thread.is_alive() and self.reader_stop.is_set():
            self.reader_thread.join(timeout=1.0)
            if self.reader_thread.is_alive():
                return

        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def stop_reader(self):
        self.reader_stop.set()

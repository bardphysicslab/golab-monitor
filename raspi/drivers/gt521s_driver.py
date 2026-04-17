"""
gt521s_driver.py
Bard Box driver for the GT-521S optical particle counter.

Channels : c03 (≥ 0.3 µm), c50 (≥ 5.0 µm)
Interface : USB serial via CP2102 (Silicon Labs)
Baud rate : 9600
Count units: count/ft³ — GT default CF mode; we send CU 0 to confirm.

GT-521S defaults (from manual):
  - Default size channels: 0.3 µm and 0.5 µm
  - This device is configured for 0.3 µm and 5.0 µm (intentional deviation)
  - Default baud: 9600
  - Default serial mode: RS-232
  - Default concentration unit: CF (particles/ft³) — matches CU 0; we send
    CU 0 explicitly at the start of every session to guarantee unit consistency.
  - Real-time serial output occurs at the end of each sample period.

Implements the Bard Box driver interface:
  get_info()         -> device metadata
  get_capabilities() -> channel and sampling description
  get_reading()      -> normalized Bard Box reading object
  get_state()        -> current run state

High-level session API (used by main.py):
  start_session(settings, on_sample) -> full start sequence
  stop()                             -> full stop sequence
"""

import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

import serial

log = logging.getLogger(__name__)

DEFAULT_UID = "bb-0002"
DEFAULT_PORT = (
    "/dev/serial/by-id/"
    "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0"
)
DEFAULT_BAUD = 9600


class GT521SDriver:
    """
    Bard Box driver for the GT-521S optical particle counter.

    High-level usage
    ----------------
    driver = GT521SDriver()
    driver.start_session(settings, on_sample=callback)
    reading = driver.get_reading()   # normalized Bard Box reading or None
    driver.stop()
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
        self.state_lock = threading.Lock()

        self.reader_thread: Optional[threading.Thread] = None
        self.reader_stop = threading.Event()
        self.reader_running = False

        self.run_active = False
        self.target_samples = 0
        self.received_samples = 0
        self.run_started_at_local: Optional[float] = None
        self.expected_sample_interval_s: Optional[float] = None
        self.watchdog_timeout_s: Optional[float] = None
        self.last_sample_received_at_local: Optional[float] = None
        self.last_gt_sample_timestamp: Optional[str] = None
        self.last_op_status: Optional[str] = None
        self.run_end_reason: Optional[str] = None
        self.suspected_missed_samples = 0
        self.consecutive_watchdog_misses = 0
        self.watchdog_thread: Optional[threading.Thread] = None
        self.watchdog_stop = threading.Event()
        self._last_gt_sample_epoch: Optional[float] = None
        self.sample_delivery_active = False

        self._latest: Optional[Dict[str, Any]] = None
        self._latest_lock = threading.Lock()

        self._on_sample: Optional[Callable[[Dict[str, Any]], None]] = None

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
                "c50": {"label": "5.0 µm", "unit": "count/ft³"},
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
        the latest parsed line and returns it atomically.
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
                    "c50": self._latest.get("c50"),
                },
                "extended": {
                    "device_ts": self._latest.get("_device_ts"),
                },
                "raw": None,
            }

    def get_state(self) -> Dict[str, Any]:
        """Return current run state."""
        with self.state_lock:
            return {
                "run_active": self.run_active,
                "received_samples": self.received_samples,
                "target_samples": self.target_samples,
                "reader_running": self.reader_running,
                "run_started_at_local": self.run_started_at_local,
                "expected_sample_interval_s": self.expected_sample_interval_s,
                "watchdog_timeout_s": self.watchdog_timeout_s,
                "last_sample_received_at_local": self.last_sample_received_at_local,
                "last_gt_sample_timestamp": self.last_gt_sample_timestamp,
                "last_op_status": self.last_op_status,
                "run_end_reason": self.run_end_reason,
                "suspected_missed_samples": self.suspected_missed_samples,
                "consecutive_watchdog_misses": self.consecutive_watchdog_misses,
                "sample_delivery_active": self.sample_delivery_active,
            }

    # ------------------------------------------------------------------
    # High-level session API
    # ------------------------------------------------------------------

    def start_session(
        self,
        settings: Dict[str, Any],
        on_sample: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Dict[str, Any]:
        """
        Full start sequence: open → wake → stop reader → stop → configure → start → verify OP → ensure reader.

        Parameters
        ----------
        settings : dict with keys sample_time_s, hold_time_s, samples
        on_sample : called with the parsed dict for each received CSV line

        Returns dict with ok, applied, mismatch, op_status.
        Raises RuntimeError on failure.
        """
        log.info("GT: driver start_session begin — settings=%s", settings)
        self._on_sample = on_sample

        log.info("GT: opening serial port %s", self._port)
        self._open()
        log.info("GT: waking device")
        self.wake()

        log.info("GT: stopping any active reader and sending E")
        self._stop_reader_wait()
        self._cmd_stop()
        time.sleep(0.2)

        log.info("GT: configuring — %s", settings)
        applied, mismatch = self._configure(settings)

        log.info("GT: starting sampling (S)")
        self._cmd_start()

        op = self._check_op_status()
        log.info("GT: OP status after start: %s", op)
        if op == "S":
            raise RuntimeError(f"GT did not start: OP returned '{op}' (Stopped)")

        self._initialize_run_tracking(settings, op)
        log.info("GT: starting reader thread")
        self._ensure_reader()
        self._ensure_watchdog()

        log.info("GT: driver start_session ready — state=%s", self.get_state())
        return {
            "ok": True,
            "applied": applied,
            "mismatch": mismatch,
            "op_status": op,
        }

    def stop(self) -> Dict[str, Any]:
        """
        Full stop sequence: stop reader → send E → verify stopped via OP.

        Returns dict with stopped, op_status.
        """
        with self.state_lock:
            already_ended = not self.run_active and self.run_end_reason is not None
        if not already_ended:
            self._finalize_run("manual_stop", force=True)

        log.info("GT: stopping watchdog thread")
        self._stop_watchdog_wait()
        log.info("GT: stopping reader thread")
        self._stop_reader_wait()

        log.info("GT: opening port for stop (no-op if already open)")
        self._open()
        log.info("GT: sending stop (E)")
        self._cmd_stop()
        time.sleep(0.15)

        stopped = False
        op = "?"
        for _ in range(6):
            op = self._check_op_status()
            with self.state_lock:
                self.last_op_status = op
            log.info("GT: OP status after stop: %s", op)
            if op in ("S", "STOP"):
                stopped = True
                break
            time.sleep(0.3)

        return {"stopped": stopped, "op_status": op}

    def abort_run(self, reason: str) -> Dict[str, Any]:
        """
        Abort an active run for an application-level fault such as invalid time.

        This is intentionally not session management: the application owns
        session metadata and storage finalization. The driver only stops the
        hardware stream and records the terminal run reason.
        """
        self._finalize_run(reason, force=True)
        log.info("GT: aborting run — reason=%s", reason)
        self._stop_watchdog_wait()
        self._stop_reader_wait()

        stopped = False
        op = "?"
        try:
            self._open()
            self._cmd_stop()
            time.sleep(0.15)
            for _ in range(6):
                op = self._check_op_status()
                with self.state_lock:
                    self.last_op_status = op
                log.info("GT: OP status after abort: %s", op)
                if op in ("S", "STOP"):
                    stopped = True
                    break
                time.sleep(0.3)
        except Exception:
            log.exception("GT: abort_run failed while stopping hardware")

        return {"stopped": stopped, "op_status": op, "run_end_reason": reason}

    # ------------------------------------------------------------------
    # Escape hatch
    # ------------------------------------------------------------------

    def raw_command(self, cmd: str) -> str:
        """Send a raw GT command string; return the response as a string."""
        ok, raw = self.send_line(cmd.encode(), read_seconds=1.2)
        return raw.decode(errors="replace")

    def vendor_get_settings(self) -> str:
        """Retrieve the full settings report from the device (command '1')."""
        ok, raw = self.send_line(b"1", read_seconds=2.0)
        return raw.decode(errors="replace")

    def vendor_get_status(self) -> str:
        """Query OP and return the raw response string."""
        ok, raw = self._cmd_op()
        return raw.decode(errors="replace")

    # ------------------------------------------------------------------
    # Run tracking
    # ------------------------------------------------------------------

    def _initialize_run_tracking(self, settings: Dict[str, Any], op_status: str) -> None:
        sample_time_s = float(settings.get("sample_time_s", 10) or 0)
        hold_time_s = float(settings.get("hold_time_s", 50) or 0)
        expected_interval = max(1.0, sample_time_s + hold_time_s)
        grace = max(15.0, min(60.0, expected_interval * 0.5))
        watchdog_timeout = expected_interval + grace

        with self.state_lock:
            self.target_samples = int(settings.get("samples", 0) or 0)
            self.received_samples = 0
            self.run_active = True
            self.run_started_at_local = time.time()
            self.expected_sample_interval_s = expected_interval
            self.watchdog_timeout_s = watchdog_timeout
            self.last_sample_received_at_local = None
            self.last_gt_sample_timestamp = None
            self.last_op_status = op_status
            self.run_end_reason = None
            self.suspected_missed_samples = 0
            self.consecutive_watchdog_misses = 0
            self._last_gt_sample_epoch = None
            self.sample_delivery_active = False

        self.watchdog_stop.clear()
        log.info(
            "GT: run tracking initialized — target=%s interval=%.1fs watchdog=%.1fs op=%s",
            self.target_samples,
            expected_interval,
            watchdog_timeout,
            op_status,
        )

    def _finalize_run(self, reason: str, force: bool = False) -> None:
        with self.state_lock:
            log.info(
                "GT_DEBUG: finalize requested — reason=%s force=%s run_active=%s prior_reason=%s received=%s target=%s",
                reason,
                force,
                self.run_active,
                self.run_end_reason,
                self.received_samples,
                self.target_samples,
            )
            if not force and self.run_end_reason is not None and self.run_end_reason != reason:
                log.info(
                    "GT_DEBUG: finalize ignored — requested_reason=%s existing_reason=%s received=%s target=%s",
                    reason,
                    self.run_end_reason,
                    self.received_samples,
                    self.target_samples,
                )
                return
            self.run_active = False
            self.run_end_reason = reason
            target_samples = self.target_samples
            received_samples = self.received_samples
            suspected_missed = self.suspected_missed_samples

        self.watchdog_stop.set()
        log.info(
            "GT: final run summary — target=%s received=%s suspected_missed=%s reason=%s",
            target_samples,
            received_samples,
            suspected_missed,
            reason,
        )

    @staticmethod
    def _parse_gt_sample_timestamp(value: Optional[str]) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp()
        except Exception:
            return None

    def _record_sample_tracking(self, parsed: Dict[str, Any]) -> tuple[int, bool]:
        now = time.time()
        device_ts = parsed.get("_device_ts")
        device_epoch = self._parse_gt_sample_timestamp(device_ts)

        with self.state_lock:
            previous_epoch = self._last_gt_sample_epoch
            expected_interval = self.expected_sample_interval_s

            if (
                previous_epoch is not None
                and device_epoch is not None
                and expected_interval
                and expected_interval > 0
            ):
                actual_gap = device_epoch - previous_epoch
                if actual_gap >= expected_interval * 1.5:
                    estimated_missing = round(actual_gap / expected_interval) - 1
                    if estimated_missing > 0:
                        self.suspected_missed_samples += estimated_missing
                        log.warning(
                            "GT: suspected missed sample(s) — gap=%.1fs expected=%.1fs estimated_missing=%d prev_ts=%s current_ts=%s",
                            actual_gap,
                            expected_interval,
                            estimated_missing,
                            self.last_gt_sample_timestamp,
                            device_ts,
                        )

            self.received_samples += 1
            self.last_sample_received_at_local = now
            self.last_gt_sample_timestamp = device_ts
            if device_epoch is not None:
                self._last_gt_sample_epoch = device_epoch
            self.consecutive_watchdog_misses = 0
            sample_number = self.received_samples
            target_samples = self.target_samples
            should_complete = (
                self.run_active
                and target_samples > 0
                and sample_number >= target_samples
            )
            log.info(
                "GT_DEBUG: received_samples incremented — sample_number=%s target=%s should_complete=%s gt_ts=%s",
                sample_number,
                target_samples,
                should_complete,
                device_ts,
            )

        log.info(
            "GT: parsed sample #%d — c03=%s c50=%s gt_ts=%s",
            sample_number,
            parsed.get("c03"),
            parsed.get("c50"),
            device_ts,
        )

        return sample_number, should_complete

    def _deliver_sample(self, parsed: Dict[str, Any]) -> None:
        with self.state_lock:
            self.sample_delivery_active = True
        sample_number, should_complete = self._record_sample_tracking(parsed)
        try:
            with self._latest_lock:
                self._latest = parsed
            if self._on_sample is not None:
                try:
                    self._on_sample({
                        "c03": parsed.get("c03"),
                        "c50": parsed.get("c50"),
                    })
                except Exception:
                    log.exception("GT: on_sample callback raised")
        finally:
            with self.state_lock:
                self.sample_delivery_active = False

        if should_complete:
            log.info("GT: target samples reached (%d)", sample_number)
            self._finalize_run("completed")
            self._stop_reader()

    def _watchdog_loop(self) -> None:
        log.info("GT: watchdog thread started")
        try:
            while not self.watchdog_stop.is_set():
                with self.state_lock:
                    run_active = self.run_active
                    last_sample_at = self.last_sample_received_at_local
                    run_started_at = self.run_started_at_local
                    timeout_s = self.watchdog_timeout_s or 60.0
                    received = self.received_samples
                    target = self.target_samples
                    sample_delivery_active = self.sample_delivery_active

                if not run_active:
                    return

                if sample_delivery_active:
                    self.watchdog_stop.wait(0.1)
                    continue

                reference_time = last_sample_at or run_started_at or time.time()
                elapsed = time.time() - reference_time
                if elapsed <= timeout_s:
                    self.watchdog_stop.wait(min(5.0, max(1.0, timeout_s - elapsed)))
                    continue

                log.warning(
                    "GT: watchdog timeout event — elapsed=%.1fs timeout=%.1fs received=%s target=%s",
                    elapsed,
                    timeout_s,
                    received,
                    target,
                )

                try:
                    op = self._check_op_status()
                    serial_failed = False
                except Exception:
                    log.exception("GT: watchdog OP check failed")
                    op = "?"
                    serial_failed = True

                with self.state_lock:
                    self.last_op_status = op
                log.info("GT: watchdog OP result — %s", op)

                if op == "S":
                    with self.state_lock:
                        if self.sample_delivery_active:
                            reason = None
                        else:
                            reason = "completed" if self.received_samples >= self.target_samples else "stopped_early"
                            log.info(
                                "GT_DEBUG: watchdog OP:S finalize decision — reason=%s received=%s target=%s",
                                reason,
                                self.received_samples,
                                self.target_samples,
                            )
                    if reason is None:
                        self.watchdog_stop.wait(0.1)
                        continue
                    self._finalize_run(reason)
                    self._stop_reader()
                    return

                if op in ("R", "H"):
                    with self.state_lock:
                        self.consecutive_watchdog_misses += 1
                        misses = self.consecutive_watchdog_misses
                    log.warning("GT: watchdog miss while OP=%s — consecutive=%d", op, misses)
                    if misses >= 3:
                        self._finalize_run("serial_fault")
                        self._stop_reader()
                        return
                    self.watchdog_stop.wait(min(10.0, max(1.0, timeout_s / 3.0)))
                    continue

                with self.state_lock:
                    self.consecutive_watchdog_misses += 1
                    misses = self.consecutive_watchdog_misses
                log.warning("GT: watchdog could not confirm OP status — consecutive=%d", misses)
                if misses >= 3:
                    self._finalize_run("serial_fault" if serial_failed else "timeout")
                    self._stop_reader()
                    return
                self.watchdog_stop.wait(min(10.0, max(1.0, timeout_s / 3.0)))
        finally:
            log.info("GT: watchdog thread stopped")

    # ------------------------------------------------------------------
    # Serial port
    # ------------------------------------------------------------------

    def _open(self) -> None:
        if self.ser and self.ser.is_open:
            return
        log.info("GT: opening %s @ %d baud", self._port, self._baud)
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

    def wake(self) -> None:
        """
        Send CR repeatedly until device responds with '*'.

        Silence on the first 1-2 attempts is normal and is not treated as failure.
        Falls back to an OP probe if CR attempts do not produce a '*' prompt.
        Raises RuntimeError if neither CR attempts nor the OP fallback succeed.
        """
        if not self.ser:
            raise RuntimeError("Serial port not open — call _open() first")
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        for attempt in range(10):
            self.ser.write(b"\r")
            self.ser.flush()
            time.sleep(0.3)
            data = self.ser.read_all()
            log.info("GT: wake attempt %d — %r", attempt + 1, data)
            if b"*" in data:
                log.info("GT: wake success on attempt %d", attempt + 1)
                return

        # CR attempts did not produce '*' — try OP as a fallback probe.
        # A valid OP <state> response with trailing '*' counts as wake success.
        log.info("GT: no '*' from CR attempts; trying OP fallback probe")
        self.ser.write(b"OP\r")
        self.ser.flush()
        time.sleep(0.5)
        data = self.ser.read_all()
        log.info("GT: OP fallback response — %r", data)
        text = data.decode(errors="ignore")
        if "OP " in text and "*" in text:
            log.info("GT: wake success via OP fallback probe")
            return

        raise RuntimeError(
            "GT not responding: no '*' after 10 CR attempts and OP fallback"
        )

    # ------------------------------------------------------------------
    # Low-level command helpers
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
        log.debug("GT: send %r", line)
        with self.lock:
            if not self.ser:
                log.error("GT: serial not open for command %r", line)
                return False, b"(serial not open)"

            all_seen = b""
            for attempt in range(3):
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
                log.debug("GT: response to %r (attempt %d): %r", line, attempt + 1, resp)
                if b"*" in resp:
                    return True, all_seen
                all_seen += self._poke_until_star()

            ok = b"*" in all_seen
            if not ok:
                log.warning("GT: no '*' received for %r after 3 attempts", line)
            return ok, all_seen

    # ---- internal command shortcuts ----
    def _cmd_start(self):  return self.send_line(b"S", read_seconds=0.9)
    def _cmd_stop(self):   return self.send_line(b"E", read_seconds=0.9)
    def _cmd_op(self):     return self.send_line(b"OP", read_seconds=0.9)

    # ---- settings commands ----
    def _set_location_id(self, loc_id: int): return self.send_line(f"ID {loc_id:03d}".encode(), read_seconds=0.9)
    def _set_sample_time(self, sec: int):    return self.send_line(f"ST {sec:04d}".encode(), read_seconds=0.9)
    def _set_hold_time(self, sec: int):      return self.send_line(f"SH {sec:04d}".encode(), read_seconds=0.9)
    def _set_samples(self, n: int):          return self.send_line(f"SN {n:03d}".encode(), read_seconds=0.9)
    def _set_count_units_ft3(self):          return self.send_line(b"CU 0", read_seconds=0.9)
    def _set_report_csv(self):               return self.send_line(b"SR 1", read_seconds=0.9)

    def _configure(self, settings: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send all configuration commands and return (applied, mismatch)."""
        self._set_location_id(1)
        self._set_sample_time(settings.get("sample_time_s", 10))
        self._set_hold_time(settings.get("hold_time_s", 50))
        self._set_samples(settings.get("samples", 480))
        self._set_count_units_ft3()  # CU 0 — count/ft³ (Bard Box standard)
        self._set_report_csv()       # SR 1 — CSV output

        ok, raw = self.send_line(b"1", read_seconds=2.0)
        applied = self._parse_settings_report(raw.decode(errors="replace"))

        wanted = {
            "sample_time_s": settings.get("sample_time_s"),
            "hold_time_s": settings.get("hold_time_s"),
            "samples": settings.get("samples"),
        }
        mismatch = {
            k: {"wanted": wanted[k], "got": applied.get(k)}
            for k in wanted
            if applied.get(k) != wanted[k]
        }
        if mismatch:
            log.warning("GT: settings mismatch after configure: %s", mismatch)
        return applied, mismatch

    @staticmethod
    def _parse_settings_report(text: str) -> Dict[str, Any]:
        """Parse the settings report ('1' command) into a dict."""
        def pick_int(label: str):
            m = re.search(rf"^\s*{re.escape(label)}\s*,\s*(\d+)", text, flags=re.MULTILINE)
            return int(m.group(1)) if m else None
        return {
            "sample_time_s": pick_int("Sample Time"),
            "hold_time_s": pick_int("Hold Time"),
            "samples": pick_int("Samples"),
        }

    def _check_op_status(self) -> str:
        """
        Query OP and return parsed status:
          'R' — Running
          'S' — Stopped
          'H' — Hold
          '?' — unrecognized response
        """
        _, raw = self._cmd_op()
        text = raw.decode(errors="replace")
        log.info("GT_DEBUG: OP raw response — %r", text)
        if "OP R" in text or "RUNNING" in text.upper():
            return "R"
        if "OP S" in text or "OP STOP" in text or "STOPPED" in text.upper():
            return "S"
        if "OP H" in text or "HOLD" in text.upper():
            return "H"
        log.warning("GT: unrecognized OP response: %r", text)
        return "?"

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_line(line: str) -> Optional[Dict[str, Any]]:
        """
        Parse one CSV line from the GT-521S into a normalized channel dict.

        Returns dict with keys c03, c50, _device_ts, _raw_line, or None.
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
            out["c50"] = cnt1
        if abs(size2 - 0.3) < 0.11:
            out["c03"] = cnt2
        if abs(size2 - 5.0) < 0.11:
            out["c50"] = cnt2
        if "c03" not in out and "c50" not in out:
            return None
        return out

    # ------------------------------------------------------------------
    # Reader thread
    # ------------------------------------------------------------------

    def _reader_loop(self) -> None:
        buf = b""
        self.reader_running = True
        log.info("GT: reader thread started")
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
                        log.info("GT_DEBUG: raw serial line — %r", s)
                        parsed = self._parse_line(s)
                        if parsed:
                            log.info(
                                "GT_DEBUG: parsed GT sample line — gt_ts=%s c03=%s c50=%s raw=%r",
                                parsed.get("_device_ts"),
                                parsed.get("c03"),
                                parsed.get("c50"),
                                parsed.get("_raw_line"),
                            )
                            self._deliver_sample(parsed)
                else:
                    time.sleep(0.05)
        finally:
            self.reader_running = False
            log.info("GT: reader thread stopped")

    def _ensure_reader(self) -> None:
        if self.reader_thread and self.reader_thread.is_alive() and not self.reader_stop.is_set():
            return
        if self.reader_thread and self.reader_thread.is_alive() and self.reader_stop.is_set():
            self.reader_thread.join(timeout=1.0)
            if self.reader_thread.is_alive():
                log.warning("GT: previous reader thread did not stop; not starting a new one")
                return
        self.reader_stop.clear()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

    def _ensure_watchdog(self) -> None:
        if self.watchdog_thread and self.watchdog_thread.is_alive() and not self.watchdog_stop.is_set():
            return
        if self.watchdog_thread and self.watchdog_thread.is_alive() and self.watchdog_stop.is_set():
            self.watchdog_thread.join(timeout=1.0)
            if self.watchdog_thread.is_alive():
                log.warning("GT: previous watchdog thread did not stop; not starting a new one")
                return
        self.watchdog_stop.clear()
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _stop_reader(self) -> None:
        self.reader_stop.set()

    def _stop_reader_wait(self) -> None:
        self.reader_stop.set()
        if (
            self.reader_thread
            and self.reader_thread.is_alive()
            and threading.current_thread() is not self.reader_thread
        ):
            self.reader_thread.join(timeout=1.0)
            if self.reader_thread.is_alive():
                log.warning("GT: reader thread did not stop within 1 s")

    def _stop_watchdog_wait(self) -> None:
        self.watchdog_stop.set()
        if (
            self.watchdog_thread
            and self.watchdog_thread.is_alive()
            and threading.current_thread() is not self.watchdog_thread
        ):
            self.watchdog_thread.join(timeout=1.0)
            if self.watchdog_thread.is_alive():
                log.warning("GT: watchdog thread did not stop within 1 s")

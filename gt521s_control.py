# gt521s_control.py
import time
import threading
from typing import Optional, Tuple
import serial

PORT = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0"
BAUD = 9600

class GT521SController:
    """
    Owns the serial port and provides start/stop with the robust
    "CR until '*', then send S/E + CR, expect '*'" handshake.
    """
    def __init__(self, port: str = PORT, baud: int = BAUD):
        self.port = port
        self.baud = baud
        self._ser: Optional[serial.Serial] = None
        self._lock = threading.Lock()

    def _open_if_needed(self) -> None:
        if self._ser and self._ser.is_open:
            return
        self._ser = serial.Serial(
            self.port,
            self.baud,
            timeout=0,          # nonblocking; we poll in_waiting
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        # Keep lines steady (screen-like)
        try:
            self._ser.dtr = True
            self._ser.rts = True
        except Exception:
            pass

        time.sleep(1.0)
        self._ser.reset_input_buffer()

    def close(self) -> None:
        with self._lock:
            if self._ser and self._ser.is_open:
                self._ser.close()
            self._ser = None

    def _read_for(self, seconds: float = 0.8) -> bytes:
        end = time.time() + seconds
        out = b""
        ser = self._ser
        assert ser is not None
        while time.time() < end:
            n = ser.in_waiting
            if n:
                out += ser.read(n)
            else:
                time.sleep(0.02)
        return out

    def _poke_cr_until_star(self, attempts: int = 12, window: float = 0.6) -> bytes:
        ser = self._ser
        assert ser is not None
        seen = b""
        for _ in range(attempts):
            ser.write(b"\r")
            ser.flush()
            time.sleep(0.08)
            chunk = self._read_for(window)
            if chunk:
                seen += chunk
                if b"*" in chunk:
                    return seen
        return seen

    def _send_cmd_with_wake(self, cmd_letter: bytes, tries: int = 3) -> bytes:
        ser = self._ser
        assert ser is not None
        all_seen = b""

        for _ in range(tries):
            all_seen += self._poke_cr_until_star()

            ser.reset_input_buffer()
            ser.write(cmd_letter + b"\r")
            ser.flush()
            time.sleep(0.10)

            resp = self._read_for(0.9)
            all_seen += resp

            if b"*" in resp:
                return all_seen

            all_seen += self._poke_cr_until_star()

        return all_seen

    def start(self) -> Tuple[bool, str]:
        with self._lock:
            self._open_if_needed()
            resp = self._send_cmd_with_wake(b"S")
            ok = (b"*" in resp)
            return ok, resp.decode(errors="replace")

    def stop(self) -> Tuple[bool, str]:
        with self._lock:
            self._open_if_needed()
            resp = self._send_cmd_with_wake(b"E")
            ok = (b"*" in resp)
            return ok, resp.decode(errors="replace")

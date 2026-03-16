#!/usr/bin/env python3
import time
import serial
from datetime import datetime

PORT = "/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0"
BAUD = 9600

def ts():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def read_for(ser, seconds=0.8):
    end = time.time() + seconds
    out = b""
    while time.time() < end:
        n = ser.in_waiting
        if n:
            out += ser.read(n)
        else:
            time.sleep(0.02)
    return out

def poke_cr_until_star(ser, attempts=12, window=0.6):
    seen = b""
    for _ in range(attempts):
        ser.write(b"\r")
        ser.flush()
        time.sleep(0.08)
        chunk = read_for(ser, window)
        if chunk:
            seen += chunk
            if b"*" in chunk:
                return seen
    return seen

def send_cmd_with_wake(ser, cmd_letter: bytes, tries=3):
    all_seen = b""
    for _ in range(tries):
        # Ensure ready
        all_seen += poke_cr_until_star(ser)

        ser.reset_input_buffer()
        ser.write(cmd_letter + b"\r")
        ser.flush()
        time.sleep(0.10)

        resp = read_for(ser, 0.9)
        all_seen += resp

        if b"*" in resp:
            return all_seen

        # If no *, wake again
        all_seen += poke_cr_until_star(ser)

    return all_seen

def main():
    with serial.Serial(PORT, BAUD, timeout=0, xonxoff=False, rtscts=False, dsrdtr=False) as ser:
        try:
            ser.dtr = True
            ser.rts = True
        except Exception:
            pass

        time.sleep(1.0)
        ser.reset_input_buffer()

        # Stop first (deterministic state)
        e_bytes = send_cmd_with_wake(ser, b"E")
        print(f"[{ts()}] E -> {e_bytes!r}")

        # Start sampling
        s_bytes = send_cmd_with_wake(ser, b"S")
        print(f"[{ts()}] S -> {s_bytes!r}")

        print(f"[{ts()}] Streaming (Ctrl-C to stop)...")

        try:
            buffer = b""
            while True:
                chunk = read_for(ser, 0.5)
                if not chunk:
                    continue

                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    line = line.rstrip(b"\r")
                    if line:
                        print(f"[{ts()}] {line.decode(errors='replace')}")
        except KeyboardInterrupt:
            pass

        # Stop on exit
        e2_bytes = send_cmd_with_wake(ser, b"E")
        print(f"[{ts()}] exit E -> {e2_bytes!r}")

if __name__ == "__main__":
    main()

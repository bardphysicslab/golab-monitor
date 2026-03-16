# gt521s_burst.py
# Start GT-521S, read a few CSV lines, stop GT-521S.
# Run:  source ~/labdash/venv/bin/activate && python gt521s_burst.py

import time
import serial

PORT = "/dev/ttyUSB0"
BAUD = 9600

LINES_TO_READ = 5
READ_TIMEOUT_S = 15  # must be > Sample Time (yours is 10s)

def write_cmd(ser: serial.Serial, cmd: str, pause: float = 0.2):
    ser.write((cmd + "\r").encode("ascii"))
    ser.flush()
    time.sleep(pause)

def main():
    with serial.Serial(PORT, BAUD, timeout=READ_TIMEOUT_S) as ser:
        # Let USB/serial settle (prevents that first-command '?' you saw)
        time.sleep(0.8)

        # "Wake" the instrument, then start (twice is robust)
        write_cmd(ser, "")      # sends just CR
        write_cmd(ser, "S")
        write_cmd(ser, "S")

        # Read a few lines; if no data arrives, don't hang forever
        got = 0
        for _ in range(LINES_TO_READ):
            line = ser.readline().decode(errors="replace").strip()
            if not line:
                break
            print(line)
            got += 1

        # Stop (once is usually enough; twice is safe)
        write_cmd(ser, "E")
        write_cmd(ser, "E")

        if got == 0:
            print("(no CSV lines received)")

if __name__ == "__main__":
    main()

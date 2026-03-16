import time
import serial
from datetime import datetime

PORT = "/dev/ttyUSB0"
BAUD = 9600

SAMPLE_TIME_S = 10     # device sample time
SAMPLES_PER_BURST = 6  # 6*10s = 1 minute burst
BURST_PERIOD_S = 3600  # 1 hour

def send(ser, cmd: str):
    ser.write((cmd.strip() + "\r").encode("ascii"))
    ser.flush()
    time.sleep(0.15)

def read_csv_lines(ser, max_lines=50, timeout_s=120):
    """Read up to max_lines non-empty lines, stop if timeout."""
    lines = []
    t0 = time.time()
    while len(lines) < max_lines and (time.time() - t0) < timeout_s:
        line = ser.readline().decode(errors="replace").strip()
        if line:
            lines.append(line)
            # CSV records contain commas and end with *checksum
            if "*" in line and line.count(",") >= 8:
                # keep collecting; caller decides what to do
                pass
    return lines

def parse_gt_line(line: str):
    parts = [p.strip() for p in line.split(",")]
    # basic format: ts, size1, cnt1, size2, cnt2, temp, rh, loc, seconds, status, checksum
    ts = parts[0]
    size1 = float(parts[1]); cnt1 = int(parts[2])
    size2 = float(parts[3]); cnt2 = int(parts[4])
    seconds = int(parts[8]); status = int(parts[9])
    return {
        "ts": ts,
        "ch1_um": size1,
        "ch1_count": cnt1,
        "ch2_um": size2,
        "ch2_count": cnt2,
        "seconds": seconds,
        "status": status,
        "raw": line,
    }

with serial.Serial(PORT, BAUD, timeout=2) as ser:
    # Configure once
    send(ser, "SR 1")                 # CSV report
    send(ser, f"ST {SAMPLE_TIME_S}")  # sample time
    send(ser, f"SN {SAMPLES_PER_BURST}")  # number of samples in burst

    while True:
        print(f"\n[{datetime.now().isoformat(timespec='seconds')}] Starting burst…")
        send(ser, "S")  # start

        # Expect about SAMPLES_PER_BURST CSV lines to arrive over ~SAMPLES_PER_BURST*SAMPLE_TIME_S seconds
        lines = read_csv_lines(ser, max_lines=200, timeout_s=(SAMPLES_PER_BURST*SAMPLE_TIME_S + 30))

        csv_lines = [ln for ln in lines if "*" in ln and ln.count(",") >= 8]
        print(f"Got {len(csv_lines)} CSV records")

        if csv_lines:
            last = parse_gt_line(csv_lines[-1])
            print("Last reading:", last)

        # Ensure it’s stopped (belt & suspenders)
        send(ser, "E")

        print(f"Sleeping {BURST_PERIOD_S}s…")
        time.sleep(BURST_PERIOD_S)

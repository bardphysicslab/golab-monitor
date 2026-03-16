import time
import serial

PORT = "/dev/ttyUSB0"
BAUD = 9600

def send(ser, cmd: str, crlf=False):
    ending = "\r\n" if crlf else "\r"
    ser.write((cmd.strip() + ending).encode("ascii"))
    ser.flush()
    time.sleep(0.2)

def read_some(ser, seconds=2.0):
    t0 = time.time()
    out = []
    while time.time() - t0 < seconds:
        line = ser.readline().decode(errors="replace").strip()
        if line:
            out.append(line)
    return out

for crlf in (False, True):
    print("\n==== Trying line ending:", "CR" if not crlf else "CRLF", "====")
    with serial.Serial(PORT, BAUD, timeout=0.5) as ser:
        ser.reset_input_buffer()
        for cmd in ["OP", "1", "E"]:
            print(f"\n>>> {cmd}")
            send(ser, cmd, crlf=crlf)
            resp = read_some(ser, 2.0)
            print("\n".join(resp) if resp else "(no response)")

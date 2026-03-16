#!/usr/bin/env python3
import time, termios, serial, select

PORT = "/dev/ttyUSB0"
BAUD = 9600

def make_terminal_like(fd):
    attrs = termios.tcgetattr(fd)
    # c_cflag is attrs[2]
    attrs[2] |= (termios.CLOCAL | termios.CREAD)
    if hasattr(termios, "HUPCL"):
        attrs[2] &= ~termios.HUPCL
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

def drain(ser, seconds=0.5):
    end = time.time() + seconds
    out = b""
    while time.time() < end:
        r, _, _ = select.select([ser.fileno()], [], [], 0.05)
        if r:
            chunk = ser.read(4096)
            if chunk:
                out += chunk
    return out

with serial.Serial(
    PORT, BAUD,
    timeout=0,          # nonblocking; we use select+drain
    xonxoff=False,
    rtscts=False,
    dsrdtr=False
) as ser:
    make_terminal_like(ser.fileno())

    # mimic terminal behavior
    ser.dtr = True
    ser.rts = True
    time.sleep(0.8)

    ser.reset_input_buffer()
    ser.reset_output_buffer()

    print("Initial drain:", drain(ser, 0.5))

    def send(cmd):
        ser.write(cmd)
        ser.flush()
        time.sleep(0.05)
        return drain(ser, 0.8)

    # IMPORTANT: commands must end with CR per manual
    print("wake ->", send(b"\r"))
    print("S1   ->", send(b"S\r"))
    print("S2   ->", send(b"S\r"))
    print("E    ->", send(b"E\r"))

import time
import serial

PORT = "/dev/ttyUSB0"
BAUD = 9600

with serial.Serial(
    PORT, BAUD,
    timeout=1,
    xonxoff=False,
    rtscts=False,
    dsrdtr=True,   # <-- change: enable DSR/DTR handling
) as ser:
    ser.dtr = True
    ser.rts = True
    time.sleep(0.8)

    ser.reset_input_buffer()
    ser.write(b"1\r")
    ser.flush()

    # wait + read whatever arrives
    time.sleep(2.0)
    data = ser.read(4096)
    print(data.decode(errors="replace") or "(no response)")

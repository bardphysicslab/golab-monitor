# Fix GT-521S Start + Data Flow After Refactor

## Problem

- `/gt/start` hangs at "Applying settings"
- Device does not start sampling
- `/gt/latest` stays null
- Graphs do not update

## Root Cause (likely)

Driver is sending commands before GT-521S is awake and ready.

---

## Fix 1: Add Wake/Sync Step

In `gt521s_driver.py`, add this function and call it BEFORE any commands:

```python
def wake_gt(ser):
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    for _ in range(10):
        ser.write(b"\r")
        ser.flush()
        time.sleep(0.2)

        data = ser.read_all().decode(errors="ignore")
        if data.strip():
            return True

    raise RuntimeError("GT not responding")
```

Call `wake_gt(ser)` at the start of your connect/start sequence.

---

## Fix 2: Correct Start Sequence

Ensure the exact command order:

```python
wake_gt(ser)

send("E")                      # stop first — always
send(f"ID {id:03d}")
send(f"ST {sample_time:04d}")
send(f"SH {hold_time:04d}")
send(f"SN {samples:03d}")

send("S")                      # start
```

Do NOT skip the `E` (stop) command before starting.

---

## Fix 3: Do NOT Block `/gt/start`

`/gt/start` must:
- Send commands to the device
- Return immediately

Reading CSV output must be handled in a background thread or async task.

Do NOT wait for sample data inside the request handler.

---

## Fix 4: Verify Device Actually Started

After sending `S`, optionally verify:

```python
status = send("OP")
# expect "R" in response
```

---

## Fix 5: Ensure Reader Loop Exists

There must be a continuous background serial reader that:
- Parses incoming CSV lines
- Updates the latest reading
- Updates session data

If this loop is missing or not running, graphs will never update.

---

## Fix 6: Frontend Field Check

Confirm the frontend uses the normalized Bard Box channel names:

```javascript
data.c03
data.c50
```

NOT the old field names:

```javascript
count_0p3
count_5p0
```

---

## Acceptance Criteria

- Start button no longer hangs
- `/state` shows `run_active: true`
- `/gt/latest` updates every sample
- Graphs update live

---

## Constraint

Keep changes minimal. Do not rewrite the architecture.

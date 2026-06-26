# Task: Create First Bard Box Serial Driver for Arduino Sensor Node on the Pi

## File to create

`bardbox_env_node_v1_driver.py`

## File Location

Create the file at:

```
/home/golab/golab-monitor/raspi/drivers/bardbox_env_node_v1_driver.py
```

---

## Working Directory

Work in the Pi repo at:

```
/home/golab/golab-monitor/raspi/
```

---

## Reference Doc

Follow `pi-driver-instructions.md` exactly.

That doc is located at:

```
/home/golab/bardbox/docs/pi-driver-instructions.md
```

---

## Branch Requirement

Work on branch: `bard-box-refactor`

Before making changes:
- Verify the current branch is `bard-box-refactor`
- If not, stop and report it

---

## Path Verification

Before writing any code:
- Verify `/home/golab/golab-monitor/raspi/` exists
- Verify `/home/golab/golab-monitor/raspi/drivers/` exists — create it if needed
- Verify `/home/golab/bardbox/docs/pi-driver-instructions.md` exists
- If any path is missing or different, stop and report it instead of guessing

Once changes are complete:
- Commit to `bard-box-refactor`
- Push the branch

Do not modify files outside the golab-monitor repo unless strictly required to read the Bard Box docs.

---

## Context

The Arduino device is already flashed and connected to the Pi over serial.

The device outputs Bard Box serial protocol lines like:

```
OK INFO uid=bb-0001 fw=1.0 sensors=PMS,BME280
HDR,v1,sample_idx,temp_c,rh_pct,press_pa,pm1_std,pm25_std,pm10_std,pm1_env,pm25_env,pm10_env,c03,c05,c10,c25,c50,c100
DAT,1,26.43,9.96,102873,1,1,1,1,1,1,294,92,7,0,0,0
```

The device accepts newline-terminated text commands:

```
PING\n
INFO\n
START\n
STOP\n
HEADER\n
STATUS\n
```

For this device, `HDR,v1,...` is emitted immediately after `START\n`, so the driver must send `START\n` before waiting for the header.

---

## Goal

Create a production-ready Pi driver that:
- Opens the serial port
- Sends `INFO\n` and parses the response
- Sends `START\n`
- Reads and validates `HDR,v1,...`
- Parses `DAT,...` lines
- Stores the latest valid reading
- Exposes normalized Bard Box output through `get_info()`, `get_capabilities()`, `get_reading()`

Also add a small standalone test mode so the driver can be run directly on the Pi for validation.

---

## Architecture Rules

- All hardware and protocol logic stays inside the driver
- The main app must not parse serial protocol directly
- Driver must return normalized data only
- `transport` must be `"serial"`
- `protocol` must be `"bardbox"`
- `source_type` must NOT include transport
- Driver must validate protocol version
- Driver must reject malformed `DAT` lines cleanly
- Driver must return bounded raw payload only

---

## Required Channels — v1

### Normalized `data` (5 channels only for v1)

```
temp_c
pm1_std
pm25_std
pm10_std
c03
```

### Extended fields (all other useful parsed values)

```
rh_pct
press_pa
pm1_env
pm25_env
pm10_env
c05
c10
c25
c50
c100
sample_idx
```

Even though the device outputs more fields, only the 5 above go in `data` for v1.
All others go in `extended`.

---

## Driver Behavior

Connection sequence must be:
1. Open serial port
2. Send `INFO\n` and parse response
3. Send `START\n`
4. Read and validate `HDR,v1,...`
5. Accept `DAT,...` lines

- Use `pyserial`
- Default baud: `115200`
- Commands must be newline-terminated with `\n`
- Serial reading must not block indefinitely
- Driver must support polling via `get_reading()` without hanging
- Ignore blank lines
- Accept lines beginning with `OK INFO`, `HDR`, or `DAT`
- Ignore all other lines
- Ignore `OK START`, `OK STOP`, `PONG` unless useful
- Validate protocol version is `v1`
- Validate header field names before accepting data
- Validate DAT field count matches header field count
- Parse values to correct numeric types
- Generate ISO 8601 UTC timestamp on the Pi side
- Maintain latest valid reading in memory
- Driver must send `START\n` once after successful connect and `INFO` parse
- Driver must not repeatedly send `START`
- If no valid data has been received yet, return a structured `"error"` reading
- If last data is too old, return `"stale"` as appropriate
- `raw` must contain only the latest raw `DAT` line or `null`

---

## Serial Port Handling

- Use a configurable serial port path passed into the driver constructor
- Default to a sensible Pi serial path if provided in config
- Support either `/dev/ttyUSB0` or `/dev/serial/by-id/...`
- Do not hardcode one specific port in the driver logic
- Port selection must come from config or constructor args

---

## Required Output Models

### get_info()

```json
{
    "uid": "bb-0001",
    "source_type": "bardbox_env_node_v1",
    "transport": "serial",
    "protocol": "bardbox",
    "firmware": "1.0",
    "info_raw": {
        "sensors": "PMS,BME280"
    }
}
```

### get_capabilities()

```json
{
    "channels": {
        "temp_c":   {"label": "Temperature",      "unit": "°C"},
        "pm1_std":  {"label": "PM1.0 Std",        "unit": "µg/m³"},
        "pm25_std": {"label": "PM2.5 Std",        "unit": "µg/m³"},
        "pm10_std": {"label": "PM10 Std",         "unit": "µg/m³"},
        "c03":      {"label": "Particles >0.3µm", "unit": "count/0.1L"}
    },
    "raw_available": true
}
```

### get_reading()

```json
{
    "uid": "bb-0001",
    "timestamp": "2026-04-08T15:20:00Z",
    "status": "ok",
    "data": {
        "temp_c": 26.43,
        "pm1_std": 1,
        "pm25_std": 1,
        "pm10_std": 1,
        "c03": 294
    },
    "extended": {
        "rh_pct": 9.96,
        "press_pa": 102873,
        "pm1_env": 1,
        "pm25_env": 1,
        "pm10_env": 1,
        "c05": 92,
        "c10": 7,
        "c25": 0,
        "c50": 0,
        "c100": 0,
        "sample_idx": 1
    },
    "raw": "DAT,1,26.43,9.96,102873,1,1,1,1,1,1,294,92,7,0,0,0"
}
```

---

## Acceptance Criteria

- Driver opens serial port and sends `INFO\n` on connect
- Driver sends `START\n` once, then waits for `HDR,v1,...`
- Driver validates `HDR,v1,...` before accepting any `DAT` lines
- Driver rejects `DAT` lines where field count does not match header
- `get_info()` returns correct shape including `firmware` and `info_raw`
- `get_capabilities()` returns only the 5 v1 channels as a dict
- `get_reading()` returns all 5 normalized channels in `data`
- `get_reading()` returns all extended fields in `extended`
- `raw` contains only the last raw `DAT` line or `null`
- `status` is `"error"` if no valid reading has been received yet
- All timestamps are ISO 8601 UTC
- No vendor field names appear in `data`
- File is complete and production-ready — no pseudocode

---

## Standalone Test Support

Include a small `if __name__ == "__main__":` block that can be run directly on the Pi to test the driver.

It should:
- Accept a serial port argument
- Instantiate the driver
- Print `get_info()`
- Print `get_capabilities()`
- Poll and print `get_reading()` in a loop

This is for direct Pi testing before integrating with the main app.

---

## Final Requirement

Return one complete, usable Python file.

Do not return pseudocode.
Do not return partial snippets.

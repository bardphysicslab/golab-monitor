# Task: Create First Bard Box Serial Driver for Arduino Sensor Node

## File to create

`bardbox_node_driver.py`

Follow `pi-driver-instructions.md` exactly.
The file is located at:
`/Users/kornelispoort/bardphysicslab/github/bardbox/docs/pi-driver-instructions.md`

Work in the local repo at:
`/Users/kornelispoort/bardphysicslab/github/golab-monitor/raspi/`

Commit and push when done. Do not touch the Pi directly.

---

## Context

The Arduino device outputs Bard Box serial protocol lines:

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

---

## Goal

Create a production-ready driver that:
- Opens the serial port
- Sends `INFO\n` and parses the response
- Sends `START\n` if needed
- Reads and validates `HDR,v1,...`
- Parses `DAT,...` lines
- Stores the latest valid reading
- Exposes normalized Bard Box output through `get_info()`, `get_capabilities()`, `get_reading()`

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

- Use `pyserial`
- Default baud: `115200`
- Commands must be newline-terminated with `\n`
- Ignore blank lines
- Ignore `OK START`, `OK STOP`, `PONG` unless useful
- Validate protocol version is `v1`
- Validate header field names before accepting data
- Validate DAT field count matches header field count
- Parse values to correct numeric types
- Generate ISO 8601 UTC timestamp on the Pi/Mac side
- Maintain latest valid reading in memory
- If no fresh data yet, return a structured `"error"` or `"stale"` reading as appropriate
- `raw` must contain only the latest raw `DAT` line or `null`

---

## Required Output Models

### get_info()

```python
{
    "uid": "bb-0001",
    "source_type": "bardbox_node",
    "transport": "serial",
    "protocol": "bardbox",
    "firmware": "1.0",
    "info_raw": {
        "sensors": "PMS,BME280"
    }
}
```

### get_capabilities()

```python
{
    "channels": {
        "temp_c":   {"label": "Temperature",   "unit": "°C"},
        "pm1_std":  {"label": "PM1.0 Std",     "unit": "µg/m³"},
        "pm25_std": {"label": "PM2.5 Std",     "unit": "µg/m³"},
        "pm10_std": {"label": "PM10 Std",      "unit": "µg/m³"},
        "c03":      {"label": "0.3 µm",        "unit": "count/ft³"}
    },
    "raw_available": true
}
```

### get_reading()

```python
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

## Final Requirement

Return one complete, usable Python file.
Do not return pseudocode.
Do not return partial snippets.

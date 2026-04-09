# Fix GT-521S Driver — Consolidation and Correctness

## Goal

- Fix broken start/stop and data flow
- Move all GT-521S protocol knowledge into `gt521s_driver.py`
- `main.py` must only call normalized driver methods
- Update docs so future drivers follow the same pattern

Work in the local repo at:
`/Users/kornelispoort/bardphysicslab/github/golab-monitor/`

Do not touch the Pi directly. Commit and push when done.

---

## Part 1 — Fix Current Code Errors

### 1. `/gt/start` must not silently fail

If driver start or config fails:
- Return HTTP 500 or 400 with `{"ok": false, "error": "..."}`
- Do not return HTTP 200 on failure
- Log the exception to server logs

Success response: `{"ok": true, ...}`

---

### 2. Add logging around all GT operations

Use a proper logger (not bare prints). Log at minimum:

- Opening serial port
- Each wake attempt
- Each command sent and response received (e.g. `GT: send ST 0010`, `GT: response "..."`)
- Start/stop reader
- Op status result
- Timeouts and failures

---

### 3. Fix count units mismatch

`set_count_units_m3()` is being called but the UI and `channel-names.md` use `count/ft³`.

Fix:
- Set GT to CF (particles/ft³) to match UI and docs
- Remove or replace the M3 call
- Normalized API field names (`c03`, `c50`) remain unchanged — only the vendor runtime unit changes

---

### 4. Confirm frontend field mapping

Frontend must use:
```javascript
latest.data.c03
latest.data.c50
```

Remove any remaining references to:
```javascript
count_0p3
count_5p0
latest.c03  // without .data
```

---

### 5. Improve wake logic

Centralize wake in driver. Requirements:
- Send CR repeatedly with slightly longer waits
- Log what was received
- Raise explicit exception if no response after all attempts

```python
def wake(self) -> None:
    # raises RuntimeError if GT does not respond
```

---

### 6. Verify OP status handling

After sending `S`, call `OP` and parse the response:
- `R` = Running
- `S` = Stopped
- `H` = Hold

Log the returned value. If status is clearly wrong after start, fail with a clear error.

---

### 7. Clean reader lifecycle

Driver must own the serial reader lifecycle:
- No manual reader start/stop in route handlers
- No duplicate reader threads
- No stale reader after restart

---

## Part 2 — Move All GT Protocol Knowledge into `gt521s_driver.py`

### 8. Clean driver API

`main.py` must only call these methods on the driver:

```python
get_info()
get_capabilities()
get_reading()
configure(settings)
start()
stop()
get_state()
```

Optional convenience method:
```python
start_session(settings)
# internally: wake → stop → configure → start → verify OP → ensure reader
```

---

### 9. Remove GT protocol sequencing from `main.py`

Move all of the following into the driver — `main.py` must not call these directly:

- `open()`, `wake()`
- `stop_reader()`, `ensure_reader()`
- `stop()`, `start()`
- `set_location_id()`, `set_sample_time()`, `set_hold_time()`, `set_samples()`
- `set_count_units_*()`, `set_report_*()`
- `read_settings_report()`, `op_status()`

After refactor, `main.py` should do roughly:
```python
gt_driver.start_session(settings)
gt_driver.stop()
```

---

### 10. Add controlled escape hatch

Add to driver (not to main.py):

```python
def raw_command(self, cmd: str) -> str:
    ...
```

Optional expert methods (driver only):
```python
vendor_get_settings()
vendor_get_status()
```

`main.py` must never build raw GT commands directly.

---

### 11. One authoritative driver instance

- One `GT521SDriver` instance owns: serial port, latest reading, session state, reader thread
- If `main.py` still contains an inline `GT521` class, remove it after migration

---

## Part 3 — Align with Manual

### 12. Verify configuration assumptions

Per the GT-521S manual:
- Default sizes: 0.3 µm and 0.5 µm (this device is configured for 0.3 µm and 5.0 µm)
- Default baud: 9600
- Default serial mode: RS-232
- Default concentration unit: CF (particles/ft³)
- Real-time serial output occurs at end of each sample

Verify driver configuration matches intended system design. Document any intentional deviations from GT defaults.

---

## Acceptance Criteria

- Start button does not hang
- `/state` shows `run_active: true` after start
- `/gt/latest` updates every sample cycle
- Graphs update live
- `main.py` contains no GT command strings or protocol sequencing
- All GT logic lives in `gt521s_driver.py`
- Logs show clear GT activity in journald

---

## Constraint

Keep changes minimal and incremental. Do not rewrite the architecture.
After all changes are local and tested, commit to the refactor branch and push.

Remove all GT-specific command logic from `main.py`. The driver must be the only
place that constructs and sends GT commands.

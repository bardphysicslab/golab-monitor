# GoLab Monitor — Bard Box Refactor Instructions for Claude Code

## Context

The golab-monitor repo at /Users/kornelispoort/bardphysicslab/github/golab-monitor/ contains a working FastAPI dashboard
for the GT-521S particle counter. It needs to be refactored to conform to the
Bard Box architecture standards defined in the docs/ folder of the bardbox repo.

The Bard Box docs are located locally at: /Users/kornelispoort/bardphysicslab/github/bardbox/docs/
The GoLab Monitor files are located locally at: /Users/kornelispoort/bardphysicslab/github/golab-monitor/
Use these local files as the source of truth.

The relevant docs (in /Users/kornelispoort/bardphysicslab/github/bardbox/docs/) are:
- implementation-guide.md
- pi-driver-instructions.md
- reading-format.md
- capabilities-schema.md
- channel-names.md
- session-model.md
- pi-app-instructions.md
- testing-guide.md

Do NOT invent new standards. Conform to what is already defined in those docs.

---

## Step 1: Audit

Before making any changes, read the following files:
- /Users/kornelispoort/bardphysicslab/github/golab-monitor/raspi/main.py
- /Users/kornelispoort/bardphysicslab/github/golab-monitor/raspi/gt521s_control.py

Then audit them against the Bard Box docs and produce a concrete list of
mismatches. Look specifically for:

1. Field naming violations
   - `count_0p3` and `count_5p0` must become `c03` and `c50` (channel-names.md)
   - `threshold_0p3` and `threshold_5p0` must be renamed accordingly
   - Note: The GT-521S second configured channel in the current GoLab implementation
     is 5.0 µm, which maps to c50, not c05. (c05 = ≥ 0.5 µm; c50 = ≥ 5.0 µm)

2. Missing driver interface
   - `GT521SController` / `GT521` class does not implement `get_info()`,
     `get_capabilities()`, or `get_reading()` (pi-driver-instructions.md)

3. Missing normalized reading format
   - No reading object conforming to reading-format.md is returned anywhere
   - No `uid`, `timestamp`, `status`, `data`, `extended`, `raw` structure

4. Missing capabilities schema
   - No `get_capabilities()` returning the dict format from capabilities-schema.md

5. Session model
   - Session logic is mixed into main.py and GT521 class
   - Should be separated per session-model.md

6. Separation of concerns
   - Hardware protocol logic, session management, and API serving are all in main.py
   - Per implementation-guide.md these should be separated into:
     * driver (gt521s_driver.py)
     * app (main.py — orchestration only)

7. API endpoints
   - `/gt/latest` returns raw `count_0p3`/`count_5p0` fields — must return
     normalized Bard Box reading format
   - `/gt/session-data` returns non-normalized field names

---

## Step 2: Propose Refactor Plan

After auditing, propose a step-by-step refactor plan that:
- Preserves all working behavior (the dashboard must still work after refactor)
- Makes incremental changes — one concern at a time
- Does not break the running systemd service
- Identifies any genuine conflicts between the current implementation and the
  docs (e.g. GT-521S specific behaviors that don't fit cleanly)

---

## Step 3: Implement Incrementally

Implement the refactor in this order:

### Phase 1 — Create gt521s_driver.py
- Create raspi/gt521s_driver.py
- Implement GT521SDriver class with get_info(), get_capabilities(), get_reading()
- Preserve ALL existing serial handshake logic from gt521s_control.py exactly
- get_info() returns:
  - uid: passed via config or constructor (a default of "bb-0001" is allowed temporarily)
  - source_type: "gt521s"
  - transport: "serial"
  - protocol: "vendor"
  - firmware: null
- get_capabilities() returns channels dict with c03 and c50 using label/unit format
- get_reading() returns normalized reading with c03, c50 in data field
- Do not delete gt521s_control.py until main.py is updated

### Phase 2 — Normalize field names
- Replace all occurrences of count_0p3 → c03 throughout main.py
- Replace all occurrences of count_5p0 → c50 throughout main.py
- Replace threshold_0p3 → threshold_c03
- Replace threshold_5p0 → threshold_c50
- Update frontend JS in index.html / dashboard to match

### Phase 3 — Normalize reading format
- Update /gt/latest to return a full Bard Box reading object:
  uid, timestamp, status, data {c03, c50}, extended {}, raw null
- Update /gt/session-data to use normalized field names

### Phase 4 — Separate session logic
- Move session state management to conform to session-model.md
- session_id should be unique per run and never reused
- session object should include uid, status, start_time, end_time, metadata, summary
- Do not redesign session behavior — only restructure code to match session-model.md
  while preserving existing runtime behavior

### Phase 5 — Clean up
- Remove gt521s_control.py if fully replaced by gt521s_driver.py
- Update CLAUDE.md with new file structure
- Commit to dev branch and open PR

---

## Rules for Implementation

- Do NOT rewrite working serial communication logic
- Do NOT change the dashboard UI behavior — only normalize the data it consumes
- Do NOT change API response shape until Phase 3 — Phases 1 and 2 must preserve current outputs
- Do NOT push to main — use dev branch
- Make all code changes in the local Mac repo first
  (/Users/kornelispoort/bardphysicslab/github/golab-monitor/), commit to git,
  push to GitHub, and only then update the Pi from the repo
- Do not treat the Pi copy as the development source of truth
- The Raspberry Pi should only be updated after local repo changes are committed and pushed
- Explain any cases where the GT-521S vendor protocol conflicts with Bard Box standards
- After each phase, confirm the service still starts correctly:
  sudo systemctl restart labdash && sudo systemctl status labdash
- All timestamps must be ISO 8601 UTC
- All channel names must match channel-names.md exactly — no aliases
- Run all changes from within the golab-monitor repo root directory.

---

## Conflict to Watch For

The GT-521S outputs data mid-session as streaming lines. The Bard Box reading
format expects atomic get_reading() calls. The driver must handle this internally
— buffering the latest parsed line and returning it on get_reading() — without
exposing the streaming behavior to the backend. Flag this if it causes any issues.

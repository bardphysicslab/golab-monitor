# PR #10 Code Review Checklist

## Context

This PR refactors the GT-521S integration to conform to the Bard Box architecture.
Two specific boundary issues need verification before merge.

---

## Checklist

### 1. `start_session()` — Driver session-execution boundary

`gt521s_driver.py` implements `start_session(settings, on_sample)` which owns
the full hardware start sequence (open → wake → configure → start → verify OP →
ensure reader).

This is an intentional exception to the general rule that session logic lives
outside the driver. It is acceptable here because the GT-521S requires a complex,
ordered command sequence that is hardware-specific and must be atomic.

Verify:
- [ ] `start_session()` only manages hardware state — not application session state
- [ ] `start_session()` does not store session metadata (start_time, session_id, summary)
- [ ] Application session state (session_id, start_time, metadata) is managed by `SessionManager` in `main.py`
- [ ] `session-model.md` is updated to document this exception pattern

---

### 2. `on_sample` callback — Clean boundary enforcement

The `on_sample` callback fires from the driver into `main.py` for each parsed CSV line.

Verify:
- [ ] The driver calls `on_sample(parsed_data)` with a normalized dict only
- [ ] The driver does not pass raw serial bytes or vendor field names through `on_sample`
- [ ] `on_sample` contains no GT-specific logic — only Bard Box normalized fields (`c03`, `c50`)
- [ ] `main.py` decides what parsed samples mean (thresholds, session tracking, etc.)
- [ ] No application logic has crept back into the driver via the callback

---

### 3. General boundary check

- [ ] `main.py` contains no GT command strings (E, S, ST, SH, SN, SR, CU, OP, ID)
- [ ] `main.py` imports `GT521SDriver` and calls only its public methods
- [ ] Logging uses `logging` module — no bare `print()` statements in GT code
- [ ] `/gt/start` returns HTTP 500 on failure with `{"ok": false, "error": "..."}`
- [ ] Count units are set to CF (CU 0) — not M3
- [ ] All field names use `c03` and `c50` — no remaining `count_0p3` or `count_5p0`
- [ ] Frontend uses `latest.data.c03` and `latest.data.c50`

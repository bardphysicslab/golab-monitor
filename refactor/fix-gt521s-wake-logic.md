# Fix GT-521S Wake Logic — Verified Device Behavior

## Task

Update `gt521s_driver.py` wake logic to match verified device behavior.

Work in the local repo at:
`/Users/kornelispoort/bardphysicslab/github/golab-monitor/`

Commit and push when done. Do not touch the Pi directly.

---

## Verified Serial Behavior (tested on Pi)

| Attempt | Input | Response |
|---------|-------|----------|
| CR #0–1 | `\r` | no response |
| CR #2+ | `\r` | `\r\n*` |
| `OP\r` | `OP\r` | `OP S\r\n*` |
| `1\r` | `1\r` | full settings report |
| `E\r` | `E\r` | `E\r\n*` |

---

## Wake Logic Rules

Update `wake()` to:

- Send repeated carriage returns (`\r`)
- Ignore initial silent attempts — silence on CR #0–1 is normal and must NOT be treated as failure
- Treat `*` as the primary ready prompt
- Do not fail before at least several attempts with proper delay between each
- If `*` is not seen after all attempts, send `OP\r` as a fallback probe; treat a valid `OP <state>` response with trailing `*` as wake success before failing
- Log each wake attempt including raw response bytes

---

## Response Parsing Rules

Verified command/response shape includes echoed commands:

```
OP\r\nOP S\r\n*
E\r\nE\r\n*
```

Parsing must tolerate:
- Echoed command line
- Actual response line
- Trailing `*`

Do not assume the response starts with the actual value — strip the echoed command first.

---

## Logging Requirement

Add a log line for each wake attempt showing:
- Attempt number
- Raw response bytes received

Example:
```
GT: wake attempt 1 — b''
GT: wake attempt 2 — b''
GT: wake attempt 3 — b'\r\n*'
GT: wake success on attempt 3
```

---

## Acceptance Criteria

- Wake does not fail on initial silence
- Wake succeeds when `*` is seen in response
- `OP` fallback probe is used if `*` is never seen after all CR attempts
- Wake success is accepted from either `*` prompt detection or a valid `OP` response if prompt-only detection fails
- All wake attempts are logged with raw bytes
- Command response parsing tolerates echoed command lines
- No change to command sequence after successful wake

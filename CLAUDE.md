# Claude Code Instructions — GoLab Pi / LabDash

## Project context
This project runs on a Raspberry Pi used for a Bard GoLab remote monitoring system.

Current deployment status:
- Raspberry Pi hostname: `golab-pi`
- Static Ethernet IP: `10.60.10.59`
- Access is through Bard internal network or Bard VPN
- SSH shortcut from Mac: `ssh golab`
- FastAPI app is deployed as a systemd service
- Service name: `labdash`
- App is currently served by uvicorn on `0.0.0.0:8000`

## Important paths
- Project root: `~/golab-monitor`
- App source: `~/golab-monitor/raspi`
- Virtual environment: `~/golab-monitor/venv`
- App entry point: `main:app` (in `raspi/main.py`)
- Systemd service file: `/etc/systemd/system/labdash.service`

## Current service behavior
The app is already running successfully as a background service.
Use these commands for inspection:

```bash
systemctl status labdash --no-pager
journalctl -u labdash -n 100 --no-pager
journalctl -u labdash -f
```

## Repo structure
- `raspi/` — FastAPI app running on the Pi
- `arduino/` — Arduino sensor node firmware and protocol docs
- `venv/` — Python virtual environment (not committed)
- `CLAUDE.md` — this file

## BardBox governance

GoLab is a working BardBox project repo. The canonical standards live in
`bardbox`; the reusable reference implementation lives in
`bardbox-project-template`.

BardBox standards are living standards. When a platform-level improvement is
made here, update:

1. This GoLab repo.
2. `bardbox-project-template`.
3. `bardbox-project-template/docs/BARDBOX_STANDARDS.md`.

If the change alters the BardBox platform/framework standard, update `bardbox`
as well.

Follow BardBox dashboard and stale-data rules unless the user explicitly says
they are changing the standard: never show stale readings as live, use clear
offline/stale states, preserve current entrypoint conventions for this project,
and keep reusable layout fixes synchronized back to the template.

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

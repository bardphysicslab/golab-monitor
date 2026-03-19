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
- Project root: `~/labdash`
- Virtual environment: `~/labdash/venv`
- App entry point: `main:app`
- Systemd service file: `/etc/systemd/system/labdash.service`

## Current service behavior
The app is already running successfully as a background service.
Use these commands for inspection:

```bash
systemctl status labdash --no-pager
journalctl -u labdash -n 100 --no-pager
journalctl -u labdash -f
```

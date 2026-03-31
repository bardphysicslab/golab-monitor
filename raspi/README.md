# raspi — GoLab Pi FastAPI App

FastAPI-based dashboard and data collection service running on the GoLab Raspberry Pi.

## What it does
- Reads particle counts from a GT-521S sensor over serial (USB-UART via CP2102)
- Serves a live web dashboard at `http://10.60.10.59:8000`
- Exposes REST endpoints for starting/stopping runs, querying live readings, and shared state
- Streams outdoor temperature from Open-Meteo via WebSocket

## Structure
- `main.py` — single-file FastAPI app (entry point: `main:app`)
- `gt521s_control.py` — GT-521S serial control utilities
- `static/` — static assets (logos, HTML)

## Running
The app runs as a systemd service (`labdash`):

```bash
sudo systemctl start labdash
sudo systemctl status labdash
journalctl -u labdash -f
```

## Key endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/` | GET | Live dashboard UI |
| `/state` | GET | Shared system state (run status, settings, thresholds) |
| `/gt/start` | POST | Apply settings and start a run |
| `/gt/stop` | POST | Stop the current run |
| `/gt/latest` | GET | Most recent sensor reading |
| `/gt/session-data` | GET | All data points from current session |
| `/gt/thresholds` | GET/POST | Particle count alert thresholds |

## Hardware
- Raspberry Pi (hostname: `golab-pi`, IP: `10.60.10.59`)
- GT-521S particle counter via `/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0`
- Count units set to particles/m³ (`CU 3`) on each run start

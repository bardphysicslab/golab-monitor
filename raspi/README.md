# raspi — GoLab Pi FastAPI App

FastAPI-based dashboard and data collection service running on the GoLab Raspberry Pi.

## What it does
- Reads particle counts from a GT-521S sensor over serial (USB-UART via CP2102)
- Reads BardBox/CESH Air web-node environmental particle readings over HTTP
- Serves a live web dashboard at `http://10.60.10.59:8000`
- Exposes REST endpoints for starting/stopping runs, querying live readings, and shared state
- Streams outdoor temperature from Open-Meteo via WebSocket

## Structure
- `main.py` — single-file FastAPI app (entry point: `main:app`)
- `drivers/gt521s_driver.py` — GT-521S serial particle counter driver
- `drivers/bardbox_env_node_v1_driver.py` — serial BardBox environmental node driver
- `drivers/web_node_driver.py` — BardBox dashboard API web-node driver
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
| `/state` | GET | Shared system state (run status, settings, thresholds, backup status) |
| `/gt/start` | POST | Apply settings and start a run |
| `/gt/stop` | POST | Stop the current run |
| `/gt/latest` | GET | Most recent sensor reading |
| `/gt/session-data` | GET | All data points from current session |
| `/gt/thresholds` | GET/POST | Particle count alert thresholds |

## Local Data and Backup

The monitor always writes session files and environmental daily averages locally
under:

```text
/home/golab/golab-monitor/data/
```

Local layout:

```text
data/
  gt521/
    bb-golab-gt521-001/
      sessions/
        gt_session_YYYY-MM-DD_HH-MM-SS.csv
  env/
    bb-golab-env-001/
      YYYY-MM-DD.csv
    bb-golab-env-002/
      YYYY-MM-DD.csv
```

Google Drive is a redundant backup destination only. It is updated by
`scripts/backup_to_drive.sh` through `rclone copy`; the app does not use the
Google Drive API and does not write monitoring data directly to USB or Drive.

Default backup destination:

```text
bardbox:sensor_data/golab-monitor
```

The Drive layout mirrors the local `data/` directory:

```text
sensor_data/
  golab-monitor/
    gt521/
      bb-golab-gt521-001/
        sessions/
    env/
      bb-golab-env-001/
      bb-golab-env-002/
```

Install the backup config and systemd units:

```bash
sudo cp deploy/golab-backup.env /etc/default/golab-backup
sudo cp deploy/golab-backup.service /etc/systemd/system/
sudo cp deploy/golab-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now golab-backup.timer
```

When the app starts, it creates the required local folders, creates today's
environmental CSV files with headers, and launches one non-blocking backup so
the Drive layout can be verified without waiting for the timer.

Run a manual full backup immediately:

```bash
sudo systemctl start golab-backup.service
sudo systemctl status golab-backup.service --no-pager
sudo tail -n 80 /var/log/golab-backup.log
```

Verify the backup:

```bash
rclone lsf bardbox:sensor_data/golab-monitor
rclone lsf bardbox:sensor_data/golab-monitor/gt521/bb-golab-gt521-001/sessions
rclone lsf bardbox:sensor_data/golab-monitor/env/bb-golab-env-001
```

Completed GT session CSVs are uploaded immediately in the background. Full data
backups run shortly after boot and then every 24 hours:

```ini
[Timer]
OnBootSec=2min
OnUnitActiveSec=24h
Persistent=true
```

If Drive or the network is unavailable, local monitoring continues normally.
The failed attempt is logged to `/var/log/golab-backup.log`, backup status is
written to `/var/lib/golab-backup/status.json`, and the next timer run retries
automatically.

## BardBox Web Nodes

Environmental particle sources can come from BardBox/CESH Air web nodes without
changing the GT-521S serial run workflow. `main.py` uses the same environmental
driver contract for serial and HTTP nodes: `connect()`, `get_info()`,
`get_capabilities()`, `get_reading()`, and `stop()`.

Default web-node configuration:

```python
ENV_NODES = [
    {
        "uid": "bb-golab-air-001",
        "label": "GoLab Air 1",
        "driver": "web_node",
        "server_url": "https://bard-box.org",
        "source_uid": "bb-golab-air-001",
        "pms_sensor": "pms_a",
        "poll_interval_s": 5,
    },
    {
        "uid": "bb-golab-air-002",
        "label": "GoLab Air 2",
        "driver": "web_node",
        "server_url": "https://bard-box.org",
        "source_uid": "bb-golab-air-002",
        "pms_sensor": "pms_a",
        "poll_interval_s": 5,
    },
]
```

For temporary testing, `source_uid` may be set to `GoLab-air-001`. To switch an
environmental source back to the serial BardBox node driver, remove
`driver: "web_node"` and configure `port` plus `baud` as before. The GT-521S
particle counter is unchanged and continues to use `GT521SDriver`.

## Hardware
- Raspberry Pi (hostname: `golab-pi`, IP: `10.60.10.59`)
- GT-521S particle counter via `/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_Y10162-if00-port0`
- Count units set to particles/m³ (`CU 3`) on each run start

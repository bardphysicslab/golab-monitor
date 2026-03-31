# arduino — GoLab Sensor Node

Arduino-based sensor node firmware and protocol documentation for the GoLab monitoring system.

## Role
The Arduino sensor node collects environmental data and communicates with the Raspberry Pi over serial.

## Serial protocol
- Baud rate: 9600
- Line termination: carriage return (`\r`)
- Commands are single-line ASCII strings

## GT-521S count unit modes (`CU` command)

| Command | Unit |
|---|---|
| `CU 0` | particles/ft³ |
| `CU 1` | particles/L |
| `CU 2` | total count |
| `CU 3` | particles/m³ |

The GoLab system uses **`CU 3`** (particles/m³). This is sent at the start of every run.

## CSV output format
When report mode is set to CSV (`SR 1`), each measurement line is:

```
YYYY-MM-DD HH:MM:SS, <size1>, <count1>, <size2>, <count2>, *<checksum>
```

Counts are in whatever unit is active (m³ if `CU 3` was sent).
Two size channels are reported per line (0.3µm and 5.0µm for the GT-521S).

## Key setup commands

| Command | Description |
|---|---|
| `ID NNN` | Set location ID (3 digits) |
| `ST NNNN` | Set sample time in seconds |
| `SH NNNN` | Set hold time in seconds |
| `SN NNN` | Set number of samples |
| `SR 1` | Set report format to CSV |
| `CU 3` | Set count units to particles/m³ |
| `S` | Start sampling |
| `E` | Stop sampling |
| `OP` | Query operational status |

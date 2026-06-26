# Environmental Node Availability

GoLab Monitor applies node freshness checks to environmental nodes.

Configuration:

- `GOLAB_ENV_NODE_STALE_AFTER_S`
- default: 30 seconds

Behavior:

- Fresh readings return `status: ok`
- Missing/stale nodes return `status: node_unavailable`
- Sensor values become `null`
- Dashboard displays `—` for unavailable values
- Dashboard shows a red error badge
- Last seen time may still be displayed

Important:

GoLab Monitor does not present cached readings as live data.
A disconnected node is shown as unavailable instead of continuing to display old readings.

API endpoints affected:

- `/env/latest`
- `/env/all`

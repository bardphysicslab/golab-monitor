"""Factory for GoLab environmental node drivers."""

from typing import Any, Dict

from drivers.bardbox_env_node_v1_driver import SensorDriver as SerialEnvDriver
from drivers.web_node_driver import SensorDriver as WebNodeDriver


def build_environment_driver(config: Dict[str, Any], stale_after_s: float):
    driver_type = str(config.get("driver", "serial_env_node"))
    if driver_type == "web_node":
        return WebNodeDriver(
            server_url=str(config["server_url"]),
            source_uid=str(config.get("source_uid", config["uid"])),
            pms_sensor=str(config.get("pms_sensor", "pms_a")),
            poll_interval_s=float(config.get("poll_interval_s", 5)),
            stale_after_s=stale_after_s,
        )
    if driver_type in {"serial_env_node", "bardbox_env_node_v1"}:
        return SerialEnvDriver(port=str(config["port"]), baud=int(config["baud"]))
    raise ValueError(f"Unknown environmental driver: {driver_type}")

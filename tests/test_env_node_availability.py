import time
import sys
import types
import unittest

if "serial" not in sys.modules:
    fake_serial = types.ModuleType("serial")
    fake_serial.Serial = object
    fake_serial.SerialException = Exception
    sys.modules["serial"] = fake_serial

if "httpx" not in sys.modules:
    fake_httpx = types.ModuleType("httpx")

    class FakeHTTPError(Exception):
        pass

    class FakeTimeoutException(FakeHTTPError):
        pass

    fake_httpx.HTTPError = FakeHTTPError
    fake_httpx.TimeoutException = FakeTimeoutException
    fake_httpx.Client = object
    sys.modules["httpx"] = fake_httpx

from raspi.drivers.bardbox_env_node_v1_driver import BardboxEnvNodeV1Driver, STALE_THRESHOLD_S
from raspi.drivers.web_node_driver import WebNodeDriver


class EnvNodeAvailabilityTests(unittest.TestCase):
    def test_stale_cached_reading_returns_unavailable_with_null_values(self):
        driver = BardboxEnvNodeV1Driver()
        driver._info = {"uid": "bb-gol-air-001"}
        driver._latest = {
            "_timestamp": "2026-05-29T12:00:00Z",
            "temp_c": 23.4,
            "rh_pct": 32.7,
            "press_pa": 101000,
            "pm1_std": 1,
            "pm25_std": 2,
            "pm10_std": 3,
            "pm1_env": 4,
            "pm25_env": 5,
            "pm10_env": 6,
            "c03": 7,
            "c05": 8,
            "c10": 9,
            "c25": 10,
            "c50": 11,
            "c100": 12,
            "sample_idx": 13,
        }
        driver._latest_time = time.monotonic() - STALE_THRESHOLD_S - 1

        reading = driver.get_reading()

        self.assertEqual(reading["status"], "node_unavailable")
        self.assertEqual(reading["extended"]["last_seen"], "2026-05-29T12:00:00Z")
        self.assertTrue(all(value is None for value in reading["data"].values()))
        sensor_extended = {
            key: value
            for key, value in reading["extended"].items()
            if key not in {"last_seen", "message"}
        }
        self.assertTrue(all(value is None for value in sensor_extended.values()))


class WebNodeDriverTests(unittest.TestCase):
    def test_extracts_configured_uid_and_pms_channel(self):
        driver = WebNodeDriver(
            server_url="https://bard-box.org",
            source_uid="GoLab-air-001",
            pms_sensor="pms_a",
        )
        payload = {
            "nodes": [
                {
                    "uid": "other-node",
                    "status": "live",
                    "pms": {"pms_a": {"c03": 999}},
                },
                {
                    "uid": "GoLab-air-001",
                    "timestamp": "2026-07-02T12:00:00Z",
                    "status": "live",
                    "temp_c": 22.4,
                    "humidity_percent": 45.6,
                    "pressure_hpa": 1013.2,
                    "rssi_dbm": -68,
                    "read_count": 42,
                    "sample_interval_ms": 5000,
                    "age_seconds": 2,
                    "stale_after_s": 30,
                    "pms": {
                        "pms_a": {
                            "pm1_std": 1,
                            "pm25_std": 2,
                            "pm10_std": 3,
                            "pm1_env": 4,
                            "pm25_env": 5,
                            "pm10_env": 6,
                            "c03": 7,
                            "c05": 8,
                            "c10": 9,
                            "c25": 10,
                            "c50": 11,
                            "c100": 12,
                        }
                    },
                },
            ]
        }

        node = driver._find_node(payload)
        channel = driver._pms_channel(node)
        reading = driver._normalize_node(node, channel)

        self.assertEqual(reading["uid"], "GoLab-air-001")
        self.assertEqual(reading["status"], "live")
        self.assertEqual(reading["data"]["c03"], 7)
        self.assertEqual(reading["data"]["pm25_std"], 2)
        self.assertEqual(reading["extended"]["rh_pct"], 45.6)
        self.assertEqual(reading["extended"]["press_pa"], 101320)
        self.assertEqual(reading["extended"]["rssi_dbm"], -68)
        self.assertEqual(reading["extended"]["sample_idx"], 42)

    def test_extracts_live_dashboard_flattened_pms_fields(self):
        driver = WebNodeDriver(source_uid="GoLab-air-001", pms_sensor="pms_a")
        payload = {
            "server_time": "2026-07-02T16:51:29Z",
            "readings": [
                {
                    "uid": "GoLab-air-001",
                    "timestamp": "2026-07-02T16:51:25Z",
                    "status": "live",
                    "data": {
                        "temp_c": 24.64,
                        "humidity_percent": 71.68,
                        "pressure_hpa": 1009.28,
                        "bme680_gas_resistance_ohms": None,
                        "rssi_dbm": -88,
                        "pms_a_valid": True,
                        "pms_a_pm1_0_std": 1,
                        "pms_a_pm2_5_std": 5,
                        "pms_a_pm10_std": 6,
                        "pms_a_pm1_0_env": 1,
                        "pms_a_pm2_5_env": 5,
                        "pms_a_pm10_env": 6,
                        "pms_a_particles_03um": 358,
                        "pms_a_particles_05um": 300,
                        "pms_a_particles_10um": 96,
                        "pms_a_particles_25um": 8,
                        "pms_a_particles_50um": 2,
                        "pms_a_particles_100um": 2,
                    },
                    "extended": {
                        "read_count": 364,
                        "sample_interval_ms": 4000,
                        "last_seen": "2026-07-02T16:51:25Z",
                        "age_seconds": 4.3,
                        "is_stale": False,
                        "stale_after_s": 30.0,
                    },
                }
            ],
        }

        node = driver._find_node(payload)
        channel = driver._pms_channel(node)
        reading = driver._normalize_node(node, channel)

        self.assertEqual(reading["status"], "live")
        self.assertEqual(reading["data"]["temp_c"], 24.64)
        self.assertEqual(reading["data"]["pm25_std"], 5)
        self.assertEqual(reading["data"]["c03"], 358)
        self.assertEqual(reading["extended"]["rh_pct"], 71.68)
        self.assertEqual(reading["extended"]["press_pa"], 100928)
        self.assertEqual(reading["extended"]["c05"], 300)
        self.assertEqual(reading["extended"]["sample_idx"], 364)

    def test_missing_uid_returns_unavailable_without_crashing(self):
        driver = WebNodeDriver(source_uid="missing-node")

        driver._fetch_payload = lambda: {"nodes": [{"uid": "other-node"}]}
        reading = driver.get_reading()

        self.assertEqual(reading["status"], "offline")
        self.assertIn("UID not found", reading["error"])
        self.assertTrue(all(value is None for value in reading["data"].values()))


if __name__ == "__main__":
    unittest.main()

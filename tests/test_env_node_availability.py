import time
import unittest

from raspi.drivers.bardbox_env_node_v1_driver import BardboxEnvNodeV1Driver, STALE_THRESHOLD_S


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


if __name__ == "__main__":
    unittest.main()

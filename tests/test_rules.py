import unittest

from src.domain import Actor, ConflictError, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.actor = Actor("tester", "admin")

    def test_telemetry_requires_known_asset_and_numeric_value(self):
        lookup = lambda kind, field, value: []
        with self.assertRaises(ValidationError):
            self.rules.validate_create(
                self.actor,
                "telemetry",
                {"asset_id": "missing", "metric": "pressure", "value": 2, "observed_at": "2026-09-27", "revision": 3},
                lookup,
            )

    def test_telemetry_folds_asset_clock_offset(self):
        asset = {"id": "a-1", "kind": "asset", "status": "healthy", "data": {"clock_offset_seconds": -300}}
        payload = {"asset_id": "a-1", "metric": "pressure", "value": 2, "observed_at": "2026-09-27T09:00:00Z", "revision": 3}
        self.rules.validate_create(
            self.actor,
            "telemetry",
            payload,
            lambda kind, field, value: [asset] if kind == "asset" else [],
        )
        # Device clock runs 300 seconds slow: adjusted time moves forward.
        self.assertEqual(payload["adjusted_observed_at"], "2026-09-27T09:05:00+00:00")

    def test_asset_requires_station(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.actor, "asset", {"station_id": "missing", "asset_type": "sensor", "serial_no": "S", "last_seen": "2026-09-27"}, lambda k, f, v: [])

    def test_asset_clock_offset_allows_negative_values(self):
        station = {"id": "s-1", "kind": "station", "status": "online", "data": {}}
        payload = {"station_id": "s-1", "asset_type": "sensor", "serial_no": "S", "last_seen": "2026-09-27", "clock_offset_seconds": -120}
        self.rules.validate_create(self.actor, "asset", payload, lambda k, f, v: [station] if k == "station" else [])
        self.assertEqual(payload["clock_offset_seconds"], -120)

    def test_incident_requires_severity(self):
        with self.assertRaises(ValidationError):
            self.rules.validate_create(self.actor, "incident", {"station_id": "s-1", "kind": "outage", "severity": "unknown", "summary": "x"}, lambda k, f, v: [])


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, InvalidTransition, PermissionDenied
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def station_asset(self):
        station = self.service.create(self.admin, "station", {"name": "S", "region": "R"})
        asset = self.service.create(self.admin, "asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "S-1", "last_seen": "2026-09-27T09:00:00Z"})
        return station, asset

    def test_permission_denied(self):
        station, asset = self.station_asset()
        with self.assertRaises(PermissionDenied):
            self.service.transition(Actor("viewer", "viewer"), asset["id"], "fail", {"reason": "x"})

    def test_version_conflict(self):
        station, asset = self.station_asset()
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, asset["id"], "fail", {"reason": "x"}, 999)

    def test_revise_with_earlier_observation_becomes_late(self):
        station, asset = self.station_asset()
        telemetry = self.service.create(self.admin, "telemetry", {"asset_id": asset["id"], "metric": "p", "value": 1, "observed_at": "2026-09-27T10:00:00Z", "revision": 2})
        late = self.service.transition(
            self.admin,
            telemetry["id"],
            "revise",
            {"value": 9, "observed_at": "2026-09-27T08:00:00Z", "revision": 1},
        )
        self.assertEqual(late["status"], "late")
        current = self.service.get(telemetry["id"])
        self.assertEqual(current["status"], "current")
        self.assertEqual(current["data"]["value"], 1)

    def test_late_version_cannot_be_revised(self):
        station, asset = self.station_asset()
        current = self.service.create(self.admin, "telemetry", {"asset_id": asset["id"], "metric": "p", "value": 1, "observed_at": "2026-09-27T10:00:00Z", "revision": 2})
        late = self.service.create(self.admin, "telemetry", {"asset_id": asset["id"], "metric": "p", "value": 9, "observed_at": "2026-09-27T08:00:00Z", "revision": 1})
        self.assertEqual(late["status"], "late")
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, late["id"], "revise", {"value": 3, "observed_at": "2026-09-27T11:00:00Z", "revision": 3})
        # The same reading posted normally still replaces the current value.
        updated = self.service.create(self.admin, "telemetry", {"asset_id": asset["id"], "metric": "p", "value": 3, "observed_at": "2026-09-27T11:00:00Z", "revision": 3})
        self.assertEqual(updated["id"], current["id"])
        self.assertEqual(updated["data"]["value"], 3)

    def test_incident_cannot_resolve_with_active_action(self):
        station, asset = self.station_asset()
        incident = self.service.create(self.admin, "incident", {"station_id": station["id"], "asset_id": asset["id"], "kind": "loss", "severity": "high", "summary": "x"})
        for action in ("diagnose", "plan_recovery", "start_recovery"):
            incident = self.service.transition(self.admin, incident["id"], action)
        self.service.create(self.admin, "recovery_action", {"incident_id": incident["id"], "action_type": "remote_restart", "dedupe_key": "k"})
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, incident["id"], "resolve", {"summary": "done"})


if __name__ == "__main__":
    unittest.main()

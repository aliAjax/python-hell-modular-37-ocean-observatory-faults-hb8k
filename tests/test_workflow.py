import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def utc_future_iso(seconds=2):
    from datetime import timedelta

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data):
        return self.service.create(self.actor, kind, data)

    def act(self, entity, action, data=None, version=None):
        return self.service.transition(self.actor, entity["id"], action, data or {}, version)

    def test_full_fault_recovery_flow(self):
        station = self.create("station", {"name": "OSN-01", "region": "East"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "S-1", "last_seen": "2026-09-27T09:00:00Z", "clock_offset_seconds": 0})
        link = self.create("link", {"station_id": station["id"], "asset_id": asset["id"], "link_type": "fiber", "capacity": 100})
        telemetry = self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 10, "observed_at": "2026-09-27T09:00:00Z", "revision": 1})
        telemetry = self.act(telemetry, "revise", {"value": 10.5, "observed_at": "2026-09-27T09:05:00Z", "revision": 2})
        self.assertEqual(telemetry["status"], "current")
        self.assertEqual(telemetry["data"]["value"], 10.5)

        incident = self.create("incident", {"station_id": station["id"], "asset_id": asset["id"], "link_id": link["id"], "kind": "link_loss", "severity": "high", "summary": "no data"})
        incident = self.act(incident, "diagnose", {})
        incident = self.act(incident, "plan_recovery", {})
        incident = self.act(incident, "start_recovery", {})

        action = self.create("recovery_action", {"incident_id": incident["id"], "asset_id": asset["id"], "action_type": "remote_restart", "dedupe_key": "restart-1"})
        action = self.act(action, "approve", {})
        action = self.act(action, "start", {})
        action = self.act(action, "succeed", {"outcome": "asset online"})

        gap = self.create("gap", {"incident_id": incident["id"], "asset_id": asset["id"], "start_at": "2026-09-27T09:00:00Z", "end_at": "2026-09-27T09:30:00Z"})
        gap = self.act(gap, "estimate", {"estimate": "interpolation"})
        gap = self.act(gap, "fill", {"estimate": "interpolated series"})

        # Resolution requires a usable reading observed after incident start.
        with self.assertRaises(ConflictError):
            self.act(incident, "resolve", {"summary": "service restored"})
        self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 11, "observed_at": utc_future_iso(), "revision": 3})

        incident = self.act(incident, "resolve", {"summary": "service restored"})
        self.assertEqual(incident["status"], "resolved")
        incident = self.act(incident, "close", {})
        self.assertEqual(incident["status"], "closed")

    def test_late_revision_can_be_merged(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z"})
        telemetry = self.create("telemetry", {"asset_id": asset["id"], "metric": "temperature", "value": 1, "observed_at": "2026-09-27T09:00:00Z", "revision": 1})
        updated = self.service.transition(
            self.actor,
            telemetry["id"],
            "revise",
            {"value": 2, "observed_at": "2026-09-27T09:10:00Z", "revision": 3},
        )
        self.assertEqual(updated["id"], telemetry["id"])
        self.assertEqual(updated["status"], "current")
        self.assertEqual(updated["data"]["revision"], 3)
        self.assertEqual(updated["data"]["value"], 2)

    def test_clock_offset_folds_observation_time(self):
        station = self.create("station", {"name": "S", "region": "R"})
        # Device clock runs 600 seconds fast: readings fold back by 10 minutes.
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z", "clock_offset_seconds": 600})
        telemetry = self.create("telemetry", {"asset_id": asset["id"], "metric": "temperature", "value": 1, "observed_at": "2026-09-27T09:10:00Z", "revision": 1})
        self.assertEqual(telemetry["data"]["adjusted_observed_at"], "2026-09-27T09:00:00+00:00")
        self.assertEqual(telemetry["data"]["clock_offset_seconds"], 600.0)

    def test_late_arriving_older_reading_kept_as_late_version(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z", "clock_offset_seconds": 0})
        current = self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 20, "observed_at": "2026-09-27T10:00:00Z", "revision": 5})
        # Offline backfill arrives later but observes an earlier moment, even
        # though it carries a bigger revision number.
        late = self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 99, "observed_at": "2026-09-27T09:00:00Z", "revision": 9})
        self.assertNotEqual(late["id"], current["id"])
        self.assertEqual(late["status"], "late")
        self.assertEqual(late["data"]["value"], 99)
        untouched = self.service.get(current["id"])
        self.assertEqual(untouched["status"], "current")
        self.assertEqual(untouched["data"]["value"], 20)
        self.assertEqual(untouched["data"]["revision"], 5)

    def test_duplicate_reading_is_idempotent(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z"})
        first = self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 1, "observed_at": "2026-09-27T09:00:00Z", "revision": 1})
        again = self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 1, "observed_at": "2026-09-27T09:00:00Z", "revision": 1})
        self.assertEqual(again["id"], first["id"])

    def test_resolve_requires_post_incident_telemetry(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z"})
        incident = self.create("incident", {"asset_id": asset["id"], "kind": "loss", "severity": "high", "summary": "x"})
        for action in ("diagnose", "plan_recovery", "start_recovery"):
            incident = self.service.transition(self.actor, incident["id"], action)
        # An old current reading from before the incident does not count.
        self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 1, "observed_at": "2000-01-01T00:00:00Z", "revision": 1})
        with self.assertRaises(ConflictError):
            self.service.transition(self.actor, incident["id"], "resolve", {"summary": "done"})
        # A late backfill after the incident start also does not count.
        self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 2, "observed_at": utc_future_iso(), "revision": 99})
        self.create("telemetry", {"asset_id": asset["id"], "metric": "pressure", "value": 3, "observed_at": "2000-01-02T00:00:00Z", "revision": 100})
        resolved = self.service.transition(self.actor, incident["id"], "resolve", {"summary": "done"})
        self.assertEqual(resolved["status"], "resolved")


if __name__ == "__main__":
    unittest.main()

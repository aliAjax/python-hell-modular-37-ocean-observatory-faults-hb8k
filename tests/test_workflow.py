import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


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
        telemetry = self.act(telemetry, "revise", {"value": 10.5, "observed_at": "2026-09-27T09:30:00Z", "revision": 2})
        self.assertEqual(telemetry["status"], "current")
        self.assertEqual(telemetry["data"]["value"], 10.5)

        incident = self.create("incident", {"station_id": station["id"], "asset_id": asset["id"], "link_id": link["id"], "kind": "link_loss", "severity": "high", "summary": "no data", "started_at": "2026-09-27T08:00:00Z"})
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

        incident = self.act(incident, "resolve", {"summary": "service restored"})
        self.assertEqual(incident["status"], "resolved")
        incident = self.act(incident, "close", {})
        self.assertEqual(incident["status"], "closed")

    def test_clock_offset_corrects_observation_time_and_keeps_late_reading_separate(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create(
            "asset",
            {
                "station_id": station["id"],
                "asset_type": "sensor",
                "serial_no": "X",
                "last_seen": "2026-09-27T09:00:00Z",
                "clock_offset_seconds": 600,
            },
        )
        current = self.create(
            "telemetry",
            {
                "asset_id": asset["id"],
                "metric": "temperature",
                "value": 20,
                "observed_at": "2026-09-27T10:10:00Z",
                "revision": 1,
            },
        )
        self.assertEqual(current["data"]["corrected_observed_at"], "2026-09-27T10:00:00Z")

        late = self.create(
            "telemetry",
            {
                "asset_id": asset["id"],
                "metric": "temperature",
                "value": 19,
                "observed_at": "2026-09-27T10:05:00Z",
                "revision": 2,
            },
        )
        self.assertEqual(late["status"], "late")
        self.assertEqual(late["data"]["corrected_observed_at"], "2026-09-27T09:55:00Z")
        unchanged = self.service.get(current["id"])
        self.assertEqual(unchanged["status"], "current")
        self.assertEqual(unchanged["data"]["value"], 20)

        duplicate = self.create(
            "telemetry",
            {
                "asset_id": asset["id"],
                "metric": "temperature",
                "value": 19.5,
                "observed_at": "2026-09-27T10:10:00Z",
                "revision": 3,
            },
        )
        self.assertEqual(duplicate["status"], "late")
        self.assertEqual(self.service.get(current["id"])["data"]["value"], 20)

        newer = self.create(
            "telemetry",
            {
                "asset_id": asset["id"],
                "metric": "temperature",
                "value": 21,
                "observed_at": "2026-09-27T10:20:00Z",
                "revision": 3,
            },
        )
        self.assertEqual(newer["id"], current["id"])
        self.assertEqual(newer["status"], "current")
        self.assertEqual(newer["data"]["value"], 21)

    def test_negative_clock_offset_moves_observation_time_later(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create(
            "asset",
            {
                "station_id": station["id"],
                "asset_type": "sensor",
                "serial_no": "N",
                "last_seen": "2026-09-27T09:00:00Z",
                "clock_offset_seconds": -300,
            },
        )
        telemetry = self.create(
            "telemetry",
            {
                "asset_id": asset["id"],
                "metric": "temperature",
                "value": 18,
                "observed_at": "2026-09-27T09:55:00Z",
                "revision": 1,
            },
        )
        self.assertEqual(telemetry["data"]["corrected_observed_at"], "2026-09-27T10:00:00Z")

    def test_revision_can_store_late_reading_without_replacing_current(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "X", "last_seen": "2026-09-27T09:00:00Z"})
        current = self.create("telemetry", {"asset_id": asset["id"], "metric": "temperature", "value": 1, "observed_at": "2026-09-27T10:00:00Z", "revision": 1})
        late = self.service.transition(
            self.actor,
            current["id"],
            "revise",
            {"value": 2, "observed_at": "2026-09-27T09:00:00Z", "revision": 3},
        )
        self.assertEqual(late["status"], "late")
        self.assertNotEqual(late["id"], current["id"])
        self.assertEqual(self.service.get(current["id"])["data"]["value"], 1)

    def test_stale_series_keeps_old_patch_late_and_can_promote_newer_reading(self):
        station = self.create("station", {"name": "S", "region": "R"})
        asset = self.create("asset", {"station_id": station["id"], "asset_type": "sensor", "serial_no": "Y", "last_seen": "2026-09-27T09:00:00Z"})
        current = self.create("telemetry", {"asset_id": asset["id"], "metric": "temperature", "value": 1, "observed_at": "2026-09-27T10:00:00Z", "revision": 1})
        late_source = self.create("telemetry", {"asset_id": asset["id"], "metric": "temperature", "value": 0, "observed_at": "2026-09-27T09:00:00Z", "revision": 2})
        self.service.repository.update_entity(late_source["id"], None, "stale", late_source["data"])
        with self.service.repository._connect() as connection:
            connection.execute("DELETE FROM entities WHERE id = ?", (current["id"],))
        # Newer observations can promote a stale series; older patches still only add late data.
        another_late = self.service.transition(
            self.actor,
            late_source["id"],
            "revise",
            {"value": 0.5, "observed_at": "2026-09-27T08:30:00Z", "revision": 4},
        )
        self.assertEqual(another_late["status"], "late")
        self.assertEqual(self.service.get(late_source["id"])["status"], "stale")

        restored = self.service.transition(
            self.actor,
            late_source["id"],
            "revise",
            {"value": 2, "observed_at": "2026-09-27T11:00:00Z", "revision": 5},
        )
        self.assertEqual(restored["id"], late_source["id"])
        self.assertEqual(restored["status"], "current")
        self.assertEqual(restored["data"]["value"], 2)


if __name__ == "__main__":
    unittest.main()

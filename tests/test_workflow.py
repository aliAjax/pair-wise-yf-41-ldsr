import tempfile
import unittest
from pathlib import Path

from src.domain import Actor
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _resolve(value, created):
    if isinstance(value, str):
        for key, item in created.items():
            value = value.replace("{" + key + "}", str(item))
        return value
    if isinstance(value, list):
        return [_resolve(item, created) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, created) for key, item in value.items()}
    return value


def publish_event(service):
    analyst = Actor("analyst-1", "analyst")
    reviewer = Actor("reviewer-1", "reviewer")
    service.create(
        Actor("admin", "admin"),
        "station",
        {"code": "STA-1", "lat": 35.0, "lon": 110.0},
    )
    event = service.create(
        analyst,
        "event",
        {
            "title": "Event-A",
            "origin_time": "2026-01-01T00:00:00Z",
            "location": "Region-A",
            "reports": [
                {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
            ],
        },
    )
    service.transition(analyst, event["id"], "associate", {})
    service.transition(
        reviewer, event["id"], "review", {"reviewer": "R-1", "magnitude": 4.2}
    )
    service.transition(
        reviewer, event["id"], "publish", {"communication_id": "C-1"}
    )
    return event["id"]


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.actor = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_workflow(self):
        created = {}
        steps = [
            {
                "op": "create",
                "as": "station",
                "kind": "station",
                "data": {"code": "STA-1", "lat": 35.0, "lon": 110.0},
            },
            {
                "op": "create",
                "as": "event",
                "kind": "event",
                "data": {
                    "title": "Event-A",
                    "origin_time": "2026-01-01T00:00:00Z",
                    "location": "Region-A",
                    "reports": [
                        {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
                        {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
                    ],
                },
            },
            {"op": "transition", "target": "event", "action": "associate", "data": {}, "expect": "associated"},
            {"op": "transition", "target": "event", "action": "review", "data": {"reviewer": "R-1", "magnitude": 4.2}, "expect": "reviewed"},
            {"op": "transition", "target": "event", "action": "publish", "data": {"communication_id": "C-1"}, "expect": "published"},
        ]
        for step in steps:
            if step["op"] == "create":
                entity = self.service.create(
                    self.actor,
                    step["kind"],
                    _resolve(step.get("data", {}), created),
                    step.get("idempotency_key"),
                )
                created[step["as"]] = entity["id"]
            else:
                entity = self.service.transition(
                    self.actor,
                    created[step["target"]],
                    step["action"],
                    _resolve(step.get("data", {}), created),
                    step.get("expected_version"),
                )
            if "expect" in step:
                self.assertEqual(entity["status"], step["expect"])

    def test_revision_order_cuts_new_catalog_snapshot(self):
        event_id = publish_event(self.service)
        analyst = Actor("analyst-1", "analyst")
        reviewer = Actor("reviewer-1", "reviewer")

        order = self.service.create(
            analyst,
            "revision_order",
            {
                "event_id": event_id,
                "basis": "new station data",
                "proposed_magnitude": 4.3,
                "proposed_location": "Region-A1",
                "proposed_depth_km": 12.0,
            },
        )
        self.assertEqual(order["status"], "pending")
        self.assertEqual(order["data"]["resubmit_count"], 0)

        # While under review the current catalog must be untouched.
        event = self.service.get(event_id)
        self.assertEqual(event["status"], "published")
        self.assertEqual(event["data"]["magnitude"], 4.2)

        approved = self.service.approve_revision_order(
            reviewer, order["id"], {"communication_id": "C-2"}
        )
        self.assertEqual(approved["status"], "approved")

        event = self.service.get(event_id)
        self.assertEqual(event["status"], "revised")
        self.assertEqual(event["data"]["magnitude"], 4.3)
        self.assertEqual(event["data"]["location"], "Region-A1")
        self.assertEqual(event["data"]["depth_km"], 12.0)

        snapshots = self.service.event_versions(event_id)
        self.assertEqual([s["source"] for s in snapshots], ["publish", "revision_order"])
        self.assertEqual(snapshots[0]["data"]["magnitude"], 4.2)
        self.assertEqual(snapshots[-1]["data"]["magnitude"], 4.3)
        self.assertEqual(snapshots[-1]["source_order_id"], order["id"])


if __name__ == "__main__":
    unittest.main()

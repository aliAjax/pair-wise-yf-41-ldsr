import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    NotFoundError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService

EVENT_DATA = {
    "title": "Event-A",
    "origin_time": "2026-01-01T00:00:00Z",
    "location": "Region-A",
    "reports": [
        {"station": "STA-1", "time_offset": 2, "distance_km": 1.0},
        {"station": "STA-2", "time_offset": -1, "distance_km": 1.5},
    ],
}


class RevisionOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")

    def tearDown(self):
        self.tmp.cleanup()

    def _published_event(self):
        event = self.service.create(self.admin, "event", dict(EVENT_DATA))
        self.service.transition(self.admin, event["id"], "associate", {})
        self.service.transition(
            self.admin, event["id"], "review", {"reviewer": "R-1", "magnitude": 4.2}
        )
        return self.service.transition(
            self.admin, event["id"], "publish", {"communication_id": "C-1"}
        )

    def _create_order(self, event, magnitude=4.3, depth=10.0):
        return self.service.create(
            self.analyst,
            "revision_order",
            {
                "event_id": event["id"],
                "basis": "relocation with new station data",
                "magnitude": magnitude,
                "location": "Region-A-north",
                "depth": depth,
            },
        )

    def test_full_revision_lifecycle(self):
        event = self._published_event()
        order = self._create_order(event)
        self.assertEqual(order["status"], "submitted")
        self.assertEqual(order["data"]["resubmit_count"], 0)
        self.assertEqual(order["data"]["event_version"], event["version"])

        # a pending order must not touch the event or the current catalog
        unchanged = self.service.get(event["id"])
        self.assertEqual(unchanged["status"], "published")
        self.assertEqual(unchanged["data"]["magnitude"], 4.2)
        catalog = self.service.current_catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["catalog_version"], 1)
        self.assertEqual(catalog[0]["data"]["magnitude"], 4.2)

        # reviewer rejects with an opinion
        rejected = self.service.transition(
            self.reviewer, order["id"], "reject", {"opinion": "depth needs justification"}
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(rejected["data"]["opinion"], "depth needs justification")

        # analyst reworks the same order and resubmits, count is kept
        resubmitted = self.service.transition(
            self.analyst,
            order["id"],
            "resubmit",
            {
                "basis": "added depth phase picks",
                "magnitude": 4.4,
                "location": "Region-A-north",
                "depth": 12.5,
            },
        )
        self.assertEqual(resubmitted["status"], "submitted")
        self.assertEqual(resubmitted["data"]["resubmit_count"], 1)
        self.assertEqual(resubmitted["data"]["basis"], "added depth phase picks")

        # still nothing leaks into the catalog before approval
        self.assertEqual(self.service.current_catalog()[0]["catalog_version"], 1)

        # approval cuts the snapshot over as the latest catalog
        approved = self.service.transition(self.reviewer, order["id"], "approve", {})
        self.assertEqual(approved["status"], "approved")

        revised = self.service.get(event["id"])
        self.assertEqual(revised["status"], "revised")
        self.assertEqual(revised["data"]["magnitude"], 4.4)
        self.assertEqual(revised["data"]["location"], "Region-A-north")
        self.assertEqual(revised["data"]["depth"], 12.5)
        self.assertEqual(revised["data"]["last_revision_order"], order["id"])

        snapshots = self.service.event_snapshots(event["id"])
        self.assertEqual([s["catalog_version"] for s in snapshots], [1, 2])
        self.assertFalse(snapshots[0]["is_current"])
        self.assertTrue(snapshots[1]["is_current"])
        self.assertEqual(snapshots[0]["data"]["magnitude"], 4.2)
        self.assertEqual(snapshots[1]["data"]["magnitude"], 4.4)
        self.assertEqual(snapshots[1]["source"], "revision_order")
        self.assertEqual(snapshots[1]["order_id"], order["id"])

        catalog = self.service.current_catalog()
        self.assertEqual(len(catalog), 1)
        self.assertEqual(catalog[0]["catalog_version"], 2)
        self.assertEqual(catalog[0]["data"]["magnitude"], 4.4)

        # create / reject opinion / resubmit / approve are all audited
        order_audit = self.service.audit_log(order["id"])
        self.assertEqual(
            [row["action"] for row in order_audit],
            ["create", "reject", "resubmit", "approve"],
        )
        self.assertEqual(order_audit[0]["actor_id"], "analyst-1")
        self.assertEqual(
            order_audit[1]["detail"]["patch"]["opinion"], "depth needs justification"
        )
        event_actions = [row["action"] for row in self.service.audit_log(event["id"])]
        self.assertIn("revise", event_actions)

    def test_direct_revise_is_closed(self):
        event = self._published_event()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.admin, event["id"], "revise", {"reason": "x", "magnitude": 5.0}
            )

    def test_create_requires_published_event(self):
        candidate = self.service.create(self.admin, "event", dict(EVENT_DATA))
        with self.assertRaises(ValidationError):
            self._create_order(candidate)
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst,
                "revision_order",
                {
                    "event_id": "missing-event",
                    "basis": "b",
                    "magnitude": 4.0,
                    "location": "L",
                    "depth": 1.0,
                },
            )

    def test_create_validates_fields(self):
        event = self._published_event()
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst,
                "revision_order",
                {"event_id": event["id"], "magnitude": 4.0, "location": "L", "depth": 1.0},
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst,
                "revision_order",
                {
                    "event_id": event["id"],
                    "basis": "b",
                    "magnitude": "4.5",
                    "location": "L",
                    "depth": 1.0,
                },
            )

    def test_role_restrictions(self):
        event = self._published_event()
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.reviewer,
                "revision_order",
                {
                    "event_id": event["id"],
                    "basis": "b",
                    "magnitude": 4.0,
                    "location": "L",
                    "depth": 1.0,
                },
            )
        order = self._create_order(event)
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.analyst, order["id"], "approve", {})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.analyst, order["id"], "reject", {"opinion": "no"}
            )
        self.service.transition(self.reviewer, order["id"], "reject", {"opinion": "no"})
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.reviewer,
                order["id"],
                "resubmit",
                {"basis": "b", "magnitude": 4.1, "location": "L", "depth": 2.0},
            )

    def test_stale_event_version_rejected_then_rebased(self):
        event = self._published_event()
        first = self._create_order(event, magnitude=4.3)
        second = self._create_order(event, magnitude=4.5)
        self.service.transition(self.reviewer, first["id"], "approve", {})

        # the second order was drafted against the old event version
        with self.assertRaises(ConflictError):
            self.service.transition(self.reviewer, second["id"], "approve", {})

        # reject + resubmit re-bases the order on the latest event version
        self.service.transition(
            self.reviewer, second["id"], "reject", {"opinion": "stale base version"}
        )
        resubmitted = self.service.transition(
            self.analyst,
            second["id"],
            "resubmit",
            {
                "basis": "rebased on latest catalog",
                "magnitude": 4.5,
                "location": "Region-A-north",
                "depth": 10.0,
            },
        )
        refreshed = self.service.get(event["id"])
        self.assertEqual(resubmitted["data"]["event_version"], refreshed["version"])
        approved = self.service.transition(self.reviewer, second["id"], "approve", {})
        self.assertEqual(approved["status"], "approved")
        self.assertEqual(self.service.get(event["id"])["data"]["magnitude"], 4.5)
        versions = [
            s["catalog_version"] for s in self.service.event_snapshots(event["id"])
        ]
        self.assertEqual(versions, [1, 2, 3])

    def test_order_version_conflict(self):
        event = self._published_event()
        order = self._create_order(event)
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.reviewer,
                order["id"],
                "reject",
                {"opinion": "x"},
                expected_version=99,
            )

    def test_invalid_transitions(self):
        event = self._published_event()
        order = self._create_order(event)
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.analyst,
                order["id"],
                "resubmit",
                {"basis": "b", "magnitude": 4.1, "location": "L", "depth": 2.0},
            )
        self.service.transition(self.reviewer, order["id"], "reject", {"opinion": "x"})
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.reviewer, order["id"], "approve", {})

    def test_snapshots_of_unknown_event(self):
        with self.assertRaises(NotFoundError):
            self.service.event_snapshots("no-such-event")


if __name__ == "__main__":
    unittest.main()

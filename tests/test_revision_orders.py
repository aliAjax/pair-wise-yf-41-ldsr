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

from tests.test_workflow import publish_event


ORDER_DATA = {
    "basis": "new station data",
    "proposed_magnitude": 4.3,
    "proposed_location": "Region-A1",
    "proposed_depth_km": 12.0,
}


class RevisionOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.analyst = Actor("analyst-1", "analyst")
        self.analyst2 = Actor("analyst-2", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")
        self.viewer = Actor("viewer-1", "viewer")
        self.event_id = publish_event(self.service)

    def tearDown(self):
        self.tmp.cleanup()

    def _open_order(self, data=None):
        payload = dict(ORDER_DATA)
        payload["event_id"] = self.event_id
        if data:
            payload.update(data)
        return self.service.create(self.analyst, "revision_order", payload)

    def test_return_and_resubmit_keeps_comments_and_count(self):
        order = self._open_order()
        returned = self.service.transition(
            self.reviewer,
            order["id"],
            "return",
            {"comment": "depth not supported by picks"},
        )
        self.assertEqual(returned["status"], "returned")
        self.assertEqual(len(returned["data"]["comments"]), 1)
        self.assertEqual(
            returned["data"]["comments"][0]["comment"],
            "depth not supported by picks",
        )

        # Analyst edits the *same* order and resubmits.
        resubmitted = self.service.transition(
            self.analyst,
            order["id"],
            "resubmit",
            {
                "basis": "corrected picks",
                "proposed_magnitude": 4.4,
                "proposed_location": "Region-A2",
                "proposed_depth_km": 15.0,
            },
        )
        self.assertEqual(resubmitted["status"], "pending")
        self.assertEqual(resubmitted["data"]["resubmit_count"], 1)
        # Reviewer comments survive the resubmission.
        self.assertEqual(len(resubmitted["data"]["comments"]), 1)

        self.service.transition(
            self.reviewer, order["id"], "return", {"comment": "still off"}
        )
        again = self.service.transition(
            self.analyst2,
            order["id"],
            "resubmit",
            dict(ORDER_DATA),
        )
        self.assertEqual(again["data"]["resubmit_count"], 2)
        self.assertEqual(len(again["data"]["comments"]), 2)

        approved = self.service.approve_revision_order(
            self.reviewer, order["id"], {"communication_id": "C-9"}
        )
        self.assertEqual(approved["status"], "approved")

        audit = self.service.audit_log(order["id"])
        actions = [row["action"] for row in audit]
        self.assertEqual(
            actions, ["create", "return", "resubmit", "return", "resubmit", "approve"]
        )
        self.assertEqual(
            audit[2]["detail"]["resubmit_count"], 1
        )

    def test_only_analyst_can_open_order(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(
                self.viewer,
                "revision_order",
                dict(ORDER_DATA, event_id=self.event_id),
            )

    def test_only_reviewer_can_approve_or_return(self):
        order = self._open_order()
        with self.assertRaises(PermissionDenied):
            self.service.approve_revision_order(
                self.analyst, order["id"], {"communication_id": "C-9"}
            )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.analyst, order["id"], "return", {"comment": "nope"}
            )

    def test_analyst_cannot_approve_and_reviewer_cannot_resubmit(self):
        order = self._open_order()
        self.service.transition(
            self.reviewer, order["id"], "return", {"comment": "redo"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.reviewer, order["id"], "resubmit", dict(ORDER_DATA)
            )

    def test_approve_requires_communication_id(self):
        order = self._open_order()
        with self.assertRaises(ValidationError):
            self.service.approve_revision_order(self.reviewer, order["id"], {})

    def test_order_requires_basis_and_proposal_fields(self):
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst,
                "revision_order",
                {"event_id": self.event_id, "basis": ""},
            )
        with self.assertRaises(ValidationError):
            self.service.create(
                self.analyst,
                "revision_order",
                dict(ORDER_DATA, event_id=self.event_id, proposed_magnitude=99),
            )

    def test_order_only_targets_published_or_revised_events(self):
        candidate = self.service.create(
            self.analyst,
            "event",
            {
                "title": "Event-B",
                "origin_time": "2026-02-01T00:00:00Z",
                "location": "Region-B",
                "reports": [
                    {"station": "X", "time_offset": 1, "distance_km": 0.5},
                    {"station": "Y", "time_offset": 2, "distance_km": 0.6},
                ],
            },
        )
        with self.assertRaises(InvalidTransition):
            self.service.create(
                self.analyst,
                "revision_order",
                dict(ORDER_DATA, event_id=candidate["id"]),
            )
        with self.assertRaises(NotFoundError):
            self.service.create(
                self.analyst,
                "revision_order",
                dict(ORDER_DATA, event_id="missing"),
            )

    def test_stale_order_rejected_when_event_revised_elsewhere(self):
        first = self._open_order()
        second = self._open_order()
        # Both orders are based on the published version. Approving the first
        # moves the event; approving the second must be refused.
        self.service.approve_revision_order(
            self.reviewer, first["id"], {"communication_id": "C-2"}
        )
        with self.assertRaises(ConflictError):
            self.service.approve_revision_order(
                self.reviewer, second["id"], {"communication_id": "C-3"}
            )
        self.assertEqual(
            self.service.get(second["id"])["status"], "pending"
        )

    def test_resubmit_rebases_order_to_latest_event_version(self):
        first = self._open_order()
        second = self._open_order()
        self.service.approve_revision_order(
            self.reviewer, first["id"], {"communication_id": "C-2"}
        )
        self.service.transition(
            self.reviewer, second["id"], "return", {"comment": "stale base"}
        )
        resubmitted = self.service.transition(
            self.analyst, second["id"], "resubmit", dict(ORDER_DATA)
        )
        current_event_version = self.service.get(self.event_id)["version"]
        self.assertEqual(
            resubmitted["data"]["event_version"], current_event_version
        )
        self.service.approve_revision_order(
            self.reviewer, second["id"], {"communication_id": "C-3"}
        )
        snapshots = self.service.event_versions(self.event_id)
        self.assertEqual(
            [s["source"] for s in snapshots],
            ["publish", "revision_order", "revision_order"],
        )

    def test_order_version_conflict_on_approve(self):
        order = self._open_order()
        with self.assertRaises(ConflictError):
            self.service.approve_revision_order(
                self.reviewer,
                order["id"],
                {"communication_id": "C-2"},
                expected_version=order["version"] + 50,
            )

    def test_cannot_act_on_order_in_wrong_state(self):
        order = self._open_order()
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.analyst, order["id"], "resubmit", dict(ORDER_DATA)
            )
        self.service.approve_revision_order(
            self.reviewer, order["id"], {"communication_id": "C-2"}
        )
        with self.assertRaises((ConflictError, InvalidTransition)):
            self.service.approve_revision_order(
                self.reviewer, order["id"], {"communication_id": "C-3"}
            )

    def test_direct_revise_action_no_longer_exists(self):
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.reviewer,
                self.event_id,
                "revise",
                {"reason": "sneaky", "magnitude": 9.0},
            )

    def test_pending_orders_do_not_touch_current_catalog(self):
        self._open_order()
        event = self.service.get(self.event_id)
        self.assertEqual(event["status"], "published")
        self.assertEqual(event["data"]["magnitude"], 4.2)
        # Only the publish snapshot exists; no revision snapshot yet.
        snapshots = self.service.event_versions(self.event_id)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["source"], "publish")


if __name__ == "__main__":
    unittest.main()

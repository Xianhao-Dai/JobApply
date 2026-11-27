"""User-action blockers wait for targeted resumption in a disposable ledger."""
import unittest
from datetime import timedelta

import test_jobapply_workflow_retention as fixtures
from jobapply_ledger import IMMEDIATE_PREPARATION


class UserActionGateTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.WorkflowRetentionTests(methodName="runTest")
        self.fx.setUp()
        self.fx.fresh_qualified(policy=True)
        self.fx.pdf()
        self.fx.ledger._setting("preparation_timing_policy", IMMEDIATE_PREPARATION)

    def tearDown(self):
        self.fx.tearDown()

    def test_automatic_user_blockers_wait_but_explicit_targets_and_technical_retries_continue(self):
        for code in ("AUTHENTICATION_REQUIRED", "USER_REQUIRED_ANSWERS",
                     "USER_PROFILE_UPDATE_REQUIRED", "TEMPORARY_UNAVAILABLE"):
            with self.subTest(blocker_code=code):
                self.fx.ledger.apply_event({
                    "event_type": "application_form_blocked", "job_id": self.fx.job_id,
                    "source_kind": "orchestrator", "source_ref": "fixture_" + code,
                    "occurred_at": fixtures.stamp(self.fx.now), "blocker_code": code,
                    "next_action": "Resume the existing official form when the prerequisite is resolved",
                })
                automatic = self.fx.tasks("prepare")
                if code == "TEMPORARY_UNAVAILABLE":
                    self.assertEqual([task["job_id"] for task in automatic], [self.fx.job_id])
                else:
                    self.assertEqual(automatic, [])
                    targeted = self.fx.tasks("prepare", job_ids=[self.fx.job_id])
                    self.assertEqual([task["job_id"] for task in targeted], [self.fx.job_id])
                    self.assertTrue(self.fx.ledger._input_is_current(targeted[0]))

    def test_retry_cooldown_survives_application_version_change_and_allows_targeted_resume(self):
        for targeted, advance, bump_version in (
                (False, timedelta(minutes=14), True),
                (True, timedelta(minutes=1), True),
                (False, timedelta(minutes=15), True),
                (True, timedelta(minutes=1), False)):
            with self.subTest(targeted=targeted, advance=advance, bump_version=bump_version):
                fx = fixtures.WorkflowRetentionTests(methodName="runTest")
                fx.setUp()
                try:
                    fx.fresh_qualified(policy=True)
                    fx.pdf()
                    fx.ledger._setting("preparation_timing_policy", IMMEDIATE_PREPARATION)
                    original = fx.tasks("prepare")[0]
                    result = fx.result(original, {})
                    result.update(status="retryable_error", issue={
                        "code": "TEMPORARY_UNAVAILABLE", "message": "Official form Save failed",
                        "retry_after_at": fixtures.stamp(fx.now + timedelta(minutes=15)),
                    })
                    self.assertEqual(fx.ledger.accept(result)["outcome"], "retry_wait")
                    if bump_version:
                        fx.ledger.apply_event({
                            "event_type": "application_form_blocked", "job_id": fx.job_id,
                            "source_kind": "orchestrator", "source_ref": "fixture_save_failed",
                            "occurred_at": fixtures.stamp(fx.now),
                            "blocker_code": "TEMPORARY_UNAVAILABLE",
                            "next_action": "Resume saved form after the cooldown",
                        })
                    tasks = fx.tasks("prepare", fx.now + advance,
                                     job_ids=[fx.job_id] if targeted else None)
                    if not targeted and advance < timedelta(minutes=15):
                        self.assertEqual(tasks, [])
                    else:
                        self.assertEqual([task["job_id"] for task in tasks], [fx.job_id])
                        self.assertTrue(fx.ledger._input_is_current(tasks[0]))
                        if bump_version:
                            self.assertNotEqual(tasks[0]["task_id"], original["task_id"])
                            self.assertGreater(tasks[0]["input_versions"]["application_version"],
                                               original["input_versions"]["application_version"])
                        else:
                            self.assertEqual(tasks[0]["task_id"], original["task_id"])
                            self.assertEqual(tasks[0]["attempt"], 2)
                finally:
                    fx.tearDown()


if __name__ == "__main__":
    unittest.main()

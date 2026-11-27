"""Immediate preparation, saved-form reuse, and safe retries in disposable ledgers."""
import json
import unittest
from datetime import datetime, time, timedelta

import test_jobapply_workflow_retention as fixtures
from jobapply_ledger import (Conflict, IMMEDIATE_PREPARATION, PT, SCHEDULED_PREPARATION,
                            current_preparation_form, preparation_due, preparation_timing_policy)


class ImmediatePreparationTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.WorkflowRetentionTests(methodName="runTest")
        self.fx.setUp()
        self.fx.fresh_qualified(policy=True)
        self.fx.pdf()
        self.root, self.ledger, self.now, self.job_id = self.fx.root, self.fx.ledger, self.fx.now, self.fx.job_id

    def tearDown(self):
        self.fx.tearDown()

    def immediate(self):
        self.ledger._setting("preparation_timing_policy", IMMEDIATE_PREPARATION)

    def job(self):
        return dict(self.ledger.db.execute("SELECT * FROM jobs WHERE job_id=?", (self.job_id,)).fetchone())

    def application(self):
        row = self.ledger.db.execute("SELECT * FROM applications WHERE job_id=?", (self.job_id,)).fetchone()
        return dict(row) if row else None

    def referral(self, state, url=None):
        self.ledger.db.execute("""INSERT INTO job_referrals(job_id,referral_state,referral_url,evidence_ref,updated_at)
            VALUES (?,?,?,?,?) ON CONFLICT(job_id) DO UPDATE SET referral_state=excluded.referral_state,
            referral_url=excluded.referral_url,evidence_ref=excluded.evidence_ref""",
            (self.job_id, state, url, "Exact-requisition referral evidence fixture", fixtures.stamp(self.now)))

    def complete(self, task, disposition, *, status="succeeded", **output):
        result = self.fx.result(task, {"disposition": disposition, **output})
        result["result_id"] += "_" + str(task["attempt"])
        result["status"] = status
        if status != "succeeded":
            result["issue"] = {"code": "FIXTURE_BLOCK", "message": "User verification needed"}
        accepted = self.ledger.accept(result)
        self.assertIn(accepted["outcome"], {"succeeded", "applied", "blocked"})
        self.assertEqual(self.ledger.db.execute("SELECT state FROM tasks WHERE task_id=?", (task["task_id"],)).fetchone()[0], status)
        path = self.root / "state/staging" / task["task_id"] / str(task["attempt"]) / "result.json"
        path.write_text(json.dumps(result))
        return result

    def test_immediate_morning_ignores_legacy_handoff_and_scheduled_default_remains(self):
        morning = datetime.combine(self.now.astimezone(PT).date() + timedelta(days=1), time(9), PT)
        self.ledger.db.execute("UPDATE jobs SET handoff_at=?", (fixtures.stamp(morning + timedelta(days=7)),))
        self.assertEqual(preparation_timing_policy(self.ledger.db), SCHEDULED_PREPARATION)
        self.assertFalse(preparation_due(self.job(), morning, SCHEDULED_PREPARATION))
        self.assertEqual(self.fx.tasks("prepare", morning), [])
        self.immediate()
        self.assertTrue(preparation_due(self.job(), morning, IMMEDIATE_PREPARATION))
        task = self.fx.tasks("prepare", morning)[0]
        self.assertEqual(task["input"]["preparation_timing_policy"], IMMEDIATE_PREPARATION)
        self.assertEqual(task["input"]["route"], "direct")
        self.assertTrue(self.ledger._input_is_current(task))
        queue = self.fx.report_queue()[0]
        self.assertIsNone(queue["handoff_at_pt"])
        self.assertIsNone(queue["referral_cutoff_at_pt"])
        self.assertIn("立即准备", queue["next_action"])

    def test_requested_referral_does_not_delay_immediate_direct_preparation(self):
        self.immediate()
        self.referral("requested")
        self.ledger.db.execute("UPDATE jobs SET referral_cutoff_at=?,handoff_at=?",
                               (fixtures.stamp(self.now + timedelta(days=2)), fixtures.stamp(self.now + timedelta(days=2))))
        task = self.fx.tasks("prepare")[0]
        self.assertEqual(task["input"]["route"], "direct")
        self.assertEqual(self.ledger.db.execute("SELECT referral_state FROM job_referrals").fetchone()[0], "requested")

    def test_valid_referral_preferred_explicit_direct_respected_and_employee_submission_blocks(self):
        self.immediate()
        link = self.job()["apply_url"] + "?ref=verified-fixture"
        self.referral("link_received", link)
        referral_task = self.fx.tasks("prepare")[0]
        self.assertEqual(referral_task["input"]["route"], "referral_link")
        self.assertEqual(referral_task["input"]["referral_url"], link)
        self.fx.fixture.save_route_preference()
        direct_task = self.fx.tasks("prepare")[0]
        self.assertEqual(direct_task["input"]["route"], "direct")
        self.referral("employee_referred", link)
        self.assertEqual(self.fx.tasks("prepare"), [])
        self.assertFalse(self.ledger._input_is_current(direct_task))
        with self.assertRaises(Conflict):
            self.ledger.dispatch({**direct_task, "task_id": "t_employee_duplicate_guard"})

    def test_ready_current_form_is_not_dispatched_again_even_if_referral_arrives_late(self):
        self.immediate()
        task = self.fx.tasks("prepare")[0]
        form = "https://payments.example/careers/apply/saved-fixture"
        self.complete(task, "ready_for_user", form_ref=form, next_action="Review saved form and click Submit")
        before = self.application()
        self.referral("link_received", self.job()["apply_url"] + "?ref=late")
        self.assertEqual(self.fx.tasks("prepare"), [])
        self.assertEqual(self.application(), before)
        saved = current_preparation_form(before, 1, "r_retained")
        self.assertEqual(saved["route_fingerprint"], task["input_versions"]["route_fingerprint"])
        self.assertEqual(saved["form_ref"], form)
        queue = self.fx.report_queue()[0]
        self.assertEqual(queue["route"], "direct")
        self.assertEqual(queue["next_action"], "Review saved form and click Submit")
        self.assertIn("新增内推入口待核对切换", queue["referral_summary"])
        clone = {**task, "task_id": "t_do_not_reopen_ready", "input_versions":
                 {**task["input_versions"], "application_version": before["row_version"]}}
        with self.assertRaises(Conflict):
            self.ledger.dispatch(clone)

    def test_blocked_form_resumes_original_route_fingerprint_despite_late_referral(self):
        self.immediate()
        task = self.fx.tasks("prepare")[0]
        form = "https://payments.example/careers/apply/partial-fixture"
        self.complete(task, "form_blocked", form_ref=form, blocker_code="EMAIL_VERIFICATION_REQUIRED",
                      next_action="Verify account, then resume this form")
        self.referral("link_received", self.job()["apply_url"] + "?ref=late")
        resumed = self.fx.tasks("prepare")[0]
        self.assertEqual(resumed["input"]["route"], "direct")
        self.assertEqual(resumed["input"]["existing_form"]["form_ref"], form)
        self.assertEqual(resumed["input_versions"]["route_fingerprint"], task["input_versions"]["route_fingerprint"])
        # A worker that hits another verification step must not erase progress
        # merely because it omitted the already-known form reference.
        self.complete(resumed, "form_blocked", blocker_code="CAPTCHA_REQUIRED", next_action="Complete verification")
        self.assertEqual(self.application()["form_ref"], form)
        self.assertEqual(self.fx.tasks("prepare")[0]["input"]["existing_form"]["form_ref"], form)

    def test_stage_blocked_retry_uses_new_attempt_even_with_existing_result_file(self):
        self.immediate()
        task = self.fx.tasks("prepare")[0]
        self.complete(task, "verification_unavailable", status="blocked")
        retry = self.fx.tasks("prepare")[0]
        self.assertEqual(retry["task_id"], task["task_id"])
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["input"], task["input"])
        self.assertTrue((self.root / "state/staging" / task["task_id"] / "1/result.json").exists())
        self.assertTrue((self.root / "state/staging" / task["task_id"] / "2/task.json").exists())

    def test_retry_wait_honors_backoff_and_then_resumes_existing_form(self):
        self.immediate()
        first = self.fx.tasks("prepare")[0]
        self.complete(first, "form_blocked", form_ref="https://payments.example/careers/apply/retry-fixture",
                      blocker_code="LOGIN_REQUIRED", next_action="Resume login")
        task = self.fx.tasks("prepare")[0]
        result = self.fx.result(task, {})
        result.update(result_id="res_retryable_" + task["task_id"], status="retryable_error",
                      issue={"code": "TEMPORARY_UNAVAILABLE", "message": "Retry later",
                             "retry_after_at": fixtures.stamp(self.now + timedelta(minutes=5))})
        self.assertEqual(self.ledger.accept(result)["outcome"], "retry_wait")
        target = self.root / "state/staging" / task["task_id"] / "1/result.json"
        target.write_text(json.dumps(result))
        self.assertEqual(self.fx.tasks("prepare", self.now + timedelta(minutes=4)), [])
        retry = self.fx.tasks("prepare", self.now + timedelta(minutes=5))[0]
        self.assertEqual(retry["task_id"], task["task_id"])
        self.assertEqual(retry["attempt"], 2)
        self.assertEqual(retry["input"]["existing_form"], task["input"]["existing_form"])

    def test_immediate_does_not_bypass_open_deadline_match_pdf_duplicate_or_terminal_guards(self):
        self.immediate()
        for field, value in (("listing_state", "closed"), ("listing_state", "unknown"),
                             ("closes_at", fixtures.stamp(self.now - timedelta(seconds=1)))):
            with self.subTest(field=field, value=value):
                self.ledger.db.execute(f"UPDATE jobs SET {field}=?", (value,))
                self.assertEqual(self.fx.tasks("prepare"), [])
                self.ledger.db.execute("UPDATE jobs SET listing_state='open',closes_at=NULL")
        self.ledger.db.execute("UPDATE matches SET decision='needs_review'")
        self.assertEqual(self.fx.tasks("prepare"), [])
        self.ledger.db.execute("UPDATE matches SET decision='no_changes'")
        self.ledger.db.execute("UPDATE settings SET value_json='2' WHERE key='candidate_constraints_revision'")
        self.assertEqual(self.fx.tasks("prepare"), [])
        self.ledger.db.execute("UPDATE settings SET value_json='1' WHERE key='candidate_constraints_revision'")
        pdf = self.root / self.ledger.db.execute("SELECT pdf_path FROM resumes WHERE resume_id='r_retained'").fetchone()[0]
        original = pdf.read_bytes()
        pdf.write_bytes(original + b"changed")
        with self.assertRaises(Conflict):
            self.fx.tasks("prepare")
        pdf.write_bytes(original)
        self.ledger.apply_event({"event_type": "application_form_blocked", "job_id": self.job_id,
                                 "source_kind": "orchestrator", "source_ref": "unresolved-duplicate",
                                 "occurred_at": fixtures.stamp(self.now), "blocker_code": "POSSIBLE_DUPLICATE_APPLICATION"})
        self.assertEqual(self.fx.tasks("prepare"), [])
        self.ledger.db.execute("UPDATE applications SET blocker_code=NULL,route='employee_submission'")
        self.assertEqual(self.fx.tasks("prepare"), [])
        self.ledger.db.execute("UPDATE applications SET route='direct'")
        for event_type in ("application_submit_clicked", "application_received", "application_withdrawn"):
            self.ledger.apply_event({"event_type": event_type, "job_id": self.job_id, "source_kind": "ats",
                                     "source_ref": event_type, "occurred_at": fixtures.stamp(self.now),
                                     "evidence_ref": "Exact-requisition terminal-state fixture"})
            self.assertEqual(self.fx.tasks("prepare"), [])


if __name__ == "__main__":
    unittest.main()

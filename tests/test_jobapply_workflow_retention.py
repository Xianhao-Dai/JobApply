"""Retained workflow behavior in disposable ledgers, never the live workspace."""
import io
import json
import sqlite3
import unittest
from contextlib import redirect_stdout
from datetime import datetime, time, timedelta, timezone
from unittest.mock import patch

import jobapply_batch
import jobapply_views
from jobapply_freshness import dispositions, workflow_dispositions
from jobapply_ledger import BASE, Conflict, IDENTITY, Ledger, PT, digest_file
import test_jobapply_repost_pipeline as fixtures

stamp = fixtures.stamp


class WorkflowRetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RepostPipelineTests(methodName="runTest")
        self.fixture.setUp()
        self.root, self.ledger, self.now = self.fixture.root, self.fixture.ledger, self.fixture.now
        self.job_id = self.fixture.job_id

    def tearDown(self):
        self.fixture.ledger = self.ledger
        self.fixture.tearDown()

    def fresh_qualified(self, *, policy=False):
        if policy:
            self.ledger.enable_workflow_retention([])
            self.now = self.fixture.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.fixture.seed_original()
        recent = stamp(self.now - timedelta(hours=2))
        self.ledger.db.execute("UPDATE jobs SET published_at=?,first_seen_at=? WHERE job_id=?",
                               (recent, recent, self.job_id))
        self.fixture.seed_qualified_match()

    def jobs(self):
        return [dict(row) for row in self.ledger.db.execute("SELECT * FROM jobs")]

    def admissions(self):
        return [dict(row) for row in self.ledger.db.execute("SELECT * FROM job_workflow_admissions")]

    def tasks(self, stage, at=None, job_ids=None):
        output = io.StringIO()
        with (patch.object(jobapply_batch, "ROOT", self.root),
              patch.object(jobapply_batch, "now_utc", return_value=at or self.now), redirect_stdout(output)):
            jobapply_batch.draft_tasks(self.ledger, stage, job_ids or [], 10)
        return [json.loads((self.root / line).read_text()) for line in output.getvalue().splitlines()]

    def result(self, task, output, artifacts=None):
        result = {key: task[key] for key in IDENTITY}
        result.update(result_id="res_" + task["task_id"], status="succeeded", completed_at=stamp(self.now),
                      output=output, artifacts=artifacts or [], evidence=[])
        return result

    def pdf(self):
        task = self.tasks("tailor")[0]
        target = self.root / "state/staging" / task["task_id"] / "1/fixture.pdf"
        target.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n")
        result = self.result(task, {"resume_id": "r_retained", "graduation_month": "2030-05",
                                    "pdf_verified": True},
                             [{"kind": "pdf", "path": str(target.relative_to(self.root)),
                               "sha256": digest_file(target)}])
        self.assertEqual(self.ledger.accept(result)["outcome"], "succeeded")

    def next_evening(self):
        day = self.now.astimezone(PT).date() + timedelta(days=1)
        return datetime.combine(day, time(20), tzinfo=PT).astimezone(timezone.utc)

    def report_queue(self, at=None):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return (at or datetime.now(timezone.utc)).astimezone(tz)
        with patch.object(jobapply_views, "ROOT", self.root), patch.object(jobapply_views, "datetime", Clock):
            books = jobapply_views.export()
        tracker = next(book for book in books if book["path"] == "applications/tracker.xlsx")
        queue = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Queue")
        return [dict(zip(queue["columns"], row)) for row in queue["rows"]]

    def test_admitted_job_prepares_next_day_at_20_and_tracks_without_contact_or_application(self):
        self.fresh_qualified(policy=True)
        self.assertEqual(self.admissions()[0]["admission_reason"], "matched")
        self.pdf()
        evening = self.next_evening()
        self.assertEqual(dispositions(self.ledger.db, self.jobs(), evening)[self.job_id], "older_than_24h")
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), evening)[self.job_id], "active")
        self.assertEqual(self.tasks("prepare", evening - timedelta(minutes=1)), [])
        prepared = self.tasks("prepare", evening)
        self.assertEqual([task["job_id"] for task in prepared], [self.job_id])
        self.assertTrue(self.ledger._input_is_current(prepared[0]))
        self.assertEqual([task["job_id"] for task in self.tasks("track", evening)], [self.job_id])

    def test_policy_transition_is_explicit_atomic_idempotent_and_can_use_confirmed_time(self):
        self.fresh_qualified()
        with self.assertRaises(Conflict):
            self.ledger.enable_workflow_retention([self.job_id, "missing-job"])
        self.assertEqual(self.admissions(), [])
        self.assertIsNone(self.ledger.setting("workflow_retention_policy"))
        # Publication has crossed the boundary now, but was still eligible at
        # the user's saved confirmation. No historical all-match backfill.
        confirmed = self.now - timedelta(minutes=1)
        old = stamp(self.now - timedelta(hours=24, seconds=30))
        self.ledger.db.execute("UPDATE jobs SET published_at=?,first_seen_at=?", (old, old))
        self.ledger.db.execute("UPDATE matches SET decided_at=?", (stamp(confirmed),))
        with self.assertRaises(Conflict):
            self.ledger.enable_workflow_retention([self.job_id])
        first = self.ledger.enable_workflow_retention([self.job_id], confirmed_at=stamp(confirmed))
        again = self.ledger.enable_workflow_retention([self.job_id], confirmed_at=stamp(confirmed))
        self.assertEqual(first, again)
        self.assertEqual(self.admissions()[0]["admitted_at"], stamp(confirmed))
        self.assertEqual(self.admissions()[0]["admission_reason"], "policy_transition")

    def test_upgrade_never_backfills_historical_qualified_matches(self):
        self.fresh_qualified()
        self.ledger.close()
        with sqlite3.connect(self.root / "state/jobapply.sqlite") as db:
            db.execute("DROP TABLE job_workflow_admissions")
            db.execute("PRAGMA user_version=2")
        self.ledger = Ledger(self.root)
        self.assertEqual(self.ledger.db.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertEqual(self.admissions(), [])
        self.ledger.enable_workflow_retention([])
        self.assertEqual(self.tasks("tailor", self.next_evening()), [])
        self.assertEqual(self.tasks("prepare", self.next_evening()), [])

    def test_admission_and_policy_cannot_appear_before_their_recorded_time(self):
        self.fresh_qualified(policy=True)
        ancient = stamp(self.now - timedelta(days=2))
        self.ledger.db.execute("UPDATE jobs SET published_at=?,first_seen_at=?", (ancient, ancient))
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), self.now)[self.job_id], "active")
        earlier = self.now - timedelta(minutes=1)
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), earlier)[self.job_id], "older_than_24h")
        self.ledger.db.execute("UPDATE settings SET value_json=? WHERE key='workflow_retention_enabled_at'",
                               (json.dumps(stamp(earlier - timedelta(minutes=1))),))
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), earlier)[self.job_id], "older_than_24h")

    def test_only_qualified_current_match_results_admit_and_replay_is_idempotent(self):
        self.fresh_qualified(policy=True)
        for decision in ("needs_edits", "needs_review", "not_fit"):
            role = self.fixture.observation(source_id="role-" + decision, official_req_id="req-" + decision,
                                            observed=self.now, published_at=stamp(self.now - timedelta(hours=1)))
            discovered = self.fixture.discover("discover_" + decision, role, jd=True)
            job_id = discovered["job_ids"][0]
            task = self.tasks("match", job_ids=[job_id])[0]
            result = self.result(task, {"decision": decision, "hr_findings": [], "hm_findings": [],
                                        "edit_plan": [], "open_questions": []})
            self.assertEqual(self.ledger.accept(result)["outcome"], "succeeded")
            before = self.admissions()
            self.assertEqual(self.ledger.accept(result)["outcome"], "duplicate")
            self.assertEqual(self.admissions(), before)
            self.assertEqual(job_id in {row["job_id"] for row in before}, decision == "needs_edits")
        self.assertEqual(len(self.admissions()), 2)

    def test_retained_views_preserve_effective_repost_date_across_discovery_ageout(self):
        self.fresh_qualified(policy=True)
        original = stamp(self.now - timedelta(days=60))
        self.ledger.db.execute("UPDATE jobs SET published_at=?,first_seen_at=?", (original, original))
        repost = self.fixture.observation(source_id="retained-repost", observed=self.now,
                                          published_at_as_listed="Reposted 1 hour ago")
        self.fixture.discover("retained_repost", repost, source="linkedin:jobs")
        current = self.report_queue(self.now)[0]
        later = self.report_queue(self.next_evening())[0]
        self.assertEqual(current["freshness_basis"], "source_repost")
        self.assertEqual(current["effective_posted_at_pt"], later["effective_posted_at_pt"])
        self.assertEqual(later["discovery_status"], "older_than_24h")
        self.assertTrue(later["workflow_admitted_at_pt"])
        self.assertEqual((self.root / "positions/discovered.csv").read_text().count("\n"), 1)

    def test_closed_unknown_real_deadline_pause_without_deleting_admission(self):
        self.fresh_qualified(policy=True)
        self.pdf()
        before = self.admissions()
        for field, value, expected in (("listing_state", "closed", "closed"),
                                       ("listing_state", "unknown", "open_status_unverified"),
                                       ("closes_at", stamp(self.now - timedelta(seconds=1)), "closed")):
            with self.subTest(field=field, value=value):
                self.ledger.db.execute("UPDATE jobs SET listing_state='open',closes_at=NULL")
                self.ledger.db.execute(f"UPDATE jobs SET {field}=?", (value,))
                self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), self.next_evening())[self.job_id], expected)
                self.assertEqual(self.tasks("prepare", self.next_evening()), [])
                self.assertEqual(self.tasks("connections"), [])
                self.assertEqual(self.admissions(), before)
        self.ledger.db.execute("UPDATE jobs SET listing_state='open',closes_at=NULL")
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), self.next_evening())[self.job_id], "active")

    def test_latest_official_unknown_wins_and_paused_job_stays_visible(self):
        self.fresh_qualified(policy=True)
        self.pdf()
        self.ledger.apply_event({"event_type": "application_form_ready", "job_id": self.job_id,
                                 "source_kind": "orchestrator", "source_ref": "ready-before-unknown",
                                 "occurred_at": stamp(self.now), "resume_id": "r_retained", "route": "direct",
                                 "form_ref": "fixture-tab", "next_action": "Review and click Submit"})
        self.ledger.db.execute("""CREATE TABLE job_freshness_rechecks
            (job_id TEXT PRIMARY KEY,observed_at TEXT,live_state TEXT,listed_by_at TEXT)""")
        self.ledger.db.execute("INSERT INTO job_freshness_rechecks VALUES (?,?,?,NULL)",
                               (self.job_id, stamp(self.now), "unknown"))
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), self.next_evening())[self.job_id],
                         "open_status_unverified")
        self.assertEqual(self.tasks("tailor"), [])
        queue = self.report_queue()
        self.assertEqual(queue[0]["blocker_code"], "OFFICIAL_STATUS_UNVERIFIED")
        self.assertTrue(queue[0]["workflow_admitted_at_pt"])
        with patch.object(jobapply_views, "ROOT", self.root):
            books = jobapply_views.export()
        tracker = next(book for book in books if book["path"] == "applications/tracker.xlsx")
        status = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Status")
        row = dict(zip(status["columns"], status["rows"][0]))
        self.assertEqual(row["application_state"], "ready_for_user")
        self.assertIn("暂停使用", row["next_action"])
        self.assertNotIn("Submit", row["next_action"])
        self.ledger.db.execute("UPDATE jobs SET last_verified_at=?", (stamp(self.now + timedelta(seconds=1)),))
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), self.next_evening())[self.job_id], "active")

    def test_manual_withdrawal_requires_evidence_and_repost_cannot_reactivate(self):
        self.fresh_qualified(policy=True)
        update = {"update_id": "u_withdraw", "action_code": "application_withdrawn", "job_id": self.job_id,
                  "occurred_at_pt": stamp(self.now), "evidence_kind": "user_report", "expected_version": 0}
        self.assertEqual(self.ledger.apply_manual(update)["import_status"], "格式错误")
        update["evidence_ref"] = "User explicitly abandoned this exact requisition"
        self.assertEqual(self.ledger.apply_manual(update)["import_status"], "已入账")
        self.assertEqual(self.ledger.apply_manual(update)["import_status"], "已处理过")
        repost = self.fixture.observation(source_id="withdrawn-repost", observed=self.now,
                                          published_at_as_listed="Reposted 1 hour ago")
        self.fixture.discover("withdrawn_repost", repost, source="linkedin:jobs")
        self.assertEqual(dispositions(self.ledger.db, self.jobs(), self.now)[self.job_id], "active")
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), self.now)[self.job_id], "withdrawn")
        for stage in ("match", "tailor", "connections", "prepare"):
            self.assertEqual(self.tasks(stage, self.next_evening()), [])
        self.assertEqual(len(self.admissions()), 1)
        self.assertEqual(self.report_queue(), [])

    def test_base_change_and_constraints_change_require_current_match_and_pdf(self):
        self.fresh_qualified(policy=True)
        self.pdf()
        connection = self.tasks("connections")[0]
        self.ledger.db.execute("UPDATE settings SET value_json='2' WHERE key='candidate_constraints_revision'")
        self.assertFalse(self.ledger._input_is_current(connection))
        self.assertEqual(self.tasks("prepare", self.next_evening()), [])
        rematch = self.tasks("match", self.next_evening())[0]
        self.assertEqual(rematch["input_versions"]["candidate_constraints_revision"], 2)
        result = self.result(rematch, {"decision": "needs_review", "hr_findings": [], "hm_findings": [],
                                       "edit_plan": [], "open_questions": []})
        self.assertEqual(self.ledger.accept(result)["outcome"], "succeeded")
        self.assertEqual(self.tasks("prepare", self.next_evening()), [])
        self.assertEqual(self.tasks("connections"), [])
        self.assertEqual(len(self.admissions()), 1)
        self.ledger.db.execute("UPDATE settings SET value_json='1' WHERE key='candidate_constraints_revision'")
        base = self.root / BASE
        base.write_bytes(base.read_bytes() + b"changed-content")
        self.ledger.register_base("2030-05")
        self.assertEqual(self.tasks("prepare", self.next_evening()), [])
        new_match = self.tasks("match", self.next_evening())[0]
        self.assertNotEqual(new_match["input_versions"]["base_resume_id"], rematch["input_versions"]["base_resume_id"])

    def test_retention_does_not_override_duplicate_or_submitted_guards(self):
        self.fresh_qualified(policy=True)
        self.pdf()
        self.ledger.apply_event({"event_type": "application_form_blocked", "job_id": self.job_id,
                                 "source_kind": "orchestrator", "source_ref": "duplicate-fixture",
                                 "occurred_at": stamp(self.now), "blocker_code": "POSSIBLE_DUPLICATE_APPLICATION",
                                 "next_action": "Verify prior receipt"})
        self.assertEqual(self.tasks("prepare", self.next_evening()), [])
        for state in ("submission_pending_verification", "submitted", "interviewing", "rejected", "offer"):
            with self.subTest(state=state):
                # Use actual verified events for transition and pending linkage.
                event = {"event_type": "application_submit_clicked" if state == "submission_pending_verification"
                         else {"submitted": "application_received", "interviewing": "application_interviewing",
                               "rejected": "application_rejected", "offer": "application_offer_received"}[state],
                         "job_id": self.job_id, "source_kind": "ats", "source_ref": "fixture-" + state,
                         "occurred_at": stamp(self.now), "evidence_ref": "Exact requisition status"}
                self.ledger.apply_event(event)
                self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(), self.next_evening())[self.job_id], "tracking")
                self.assertEqual(self.tasks("prepare", self.next_evening()), [])
                self.assertEqual(self.tasks("connections"), [])


if __name__ == "__main__":
    unittest.main()

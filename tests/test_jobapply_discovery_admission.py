"""Discovery-window admission must not expire while matching is in flight."""
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jobapply_batch
from jobapply_freshness import SOURCE_CONFIG_REVISION, dispositions, match_dispositions, workflow_dispositions
from jobapply_ledger import BASE, Conflict, IDENTITY
import test_jobapply_repost_pipeline as fixtures

stamp = fixtures.stamp


class DiscoveryAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fx = fixtures.RepostPipelineTests(methodName="runTest")
        self.fx.setUp()
        self.ledger, self.root = self.fx.ledger, self.fx.root
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.ledger.enable_workflow_retention([])
        self.ledger._setting("workflow_retention_enabled_at", stamp(self.now - timedelta(hours=1)))
        base = self.root / BASE
        base.parent.mkdir()
        with ZipFile(base, "w") as document:
            document.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Disposable resume</w:t></w:r></w:p></w:body></w:document>')
        self.ledger.register_base("2030-05")

    def tearDown(self):
        self.fx.tearDown()

    def discover(self, name="aged", *, raw="23 hours ago", policy_before=True,
                 source="newgrad-jobs:us", source_revision=2, **fields):
        observed = self.now - timedelta(minutes=10)
        until = observed - timedelta(minutes=1)
        if not policy_before:
            self.ledger._setting("workflow_retention_enabled_at", stamp(self.now - timedelta(minutes=1)))
        values = {"published_at_as_listed": raw, "published_at": None, "published_at_basis": None, **fields}
        observation = self.fx.observation(
            source_id=name, official_req_id="req-" + name, observed=observed, **values)
        task = {"schema_version": 1, "task_id": "t_discovery_" + name, "attempt": 1,
                "stage": "discover", "scope_key": source, "job_id": None,
                "input_versions": {"source_config_revision": source_revision, "scan_window_id": name},
                "deadline_at": stamp(self.now + timedelta(hours=1)),
                "input": {"source_key": source, "since_at": stamp(until - timedelta(hours=24)),
                          "until_at": stamp(until), "target_filters": "US SDE"}}
        path = self.root / "state/staging" / task["task_id"] / "1/jd.txt"
        path.parent.mkdir(parents=True)
        path.write_text("Software Engineer, New Grad\nProgramming and collaboration.\n")
        observation["jd_path"] = str(path.relative_to(self.root))
        self.ledger.dispatch(task)
        # The fixture is assembled after the simulated scan: restore the
        # dispatch time to the fixed scan start, before its HTTP observation.
        self.ledger.db.execute("UPDATE tasks SET created_at=? WHERE task_id=?",
                               (stamp(until), task["task_id"]))
        result = {key: task[key] for key in IDENTITY}
        result.update(result_id="res_" + task["task_id"], status="succeeded", completed_at=stamp(self.now),
                      output={"observations": [observation], "next_cursor": stamp(until)},
                      artifacts=[{"kind": "jd", "path": observation["jd_path"],
                                  "sha256": fixtures.digest_file(path)}],
                      evidence=[{"kind": "official_job", "ref": observation["official_url"],
                                 "observed_at": observation["observed_at"]}])
        accepted = self.ledger.accept(result)
        self.assertEqual(accepted["outcome"], "succeeded")
        return task, result, accepted["job_ids"][0]

    def jobs(self, job_id):
        return [dict(self.ledger.db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())]

    def tasks(self, stage, job_id):
        output = io.StringIO()
        with (patch.object(jobapply_batch, "ROOT", self.root),
              patch.object(jobapply_batch, "now_utc", return_value=self.now), redirect_stdout(output)):
            jobapply_batch.draft_tasks(self.ledger, stage, [job_id], 10)
        return [json.loads((self.root / line).read_text()) for line in output.getvalue().splitlines()]

    def match_result(self, task, decision="no_changes", status="succeeded"):
        result = {key: task[key] for key in IDENTITY}
        result.update(result_id="res_" + task["task_id"], status=status, completed_at=stamp(self.now),
                      output={"decision": decision, "hr_findings": [], "hm_findings": [],
                              "edit_plan": [], "open_questions": []}, artifacts=[], evidence=[])
        if status != "succeeded":
            result["issue"] = {"code": "TEMPORARY_TEST_FAILURE", "message": "Disposable matching failure",
                               "retry_after_at": stamp(self.now + timedelta(minutes=5)), "needs_user": False}
        return result

    def admitted(self, job_id):
        return self.ledger.setting("discovery_admission:" + job_id)

    def workflow_admitted(self, job_id):
        return self.ledger.db.execute("SELECT * FROM job_workflow_admissions WHERE job_id=?", (job_id,)).fetchone()

    def test_glassdoor_leads_require_posting_evidence(self):
        source = "glassdoor:jobs"
        _, _, job_id = self.discover(
            "glassdoor_recent", source=source, source_revision=SOURCE_CONFIG_REVISION,
            raw=stamp(self.now - timedelta(hours=2)),
            observed_url="https://alerts.example/jobs/example?jl=123",
        )
        self.assertEqual(self.admitted(job_id)["source_key"], source)
        self.assertEqual(dispositions(self.ledger.db, self.jobs(job_id), self.now)[job_id], "active")
        self.assertEqual(len(self.tasks("match", job_id)), 1)

        _, _, unverified_id = self.discover(
            "glassdoor_alert_only", source=source, source_revision=SOURCE_CONFIG_REVISION,
            raw=None, observed_url="https://alerts.example/jobs/example?jl=456",
        )
        self.assertIsNone(self.admitted(unverified_id))
        self.assertEqual(self.tasks("match", unverified_id), [])

    def test_23_hour_observation_survives_ten_minute_match_delay(self):
        task, _, job_id = self.discover()
        self.assertIsNotNone(self.admitted(job_id))
        self.assertNotEqual(dispositions(self.ledger.db, self.jobs(job_id), self.now)[job_id], "active")
        self.assertEqual(match_dispositions(self.ledger.db, self.jobs(job_id), self.now)[job_id], "active")
        self.assertNotEqual(workflow_dispositions(self.ledger.db, self.jobs(job_id), self.now)[job_id], "active")
        self.assertEqual(self.tasks("tailor", job_id), [])
        self.assertEqual(self.tasks("prepare", job_id), [])
        match = self.tasks("match", job_id)[0]
        self.assertTrue(self.ledger._input_is_current(match))
        self.assertEqual(self.ledger.accept(self.match_result(match))["outcome"], "succeeded")
        self.assertIsNotNone(self.workflow_admitted(job_id))
        self.assertEqual(workflow_dispositions(self.ledger.db, self.jobs(job_id), self.now)[job_id], "active")

    def test_only_qualified_success_upgrades_to_workflow(self):
        for decision, status in (("needs_edits", "succeeded"), ("needs_review", "succeeded"),
                                 ("not_fit", "succeeded"), ("no_changes", "retryable_error")):
            with self.subTest(decision=decision, status=status):
                _, _, job_id = self.discover(decision + "_" + status)
                match = self.tasks("match", job_id)[0]
                self.ledger.accept(self.match_result(match, decision, status))
                self.assertEqual(bool(self.workflow_admitted(job_id)), status == "succeeded" and decision == "needs_edits")

    def test_snapshot_backfill_is_exact_idempotent_and_not_pre_policy(self):
        source, _, job_id = self.discover()
        self.ledger.db.execute("DELETE FROM settings WHERE key=?", ("discovery_admission:" + job_id,))
        self.ledger.admit_discovery_candidates(source["task_id"], [job_id])
        self.assertIsNotNone(self.admitted(job_id))
        again = self.admitted(job_id)
        self.ledger.admit_discovery_candidates(source["task_id"], [job_id])
        self.assertEqual(self.admitted(job_id), again)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM job_workflow_admissions").fetchone()[0], 0)
        with self.assertRaises(Conflict):
            self.ledger.admit_discovery_candidates(source["task_id"], ["not-in-source"])
        old, _, old_id = self.discover("prepolicy", policy_before=False)
        self.assertIsNone(self.admitted(old_id))
        with self.assertRaises(Conflict):
            self.ledger.admit_discovery_candidates(old["task_id"], [old_id])

    def test_ambiguous_future_and_known_old_refresh_do_not_admit(self):
        variants = [
            ("ambiguous", {"raw": "1d"}),
            ("future", {"raw": stamp(self.now + timedelta(hours=1))}),
            ("oldfirst", {"published_at": stamp(self.now - timedelta(days=5)),
                          "published_at_basis": "official_first_published"}),
        ]
        for name, changes in variants:
            with self.subTest(name=name):
                _, _, job_id = self.discover(name, **changes)
                self.assertIsNone(self.admitted(job_id))
                self.assertEqual(self.tasks("match", job_id), [])
        # Merely enabling the policy never reconstructs admissions for history.
        self.ledger.enable_workflow_retention([])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM settings WHERE key LIKE 'discovery_admission:%'").fetchone()[0], 0)

    def test_latest_unknown_deadline_and_terminal_states_still_block(self):
        _, _, job_id = self.discover()
        pending = self.tasks("match", job_id)[0]
        self.ledger.db.execute("UPDATE jobs SET listing_state='unknown' WHERE job_id=?", (job_id,))
        self.assertEqual(self.tasks("match", job_id), [])
        self.ledger.db.execute("UPDATE jobs SET listing_state='open' WHERE job_id=?", (job_id,))
        self.ledger.db.execute("CREATE TABLE job_freshness_rechecks (job_id TEXT, observed_at TEXT, live_state TEXT, listed_by_at TEXT)")
        self.ledger.db.execute("INSERT INTO job_freshness_rechecks VALUES (?,?,?,NULL)", (job_id, stamp(self.now), "unknown"))
        self.assertEqual(self.tasks("match", job_id), [])
        self.assertFalse(self.ledger._input_is_current(pending))
        self.ledger.db.execute("DELETE FROM job_freshness_rechecks")
        self.ledger.db.execute("UPDATE jobs SET listing_state='open',closes_at=? WHERE job_id=?",
                               (stamp(self.now - timedelta(seconds=1)), job_id))
        self.assertEqual(self.tasks("match", job_id), [])
        self.ledger.db.execute("UPDATE jobs SET closes_at=NULL WHERE job_id=?", (job_id,))
        update = {"update_id": "u_discovery_withdraw", "action_code": "application_withdrawn", "job_id": job_id,
                  "occurred_at_pt": stamp(self.now), "evidence_kind": "user_report", "expected_version": 0,
                  "evidence_ref": "User withdrew this exact role"}
        self.assertEqual(self.ledger.apply_manual(update)["import_status"], "已入账")
        self.assertEqual(self.tasks("match", job_id), [])

    def test_submitted_candidate_is_not_rematched_or_prepared(self):
        _, _, job_id = self.discover()
        self.ledger.apply_event({"event_type": "application_received", "job_id": job_id,
                                 "source_kind": "gmail", "source_ref": "discovery-receipt",
                                 "occurred_at": stamp(self.now), "evidence_kind": "gmail_message",
                                 "evidence_ref": "Disposable exact requisition receipt"})
        self.assertEqual(self.tasks("match", job_id), [])
        self.assertEqual(self.tasks("prepare", job_id), [])
        self.assertIsNone(self.workflow_admitted(job_id))

    def test_current_jd_resume_and_constraints_are_not_bypassed(self):
        _, _, job_id = self.discover()
        task = self.tasks("match", job_id)[0]
        self.assertTrue(self.ledger._input_is_current(task))
        self.ledger.db.execute("UPDATE settings SET value_json='2' WHERE key='candidate_constraints_revision'")
        self.assertFalse(self.ledger._input_is_current(task))
        self.ledger.db.execute("UPDATE settings SET value_json='1' WHERE key='candidate_constraints_revision'")
        changed = dict(task)
        changed["input_versions"] = {**task["input_versions"], "jd_revision": 999}
        self.assertFalse(self.ledger._input_is_current(changed))
        base = self.root / BASE
        base.write_bytes(base.read_bytes() + b"modified")
        self.assertFalse(self.ledger._input_is_current(task))


if __name__ == "__main__":
    unittest.main()

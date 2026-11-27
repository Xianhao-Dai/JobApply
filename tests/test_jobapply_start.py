"""Startup validation and task planning use disposable local workspaces only."""
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

PUBLIC_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PUBLIC_ROOT))
sys.path.insert(0, str(PUBLIC_ROOT / "scripts"))
from demo import synthetic_pdf
from jobapply_start import start


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jobapply-start-test-")
        self.root = Path(self.temp.name) / ".jobapply"
        self.root.mkdir()
        self.config = json.loads((PUBLIC_ROOT / "config.example.json").read_text())
        self.config["candidate"]["graduation_month"] = "2030-04"
        self.path = self.root / "config.json"
        synthetic_pdf(self.root / self.config["resume"]["path"])
        self.save()

    def tearDown(self):
        self.temp.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.config), encoding="utf-8")

    def settings(self):
        with sqlite3.connect(self.root / "state/jobapply.sqlite") as db:
            return {k: json.loads(v) for k, v in db.execute("SELECT key,value_json FROM settings")}

    def test_hash_is_pinned_tasks_are_relative_and_startup_has_no_network(self):
        self.config["sources"]["newgrad-jobs:us"]["enabled"] = True
        self.config["capabilities"]["prepare_forms"] = True
        self.save()
        raw = "https://careers.example/jobs/DEMO-9?utm_source=manual"
        with (patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")),
              patch.object(socket, "create_connection", side_effect=AssertionError("Network forbidden"))):
            report = start(self.path, raw)
        self.assertEqual(len(report["tasks"]), 2)
        self.assertFalse(report["external_io_performed"])
        self.assertFalse(report["schedule_created"])
        self.assertFalse(report["capabilities"]["final_submit"])
        self.assertEqual(report["base_pdf_sha256"], hashlib.sha256(
            (self.root / self.config["resume"]["path"]).read_bytes()).hexdigest())
        for item in report["tasks"]:
            self.assertFalse(Path(item["path"]).is_absolute())
            self.assertFalse(item["execution_authorized"])
            task = json.loads((self.root / item["path"]).read_text())
            self.assertEqual(task["input"]["until_at"], report["scan_window"]["until_at"])
            if task["scope_key"] == "manual:joblead":
                self.assertEqual(task["input"]["lead_id"], task["input"]["source_listing_id"])
                self.assertEqual(task["input"]["observed_url"], raw)
                self.assertFalse(task["input"]["apply_24h_discovery_gate"])
        values = self.settings()
        self.assertIsNone(values["candidate_sponsorship"])
        self.assertIsNone(values["candidate_work_authorization"])
        self.assertEqual(values["employment_scope"]["locations"], ["US", "US Remote"])
        self.assertIsNone(values["candidate_answers_ref"])

    def test_template_missing_facts_and_wrong_hash_fail_before_initializing_state(self):
        self.config["candidate"]["graduation_month"] = None
        self.save()
        with self.assertRaisesRegex(ValueError, "confirmed YYYY-MM"):
            start(self.path)
        self.assertFalse((self.root / "state").exists())
        self.config["candidate"]["graduation_month"] = "2030-04"
        self.config["resume"]["sha256"] = "0" * 64
        self.save()
        with self.assertRaisesRegex(ValueError, "hash does not match"):
            start(self.path)
        self.assertFalse((self.root / "state").exists())

    def test_paths_sources_unknown_fields_and_private_leads_are_rejected(self):
        original = json.loads(json.dumps(self.config))
        variants = []
        invalid = json.loads(json.dumps(original)); invalid["resume"]["path"] = "../outside.pdf"; variants.append(invalid)
        invalid = json.loads(json.dumps(original)); invalid["candidate"]["answers_ref"] = "/etc/passwd"; variants.append(invalid)
        invalid = json.loads(json.dumps(original)); invalid["sources"]["unapproved:jobs"] = {"enabled": True}; variants.append(invalid)
        invalid = json.loads(json.dumps(original)); invalid["sources"]["newgrad-jobs:us"]["url"] = "https://unapproved.example/jobs"; variants.append(invalid)
        invalid = json.loads(json.dumps(original)); invalid["secrets"] = {"password": "not-a-real-secret"}; variants.append(invalid)
        for config in variants:
            with self.subTest(config=config):
                self.config = config; self.save()
                with self.assertRaises(ValueError): start(self.path)
                self.assertFalse((self.root / "state").exists())
        self.config = original; self.save()
        for lead in ("http://careers.example/jobs/1", "https://127.0.0.1/jobs/1", "https://10.1.2.3/jobs/1",
                     "https://localhost/jobs/1", "https://user:password@careers.example/jobs/1"):
            with self.subTest(lead=lead), self.assertRaises(ValueError): start(self.path, lead)
        outside_config = Path(self.temp.name) / "config.json"
        outside_config.write_text(self.path.read_text())
        with self.assertRaisesRegex(ValueError, "private .jobapply"):
            start(outside_config)

    def test_symlinks_cannot_escape_configured_workspace(self):
        external = Path(self.temp.name) / "outside.pdf"
        synthetic_pdf(external)
        linked = self.root / "resume/linked.pdf"
        linked.symlink_to(external)
        self.config["resume"]["path"] = "resume/linked.pdf"
        self.save()
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            start(self.path)
        self.assertFalse((self.root / "state").exists())

    def test_neither_control_can_be_overridden_by_startup(self):
        start(self.path)
        database = self.root / "state/jobapply.sqlite"
        control = self.root / "state/workflow-control.json"
        control.write_text(json.dumps({"status": "PAUSED_BY_USER"}))
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "never overrides a pause"):
            start(self.path)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)
        control.write_text(json.dumps({"status": "ACTIVE"}))
        with sqlite3.connect(database) as db:
            db.execute("UPDATE settings SET value_json=? WHERE key='workflow_execution_control'",
                       (json.dumps({"status": "PAUSED_BY_USER"}),))
        before = hashlib.sha256(database.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError, "never overrides a pause"):
            start(self.path)
        self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), before)

    def test_truthful_fact_changes_revise_both_routes_but_restarts_do_not(self):
        self.config["scope"]["employment_types"].append("internship")
        self.config["scope"]["career_levels"].append("internship")
        self.config["candidate"]["internship_plan"] = {
            "graduation_month": "2030-12", "earliest_start_date": "2030-06-01",
            "max_duration_months": 3, "school_approval": None}
        self.save(); start(self.path)
        initial = self.settings()
        start(self.path)
        stable = self.settings()
        self.assertEqual(initial["candidate_constraints_revision"], stable["candidate_constraints_revision"])
        self.assertEqual(initial["internship_constraints_revision"], stable["internship_constraints_revision"])
        self.config["candidate"]["future_sponsorship_required"] = False
        self.save(); start(self.path)
        changed = self.settings()
        self.assertIs(changed["candidate_sponsorship"], False)
        self.assertEqual(changed["candidate_constraints_revision"], stable["candidate_constraints_revision"] + 1)
        self.assertEqual(changed["internship_constraints_revision"], stable["internship_constraints_revision"] + 1)

    def test_task_budget_preserves_deferred_sources_and_manual_intake_dedup(self):
        for source in self.config["sources"].values(): source["enabled"] = True
        self.config["limits"]["max_tasks"] = 2
        self.save()
        report = start(self.path, "https://careers.example/jobs/DEMO-1?utm_source=first")
        self.assertEqual(len(report["tasks"]), 2)
        self.assertEqual(len(report["deferred"]), 3)
        report = start(self.path, "https://careers.example/jobs/DEMO-1?utm_source=second")
        self.assertEqual(report["manual_lead_outcome"], "duplicate")
        with sqlite3.connect(self.root / "state/jobapply.sqlite") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM manual_job_leads").fetchone()[0], 1)
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()

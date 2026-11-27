"""A one-job user attempt gets a new version without changing candidate facts."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from jobapply_scope import candidate_context, constraints_match_context, match_context_is_current


class ApplicationDirectiveTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT);
            CREATE TABLE resumes (resume_id TEXT PRIMARY KEY, graduation_month TEXT);
            CREATE TABLE jobs (job_id TEXT PRIMARY KEY, official_req_id TEXT,
                               current_jd_revision INTEGER, title TEXT);
            CREATE TABLE matches (job_id TEXT, jd_revision INTEGER, base_resume_id TEXT,
                candidate_constraints_revision INTEGER, decision TEXT, task_id TEXT,
                PRIMARY KEY (job_id,jd_revision,base_resume_id,candidate_constraints_revision));
            CREATE TABLE task_attempts (task_id TEXT, attempt INTEGER, task_json TEXT);
            INSERT INTO resumes VALUES ('base', '2030-05');
            INSERT INTO jobs VALUES ('selected', '4421859774', 1, 'Software Engineer');
            INSERT INTO jobs VALUES ('other', 'other-req', 1, 'Software Engineer');
            INSERT INTO jobs VALUES ('intern', 'intern-req', 1, 'Software Engineer Intern');
            INSERT INTO matches VALUES ('selected', 1, 'base', 2, 'not_fit', 'old');
        """)
        for key, value in {
            "active_base_resume_id": "base", "candidate_constraints_revision": 2,
            "internship_constraints_revision": 4,
            "candidate_fact:earliest_start_date": {"value": "2030-07-01"},
            "employment_scope": {"employment_types": ["full_time", "internship"]},
            "internship_plan": {"graduation_month": "2030-12", "earliest_start_date": "2029-12-01",
                "max_duration_months": 3, "plan_status": "conditional_on_internship",
                "school_approval": "unconfirmed", "return_to_study_details": "unconfirmed"},
        }.items():
            self.setting(key, value)
        self.directive = {
            "job_id": "selected", "official_req_id": "4421859774", "jd_revision": 1,
            "base_resume_id": "base", "mode": "user_directed_attempt",
            "accepted_gap": "industry_experience",
            "evidence_kind": "user_report", "evidence_ref": "User explicitly accepts the industry experience gap",
            "candidate_constraints_revision": 1000002,
        }

    def tearDown(self):
        self.db.close()

    def setting(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def context(self, job_id="selected"):
        return candidate_context(self.db, self.db.execute(
            "SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone())

    def activate(self, directive=None):
        self.setting("job_application_directive:selected", self.directive if directive is None else directive)

    def constraints(self, context):
        return {key: copy.deepcopy(value) for key, value in context.items()
                if key not in ("candidate_constraints_revision", "requires_review")}

    def test_legacy_contexts_and_other_jobs_stay_unchanged(self):
        baseline = {job_id: self.context(job_id) for job_id in ("selected", "other", "intern")}
        self.assertEqual(baseline["selected"]["candidate_constraints_revision"], 2)
        self.assertEqual(baseline["intern"]["candidate_constraints_revision"], 4)
        self.assertTrue(constraints_match_context(baseline["selected"], {}))
        self.assertFalse(constraints_match_context(baseline["intern"], {}))
        self.assertTrue(constraints_match_context(baseline["intern"], self.constraints(baseline["intern"])))
        self.activate()
        selected = self.context()
        self.assertEqual(selected.pop("application_directive"), self.directive)
        self.assertEqual(selected.pop("candidate_constraints_revision"), 1000002)
        unchanged_facts = dict(baseline["selected"])
        unchanged_facts.pop("candidate_constraints_revision")
        self.assertEqual(selected, unchanged_facts)
        self.assertEqual(self.context("other"), baseline["other"])
        self.assertEqual(self.context("intern"), baseline["intern"])

    def test_new_revision_can_preserve_the_original_not_fit_primary_key(self):
        original = dict(self.db.execute("SELECT * FROM matches").fetchone())
        self.activate()
        self.db.execute("INSERT INTO matches VALUES ('selected',1,'base',?,'no_changes','new')",
                        (self.context()["candidate_constraints_revision"],))
        rows = [dict(row) for row in self.db.execute(
            "SELECT * FROM matches ORDER BY candidate_constraints_revision")]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], original)
        self.assertEqual(rows[1]["candidate_constraints_revision"], 1000002)

    def test_wrong_or_incomplete_directives_do_not_activate(self):
        baseline = self.context()
        invalid = {"job_id": "other", "official_req_id": "wrong", "jd_revision": 2,
            "base_resume_id": "different-base", "mode": "waive_eligibility",
            "accepted_gap": "legal_eligibility",
            "evidence_kind": "ats_receipt", "evidence_ref": " ",
            "candidate_constraints_revision": 2}
        for key, value in invalid.items():
            with self.subTest(field=key):
                directive = dict(self.directive, **{key: value})
                self.activate(directive)
                self.assertEqual(self.context(), baseline)
        for key in self.directive:
            with self.subTest(missing=key):
                directive = dict(self.directive)
                directive.pop(key)
                self.activate(directive)
                self.assertEqual(self.context(), baseline)
        for value in (None, [], "user_directed_attempt", True, 0):
            with self.subTest(value=value):
                self.activate(value)
                if value is None:
                    self.setting("job_application_directive:selected", None)
                self.assertEqual(self.context(), baseline)
        for revision in (True, -1, "1000002", 1.5):
            with self.subTest(revision=revision):
                self.activate(dict(self.directive, candidate_constraints_revision=revision))
                self.assertEqual(self.context(), baseline)

    def test_task_constraints_must_bind_the_exact_active_directive(self):
        self.activate()
        context = self.context()
        constraints = self.constraints(context)
        self.assertTrue(constraints_match_context(context, constraints))
        for key in ("application_directive",):
            changed = copy.deepcopy(constraints)
            changed.pop(key)
            self.assertFalse(constraints_match_context(context, changed))
        changed = copy.deepcopy(constraints)
        changed["application_directive"]["evidence_ref"] = "Different instruction"
        self.assertFalse(constraints_match_context(context, changed))
        changed = copy.deepcopy(constraints)
        changed["application_directive"]["jd_revision"] = True
        self.assertFalse(constraints_match_context(context, changed))
        self.db.execute("DELETE FROM settings WHERE key='job_application_directive:selected'")
        self.assertFalse(constraints_match_context(self.context(), constraints))

    def test_immutable_scoped_match_stales_on_jd_base_or_directive_changes(self):
        self.activate()
        context = self.context()
        self.db.execute("INSERT INTO task_attempts VALUES ('new',1,?)", (json.dumps({
            "input": {"candidate_constraints": self.constraints(context)}}),))
        match = {"job_id": "selected", "jd_revision": 1, "base_resume_id": "base",
            "candidate_constraints_revision": 1000002, "decision": "no_changes", "task_id": "new"}
        self.assertTrue(match_context_is_current(self.db, match))
        self.db.execute("UPDATE jobs SET current_jd_revision=2 WHERE job_id='selected'")
        self.assertFalse(match_context_is_current(self.db, match))
        self.db.execute("UPDATE jobs SET current_jd_revision=1 WHERE job_id='selected'")
        self.db.execute("INSERT INTO resumes VALUES ('next-base','2030-05')")
        self.setting("active_base_resume_id", "next-base")
        self.assertFalse(match_context_is_current(self.db, match))
        self.setting("active_base_resume_id", "base")
        self.activate(dict(self.directive, evidence_ref="A different user instruction"))
        self.assertFalse(match_context_is_current(self.db, match))


if __name__ == "__main__":
    unittest.main()

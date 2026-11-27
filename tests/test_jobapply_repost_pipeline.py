"""Exercise repost ingestion, migration, and views in a disposable ledger."""
import csv
from contextlib import redirect_stdout
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import jobapply_views
import jobapply_batch
from jobapply_freshness import dispositions, source_repost_floor
from jobapply_ledger import ContractError, IDENTITY, Ledger, current_route_preference, digest_file, get_job_id


def stamp(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class RepostPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="jobapply-repost-test-")
        self.root = Path(self.temp.name)
        self.ledger = Ledger(self.root)
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.original_time = stamp(self.now - timedelta(days=60))
        self.job_id = get_job_id("example-payments", "PAY-101")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def observation(self, *, source_id="old", observed=None, **fields):
        return {"source_listing_id": source_id, "observed_url": "https://listings.example/jobs/old",
                "company_as_listed": "Example Payments", "title_as_listed": "Software Engineer, New Grad",
                "location_as_listed": "Seattle, WA", "published_at_as_listed": self.original_time,
                "observed_at": stamp(observed or (self.now - timedelta(hours=2))),
                "company_key": "example-payments", "official_req_id": "PAY-101",
                "official_url": "https://payments.example/jobs/search?gh_jid=PAY-101",
                "apply_url": "https://payments.example/careers/apply/software-engineer-new-grad/PAY-101",
                "listing_state": "open", "published_at": self.original_time,
                "published_at_basis": "official_first_published", **fields}

    def envelope(self, name, observations, *, source="newgrad-jobs:us", jd=False):
        task = {"schema_version": 1, "task_id": "t_" + name, "attempt": 1, "stage": "discover",
                "scope_key": source, "job_id": None,
                "input_versions": {"source_config_revision": 2, "scan_window_id": name},
                "deadline_at": stamp(self.now + timedelta(hours=1)),
                "input": {"source_key": source, "since_at": stamp(self.now - timedelta(hours=24)),
                          "target_filters": "US SDE"}}
        artifacts = []
        if jd:
            path = self.root / "state" / "staging" / task["task_id"] / "1" / "jd.txt"
            path.parent.mkdir(parents=True)
            path.write_text("Software Engineer, New Grad\nProgramming and collaboration requirements.\n")
            relative = str(path.relative_to(self.root))
            observations[0]["jd_path"] = relative
            artifacts.append({"kind": "jd", "path": relative, "sha256": digest_file(path)})
        result = {key: task[key] for key in IDENTITY}
        result.update(result_id="res_" + name, status="succeeded", completed_at=stamp(self.now),
                      output={"observations": observations, "next_cursor": None},
                      artifacts=artifacts, evidence=[])
        self.ledger.dispatch(task)
        return result

    def discover(self, name, observation, **kwargs):
        return self.ledger.accept(self.envelope(name, [observation], **kwargs))

    def seed_original(self):
        self.discover("initial", self.observation(), jd=True)

    def seed_existing_progress(self):
        db = self.ledger.db
        db.execute("INSERT INTO contacts(contact_id,display_name,linkedin_url,relationship_kind) VALUES (?,?,?,?)",
                   ("c_fixture", "Fixture Alum", "https://contacts.example/in/fixture-alum", "university"))
        db.execute("INSERT INTO company_contacts(company_key,contact_id,employment_state) VALUES (?,?,?)",
                   ("example-payments", "c_fixture", "current"))
        db.execute("INSERT INTO job_contacts(job_id,company_key,contact_id,rank,updated_at) VALUES (?,?,?,?,?)",
                   (self.job_id, "example-payments", "c_fixture", 1, stamp(self.now)))
        self.ledger.apply_event({"event_type": "connection_accepted", "company_key": "example-payments",
                                 "contact_id": "c_fixture", "source_kind": "manual", "source_ref": "fixture_connected",
                                 "occurred_at": stamp(self.now - timedelta(minutes=10)),
                                 "evidence_kind": "user_report", "evidence_ref": "Disposable fixture connection"})
        self.ledger.apply_event({"event_type": "application_received", "job_id": self.job_id,
                                 "source_kind": "gmail", "source_ref": "fixture_receipt",
                                 "occurred_at": stamp(self.now - timedelta(minutes=5)),
                                 "evidence_kind": "gmail_message", "evidence_ref": "Disposable exact-job receipt"})

    def seed_qualified_match(self):
        base = self.root / "resume" / "Candidate Resume.docx"
        base.parent.mkdir()
        with ZipFile(base, "w") as document:
            document.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Disposable resume fixture</w:t></w:r></w:p></w:body></w:document>')
        registered = self.ledger.register_base("2030-05")
        # Seed an already completed historical match, accepted while its
        # original publication was fresh. Restore the historical dates after
        # acceptance so later repost checks do not gain artificial freshness.
        historical = self.ledger.db.execute("SELECT published_at,first_seen_at FROM jobs WHERE job_id=?",
                                            (self.job_id,)).fetchone()
        recent = stamp(self.now - timedelta(hours=1))
        self.ledger.db.execute("UPDATE jobs SET published_at=?,first_seen_at=? WHERE job_id=?",
                               (recent, recent, self.job_id))
        task = {"schema_version": 1, "task_id": "t_match_fixture", "attempt": 1,
                "stage": "match", "scope_key": self.job_id, "job_id": self.job_id,
                "input_versions": {"jd_revision": 1, "base_resume_id": registered["base_resume_id"],
                                   "candidate_constraints_revision": registered["candidate_constraints_revision"]},
                "deadline_at": stamp(self.now + timedelta(hours=1)), "input": {}}
        self.ledger.dispatch(task)
        result = {key: task[key] for key in IDENTITY}
        result.update(result_id="res_match_fixture", status="succeeded", completed_at=stamp(self.now),
                      output={"decision": "no_changes", "hr_findings": [], "hm_findings": [],
                              "edit_plan": [], "open_questions": []}, artifacts=[], evidence=[])
        self.ledger.accept(result)
        self.ledger.db.execute("UPDATE jobs SET published_at=?,first_seen_at=? WHERE job_id=?",
                               (*historical, self.job_id))

    def rows(self, table):
        return [dict(r) for r in self.ledger.db.execute("SELECT * FROM " + table)]

    def seed_route_test(self):
        self.seed_original()
        self.seed_qualified_match()
        self.discover("route_recent", self.observation(source_id="route-recent", observed=self.now,
                                                      published_at_as_listed="Reposted 2 hours ago"), source="linkedin:jobs")
        handoff = self.now + timedelta(hours=1)
        self.ledger.db.execute("UPDATE jobs SET handoff_at=?,referral_cutoff_at=? WHERE job_id=?",
                               (stamp(handoff), stamp(handoff + timedelta(hours=1)), self.job_id))
        tailor = self.draft_stage("tailor", self.now)
        pdf = self.root / "state" / "staging" / tailor["task_id"] / "1" / "fixture.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n")
        result = {key: tailor[key] for key in IDENTITY}
        result.update(result_id="res_" + tailor["task_id"], status="succeeded", completed_at=stamp(self.now),
                      output={"resume_id": "r_route_fixture", "graduation_month": "2030-05", "pdf_verified": True},
                      artifacts=[{"kind": "pdf", "path": str(pdf.relative_to(self.root)), "sha256": digest_file(pdf)}],
                      evidence=[])
        self.ledger.accept(result)
        return handoff

    def draft_stage(self, stage, now):
        output = io.StringIO()
        with patch.object(jobapply_batch, "ROOT", self.root), patch.object(jobapply_batch, "now_utc", return_value=now), redirect_stdout(output):
            jobapply_batch.draft_tasks(self.ledger, stage, [self.job_id], 1)
        relative = output.getvalue().strip()
        return json.loads((self.root / relative).read_text()) if relative else None

    def save_route_preference(self, **changes):
        preference = {"request_id": "fixture-direct-1", "job_id": self.job_id, "official_req_id": "PAY-101",
                      "route": "direct", "seek_referral": False, "requested_at": stamp(self.now),
                      "instructions": "Use the official direct route at handoff; user submits", **changes}
        self.ledger.db.execute("INSERT OR REPLACE INTO settings(key,value_json) VALUES (?,?)",
                               ("route_preference:" + self.job_id, json.dumps(preference)))
        return preference

    def test_v1_migration_keeps_existing_progress_and_reopening_is_idempotent(self):
        self.seed_original()
        self.seed_existing_progress()
        before = {table: self.rows(table) for table in ("jobs", "applications", "contacts", "events", "tasks")}
        source_before = {key: self.rows("job_sources")[0][key] for key in (
            "source_key", "source_listing_id", "job_id", "first_seen_at", "last_seen_at", "verification_state")}
        self.ledger.close()
        path = self.root / "state" / "jobapply.sqlite"
        with sqlite3.connect(path) as old:
            old.execute("DROP TABLE job_workflow_admissions")
            for column in ("reposted_at", "reposted_at_basis", "repost_observed_at", "repost_label"):
                old.execute("ALTER TABLE job_sources DROP COLUMN " + column)
            old.execute("PRAGMA user_version=1")
        for _ in range(2):
            self.ledger = Ledger(self.root)
            self.assertEqual(self.ledger.db.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(self.rows("job_workflow_admissions"), [])
            self.assertEqual({table: self.rows(table) for table in before}, before)
            source_after = self.rows("job_sources")[0]
            self.assertEqual({key: source_after[key] for key in source_before}, source_before)
            self.assertIsNone(source_after["reposted_at"])
            self.assertEqual(list(self.ledger.db.execute("PRAGMA foreign_key_check")), [])
            self.ledger.close()
        self.ledger = Ledger(self.root)

    def test_repost_merges_old_job_and_preserves_submission_connection_and_first_date(self):
        self.seed_original()
        self.seed_qualified_match()
        self.seed_existing_progress()
        application, contact = self.rows("applications"), self.rows("contacts")
        repost_observed = self.now - timedelta(hours=1)
        repost = self.observation(source_id="linked-repost", observed=repost_observed,
                                  observed_url="https://listings.example/jobs/repost-42",
                                  published_at_as_listed="Reposted 2 hours ago",
                                  published_at=stamp(repost_observed - timedelta(hours=2)),
                                  published_at_basis="official_last_published")
        result = self.discover("repost", repost, source="linkedin:jobs")
        self.assertEqual(result["job_ids"], [self.job_id])
        self.assertEqual(len(self.rows("jobs")), 1)
        self.assertEqual(self.rows("applications"), application)
        self.assertEqual(self.rows("contacts"), contact)
        self.assertEqual(self.rows("jobs")[0]["published_at"], self.original_time)
        source = next(r for r in self.rows("job_sources") if r["source_key"] == "linkedin:jobs")
        self.assertEqual(source["reposted_at"], stamp(repost_observed - timedelta(hours=3)))
        self.assertEqual(source["reposted_at_basis"], "source_relative_lower_bound")
        repeated = {**repost, "observed_at": stamp(self.now)}
        self.discover("repost_repeat", repeated, source="linkedin:jobs")
        after = next(r for r in self.rows("job_sources") if r["source_key"] == "linkedin:jobs")
        self.assertEqual(after["reposted_at"], source["reposted_at"])
        self.assertEqual(after["repost_observed_at"], source["repost_observed_at"])
        self.assertEqual(dispositions(self.ledger.db, self.rows("jobs"), self.now)[self.job_id], "active")
        with patch.object(jobapply_views, "ROOT", self.root):
            books = jobapply_views.export()
        with (self.root / "positions" / "discovered.csv").open(newline="") as stream:
            discovered = list(csv.DictReader(stream))
        self.assertEqual(discovered[0]["freshness_basis"], "source_repost")
        self.assertEqual(datetime.fromisoformat(discovered[0]["effective_posted_at"]), repost_observed - timedelta(hours=3))
        self.assertEqual(discovered[0]["published_at"], self.original_time)
        matches = next(book for book in books if book["path"] == "positions/no_changes.xlsx")["sheets"][0]
        self.assertEqual(len(matches["rows"]), 1)
        qualified = dict(zip(matches["columns"], matches["rows"][0]))
        self.assertEqual(qualified["freshness_basis"], "source_repost")
        self.assertEqual(datetime.fromisoformat(qualified["effective_posted_at_pt"]), repost_observed - timedelta(hours=3))
        tracker = next(book for book in books if book["path"] == "applications/tracker.xlsx")
        queue = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Queue")
        statuses = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Status")
        self.assertEqual(queue["rows"], [])
        status = dict(zip(statuses["columns"], statuses["rows"][0]))
        self.assertEqual(status["application_state"], "submitted")

    def test_new_board_for_same_req_keeps_original_publication_and_progress(self):
        self.job_id = get_job_id("example_streaming", "STR-201")
        first = "2022-09-10T18:39:50Z"
        new_post_first = "2022-09-17T00:30:12Z"
        old_url = "https://stream.example/jobs/BOARD-201A"
        new_url = "https://stream.example/jobs/BOARD-201B"
        old = self.observation(source_id="BOARD-201A", company_key="example_streaming", company_as_listed="Example Streaming",
                               official_req_id="STR-201", title_as_listed="Software Engineer I, Catalog",
                               official_url=old_url, apply_url=old_url, published_at=first,
                               published_at_as_listed=first)
        self.discover("old_board", old, source="linkedin:jobs", jd=True)
        original_seen = self.rows("jobs")[0]["first_seen_at"]
        db = self.ledger.db
        db.execute("INSERT INTO contacts(contact_id,display_name,linkedin_url,relationship_kind) VALUES (?,?,?,?)",
                   ("c_example_streaming", "Fixture Alum", "https://contacts.example/in/streaming-fixture", "university"))
        db.execute("INSERT INTO company_contacts(company_key,contact_id,employment_state) VALUES (?,?,?)",
                   ("example_streaming", "c_example_streaming", "current"))
        db.execute("INSERT INTO job_contacts(job_id,company_key,contact_id,rank,updated_at) VALUES (?,?,?,?,?)",
                   (self.job_id, "example_streaming", "c_example_streaming", 1, stamp(self.now)))
        self.ledger.apply_event({"event_type": "connection_accepted", "company_key": "example_streaming",
                                 "contact_id": "c_example_streaming", "source_kind": "manual", "source_ref": "example_streaming_connected",
                                 "occurred_at": stamp(self.now - timedelta(minutes=10)),
                                 "evidence_kind": "user_report", "evidence_ref": "Disposable connection fixture"})
        for event_type, source_ref in (("referral_submitted_by_employee", "example_streaming_referred"),
                                       ("application_received", "example_streaming_received")):
            self.ledger.apply_event({"event_type": event_type, "job_id": self.job_id,
                                     **({"contact_id": "c_example_streaming"} if event_type.startswith("referral") else {}),
                                     "source_kind": "gmail", "source_ref": source_ref,
                                     "occurred_at": stamp(self.now - timedelta(minutes=5)),
                                     "evidence_kind": "gmail_message", "evidence_ref": "Disposable exact-job evidence"})
        history = {table: self.rows(table) for table in ("applications", "company_contacts", "job_contacts", "events")}
        new = {**old, "source_listing_id": "BOARD-201B", "observed_at": stamp(self.now - timedelta(minutes=1)),
               "official_url": new_url, "apply_url": new_url, "published_at": new_post_first,
               "published_at_as_listed": new_post_first}
        new.pop("jd_path")
        self.discover("new_board", new, source="linkedin:jobs")
        job = self.rows("jobs")[0]
        self.assertEqual(job["published_at"], first)
        self.assertEqual(job["published_at_basis"], "official_first_published")
        self.assertEqual(job["first_seen_at"], original_seen)
        self.assertEqual(job["official_url"], new_url)
        self.assertIsNone(source_repost_floor(next(row for row in self.rows("job_sources")
                                                  if row["source_listing_id"] == "BOARD-201B")))
        self.assertEqual(dispositions(self.ledger.db, self.rows("jobs"), self.now)[self.job_id], "older_than_24h")

        # Separate explicit repost evidence can reactivate the same canonical
        # job and a changed JD can advance, without mutating original history.
        repost = {**new, "observed_at": stamp(self.now), "published_at_as_listed": "Reposted 1 hour ago",
                  "reposted_at": stamp(self.now - timedelta(hours=2)),
                  "reposted_at_basis": "source_relative_lower_bound", "repost_observed_at": stamp(self.now),
                  "repost_label": "Reposted 1 hour ago"}
        result = self.envelope("new_board_repost", [repost], source="linkedin:jobs", jd=True)
        artifact = result["artifacts"][0]
        path = self.root / artifact["path"]
        path.write_text("Software Engineer I, Catalog\nUpdated official responsibilities fixture.\n")
        artifact["sha256"] = digest_file(path)
        self.ledger.accept(result)
        job = self.rows("jobs")[0]
        self.assertEqual(job["published_at"], first)
        self.assertEqual(job["first_seen_at"], original_seen)
        self.assertEqual(job["current_jd_revision"], 2)
        self.assertEqual(len(self.rows("jobs")), 1)
        self.assertEqual(dispositions(self.ledger.db, self.rows("jobs"), self.now)[self.job_id], "active")
        self.assertEqual({table: self.rows(table) for table in history}, history)

    def test_first_publication_accepts_only_earlier_exact_corrections(self):
        self.seed_original()
        original_seen = self.rows("jobs")[0]["first_seen_at"]
        earlier = stamp(self.now - timedelta(days=70))
        self.discover("earlier_first", self.observation(observed=self.now, published_at=earlier))
        self.assertEqual(self.rows("jobs")[0]["published_at"], earlier)
        cases = (
            {"published_at": stamp(self.now - timedelta(hours=1)), "published_at_basis": "official_first_published"},
            {"published_at": stamp(self.now + timedelta(days=7)), "published_at_basis": "official_first_published"},
            {"published_at": stamp(self.now), "published_at_basis": "official_last_published"},
            {"published_at": self.now.date().isoformat(), "published_at_basis": "official_date_only"},
            {"published_at": None, "published_at_basis": None, "updated_at": stamp(self.now)},
        )
        for index, fields in enumerate(cases):
            with self.subTest(fields=fields):
                observation = self.observation(source_id="unproven-repost-" + str(index), observed=self.now, **fields)
                self.discover("retain_earliest_" + str(index), observation)
                job = self.rows("jobs")[0]
                self.assertEqual(job["published_at"], earlier)
                self.assertEqual(job["published_at_basis"], "official_first_published")
                self.assertEqual(job["first_seen_at"], original_seen)
                self.assertTrue(all(source_repost_floor(row) is None for row in self.rows("job_sources")))
                self.assertEqual(dispositions(self.ledger.db, self.rows("jobs"), self.now)[self.job_id], "older_than_24h")

    def test_fractional_repost_ingestion_preserves_valid_fixed_evidence(self):
        self.seed_original()
        observed = (self.now - timedelta(hours=1)).replace(microsecond=888000)
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                observation = self.observation(source_id="fractional-" + str(explicit),
                                               observed_at=observed.isoformat(),
                                               published_at_as_listed="Reposted 11 hours ago")
                if explicit:
                    observation.update(reposted_at=(observed - timedelta(hours=12)).isoformat(),
                                       reposted_at_basis="source_relative_lower_bound",
                                       repost_observed_at=observed.isoformat(), repost_label="Reposted 11 hours ago")
                accepted = self.discover("fractional_" + str(explicit), observation, source="linkedin:jobs")
                self.assertEqual(accepted["job_ids"], [self.job_id])
                source = next(row for row in self.rows("job_sources")
                              if row["source_listing_id"] == observation["source_listing_id"])
                self.assertEqual(source["last_seen_at"], stamp(observed))
                self.assertEqual(source_repost_floor(source), (observed - timedelta(hours=12)).replace(microsecond=0))
                self.assertEqual(dispositions(self.ledger.db, self.rows("jobs"), self.now)[self.job_id], "active")

    def test_invalid_explicit_repost_rolls_back_entire_discovery_result(self):
        cases = ({"reposted_at": stamp(self.now - timedelta(hours=1))},
                 {"reposted_at": stamp(self.now + timedelta(minutes=1)), "reposted_at_basis": "source_exact",
                  "repost_observed_at": stamp(self.now), "repost_label": "Reposted"})
        for index, invalid in enumerate(cases):
            with self.subTest(invalid=invalid):
                good = self.observation(source_id="would-create-" + str(index))
                bad = self.observation(source_id="invalid-" + str(index), official_req_id="bad-" + str(index),
                                       observed=self.now, **invalid)
                result = self.envelope("bad_" + str(index), [good, bad], jd=True)
                with self.assertRaises(ContractError):
                    self.ledger.accept(result)
                self.assertEqual(self.rows("jobs"), [])
                self.assertEqual(self.rows("companies"), [])
                self.assertEqual(self.rows("job_sources"), [])
                self.assertEqual(self.rows("job_jd_versions"), [])
                self.assertEqual(list((self.root / "positions").rglob("*.txt")), [])

    def test_plain_updated_field_cannot_reactivate_old_post(self):
        self.seed_original()
        update = self.observation(observed=self.now, published_at_as_listed="Updated 1 hour ago",
                                  updated_at=stamp(self.now), published_at=stamp(self.now),
                                  published_at_basis="official_last_published")
        self.discover("plain_update", update)
        job, source = self.rows("jobs")[0], self.rows("job_sources")[0]
        self.assertEqual(job["published_at"], self.original_time)
        self.assertIsNone(source["reposted_at"])
        self.assertEqual(dispositions(self.ledger.db, [job], self.now)[self.job_id], "older_than_24h")

    def test_duplicate_history_blocks_only_prepare_and_stays_visible_with_new_pdf(self):
        self.seed_original()
        self.seed_qualified_match()
        repost = self.observation(source_id="recent", observed=self.now,
                                  published_at_as_listed="Reposted 2 hours ago")
        self.discover("recent_for_duplicate_check", repost, source="linkedin:jobs")
        self.ledger.apply_event({"event_type": "application_form_blocked", "job_id": self.job_id,
                                 "source_kind": "orchestrator", "source_ref": "fixture_possible_duplicate",
                                 "occurred_at": stamp(self.now), "route": "direct", "resume_id": None,
                                 "blocker_code": "POSSIBLE_DUPLICATE_APPLICATION",
                                 "next_action": "Verify unmatched historical same-title receipt before applying",
                                 "evidence_kind": "gmail_message", "evidence_ref": "Disposable ambiguous receipt"})
        self.ledger.db.execute("UPDATE jobs SET handoff_at=? WHERE job_id=?",
                               (stamp(self.now - timedelta(hours=1)), self.job_id))
        # A fresh constraints revision requires rematching, independently of the
        # unresolved application history.
        self.ledger.db.execute("UPDATE settings SET value_json='2' WHERE key='candidate_constraints_revision'")

        def draft(stage):
            output = io.StringIO()
            with patch.object(jobapply_batch, "ROOT", self.root), patch.object(jobapply_batch, "now_utc", return_value=self.now), redirect_stdout(output):
                jobapply_batch.draft_tasks(self.ledger, stage, [self.job_id], 1)
            relative = output.getvalue().strip()
            return json.loads((self.root / relative).read_text()) if relative else None

        def accept(task, output, artifacts=None):
            result = {key: task[key] for key in IDENTITY}
            result.update(result_id="res_" + task["task_id"], status="succeeded", completed_at=stamp(self.now),
                          output=output, artifacts=artifacts or [], evidence=[])
            return self.ledger.accept(result)

        match = draft("match")
        self.assertIsNotNone(match)
        self.assertEqual(accept(match, {"decision": "no_changes", "hr_findings": [], "hm_findings": [],
                                       "edit_plan": [], "open_questions": []})["outcome"], "succeeded")
        tailor = draft("tailor")
        self.assertIsNotNone(tailor)
        self.assertEqual(tailor["input"]["mode"], "export_only")
        pdf = self.root / "state" / "staging" / tailor["task_id"] / "1" / "fixture.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n")
        relative_pdf = str(pdf.relative_to(self.root))
        self.assertEqual(accept(tailor, {"resume_id": "r_duplicate_fixture", "graduation_month": "2030-05",
                                         "pdf_verified": True},
                                [{"kind": "pdf", "path": relative_pdf, "sha256": digest_file(pdf)}])["outcome"], "succeeded")
        connection = draft("connections")
        self.assertIsNotNone(connection)
        self.assertEqual(accept(connection, {"candidates": []})["outcome"], "succeeded")
        self.assertIsNone(draft("prepare"))

        with patch.object(jobapply_views, "ROOT", self.root):
            books = jobapply_views.export()
        tracker = next(book for book in books if book["path"] == "applications/tracker.xlsx")
        queue = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Queue")
        self.assertEqual(len(queue["rows"]), 1)
        pending = dict(zip(queue["columns"], queue["rows"][0]))
        self.assertTrue(pending["resume_pdf"])
        self.assertEqual(pending["blocker_code"], "POSSIBLE_DUPLICATE_APPLICATION")
        self.assertEqual(pending["next_action"], "Verify unmatched historical same-title receipt before applying")
        statuses = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Status")
        status = dict(zip(statuses["columns"], statuses["rows"][0]))
        self.assertEqual(status["application_state"], "form_blocked")
        self.assertEqual(status["next_action"], pending["next_action"])

        material = next(row for row in self.rows("resumes") if row["resume_id"] == "r_duplicate_fixture")
        app = self.rows("applications")[0]
        prepare = {"schema_version": 1, "task_id": "t_prepare_direct_fixture", "attempt": 1,
                   "stage": "prepare", "scope_key": self.job_id, "job_id": self.job_id,
                   "input_versions": {"jd_revision": 1, "resume_id": material["resume_id"],
                                      "application_version": app["row_version"], "route_fingerprint": "fixture_direct"},
                   "deadline_at": stamp(self.now + timedelta(hours=1)),
                   "input": {"resume_pdf_path": material["pdf_path"], "resume_pdf_sha256": material["pdf_sha256"]}}
        with self.assertRaises(ContractError):
            self.ledger.dispatch(prepare)
        # The same complete material/version can prepare once the duplicate
        # check is resolved; no unrelated missing-input gate masks this check.
        self.ledger.db.execute("UPDATE applications SET blocker_code=NULL WHERE job_id=?", (self.job_id,))
        self.assertEqual(self.ledger.dispatch(prepare)["outcome"], "dispatched")

    def test_unresolved_duplicate_remains_tracked_after_ageout_or_closure(self):
        self.seed_original()
        self.seed_qualified_match()
        ordinary_id = get_job_id("example-payments", "PAY-102")
        self.discover("ordinary_old", self.observation(source_id="ordinary-old", official_req_id="PAY-102"))
        for job_id, blocker in ((self.job_id, "POSSIBLE_DUPLICATE_APPLICATION"),
                                (ordinary_id, "LOGIN_REQUIRED")):
            self.ledger.apply_event({"event_type": "application_form_blocked", "job_id": job_id,
                                     "source_kind": "orchestrator", "source_ref": "old_" + job_id,
                                     "occurred_at": stamp(self.now), "blocker_code": blocker,
                                     "next_action": "Resolve the recorded application check"})
        self.assertEqual(dispositions(self.ledger.db, self.rows("jobs"), self.now)[self.job_id], "older_than_24h")

        def track_at(now):
            output = io.StringIO()
            with patch.object(jobapply_batch, "ROOT", self.root), patch.object(jobapply_batch, "now_utc", return_value=now), redirect_stdout(output):
                jobapply_batch.draft_tasks(self.ledger, "track", [self.job_id, ordinary_id], 2)
            return [json.loads((self.root / relative).read_text()) for relative in output.getvalue().splitlines()]

        def report():
            with patch.object(jobapply_views, "ROOT", self.root):
                books = jobapply_views.export()
            tracker = next(book for book in books if book["path"] == "applications/tracker.xlsx")
            status = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Status")
            queue = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Queue")
            self.assertEqual(queue["rows"], [])
            matches = next(book for book in books if book["path"] == "positions/no_changes.xlsx")["sheets"][0]
            self.assertEqual(matches["rows"], [])
            return [dict(zip(status["columns"], row)) for row in status["rows"]]

        tasks = track_at(self.now)
        self.assertEqual([task["job_id"] for task in tasks], [self.job_id])
        self.assertEqual(tasks[0]["input"]["current_application_state"], "form_blocked")
        statuses = report()
        self.assertEqual([row["job_id"] for row in statuses], [self.job_id])
        self.assertEqual(statuses[0]["application_state"], "form_blocked")
        self.assertEqual(statuses[0]["next_action"], "Resolve the recorded application check")

        self.ledger.db.execute("UPDATE jobs SET listing_state='closed' WHERE job_id=?", (self.job_id,))
        tasks = track_at(self.now + timedelta(hours=1))
        self.assertEqual([task["job_id"] for task in tasks], [self.job_id])
        self.assertEqual(report()[0]["job_id"], self.job_id)
        self.ledger.apply_event({"event_type": "application_form_blocked", "job_id": self.job_id,
                                 "source_kind": "orchestrator", "source_ref": "duplicate_check_resolved",
                                 "occurred_at": stamp(self.now), "blocker_code": "LOGIN_REQUIRED",
                                 "next_action": "Ordinary closed-role form blocker"})
        self.assertEqual(track_at(self.now + timedelta(hours=2)), [])
        self.assertEqual(report(), [])

    def test_reapply_request_is_exact_scoped_current_and_keeps_handoff_and_submission_gates(self):
        self.seed_original()
        self.seed_qualified_match()
        self.discover("reapply_recent", self.observation(source_id="reapply-recent", observed=self.now,
                                                        published_at_as_listed="Reposted 2 hours ago"), source="linkedin:jobs")
        action = "At handoff, retry with original email; stop if already applied; user submits"
        self.ledger.apply_event({"event_type": "application_form_blocked", "job_id": self.job_id,
                                 "source_kind": "orchestrator", "source_ref": "fixture_reapply_history",
                                 "occurred_at": stamp(self.now), "blocker_code": "POSSIBLE_DUPLICATE_APPLICATION",
                                 "next_action": action})
        handoff = self.now + timedelta(hours=1)
        self.ledger.db.execute("UPDATE jobs SET handoff_at=? WHERE job_id=?", (stamp(handoff), self.job_id))

        def draft(stage, now):
            output = io.StringIO()
            with patch.object(jobapply_batch, "ROOT", self.root), patch.object(jobapply_batch, "now_utc", return_value=now), redirect_stdout(output):
                jobapply_batch.draft_tasks(self.ledger, stage, [self.job_id], 1)
            relative = output.getvalue().strip()
            return json.loads((self.root / relative).read_text()) if relative else None

        def accept(task, output, artifacts=None):
            result = {key: task[key] for key in IDENTITY}
            result.update(result_id="res_" + task["task_id"], status="succeeded", completed_at=stamp(self.now),
                          output=output, artifacts=artifacts or [], evidence=[])
            return self.ledger.accept(result)

        tailor = draft("tailor", self.now)
        pdf = self.root / "state" / "staging" / tailor["task_id"] / "1" / "fixture.pdf"
        pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n")
        accept(tailor, {"resume_id": "r_reapply_fixture", "graduation_month": "2030-05", "pdf_verified": True},
               [{"kind": "pdf", "path": str(pdf.relative_to(self.root)), "sha256": digest_file(pdf)}])
        self.assertIsNone(draft("prepare", handoff))
        request = {"request_id": "fixture-reapply-1", "job_id": self.job_id, "official_req_id": "PAY-101",
                   "requested_at": stamp(self.now), "instructions": action}

        def save_request(value):
            self.ledger.db.execute("INSERT OR REPLACE INTO settings(key,value_json) VALUES (?,?)",
                                   ("reapply_request:" + self.job_id, json.dumps(value)))

        for wrong_scope in ({"job_id": "j_other"}, {"official_req_id": "different-req"}):
            save_request({**request, **wrong_scope})
            self.assertIsNone(draft("prepare", handoff))
        save_request(request)
        self.assertIsNone(draft("prepare", self.now))
        prepare = draft("prepare", handoff)
        self.assertIsNotNone(prepare)
        self.assertEqual(prepare["input"]["reapply_request"], request)
        for suffix, alteration in (("missing", None), ("forged", {**request, "request_id": "forged"})):
            direct = {**prepare, "task_id": "t_prepare_" + suffix, "input": dict(prepare["input"])}
            if alteration is None:
                direct["input"].pop("reapply_request")
            else:
                direct["input"]["reapply_request"] = alteration
            with self.assertRaises(ContractError):
                self.ledger.dispatch(direct)
        with patch.object(jobapply_views, "ROOT", self.root):
            tracker = next(book for book in jobapply_views.export() if book["path"] == "applications/tracker.xlsx")
        queue = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Queue")
        pending = dict(zip(queue["columns"], queue["rows"][0]))
        self.assertEqual(pending["blocker_code"], "REAPPLY_REQUESTED")
        self.assertEqual(pending["next_action"], action)
        status = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Status")
        self.assertEqual(dict(zip(status["columns"], status["rows"][0]))["application_state"], "form_blocked")
        self.assertEqual(self.rows("applications")[0]["blocker_code"], "POSSIBLE_DUPLICATE_APPLICATION")

        renewed = {**request, "request_id": "fixture-reapply-2"}
        save_request(renewed)
        stale = {**prepare, "task_id": "t_prepare_stale_manifest"}
        with self.assertRaises(ContractError):
            self.ledger.dispatch(stale)
        self.assertEqual(accept(prepare, {})["outcome"], "superseded")
        renewed_task = draft("prepare", handoff)
        self.assertNotEqual(renewed_task["task_id"], prepare["task_id"])
        self.assertEqual(renewed_task["input"]["reapply_request"], renewed)
        self.ledger.apply_event({"event_type": "application_received", "job_id": self.job_id,
                                 "source_kind": "gmail", "source_ref": "fixture_exact_prior_receipt",
                                 "occurred_at": stamp(self.now), "evidence_kind": "gmail_message",
                                 "evidence_ref": "Disposable exact requisition receipt"})
        self.assertIsNone(draft("prepare", handoff))
        submitted = {**renewed_task, "task_id": "t_prepare_after_submitted"}
        submitted["input_versions"] = {**submitted["input_versions"], "application_version": self.rows("applications")[0]["row_version"]}
        with self.assertRaises(ContractError):
            self.ledger.dispatch(submitted)

    def test_direct_preference_keeps_referral_facts_skips_connections_and_requires_current_manifest(self):
        handoff = self.seed_route_test()
        pending_connections = self.draft_stage("connections", self.now)
        self.ledger.db.execute("INSERT INTO job_referrals(job_id,referral_state,updated_at) VALUES (?,?,?)",
                               (self.job_id, "requested", stamp(self.now)))
        job = self.rows("jobs")[0]
        self.assertIsNone(jobapply_batch.select_route(job, self.rows("job_referrals"), handoff))
        preference = self.save_route_preference()
        self.assertIsNone(self.draft_stage("connections", self.now))
        stale_result = {key: pending_connections[key] for key in IDENTITY}
        stale_result.update(result_id="res_stale_connections", status="succeeded", completed_at=stamp(self.now),
                            output={"candidates": [{"display_name": "Must not be promoted"}]}, artifacts=[], evidence=[])
        self.assertEqual(self.ledger.accept(stale_result)["outcome"], "superseded")
        self.assertEqual(self.rows("contacts"), [])
        self.assertIsNone(self.draft_stage("prepare", self.now))
        prepare = self.draft_stage("prepare", handoff)
        self.assertEqual(prepare["input"]["route"], "direct")
        self.assertEqual(prepare["input"]["route_preference"], preference)
        self.assertEqual(self.rows("job_referrals")[0]["referral_state"], "requested")
        link = "https://payments.example/careers/apply/software-engineer-new-grad/PAY-101?ref=fixture"
        self.ledger.db.execute("UPDATE job_referrals SET referral_state='link_received',referral_url=?", (link,))
        self.assertEqual(jobapply_batch.select_route(job, self.rows("job_referrals"), handoff)[0], "referral_link")
        self.assertEqual(self.draft_stage("prepare", handoff)["input"]["route"], "direct")
        self.assertEqual(self.rows("job_referrals")[0]["referral_url"], link)
        other = {**job, "job_id": "j_other", "official_req_id": "other"}
        self.assertIsNone(current_route_preference(self.ledger.db, other))
        self.assertEqual(jobapply_batch.select_route(other, self.rows("job_referrals"), handoff)[0], "referral_link")
        for suffix, input_change in (("missing_policy", {key: value for key, value in prepare["input"].items() if key != "route_preference"}),
                                     ("wrong_route", {**prepare["input"], "route": "referral_link"})):
            with self.assertRaises(ContractError):
                self.ledger.dispatch({**prepare, "task_id": "t_" + suffix, "input": input_change})
        with patch.object(jobapply_views, "ROOT", self.root):
            tracker = next(book for book in jobapply_views.export() if book["path"] == "applications/tracker.xlsx")
        queue = next(sheet for sheet in tracker["sheets"] if sheet["name"] == "Queue")
        queued = dict(zip(queue["columns"], queue["rows"][0]))
        self.assertEqual(queued["route"], "direct")
        self.assertIn("用户选择直投，不寻找内推", queued["referral_summary"])
        self.assertIn("link_received", queued["referral_summary"])
        self.assertEqual(queued["next_action"], "按交接时间准备官方表单")
        self.assertEqual(self.rows("applications"), [])
        renewed = self.save_route_preference(request_id="fixture-direct-2")
        with self.assertRaises(ContractError):
            self.ledger.dispatch({**prepare, "task_id": "t_stale_route_preference"})
        new_prepare = self.draft_stage("prepare", handoff)
        self.assertNotEqual(new_prepare["task_id"], prepare["task_id"])
        self.assertEqual(new_prepare["input"]["route_preference"], renewed)
        self.ledger.db.execute("UPDATE jobs SET listing_state='closed' WHERE job_id=?", (self.job_id,))
        self.assertIsNone(self.draft_stage("prepare", handoff))

    def test_direct_preference_does_not_authorize_reapplying_or_bypass_submitted_state(self):
        handoff = self.seed_route_test()
        preference = self.save_route_preference()
        complete_prepare = self.draft_stage("prepare", handoff)
        self.assertEqual(complete_prepare["input"]["route_preference"], preference)
        self.ledger.apply_event({"event_type": "application_form_blocked", "job_id": self.job_id,
                                 "source_kind": "orchestrator", "source_ref": "fixture_direct_not_reapply",
                                 "occurred_at": stamp(self.now), "blocker_code": "POSSIBLE_DUPLICATE_APPLICATION",
                                 "next_action": "Resolve previous application"})
        self.assertIsNone(self.draft_stage("prepare", handoff))
        current = {**complete_prepare, "task_id": "t_direct_duplicate_guard",
                   "input_versions": {**complete_prepare["input_versions"],
                                      "application_version": self.rows("applications")[0]["row_version"]}}
        with self.assertRaises(ContractError):
            self.ledger.dispatch(current)
        self.ledger.apply_event({"event_type": "application_received", "job_id": self.job_id,
                                 "source_kind": "gmail", "source_ref": "fixture_direct_submitted",
                                 "occurred_at": stamp(self.now), "evidence_kind": "gmail_message",
                                 "evidence_ref": "Disposable exact requisition receipt"})
        self.assertIsNone(self.draft_stage("prepare", handoff))
        current["task_id"] = "t_direct_submitted_guard"
        current["input_versions"]["application_version"] = self.rows("applications")[0]["row_version"]
        with self.assertRaises(ContractError):
            self.ledger.dispatch(current)


if __name__ == "__main__":
    unittest.main()

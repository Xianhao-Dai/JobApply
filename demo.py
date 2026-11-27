#!/usr/bin/env python3
"""Replay the real ledger offline with entirely fictional, deterministic data.

    python3 demo.py
    python3 demo.py --output /tmp/jobapply-demo-fresh --with-receipt

The external-worker results are fixtures, not live AI decisions, website checks,
mail, or applications. No network, browser, credentials, or personal data is used.
The output directory must be new or empty. Existing workspaces are never loaded.
Requires Python 3.11+ with SQLite 3.37+; the demo itself uses only the stdlib.
"""

from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timedelta, timezone
import hashlib
import html
import importlib
import io
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch


DEFAULT_AS_OF = "2030-01-15T17:00:00Z"
COMPANY = "Example Labs"
REQ = "DEMO-100"
OFFICIAL_URL = "https://careers.example-labs.example/jobs/DEMO-100"
APPLY_URL = OFFICIAL_URL + "/apply"
GRADUATION = "2030-04"
JD = """SYNTHETIC DEMO JOB - NOT A REAL VACANCY
Software Engineer, New Grad | Example Labs | DEMO-100
Remote, United States | Full-time
Build and debug small backend services and test data-processing workflows.
Required: a computer science degree completed by June 2030, programming
experience with Go or Python, SQL basics, and collaboration with a team.
No mandatory professional experience minimum. Docker familiarity is preferred.
This text is a fixture, not an employer JD or a live-status verification.
"""


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError("Demo invariant failed: " + message)


def synthetic_pdf(path: Path) -> None:
    """Write a genuine one-page Letter PDF, including its xref and text stream."""
    lines = [
        "Alex Example - FICTIONAL DEMO APPLICANT",
        "alex@candidate.example | https://portfolio.candidate.example",
        "EDUCATION",
        "Example University | BS Computer Science | Expected Apr 2030",
        "PROJECTS",
        "Built a small Go service that validates and aggregates synthetic events.",
        "Added SQL queries and Python tests for deterministic sample datasets.",
        "Collaborated with a fictional classmate to review and document the API.",
        "SKILLS",
        "Go, Python, SQL, Git, Docker basics",
        "All names, experience, education and contact details are invented.",
    ]
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in lines]
    commands = ["BT", "/F1 11 Tf", "50 740 Td", "18 TL"]
    for index, line in enumerate(escaped):
        if index:
            commands.append("T*")
        commands.append(f"({line}) Tj")
    commands.append("ET")
    stream = ("\n".join(commands) + "\n").encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"endstream",
    ]
    data = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for offset in offsets[1:]:
        data.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    data.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load_core():
    scripts = Path(__file__).resolve().parent / "scripts"
    names = ("jobapply_ledger", "jobapply_batch", "jobapply_freshness",
             "jobapply_scope", "jobapply_intake", "jobapply_views")
    missing = [name + ".py" for name in names if not (scripts / (name + ".py")).is_file()]
    if not (scripts / "schema.sql").is_file():
        missing.append("schema.sql")
    if missing:
        raise RuntimeError("Missing public core files in scripts/: " + ", ".join(missing))
    sys.path.insert(0, str(scripts))
    modules = {name: importlib.import_module(name) for name in names}
    for name, module in modules.items():
        require(Path(module.__file__).resolve().is_relative_to(scripts),
                name + " must come from this public repository")
    return modules


def render_report(path: Path, summary: dict, books: list) -> None:
    sections = []
    for book in books:
        for sheet in book["sheets"]:
            if not sheet["rows"]:
                continue
            headers = "".join("<th>" + html.escape(str(col)) + "</th>" for col in sheet["columns"])
            rows = "".join("<tr>" + "".join("<td>" + html.escape(str(cell if cell is not None else "")) +
                                          "</td>" for cell in row) + "</tr>" for row in sheet["rows"])
            sections.append("<h2>" + html.escape(book["path"] + " / " + sheet["name"]) +
                            "</h2><div class=scroll><table><thead><tr>" + headers +
                            "</tr></thead><tbody>" + rows + "</tbody></table></div>")
    body = """<!doctype html><html lang=en><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>JobApply - offline synthetic replay</title>
<style>body{font:15px/1.5 system-ui,sans-serif;max-width:1200px;margin:40px auto;padding:0 20px;color:#17313d}
h1{margin-bottom:8px}aside{padding:16px;background:#fff4d9;border:1px solid #d8b660;border-radius:8px}
pre{padding:16px;background:#eef4f6;overflow:auto}h2{margin-top:32px;font-size:18px}
.scroll{overflow:auto}table{border-collapse:collapse;font-size:13px}th,td{border:1px solid #cad5db;padding:8px;min-width:130px;vertical-align:top;text-align:left}
th{background:#e7f1f4}td{white-space:pre-wrap;max-width:420px}footer{margin-top:32px}</style>
<h1>JobApply: offline replay</h1>
<aside>Everything shown here is synthetic. No website was checked, no account was accessed,
no message was sent, and no application was submitted. Worker decisions and receipts are
deterministic fixtures passed through the real ledger and its validation rules.</aside>
<h2>Verified demo invariants</h2><pre>SUMMARY</pre>SECTIONS
<footer>These are core export definitions rendered as HTML; XLSX creation is intentionally not required.</footer></html>"""
    path.write_text(body.replace("SUMMARY", html.escape(json.dumps(summary, indent=2))).replace(
        "SECTIONS", "\n".join(sections)), encoding="utf-8")


def replay(root: Path, as_of: datetime, with_receipt: bool) -> dict:
    modules = load_core()
    core = modules["jobapply_ledger"]
    batch = modules["jobapply_batch"]
    views = modules["jobapply_views"]

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of.astimezone(tz) if tz is not None else as_of.replace(tzinfo=None)

    transcript = []
    checks = {}
    with ExitStack() as stack:
        # Existing helpers use module ROOT rather than ledger.root. Keep these
        # bindings scoped to the disposable demo, without editing the core.
        for module in modules.values():
            if hasattr(module, "ROOT"):
                stack.enter_context(patch.object(module, "ROOT", root))
            if hasattr(module, "datetime"):
                stack.enter_context(patch.object(module, "datetime", FrozenDateTime))
        stack.enter_context(patch.object(batch, "now_utc", return_value=as_of))
        ledger = core.Ledger(root=root)
        stack.callback(ledger.close)

        candidate_path = root / "fixtures/candidate.json"
        write_json(candidate_path, {"synthetic": True, "name": "Alex Example",
                   "email": "alex@candidate.example", "graduation_month": GRADUATION,
                   "earliest_full_time_start": "2030-05-01", "skills": ["Go", "Python", "SQL"]})
        write_json(root / "state/workflow-control.json", {"status": "ACTIVE", "mode": "offline_demo",
                   "external_actions_allowed": False, "final_submission_actor": "simulated_user_only"})
        base_path = root / "resume/Candidate Resume.pdf"
        synthetic_pdf(base_path)
        ledger.register_base(GRADUATION, source_path=str(base_path.relative_to(root)))
        candidate_ref = str(candidate_path.relative_to(root))
        ledger._setting("candidate_answers_ref", candidate_ref)
        ledger._setting("candidate_fact:earliest_start_date", {"value": "2030-05-01", "synthetic": True})
        ledger._setting("preparation_timing_policy", core.IMMEDIATE_PREPARATION)
        ledger.enable_workflow_retention([])

        def result_for(task, output, *, artifacts=None, status="succeeded", issue=None):
            result = {key: task[key] for key in core.IDENTITY}
            result.update(result_id="res_" + task["task_id"] + "_a" + str(task["attempt"]),
                          completed_at=stamp(as_of), status=status, output=output,
                          artifacts=artifacts or [], evidence=[{"kind": "synthetic_fixture",
                          "ref": "fixtures/candidate.json", "observed_at": stamp(as_of)}], issue=issue)
            return result

        def accept(task, result):
            directory = root / "state/staging" / task["task_id"] / str(task["attempt"])
            write_json(directory / "task.json", task)
            ledger.dispatch(task)
            write_json(directory / "result.json", result)
            accepted = ledger.accept(result)
            require(accepted["outcome"] in ("succeeded", "blocked", "applied"),
                    f"{task['stage']} result accepted, not stale: {accepted}")
            transcript.append({"stage": task["stage"], "task_id": task["task_id"],
                               "attempt": task["attempt"], "outcome": accepted["outcome"],
                               "task_state": ledger.db.execute("SELECT state FROM tasks WHERE task_id=?",
                                                               (task["task_id"],)).fetchone()[0],
                               "decision": result["output"].get("decision"),
                               "disposition": result["output"].get("disposition")})
            return accepted

        def draft(stage, job_ids=()):
            stream = io.StringIO()
            with redirect_stdout(stream):
                batch.draft_tasks(ledger, stage, list(job_ids), 10)
            return [json.loads((root / line).read_text(encoding="utf-8"))
                    for line in stream.getvalue().splitlines() if line.strip()]

        intake = modules["jobapply_intake"].Intake(ledger)
        lead = intake.add(company=COMPANY, title="Software Engineer, New Grad",
                          url=OFFICIAL_URL + "?utm_source=demo", req_id=REQ,
                          note="Synthetic manually supplied lead; never fetched.")
        duplicate_lead = intake.add(url=OFFICIAL_URL + "?utm_campaign=another-demo")
        require(lead["lead"]["lead_id"] == duplicate_lead["lead"]["lead_id"], "manual URL tracking dedup")
        checks["manual_lead_tracking_parameters_deduplicated"] = True

        job_ids = []
        for short, source in (("board_a", "linkedin:jobs"), ("board_b", "newgrad-jobs:us")):
            task = {"schema_version": 1, "task_id": "t_demo_discover_" + short, "attempt": 1,
                    "stage": "discover", "scope_key": source, "job_id": None,
                    "input_versions": {"source_config_revision": 5, "scan_window_id": stamp(as_of)},
                    "deadline_at": stamp(as_of + timedelta(hours=2)),
                    "input": {"source_key": source, "since_at": stamp(as_of - timedelta(hours=24)),
                              "until_at": stamp(as_of), "target_filters": {"synthetic": True}, "cursor": None}}
            jd_path = root / "state/staging" / task["task_id"] / "1/jd.txt"
            jd_path.parent.mkdir(parents=True, exist_ok=True)
            jd_path.write_text(JD, encoding="utf-8")
            observation = {"source_listing_id": "demo_" + short,
                           "observed_url": "https://" + short.replace("_", "-") + ".example/jobs/DEMO-100",
                           "company_as_listed": COMPANY, "company_key": "example-labs",
                           "title_as_listed": "Software Engineer, New Grad", "location_as_listed": "US Remote",
                           "published_at_as_listed": stamp(as_of - timedelta(hours=2)),
                           "observed_at": stamp(as_of), "official_req_id": REQ,
                           "official_url": OFFICIAL_URL, "apply_url": APPLY_URL,
                           "listing_state": "open", "published_at": stamp(as_of - timedelta(hours=2)),
                           "published_at_basis": "official_first_published", "closes_at": None,
                           "employment_type": "full_time", "jd_path": str(jd_path.relative_to(root))}
            result = result_for(task, {"observations": [observation],
                                "next_cursor": {"synthetic": True, "rows_read": 1}}, artifacts=[
                                {"kind": "jd", "path": observation["jd_path"], "sha256": checksum(jd_path)}])
            job_ids.extend(accept(task, result)["job_ids"])
        require(len(set(job_ids)) == 1, "same company and requisition merge across sources")
        job_id = job_ids[0]
        require(ledger.db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1, "one canonical job")
        require(ledger.db.execute("SELECT count(*) FROM job_jd_versions").fetchone()[0] == 1,
                "identical JD text reuses its revision")
        checks["two_sources_one_job_and_one_jd_revision"] = True
        intake.resolve(lead["lead"]["lead_id"], job_id)

        task = draft("match", [job_id])[0]
        findings = [{"requirement": "Go or Python programming and SQL basics", "finding": "met",
                     "evidence_ref": "Synthetic candidate PDF: Go service, SQL queries and Python tests"}]
        match_result = result_for(task, {"decision": "no_changes", "hr_findings": [
            {"requirement": "CS degree completed by June 2030", "finding": "met",
             "evidence_ref": "Synthetic candidate PDF: expected Apr2030"}],
            "hm_findings": findings, "edit_plan": [], "open_questions": []})
        accept(task, match_result)
        require(ledger.db.execute("SELECT count(*) FROM job_workflow_admissions").fetchone()[0] == 1,
                "qualified match admits the workflow")
        checks["qualified_current_match_admitted"] = True
        require(ledger.accept(match_result)["outcome"] == "duplicate", "replayed result is idempotent")
        checks["result_replay_is_idempotent"] = True

        task = draft("tailor", [job_id])[0]
        require(task["input"]["mode"] == "export_only", "no unnecessary resume rewriting")
        copy_path = root / "state/staging" / task["task_id"] / "1/Candidate Resume.pdf"
        shutil.copyfile(base_path, copy_path)
        accept(task, result_for(task, {"resume_id": "r_demo_exact_pdf", "graduation_month": GRADUATION,
               "pdf_verified": True, "changes_path": None}, artifacts=[
               {"kind": "pdf", "path": str(copy_path.relative_to(root)), "sha256": checksum(copy_path)}]))
        material = dict(ledger.db.execute("SELECT * FROM resumes WHERE resume_id='r_demo_exact_pdf'").fetchone())
        require(checksum(root / material["pdf_path"]) == checksum(base_path), "promoted PDF is byte-identical")
        checks["job_pdf_is_byte_identical_to_base"] = True

        # An empty, explicitly simulated contacts result is legitimate. It does
        # not manufacture people or delay preparation while outreach is pending.
        contacts = draft("connections", [job_id])
        if contacts:
            accept(contacts[0], result_for(contacts[0], {"candidates": []}))

        task = draft("prepare", [job_id])[0]
        require(task["input"].get("candidate_answers_ref") == candidate_ref,
                "prepare pins only the project-relative synthetic candidate fixture")
        accept(task, result_for(task, {"disposition": "form_blocked", "form_ref": APPLY_URL,
               "blocker_code": "USER_REQUIRED_ANSWERS",
               "next_action": "SIMULATION: applicant must resolve a required answer; no live form exists."},
               status="blocked", issue={"code": "USER_REQUIRED_ANSWERS",
               "message": "Synthetic required-answer blocker", "retry_after_at": None, "needs_user": True}))
        require(draft("prepare") == [], "automatic scans wait for a human-input blocker")
        checks["human_input_blocker_does_not_auto_retry"] = True
        resumed = draft("prepare", [job_id])[0]
        require(resumed["input"].get("existing_form", {}).get("form_ref") == APPLY_URL,
                "targeted recovery retains existing form context")
        require(resumed["input_versions"]["route_fingerprint"] == task["input_versions"]["route_fingerprint"],
                "targeted recovery preserves route")
        accept(resumed, result_for(resumed, {"disposition": "ready_for_user", "form_ref": APPLY_URL,
               "blocker_code": None, "next_action": "SIMULATION: ready for applicant review; no external Submit executed."}))
        checks["targeted_retry_retains_form_and_route"] = True
        require(draft("prepare", [job_id]) == [], "valid ready form is not prepared twice")
        checks["ready_form_is_not_redispatched"] = True

        application = dict(ledger.db.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone())
        stale_update = {"update_id": "demo_stale_manual_update", "action_code": "application_submit_clicked",
                        "job_id": job_id, "occurred_at_pt": stamp(as_of), "expected_version": 0,
                        "evidence_kind": "user_report", "evidence_ref": "Synthetic user click, deliberately stale version"}
        feedback = ledger.apply_manual(stale_update)
        unchanged = dict(ledger.db.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone())
        require(application == unchanged and not feedback.get("event_id"), "stale manual version cannot overwrite state")
        checks["optimistic_manual_update_conflict_preserves_state"] = True
        write_json(root / "fixtures/stale-manual-update.json", {"input": stale_update, "feedback": feedback})

        if with_receipt:
            user_click = {**stale_update, "update_id": "demo_simulated_user_submit",
                          "expected_version": application["row_version"],
                          "evidence_ref": "SIMULATED user click; no real form or submission"}
            feedback = ledger.apply_manual(user_click)
            require(bool(feedback.get("event_id")), "simulated manual user click accepted")
            pending = dict(ledger.db.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone())
            require(pending["state"] == "submission_pending_verification", "click is not proof of submission")
            checks["user_click_alone_is_pending_verification"] = True
            task = draft("track", [job_id])[0]
            receipt = {"event_type": "application_received", "job_id": job_id, "source_kind": "ats",
                       "source_ref": "demo_synthetic_exact_req_receipt", "occurred_at": stamp(as_of),
                       "evidence_kind": "ats_receipt", "evidence_ref": "SIMULATED exact DEMO-100 receipt"}
            accept(task, result_for(task, {"proposed_events": [receipt], "unmatched_evidence": [],
                   "next_cursor": {"synthetic": True, "receipt_id": "demo_synthetic_exact_req_receipt"}}))
            require(draft("prepare", [job_id]) == [], "confirmed simulated submission cannot be reapplied")
            checks["simulated_receipt_prevents_duplicate_application"] = True
            write_json(root / "fixtures/simulated-user-click.json", {"input": user_click, "feedback": feedback})

        final_app = dict(ledger.db.execute("SELECT * FROM applications WHERE job_id=?", (job_id,)).fetchone())
        books = views.export()
        write_json(root / "exports.json", books)
        summary = {"mode": "offline_synthetic_replay", "as_of": stamp(as_of),
                   "external_worker_results": "deterministic fixtures, not live agent executions",
                   "network_calls": 0, "browser_actions": 0, "emails_read_or_sent": 0,
                   "real_applications_submitted": 0, "canonical_jobs": 1, "source_observations": 2,
                   "job_id": job_id, "application_state": final_app["state"],
                   "receipt_simulated": with_receipt, "base_pdf_sha256": checksum(base_path),
                   "job_pdf_sha256": material["pdf_sha256"], "checks": checks,
                   "stages": transcript,
                   "limitations": ["No live discovery, LLM screening or resume tailoring is claimed.",
                                   "No browser form, login, message, signature or real submission exists.",
                                   "Core workbook definitions are rendered as JSON/HTML; no XLSX dependency is needed."]}
        write_json(root / "summary.json", summary)
        render_report(root / "report.html", summary, books)
        require(not ledger.db.execute("PRAGMA foreign_key_check").fetchall(), "foreign keys remain valid")
        require(ledger.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "SQLite integrity")
        return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, help="New or empty disposable directory; default creates a fresh temp directory.")
    parser.add_argument("--with-receipt", action="store_true", help="Also simulate a user click and an exact-requisition ATS receipt.")
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, help="Fixed ISO timestamp with timezone, for repeatable fixtures.")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11) or sqlite3.sqlite_version_info < (3, 37):
        parser.error("Python3.11+ and SQLite3.37+ are required")
    try:
        as_of = datetime.fromisoformat(args.as_of.replace("Z", "+00:00"))
        if as_of.tzinfo is None:
            raise ValueError("include a timezone")
        as_of = as_of.astimezone(timezone.utc).replace(microsecond=0)
        if args.output is not None:
            root = args.output.expanduser().resolve()
            if root.exists() and (not root.is_dir() or any(root.iterdir())):
                raise ValueError("--output must be a new or empty directory; existing data is never overwritten")
            root.mkdir(parents=True, exist_ok=True)
        else:
            root = Path(tempfile.mkdtemp(prefix="jobapply-offline-demo-"))
        summary = replay(root, as_of, args.with_receipt)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        parser.exit(1, f"Demo failed: {exc}\n")
    print("Offline synthetic replay completed; no external actions were performed.")
    print(f"Simulated application state: {summary['application_state']} | {len(summary['checks'])} invariants passed")
    print(f"Output: {root}")
    print(f"Open: {root / 'report.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Dispatch and collect the local JSON task envelopes described in docs/architecture.md."""
import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jobapply_freshness import (application_needs_tracking, utc_timestamp, workflow_dispositions,
                               workflow_policy_enabled, match_dispositions)
from jobapply_ledger import (FORM_SCOPE, Ledger, base_resume_input, current_form, current_interview, current_job_pdf,
                            current_reapply_request, current_route_preference, current_preparation_form, IMMEDIATE_PREPARATION,
                            preparation_due, preparation_route, preparation_timing_policy)
from jobapply_scope import candidate_context, match_graduation_month


ROOT = Path(__file__).resolve().parents[1]
PT = ZoneInfo("America/Los_Angeles")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def json_file(path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def collect(ledger, paths):
    accepted = 0
    errors = []
    for path in paths:
        try:
            file = Path(path)
            task = json_file(file.with_name("task.json"))
            result = json_file(file)
            # Existing dispatches are safe to replay. Accept verifies the live attempt.
            ledger.dispatch(task)
            response = ledger.accept(result)
            accepted += 1
            print(json.dumps({"stage": task["stage"], "scope": task["scope_key"], "result": response}, ensure_ascii=False))
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as error:
            errors.append((path, str(error)))
    for path, error in errors:
        print(f"{path}: {error}", file=sys.stderr)
    print(f"Processed {accepted} envelope pairs, {len(errors)} errors.")
    return int(bool(errors))


def rows(db, sql, args=()):
    result = db.execute(sql, args)
    keys = [item[0] for item in result.description]
    return [dict(zip(keys, row)) for row in result.fetchall()]


def first(db, sql, args=()):
    return next(iter(rows(db, sql, args)), {})


def select_route(job, referral_records, now, policy=None, preference=None):
    return preparation_route(job, referral_records, now, policy, preference)


def draft_tasks(ledger, stage, job_ids, limit):
    db = ledger.db
    settings = {p["key"]: json.loads(p["value_json"]) for p in rows(db, "SELECT * FROM settings")}
    base = first(db, "SELECT * FROM resumes WHERE resume_id=?", (settings.get("active_base_resume_id"),))
    now = now_utc()
    timing_policy = preparation_timing_policy(db)
    answers_ref = settings.get("candidate_answers_ref")
    if isinstance(answers_ref, str):
        answers_path = ROOT / answers_ref
        if (Path(answers_ref).is_absolute() or not answers_path.resolve().is_relative_to(ROOT.resolve())
                or not answers_path.is_file()):
            answers_ref = None
    else:
        answers_ref = None
    selection = "AND j.job_id IN (" + ",".join("?" for _ in job_ids) + ")" if job_ids else ""
    order = """
        julianday(CASE WHEN j.published_at_basis IN ('official_first_published', 'official_date_only')
                       THEN j.published_at END) DESC,
        julianday(j.last_verified_at) DESC,
        CASE WHEN lower(j.title) LIKE '%backend%' OR lower(j.title) LIKE '%infrastructure%' THEN 0 ELSE 1 END,
        j.job_id
    """ if stage == "match" else "COALESCE(j.published_at,j.first_seen_at) DESC, j.job_id"
    jobs = rows(db, f"""
        SELECT j.*, c.display_name AS company, d.file_path AS jd_path
        FROM jobs j JOIN companies c USING(company_key)
        JOIN job_jd_versions d ON d.job_id=j.job_id AND d.jd_revision=j.current_jd_revision
        WHERE (j.listing_state='open' OR ?='track') {selection}
        ORDER BY {order}
    """, (stage, *job_ids))
    freshness = (match_dispositions if stage == "match" else workflow_dispositions)(db, jobs, now)
    admitted = ({r["job_id"] for r in db.execute("SELECT job_id,admitted_at FROM job_workflow_admissions")
                 if (stamp := utc_timestamp(r["admitted_at"])) and stamp <= now}
                if workflow_policy_enabled(db, now) else set())
    emitted = 0
    for job in jobs:
        if emitted >= limit:
            break
        job_id = job["job_id"]
        context = candidate_context(db, job)
        if stage != "track" and context["requires_review"]:
            continue
        if stage != "track" and freshness[job_id] != "active":
            continue
        version = job["current_jd_revision"]
        current_match = ledger._current_match(job_id, version, base.get("resume_id"))
        match = dict(current_match) if current_match else {}
        app = first(db, "SELECT * FROM applications WHERE job_id=?", (job_id,))
        preference = current_route_preference(db, job)
        if stage == "connections" and preference and not preference["seek_referral"]:
            continue
        if (stage == "track" and freshness[job_id] != "active" and job_id not in admitted
                and not application_needs_tracking(app)):
            continue
        if stage in {"tailor", "connections", "prepare"} and match.get("decision") not in {"no_changes", "needs_edits"}:
            continue
        if stage in {"match", "tailor"} and not base:
            raise ValueError("No current base resume is registered")
        interview = current_interview(db, match) if stage in {"tailor", "prepare"} else None
        pdf = current_job_pdf(db, match, interview) if stage in {"tailor", "prepare"} else None
        contacts = rows(db, """SELECT j.*,c.display_name,c.linkedin_url FROM job_contacts j
                         JOIN contacts c USING(contact_id) WHERE j.job_id=? ORDER BY j.rank""", (job_id,))
        ref = first(db, "SELECT * FROM job_referrals WHERE job_id=?", (job_id,))
        if stage == "match":
            if match:
                continue
            versions = {"jd_revision": version, "base_resume_id": base["resume_id"],
                        "candidate_constraints_revision": context["candidate_constraints_revision"]}
            data = {"jd_path": job["jd_path"], **base_resume_input(base),
                    "candidate_constraints": {"graduation_month": context["graduation_month"],
                                              "employment_type": context["employment_type"],
                                              "availability": context["availability"], "locations": "US / US Remote",
                                              "target": ("Software development internship" if
                                                         context["employment_type"] == "internship" else
                                                         "Full-time software engineering"),
                                              "sponsorship": settings.get("candidate_sponsorship"),
                                              "experience_evidence": "current_resume_only"}}
            if "career_scope" in context:
                data["candidate_constraints"]["career_scope"] = context["career_scope"]
            if "application_directive" in context:
                data["candidate_constraints"]["application_directive"] = context["application_directive"]
        elif stage == "tailor":
            if pdf or match["decision"] == "needs_edits" and not interview:
                continue
            versions = {"jd_revision": version, "base_resume_id": base["resume_id"]}
            data = {"jd_path": job["jd_path"], **base_resume_input(base),
                    "mode": "tailor" if interview else "export_only",
                    "graduation_month": match_graduation_month(db, match), "edit_plan": []}
            if interview:
                versions["interview_sha256"] = interview["interview_sha256"]
                data["approved_edits"] = interview["packet"]["approved_edits"]
        elif stage == "connections":
            versions = {"jd_revision": version}
            existing = rows(db, """SELECT t.*, c.outreach_state, c.linkedin_url FROM company_contacts t
                            JOIN contacts c USING(contact_id) WHERE t.company_key=? ORDER BY t.contact_id""", (job["company_key"],))
            data = {"company_key": job["company_key"], "company_name": job["company"], "role_title": job["title"],
                    "relationship_priority": ["high_school", "university", "former_coworker", "graduate_school"],
                    "existing_contacts": existing}
        elif stage == "track":
            if not app and not contacts and not job_ids and job_id not in admitted:
                continue
            cursor = settings.get(f"track_cursor:{job_id}")
            versions = {"application_version": app.get("row_version", 0), "scan_window_id": now.astimezone(PT).strftime("%Y-%m-%d-%H")}
            messages = rows(db, "SELECT DISTINCT source_ref FROM events WHERE job_id=? AND source_kind='gmail'", (job_id,))
            data = {"official_req_id": job["official_req_id"], "company_name": job["company"], "role_title": job["title"],
                    "since_at": (now - timedelta(days=7)).isoformat(), "known_message_refs": [m["source_ref"] for m in messages],
                    "job_contacts": [{"contact_id": c["contact_id"], "display_name": c["display_name"]} for c in contacts],
                    "current_application_state": app.get("state", "not_started"), "submit_clicked_at": app.get("submit_clicked_at"),
                    "pending_submit_event_id": app.get("pending_submit_event_id"), "cursor": cursor}
        else:
            # Automatic scans must wait for user input; an explicit job target
            # resumes the form after the user supplies answers or logs in.
            if not job_ids and app.get("blocker_code") in {
                    "AUTHENTICATION_REQUIRED", "USER_REQUIRED_ANSWERS", "USER_PROFILE_UPDATE_REQUIRED"}:
                continue
            reapply = current_reapply_request(db, job)
            if app.get("blocker_code") == "POSSIBLE_DUPLICATE_APPLICATION" and not reapply:
                continue
            if app.get("state", "not_started") not in {"not_started", "form_blocked", "ready_for_user"} or not pdf:
                continue
            if not job_ids:
                # A blocker event may bump application_version and therefore
                # change task_id. Keep the latest retry cooldown for this JD/PDF.
                retry = first(db, """SELECT t.run_after_at FROM tasks t
                    JOIN task_attempts a ON a.task_id=t.task_id AND a.attempt=t.current_attempt
                    WHERE t.stage='prepare' AND t.job_id=? AND t.state='retry_wait'
                    AND json_extract(a.task_json, '$.input_versions.jd_revision')=?
                    AND json_extract(a.task_json, '$.input_versions.resume_id')=?
                    ORDER BY julianday(t.updated_at) DESC, julianday(t.created_at) DESC, t.task_id DESC
                    LIMIT 1""", (job_id, version, pdf["resume_id"]))
                if (retry_at := utc_timestamp(retry.get("run_after_at"))) and retry_at > now:
                    continue
            if timing_policy == IMMEDIATE_PREPARATION and app.get("route") == "employee_submission":
                continue
            if not preparation_due(job, now, timing_policy):
                continue
            route = select_route(job, ([ref] if ref else []) + contacts, now, timing_policy, preference)
            if not route or not route[1]:
                continue
            retained_form = current_preparation_form(app, version, pdf["resume_id"])
            if retained_form and app.get("state") == "ready_for_user":
                continue
            if retained_form:
                # A late referral is a separate user choice. Resume the saved
                # form and keep its entry fingerprint instead of replacing it.
                if app.get("route") not in {"direct", "referral_link"} or not retained_form.get("route_fingerprint"):
                    continue
                if preference and preference["route"] == "direct" and app["route"] != "direct":
                    continue
                route = app["route"], app["form_ref"], None
            route_type, url, evidence = route
            versions = {"jd_revision": version, "resume_id": pdf["resume_id"], "application_version": app.get("row_version", 0),
                        "route_fingerprint": digest(route)}
            if retained_form:
                versions["route_fingerprint"] = retained_form["route_fingerprint"]
            data = {"official_req_id": job["official_req_id"], "apply_url": job["apply_url"], "route": route_type,
                    "referral_url": url if route_type == "referral_link" else None, "resume_pdf_path": pdf["pdf_path"],
                    "resume_pdf_sha256": pdf["pdf_sha256"], "graduation_month": pdf["graduation_month"],
                    "candidate_answers_ref": answers_ref,
                    "employment_type": context["employment_type"], "availability": context["availability"],
                    "current_application_state": app.get("state", "not_started")}
            if timing_policy == IMMEDIATE_PREPARATION:
                data["preparation_timing_policy"] = timing_policy
            if reapply:
                data["reapply_request"] = reapply
            if preference:
                data["route_preference"] = preference
            existing_form = current_form(app, {key: versions[key] for key in FORM_SCOPE})
            if existing_form:
                data["existing_form"] = existing_form
        identity = [job_id, versions]
        if stage == "prepare" and data.get("reapply_request"):
            identity.append(data["reapply_request"])
        if stage == "prepare" and data.get("route_preference"):
            identity.append(data["route_preference"])
        if stage == "prepare" and data.get("preparation_timing_policy"):
            identity.append(data["preparation_timing_policy"])
        task_id = "t_" + stage + "_" + digest(identity)[:24]
        attempt = 1
        previous = first(db, "SELECT * FROM tasks WHERE task_id=?", (task_id,))
        if stage == "prepare" and previous and previous["state"] in {"retry_wait", "blocked"}:
            if not job_ids and previous["run_after_at"] and datetime.fromisoformat(previous["run_after_at"]) > now:
                continue
            attempt = previous["current_attempt"] + 1
        directory = ROOT / "state/staging" / task_id / str(attempt)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "task.json"
        if target.exists():
            # Leave completed envelopes and the originally dispatched input intact.
            if (directory / "result.json").exists():
                continue
            task = json_file(target)
        else:
            task = {"schema_version": 1, "task_id": task_id, "attempt": attempt, "stage": stage, "scope_key": job_id,
                    "job_id": job_id, "input_versions": versions, "deadline_at": (now + timedelta(hours=2)).isoformat(),
                    "input": data}
            with target.open("x", encoding="utf-8") as out:
                json.dump(task, out, ensure_ascii=False, indent=2)
        if ledger.dispatch(task)["outcome"] == "superseded":
            continue
        print(target.relative_to(ROOT))
        emitted += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    batch = commands.add_parser("collect")
    batch.add_argument("results", nargs="+")
    tasks = commands.add_parser("tasks")
    tasks.add_argument("stage", choices=["match", "tailor", "connections", "prepare", "track"])
    tasks.add_argument("--limit", type=int, default=6)
    tasks.add_argument("job_ids", nargs="*")
    args = parser.parse_args()
    ledger = Ledger(root=ROOT)
    if args.command == "collect":
        return collect(ledger, args.results)
    draft_tasks(ledger, args.stage, args.job_ids, args.limit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

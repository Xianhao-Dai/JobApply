#!/usr/bin/env python3
"""Configure an isolated .jobapply workspace and draft one run's v1 tasks.

This command performs local file/SQLite operations only. It does not run workers,
open websites, access mail, create accounts, send messages, submit applications,
or install a schedule. Configuration is private and relative to its .jobapply
directory; copy config.example.json there and supply your own confirmed facts.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlsplit

from jobapply_freshness import SOURCE_CONFIG_REVISION
from jobapply_intake import Intake
from jobapply_ledger import ContractError, IMMEDIATE_PREPARATION, Ledger


REPOSITORY = Path(__file__).resolve().parents[1]
SOURCES = {
    "linkedin:jobs": "https://www.linkedin.com/jobs/",
    "newgrad-jobs:us": "https://www.newgrad-jobs.com/",
    "speedyapply:2027-us-newgrad": "https://github.com/speedyapply/2027-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md",
    "glassdoor:jobs": None,
}
CAPABILITIES = {"web_research", "prepare_forms", "contact_research", "mail_read"}
CAREER_LEVELS = {"internship", "new_grad", "early_career", "experienced_2_3_years"}


def fail(message):
    raise ValueError(message)


def object_keys(value, allowed, label, required=()):
    if not isinstance(value, dict):
        fail(label + " must be an object")
    unknown = set(value) - set(allowed)
    missing = set(required) - set(value)
    if unknown or missing:
        fail(label + ": unknown keys " + repr(sorted(unknown)) + "; missing keys " + repr(sorted(missing)))
    return value


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail("JSON contains a duplicate key: " + key)
        value[key] = item
    return value


def read_json(path):
    if path.stat().st_size > 256 * 1024:
        fail("Configuration/control file is unexpectedly large")
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)


def relative_path(root, ref, label, *, existing=False):
    if not isinstance(ref, str) or not ref.strip() or "\x00" in ref:
        fail(label + " must be a nonempty workspace-relative path")
    part = Path(ref)
    if part.is_absolute() or ".." in part.parts or ref != ref.strip():
        fail(label + " must stay inside the .jobapply workspace")
    current = root
    for component in part.parts:
        current = current / component
        if current.is_symlink():
            fail(label + " cannot use a symbolic link")
    result = root / part
    if not result.resolve().is_relative_to(root):
        fail(label + " escapes the workspace")
    if existing and not result.is_file():
        fail(label + " does not name an existing regular file")
    return result


def workspace_config(path):
    path = path.expanduser().absolute()
    if path.is_symlink() or path.parent.is_symlink():
        fail("Config and its .jobapply directory cannot be symbolic links")
    root = path.parent.resolve()
    if path.parent.name != ".jobapply" or root.name != ".jobapply":
        fail("Config must live directly inside a private .jobapply directory")
    if root.is_relative_to(REPOSITORY) and root != REPOSITORY / ".jobapply":
        fail("Inside this repository only the root .jobapply directory is a runtime workspace")
    if not path.is_file() or path.resolve().parent != root:
        fail("Config file does not exist inside its .jobapply workspace")
    # Ledger and task output paths must not follow preexisting links out of it.
    for item in ("state", "state/jobapply.sqlite", "state/staging", "state/workflow-control.json",
                 "resume", "positions", "connection", "applications"):
        relative_path(root, item, "Runtime path")
    return path.resolve(), root


def nullable_boolean(value, label):
    if value is not None and type(value) is not bool:
        fail(label + " must be true, false or null (unknown)")


def month(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        fail(label + " must be an explicitly confirmed YYYY-MM month")


def optional_date(value, label):
    if value is not None:
        if not isinstance(value, str):
            fail(label + " must be YYYY-MM-DD or null")
        try:
            date.fromisoformat(value)
        except ValueError:
            fail(label + " must be YYYY-MM-DD or null")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            fail(label + " must be YYYY-MM-DD or null")


def string_list(value, label, *, allowed=None):
    if (not isinstance(value, list) or not value or len(value) > 30 or
            any(not isinstance(x, str) or not x.strip() or len(x) > 150 or
                any(ord(c) < 32 for c in x) for x in value) or len(set(value)) != len(value)):
        fail(label + " must be a nonempty list of distinct short strings")
    if allowed is not None and not set(value) <= allowed:
        fail(label + " contains an unsupported value")


def public_https(value, label):
    if not isinstance(value, str) or len(value) > 4096 or any(c.isspace() for c in value):
        fail(label + " must be a plain HTTPS URL")
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError:
        fail(label + " has an invalid URL authority")
    if (url.scheme != "https" or not url.hostname or url.username is not None or
            url.password is not None or port not in (None, 443)):
        fail(label + " must use HTTPS without credentials or a nonstandard port")
    host = url.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")) or "." not in host:
        fail(label + " cannot target a local host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        # A hostname is not resolved: startup must never perform DNS/network I/O.
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host):
            fail(label + " contains an invalid hostname")
    if address is not None and not address.is_global:
        fail(label + " cannot target a private/reserved IP address")
    return url


def source_url(key, value):
    if value is None:
        return SOURCES[key]
    url = public_https(value, "Source URL")
    host, path = url.hostname.lower(), url.path
    if key == "linkedin:jobs":
        valid = host in {"linkedin.com", "www.linkedin.com"} and (path == "/jobs" or path.startswith("/jobs/"))
    elif key == "newgrad-jobs:us":
        valid = host in {"newgrad-jobs.com", "www.newgrad-jobs.com"}
    elif key == "speedyapply:2027-us-newgrad":
        valid = (host == "github.com" and path == "/speedyapply/2027-SWE-College-Jobs/blob/main/NEW_GRAD_USA.md")
    else:
        valid = host in {"glassdoor.com", "www.glassdoor.com", "help.glassdoor.com"}
    if not valid:
        fail("Source URL does not match its approved source: " + key)
    return value


def validated_config(raw, root):
    object_keys(raw, {"schema_version", "resume", "candidate", "scope", "sources", "limits", "capabilities"},
                "config", {"schema_version", "resume", "candidate", "scope", "limits", "capabilities"})
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        fail("Only config schema_version 1 is supported")
    resume = object_keys(raw["resume"], {"path", "sha256"}, "resume", {"path", "sha256"})
    candidate = object_keys(raw["candidate"], {"graduation_month", "earliest_start_date", "work_authorization",
        "future_sponsorship_required", "answers_ref", "internship_plan"}, "candidate", {"graduation_month"})
    month(candidate["graduation_month"], "candidate.graduation_month")
    optional_date(candidate.get("earliest_start_date"), "candidate.earliest_start_date")
    for key in ("work_authorization", "future_sponsorship_required"):
        nullable_boolean(candidate.get(key), "candidate." + key)
    pdf = relative_path(root, resume["path"], "resume.path", existing=True)
    if pdf.stat().st_size > 20 * 1024 * 1024:
        fail("Approved resume PDF exceeds the 20MiB local input bound")
    expected = resume["sha256"]
    if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
        fail("resume.sha256 must be null or the lowercase SHA-256 of your approved PDF")
    if pdf.suffix.lower() != ".pdf" or pdf.read_bytes()[:5] != b"%PDF-":
        fail("resume.path must be an approved PDF")
    actual_hash = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if expected is not None and actual_hash != expected:
        fail("Approved resume hash does not match the file; no ledger changes were made")
    answers = candidate.get("answers_ref")
    if answers is not None:
        answer_path = relative_path(root, answers, "candidate.answers_ref", existing=True)
        if answer_path.stat().st_size > 2 * 1024 * 1024:
            fail("Candidate answers exceed the 2MiB local input bound")
        if answer_path.suffix.lower() not in {".md", ".txt", ".json"}:
            fail("Candidate answers must be a local .md, .txt or .json file")
    scope = object_keys(raw["scope"], {"employment_types", "locations", "career_levels", "max_required_experience_years"},
                        "scope", {"employment_types", "locations", "career_levels", "max_required_experience_years"})
    string_list(scope["employment_types"], "scope.employment_types", allowed={"full_time", "internship"})
    string_list(scope["locations"], "scope.locations")
    string_list(scope["career_levels"], "scope.career_levels", allowed=CAREER_LEVELS)
    years = scope["max_required_experience_years"]
    if type(years) is not int or not 0 <= years <= 3:
        fail("scope.max_required_experience_years must be an integer from 0 through 3")
    plan = candidate.get("internship_plan")
    if plan is not None:
        object_keys(plan, {"graduation_month", "earliest_start_date", "max_duration_months", "plan_status",
                          "school_approval", "return_to_study_details"}, "candidate.internship_plan",
                         {"graduation_month", "earliest_start_date", "max_duration_months"})
        month(plan["graduation_month"], "internship_plan.graduation_month")
        optional_date(plan["earliest_start_date"], "internship_plan.earliest_start_date")
        if plan["earliest_start_date"] is None or type(plan["max_duration_months"]) is not int or not 1 <= plan["max_duration_months"] <= 12:
            fail("Internship plan needs a real earliest date and 1-12 maximum calendar months")
        nullable_boolean(plan.get("school_approval"), "internship_plan.school_approval")
        for key in ("plan_status", "return_to_study_details"):
            if plan.get(key) is not None and (not isinstance(plan[key], str) or len(plan[key]) > 2000):
                fail("internship_plan." + key + " must be text or null")
    if "internship" in scope["employment_types"] and plan is None:
        fail("Enabling internships requires a separately confirmed internship_plan")
    caps = object_keys(raw["capabilities"], CAPABILITIES, "capabilities", CAPABILITIES)
    if any(type(value) is not bool for value in caps.values()):
        fail("All capability flags must be explicit true/false values")
    limits = object_keys(raw["limits"], {"max_jobs", "max_source_rows", "max_tasks"}, "limits",
                         {"max_jobs", "max_source_rows", "max_tasks"})
    for key, ceiling in (("max_jobs", 100), ("max_source_rows", 2000), ("max_tasks", 100)):
        if type(limits[key]) is not int or not 1 <= limits[key] <= ceiling:
            fail(f"limits.{key} must be an integer from 1 through {ceiling}")
    sources = object_keys(raw.get("sources", {}), SOURCES, "sources")
    normalized_sources = {}
    for key, spec in sources.items():
        object_keys(spec, {"enabled", "url"}, "source " + key, {"enabled"})
        if type(spec["enabled"]) is not bool:
            fail("Source enabled flags must be explicit booleans")
        normalized_sources[key] = {"enabled": spec["enabled"], "url": source_url(key, spec.get("url"))}
    return {**raw, "resume": {**resume, "sha256": actual_hash}, "sources": normalized_sources}


def assert_active_or_new(root):
    control = root / "state/workflow-control.json"
    if control.is_file():
        value = read_json(control)
        if not isinstance(value, dict) or value.get("status") != "ACTIVE":
            fail("Existing workspace control is not ACTIVE; startup never overrides a pause")
    database = root / "state/jobapply.sqlite"
    if database.is_file():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='settings'").fetchone():
                row = db.execute("SELECT value_json FROM settings WHERE key='workflow_execution_control'").fetchone()
                if row:
                    value = json.loads(row[0])
                    if not isinstance(value, dict) or value.get("status") != "ACTIVE":
                        fail("Existing ledger control is not ACTIVE; startup never overrides a pause")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as out:
        pending = Path(out.name)
        json.dump(value, out, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")
    try:
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def start(config_path, lead=None):
    path, root = workspace_config(Path(config_path))
    cfg = validated_config(read_json(path), root)
    if lead is not None:
        public_https(lead, "Manual lead")
    assert_active_or_new(root)
    started = datetime.now(timezone.utc)
    now = started.replace(microsecond=0)
    iso = lambda value: value.isoformat(timespec="seconds").replace("+00:00", "Z")
    run_id = started.strftime("%Y%m%dT%H%M%S%fZ")
    tasks = []
    deferred = []
    ledger = Ledger(root=root)
    try:
        base = ledger.register_base(cfg["candidate"]["graduation_month"], source_path=cfg["resume"]["path"])
        if base["base_pdf_sha256"] != cfg["resume"]["sha256"]:
            fail("Resume changed during startup; no workers were dispatched. Review the PDF and start again.")
        candidate = cfg["candidate"]
        answers = candidate.get("answers_ref")
        answer_hash = hashlib.sha256((root / answers).read_bytes()).hexdigest() if answers else None
        full_snapshot = {key: candidate.get(key) for key in ("graduation_month", "earliest_start_date",
                        "work_authorization", "future_sponsorship_required", "answers_ref")}
        full_snapshot.update(scope=cfg["scope"], answers_sha256=answer_hash)
        previous = ledger.setting("startup_candidate_snapshot")
        internship_snapshot = {"plan": candidate.get("internship_plan"), "scope": cfg["scope"],
                               "work_authorization": candidate.get("work_authorization"),
                               "future_sponsorship_required": candidate.get("future_sponsorship_required"),
                               "answers_ref": answers, "answers_sha256": answer_hash}
        previous_internship = ledger.setting("startup_internship_snapshot")
        unpinned_old_matches = previous is None and bool(ledger.db.execute("SELECT 1 FROM matches LIMIT 1").fetchone())
        with ledger.transaction():
            if unpinned_old_matches or (previous is not None and previous != full_snapshot):
                ledger._setting("candidate_constraints_revision", ledger.setting("candidate_constraints_revision", 1) + 1)
            if candidate.get("internship_plan") is not None:
                if unpinned_old_matches or previous_internship != internship_snapshot:
                    ledger._setting("internship_constraints_revision", ledger.setting("internship_constraints_revision", 0) + 1)
                ledger._setting("internship_plan", candidate["internship_plan"])
            ledger._setting("startup_internship_snapshot", internship_snapshot)
            ledger._setting("startup_candidate_snapshot", full_snapshot)
            ledger._setting("candidate_answers_ref", answers)
            ledger._setting("candidate_sponsorship", candidate.get("future_sponsorship_required"))
            ledger._setting("candidate_work_authorization", candidate.get("work_authorization"))
            ledger._setting("candidate_fact:earliest_start_date", {"value": candidate.get("earliest_start_date"),
                            "evidence_ref": path.name + "#candidate.earliest_start_date"})
            ledger._setting("employment_scope", {**cfg["scope"], "include_experienced_hires":
                            "experienced_2_3_years" in cfg["scope"]["career_levels"], "classify_by_official_jd": True})
            ledger._setting("preparation_timing_policy", IMMEDIATE_PREPARATION)
            ledger._setting("agent_capabilities", cfg["capabilities"])
            ledger._setting("run_limits", cfg["limits"])
            control = {"status": "ACTIVE", "configured_at": iso(now), "config_ref": path.name,
                       "new_application_preparation_allowed": cfg["capabilities"]["prepare_forms"],
                       "agent_final_submission_allowed": False, "final_submission_actor": "user"}
            ledger._setting("workflow_execution_control", control)
        ledger.enable_workflow_retention([])
        atomic_json(root / "state/workflow-control.json", control)
        intake = Intake(ledger)
        lead_result = intake.add(url=lead) if lead is not None else None
        pending = intake.pending()
        # Start the immutable discovery window after first-run policy setup, so
        # its right edge cannot precede workflow_retention_enabled_at.
        now = datetime.now(timezone.utc).replace(microsecond=0)
        window = {"since_at": iso(now - timedelta(hours=24)), "until_at": iso(now)}
        if lead_result:
            target = lead_result["lead"]["lead_id"]
            pending.sort(key=lambda item: item["lead_id"] != target)
        work = [("manual:joblead", {"lead": item}) for item in pending]
        work += [(key, spec) for key, spec in cfg["sources"].items() if spec["enabled"]]
        for index, (source, spec) in enumerate(work):
            if len(tasks) >= cfg["limits"]["max_tasks"]:
                deferred.append({"source_key": source, "lead_id": spec.get("lead", {}).get("lead_id")})
                continue
            task_id = "t_discover_" + run_id + "_" + str(index + 1)
            data = {"source_key": source, **window, "target_filters": cfg["scope"],
                    "cursor": ledger.setting("discover_cursor:" + source),
                    "limit": cfg["limits"]["max_jobs"], "max_rows": cfg["limits"]["max_source_rows"],
                    "capabilities": cfg["capabilities"], "run_id": run_id}
            if source == "manual:joblead":
                item = spec["lead"]
                data.update(lead_id=item["lead_id"], source_listing_id=item["lead_id"],
                            source_url=item["observed_url"], observed_url=item["observed_url"], lead=item,
                            apply_24h_discovery_gate=False)
                needed = "web_research"
            else:
                data.update(source_url=spec["url"], apply_24h_discovery_gate=True)
                needed = "mail_read" if source == "glassdoor:jobs" else "web_research"
            data["required_capability"] = needed
            task = {"schema_version": 1, "task_id": task_id, "attempt": 1, "stage": "discover",
                    "scope_key": source, "job_id": None, "input_versions": {
                        "source_config_revision": SOURCE_CONFIG_REVISION, "scan_window_id": iso(now)},
                    "deadline_at": iso(now + timedelta(hours=2)), "input": data}
            relative = f"state/staging/{task_id}/1/task.json"
            destination = relative_path(root, relative, "Task output")
            atomic_json(destination, task)
            ledger.dispatch(task)
            tasks.append({"path": relative, "task_id": task_id, "stage": "discover", "source_key": source,
                          "lead_id": data.get("lead_id"), "required_capability": needed,
                          "execution_authorized": cfg["capabilities"][needed]})
        return {"schema_version": 1, "workspace": str(root), "config": path.name, "run_id": run_id,
                "scan_window": window, "tasks": tasks, "deferred": deferred, "limits": cfg["limits"],
                "capabilities": {**cfg["capabilities"], "send_messages": False, "send_mail": False,
                                 "final_submit": False}, "base_resume_id": base["base_resume_id"],
                "base_pdf_sha256": cfg["resume"]["sha256"], "manual_lead_outcome":
                    lead_result["outcome"] if lead_result else None,
                "worker_execution_performed": False, "external_io_performed": False,
                "schedule_created": False,
                "notes": ["Task paths are relative to workspace; all workers must use this root.",
                          "Limits apply to the entire run, not independently to each source.",
                          "A drafted task whose execution_authorized is false must wait for capability authorization.",
                          "For manual leads use input.lead_id as observation.source_listing_id, then resolve via Intake.",
                          "No worker, website, mailbox, form or scheduled automation was started by this command."]}
    finally:
        ledger.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=REPOSITORY / ".jobapply/config.json")
    parser.add_argument("--lead", help="Record an explicit HTTPS job lead and draft its verification task")
    args = parser.parse_args(argv)
    try:
        result = start(args.config, args.lead)
    except (ValueError, OSError, ContractError, sqlite3.Error) as exc:
        parser.exit(1, "Startup failed: " + str(exc) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

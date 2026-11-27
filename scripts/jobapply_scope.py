"""Resolve role-specific candidate facts without invalidating unrelated matches."""
import json
import math
import re
from datetime import date


def setting(db, key, default=None):
    row = db.execute("SELECT value_json FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row[0]) if row else default


def normalize_employment_type(value):
    if isinstance(value, dict):
        value = value.get("employment_type")
    if not isinstance(value, str):
        return None
    token = re.sub(r"[\s_-]+", "", value).casefold()
    return {"fulltime": "full_time", "intern": "internship", "internship": "internship", "coop": "internship",
            "unknown": "unknown"}.get(token)


def employment_type(db, job):
    job = dict(job)
    explicit = setting(db, "job_employment_type:" + job["job_id"])
    if explicit is not None:
        return normalize_employment_type(explicit) or "unknown"
    title = job.get("title") or ""
    # Intern conversion is a full-time hiring route, not a new internship.
    if re.search(r"\bintern(?:ship)?[\s-]+(?:conversion|to[\s-]+full[\s-]*time)\b", title, re.I):
        return "full_time"
    intern = re.search(r"\b(?:intern(?:ship)?|co[ -]?op)\b", title, re.I)
    full_time = re.search(r"\bfull[ -]?time\b", title, re.I)
    if intern and full_time:
        return "unknown"
    return "internship" if intern else "full_time"


def career_scope_assessment(title, required_min_years=None, *, ambiguous_requirement=False,
                            max_required_experience_years=3):
    """Assess career scope from an explicit JD minimum, never preferred years.

    This is a discovery/matching aid, not a claim that the candidate satisfies
    the JD. Missing requirements and mixed levels must still be read by Match.
    """
    if (type(max_required_experience_years) not in (int, float) or
            not math.isfinite(max_required_experience_years) or max_required_experience_years < 0):
        raise ValueError("Invalid career-scope experience ceiling")
    if (required_min_years is not None and
            (type(required_min_years) not in (int, float) or not math.isfinite(required_min_years) or required_min_years < 0)):
        raise ValueError("Use the explicit mandatory minimum in years, or None")
    if ambiguous_requirement:
        return {"decision": "needs_review", "reason": "Ambiguous or alternative JD qualification paths"}
    if required_min_years is not None and required_min_years > max_required_experience_years:
        return {"decision": "out_of_scope", "reason": "Mandatory experience minimum exceeds the configured scope"}
    if re.search(r"\b(?:manager|principal|director|staff|apprentice)\b", title, re.I):
        return {"decision": "out_of_scope", "reason": "Title indicates a different career track or seniority"}
    if re.search(r"\b(?:senior|lead|sr\.?)\b", title, re.I):
        return {"decision": "needs_review", "reason": "Senior title needs official JD level verification"}
    if required_min_years is not None:
        return {"decision": "in_scope", "reason": "Explicit mandatory minimum is within the configured range"}
    if re.search(r"\b(?:intern(?:ship)?|co[ -]?op|new grad(?:uate)?s?|early career|"
                 r"(?:university|college) grad(?:uate)?s?|entry.level|junior|associate|"
                 r"(?:engineer|developer) I)\b", title, re.I):
        return {"decision": "in_scope", "reason": "Internship or early-career title; verify remaining JD requirements"}
    return {"decision": "needs_review", "reason": "General or level-II title requires official JD experience evidence"}


def candidate_context(db, job):
    """Keep the unchanged full-time profile and the conditional internship plan distinct."""
    job = dict(job)
    kind = employment_type(db, job)
    base_id = setting(db, "active_base_resume_id")
    base = db.execute("SELECT graduation_month FROM resumes WHERE resume_id=?", (base_id,)).fetchone()
    if kind == "internship":
        plan = setting(db, "internship_plan", {})
        plan = plan if isinstance(plan, dict) else {}
        availability = {key: plan.get(key) for key in (
            "earliest_start_date", "max_duration_months", "plan_status", "school_approval",
            "return_to_study_details")}
        revision = setting(db, "internship_constraints_revision")
        month = plan.get("graduation_month")
        try:
            date.fromisoformat(availability["earliest_start_date"])
            valid_date = True
        except (TypeError, ValueError):
            valid_date = False
        duration = availability["max_duration_months"]
        needs_review = not (type(revision) is int and revision >= 1 and isinstance(month, str) and
                            re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) and valid_date and
                            type(duration) is int and duration > 0)
    else:
        fact = setting(db, "candidate_fact:earliest_start_date", {})
        availability = {"earliest_start_date": fact.get("value"), "max_duration_months": None}
        revision = setting(db, "candidate_constraints_revision", 1)
        month = base[0] if base else None
        needs_review = kind == "unknown"
    scope = setting(db, "employment_scope", {})
    if isinstance(scope, dict) and "employment_types" in scope and kind not in scope["employment_types"]:
        needs_review = True
    context = {"employment_type": kind, "candidate_constraints_revision": revision,
               "graduation_month": month, "availability": availability, "requires_review": needs_review}
    if isinstance(scope, dict) and "career_levels" in scope:
        context["career_scope"] = {key: scope.get(key) for key in (
            "career_levels", "max_required_experience_years", "include_experienced_hires", "classify_by_official_jd")}
    directive = setting(db, "job_application_directive:" + job["job_id"])
    if _valid_application_directive(directive, job, base_id, revision):
        context["candidate_constraints_revision"] = directive["candidate_constraints_revision"]
        context["application_directive"] = directive
    return context


def _valid_application_directive(directive, job, base_id, revision):
    """An explicit attempt changes one match identity, never candidate facts."""
    if not isinstance(directive, dict):
        return False
    scoped_revision = directive.get("candidate_constraints_revision")
    return (directive.get("job_id") == job["job_id"] and
            isinstance(job.get("official_req_id"), str) and bool(job["official_req_id"].strip()) and
            directive.get("official_req_id") == job["official_req_id"] and
            type(directive.get("jd_revision")) is int and
            directive["jd_revision"] == job.get("current_jd_revision") and
            isinstance(base_id, str) and bool(base_id) and directive.get("base_resume_id") == base_id and
            directive.get("mode") == "user_directed_attempt" and
            directive.get("accepted_gap") == "industry_experience" and
            directive.get("evidence_kind") == "user_report" and
            isinstance(directive.get("evidence_ref"), str) and bool(directive["evidence_ref"].strip()) and
            type(revision) is int and revision > 0 and
            type(scoped_revision) is int and scoped_revision > revision)


def constraints_match_context(context, constraints):
    if context["requires_review"] or not isinstance(constraints, dict):
        return False
    if ("career_scope" in context or "career_scope" in constraints) and constraints.get("career_scope") != context.get("career_scope"):
        return False
    # Require the exact user instruction snapshot, including its pinned scope.
    # Its removal or replacement invalidates the task even when numbers agree.
    if ("application_directive" in context or "application_directive" in constraints) and (
            "application_directive" not in context or "application_directive" not in constraints or
            json.dumps(constraints["application_directive"], sort_keys=True) !=
            json.dumps(context["application_directive"], sort_keys=True)):
        return False
    keys = ("employment_type", "graduation_month", "availability")
    if context["employment_type"] == "internship":
        return all(constraints.get(key) == context[key] for key in keys)
    # Previously accepted full-time task envelopes predate role-scoped fields.
    return all(key not in constraints or constraints[key] == context[key] for key in keys)


def match_constraints(db, match):
    match = dict(match)
    row = db.execute("SELECT task_json FROM task_attempts WHERE task_id=? "
                     "ORDER BY attempt DESC LIMIT 1", (match.get("task_id"),)).fetchone()
    return json.loads(row[0]).get("input", {}).get("candidate_constraints", {}) if row else {}


def match_context_is_current(db, match):
    match = dict(match)
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (match["job_id"],)).fetchone()
    if not job:
        return False
    context = candidate_context(db, job)
    if (match["candidate_constraints_revision"] != context["candidate_constraints_revision"] or
            not constraints_match_context(context, match_constraints(db, match))):
        return False
    if match.get("decision") == "no_changes":
        base = db.execute("SELECT graduation_month FROM resumes WHERE resume_id=?",
                          (match["base_resume_id"],)).fetchone()
        if not base or match_graduation_month(db, match) != base[0]:
            return False
    return True


def match_graduation_month(db, match):
    """Use the immutable match input; only legacy full-time matches may use the base month."""
    match = dict(match)
    constraints = match_constraints(db, match)
    if constraints.get("graduation_month"):
        return constraints["graduation_month"]
    job = db.execute("SELECT * FROM jobs WHERE job_id=?", (match["job_id"],)).fetchone()
    if not job or employment_type(db, job) != "full_time":
        return None
    base = db.execute("SELECT graduation_month FROM resumes WHERE resume_id=?",
                      (match["base_resume_id"],)).fetchone()
    return base[0] if base else None

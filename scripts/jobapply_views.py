"""Read the SQLite ledger and build the data behind the JobApply workbooks."""
import argparse
import csv
import json
import sqlite3
import sys
import tempfile
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from jobapply_freshness import (CURRENT_SOURCES, WINDOW, application_needs_tracking, dispositions, exists,
                               source_repost_floor, source_time_floor, utc_timestamp, visible_lead,
                               workflow_dispositions)
from jobapply_ledger import (IMMEDIATE_PREPARATION, current_interview, current_job_pdf,
                            current_preparation_form, current_reapply_request, current_route_preference,
                            preparation_due, preparation_route, preparation_timing_policy)
from jobapply_scope import match_context_is_current


ROOT = Path(__file__).resolve().parents[1]
PT = ZoneInfo("America/Los_Angeles")
MANUAL = (
    "update_id action_code company_key job_id contact_id occurred_at_pt expected_version "
    "evidence_kind evidence_ref referral_url pending_submit_event_id submitted_at_pt note "
    "import_status imported_at_pt feedback"
)
MATCH = (
    "job_id company_key company official_req_id title location official_url effective_posted_at_pt freshness_basis "
    "discovery_status workflow_admitted_at_pt jd_revision "
    "base_resume_id candidate_constraints_revision decision hr_findings hm_findings "
    "edit_plan open_questions resume_pdf decided_at_pt"
)


def connect(root):
    db = sqlite3.connect(f"{(root / 'state/jobapply.sqlite').as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    return db


def table(db, sql, params=()):
    return [dict(row) for row in db.execute(sql, params)]


def csv_export(root, relative, fields, rows):
    target = root / relative
    target.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="", dir=target.parent, delete=False) as handle:
        pending = Path(handle.name)
        try:
            writer = csv.DictWriter(handle, fields)
            writer.writeheader()
            for row in rows:
                values = {key: row.get(key) for key in fields}
                for key, value in values.items():
                    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                        values[key] = "'" + value
                writer.writerow(values)
            handle.flush()
            pending.replace(target)
        finally:
            pending.unlink(missing_ok=True)


def sheet(name, title, fields, rows):
    columns = fields.split()
    return {
        "name": name, "title": title, "columns": columns,
        "rows": [[row.get(key) for key in columns] for row in rows],
    }


def readable(values):
    values = json.loads(values or "[]")
    return "\n".join(
        f"{v['requirement']} · {v['finding']} · {v.get('evidence_ref') or ''}".strip(" ·")
        if isinstance(v, dict) and {"requirement", "finding"} <= v.keys() else str(v)
        for v in values
    )


def freshness_metadata(job, sources, now, manual=False, retained=False):
    verified = [s for s in sources if s["source_key"] in CURRENT_SOURCES and s["verification_state"] == "verified"]
    reposts = [(stamp, source) for source in verified if (stamp := source_repost_floor(source))]
    latest = max(reposts, key=lambda pair: pair[0]) if reposts else None
    metadata = {key: None for key in ("reposted_at", "reposted_at_basis", "repost_observed_at", "repost_label")}
    if latest:
        stamp, source = latest
        metadata.update(reposted_at=stamp.isoformat(),
                        reposted_at_basis=source.get("reposted_at_basis") or "source_relative_lower_bound",
                        repost_observed_at=source.get("repost_observed_at") or source["last_seen_at"],
                        repost_label=source.get("repost_label") or source["published_at_as_listed"])
    posted = utc_timestamp(job.get("published_at"))
    source_posts = [stamp for source in verified if (stamp := source_time_floor(source))]
    if latest and now - WINDOW <= latest[0] <= now:
        effective, basis = latest[0].isoformat(), "source_repost"
    elif posted and now - WINDOW <= posted <= now:
        effective, basis = posted.isoformat(), job.get("published_at_basis")
    elif not posted and source_posts and now - WINDOW <= max(source_posts) <= now:
        effective, basis = max(source_posts).isoformat(), "source_publication"
    elif retained and latest and latest[0] <= now:
        effective, basis = latest[0].isoformat(), "source_repost"
    elif retained and not posted and source_posts and max(source_posts) <= now:
        effective, basis = max(source_posts).isoformat(), "source_publication"
    elif manual:
        effective, basis = None, "manual_override"
    else:
        effective, basis = job.get("published_at"), job.get("published_at_basis")
    return {**metadata, "effective_posted_at": effective, "freshness_basis": basis,
            "effective_posted_at_pt": effective}


def export(root=None):
    root = Path(root or ROOT).resolve()
    with connect(root) as db:
        jobs = table(db, """
          SELECT j.*, c.display_name company FROM jobs j JOIN companies c USING (company_key)
          ORDER BY COALESCE(j.published_at,'') DESC, j.first_seen_at DESC, j.job_id
        """)
        job_by_id = {job["job_id"]: job for job in jobs}
        now = datetime.now(timezone.utc)
        freshness = dispositions(db, jobs, now)
        active_ids = {job_id for job_id, disposition in freshness.items() if disposition == "active"}
        workflow = workflow_dispositions(db, jobs, now)
        workflow_ids = {job_id for job_id, disposition in workflow.items() if disposition == "active"}
        admissions = {r["job_id"]: r["admitted_at"] for r in table(db, "SELECT * FROM job_workflow_admissions")} if exists(db, "job_workflow_admissions") else {}
        retained_ids = {job_id for job_id in admissions
                        if workflow[job_id] in {"active", "open_status_unverified"}}
        visible_ids = (active_ids | retained_ids) - {
            job_id for job_id, disposition in workflow.items() if disposition == "withdrawn"}
        sources = table(db, "SELECT * FROM job_sources ORDER BY last_seen_at DESC")
        manual_leads = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='manual_job_leads' AND type='table'").fetchone():
            manual_leads = table(db, """
              SELECT 'manual:joblead' source_key, lead_id source_listing_id, company company_as_listed,
                     title title_as_listed, location location_as_listed, observed_url,
                     first_seen_at, last_seen_at, job_id,
                     CASE status WHEN 'verified' THEN 'verified' ELSE 'unverified' END verification_state
              FROM manual_job_leads ORDER BY last_seen_at DESC
            """)
        source_keys = {}
        sources_by_job = {}
        for row in sources:
            source_keys.setdefault(row["job_id"], set()).add(row["source_key"])
            sources_by_job.setdefault(row["job_id"], []).append(row)
        manual_ids = {r[0] for r in db.execute("SELECT job_id FROM manual_job_leads WHERE status='verified'")} if exists(db, "manual_job_leads") else set()
        for job in jobs:
            job["jd_revision"] = job["current_jd_revision"]
            job["source_keys"] = ", ".join(sorted(source_keys.get(job["job_id"], [])))
            job.update(freshness_metadata(job, sources_by_job.get(job["job_id"], []), now,
                                          job["job_id"] in manual_ids, job["job_id"] in admissions))
            job["discovery_status"] = freshness[job["job_id"]]
            job["workflow_admitted_at_pt"] = admissions.get(job["job_id"])
        csv_export(root,
            "positions/discovered.csv",
            ("job_id company_key company official_req_id title location listing_state official_url "
             "published_at published_at_basis reposted_at reposted_at_basis repost_observed_at repost_label "
             "effective_posted_at freshness_basis first_seen_at last_verified_at closes_at jd_revision source_keys").split(),
            [j for j in jobs if j["job_id"] in active_ids],
        )
        csv_export(root,
            "positions/curated_leads.csv",
            ("source_key source_listing_id company_as_listed title_as_listed location_as_listed "
             "observed_url published_at_as_listed reposted_at reposted_at_basis repost_observed_at repost_label "
             "first_seen_at last_seen_at verification_state job_id").split(),
            [m for m in manual_leads if not m["job_id"] or m["job_id"] in active_ids] +
            [s for s in sources if s["source_key"] in CURRENT_SOURCES and
             (s["job_id"] in active_ids if s["job_id"] else visible_lead(s, now))],
        )
        settings = {r["key"]: json.loads(r["value_json"]) for r in table(db, "SELECT * FROM settings")}
        timing_policy = preparation_timing_policy(db)
        immediate = timing_policy == IMMEDIATE_PREPARATION
        active_base = settings.get("active_base_resume_id")
        matches = table(db, """
          SELECT m.*, j.company_key, c.display_name company, j.official_req_id, j.title,
                 j.location, j.official_url, j.published_at, j.referral_cutoff_at, j.handoff_at, j.closes_at
          FROM matches m JOIN jobs j USING(job_id) JOIN companies c USING(company_key)
          WHERE m.base_resume_id=? AND j.current_jd_revision=m.jd_revision
          ORDER BY COALESCE(j.published_at,j.first_seen_at) DESC, j.job_id
        """, (active_base,))
        matches = [row for row in matches if row["job_id"] in visible_ids and match_context_is_current(db, row)]
        for row in matches:
            row.update({key: job_by_id[row["job_id"]][key] for key in (
                "effective_posted_at_pt", "freshness_basis", "discovery_status", "workflow_admitted_at_pt")})
            interview = current_interview(db, row)
            pdf = current_job_pdf(db, row, interview)
            row["interview_approved"] = bool(interview)
            row["resume_pdf"] = pdf["pdf_path"] if pdf else None
            row["current_resume_id"] = pdf["resume_id"] if pdf else None
            for key in ("hr_findings", "hm_findings", "edit_plan", "open_questions"):
                row[key] = readable(row[key + "_json"])
            row["decided_at_pt"] = row["decided_at"]
        applications = table(db, "SELECT * FROM applications ORDER BY updated_at DESC,job_id")
        apps = {row["job_id"]: row for row in applications}
        events = table(db, "SELECT * FROM events ORDER BY occurred_at DESC,event_id")
        latest = {}
        for ev in events:
            latest.setdefault(ev["job_id"], ev["occurred_at"])
            ev["occurred_at_pt"] = ev["occurred_at"]
        contacts = table(db, """
          SELECT cc.*, c.display_name, c.linkedin_url, c.relationship_kind, c.relationship_evidence_ref,
                 c.outreach_state, c.last_contact_at last_contact_at_pt, cc.verified_at verified_at_pt,
                 c.row_version contact_version
          FROM company_contacts cc JOIN contacts c USING(contact_id) ORDER BY c.display_name, c.contact_id
        """)
        links = table(db, """
          SELECT jc.*, j.official_req_id, j.title, c.display_name,
                 j.referral_cutoff_at referral_cutoff_at_pt, jc.updated_at updated_at_pt, jc.row_version referral_version
          FROM job_contacts jc JOIN jobs j USING(job_id) JOIN contacts c USING(contact_id)
          ORDER BY jc.job_id, jc.rank, jc.contact_id
        """)
        jobs_by_contact = {}
        for link in links:
            jobs_by_contact.setdefault((link["company_key"], link["contact_id"]), set()).add(link["job_id"])
        expired_ids = {key for key, status in freshness.items()
                       if status in {"older_than_24h", "closed"} and key not in retained_ids}
        contacts = [contact for contact in contacts
                    if not jobs_by_contact.get((contact["company_key"], contact["contact_id"]))
                    or jobs_by_contact[(contact["company_key"], contact["contact_id"])] - expired_ids]
        referrals = {r["job_id"]: r for r in table(db, "SELECT * FROM job_referrals")}
        links_by_job = {}
        for link in links:
            links_by_job.setdefault(link["job_id"], []).append(link)
        queue = []
        stale_forms = {}
        now = now.astimezone(PT)
        default_cutoff = datetime.combine(now.date(), time(19, 45), PT)
        default_handoff = datetime.combine(now.date(), time(20), PT)
        ready_states = {"not_started", "ready_for_user", "form_blocked"}
        for row in matches:
            if row["decision"] not in {"no_changes", "needs_edits"}:
                continue
            jid = row["job_id"]
            if jid not in workflow_ids and jid not in retained_ids:
                continue
            app = apps.get(jid, {})
            app_state = app.get("state", "not_started")
            if app_state not in ready_states:
                continue
            ref = referrals.get(jid, {})
            ref_rows = [ref, *links_by_job.get(jid, [])]
            ref_states = {p.get("referral_state") for p in ref_rows}
            employee_submission = "employee_referred" in ref_states or app.get("route") == "employee_submission"
            cutoff = None if immediate else (datetime.fromisoformat(row["referral_cutoff_at"]) if row["referral_cutoff_at"] else default_cutoff)
            handoff = None if immediate else (datetime.fromisoformat(row["handoff_at"]) if row["handoff_at"] else default_handoff)
            preference = current_route_preference(db, job_by_id[jid])
            direct = preference and preference["route"] == "direct"
            selected_route = preparation_route(job_by_id[jid], ref_rows, now, timing_policy, preference)
            route = selected_route[0] if selected_route and not employee_submission else None
            same_resume = bool(row["current_resume_id"] and row["current_resume_id"] == app.get("resume_id"))
            preserved_form = current_preparation_form(app, row["jd_revision"], row["current_resume_id"])
            if preserved_form and not employee_submission:
                route = app["route"]
            blocker = app.get("blocker_code") if same_resume else None
            if workflow[jid] == "open_status_unverified":
                blocker, action = "OFFICIAL_STATUS_UNVERIFIED", "核实官网开放状态后继续；岗位仍保留在待投流程"
            elif employee_submission:
                blocker, action = "REFERRAL_ROUTE_UNCLEAR", "核实员工内推是否已经形成申请"
            elif app.get("blocker_code") == "POSSIBLE_DUPLICATE_APPLICATION":
                reapply = current_reapply_request(db, job_by_id[jid])
                blocker = "REAPPLY_REQUESTED" if reapply else app["blocker_code"]
                action = ("立即用原邮箱核对官网已有申请及重投限制；可申请时准备并停在 Submit"
                          if immediate and reapply else app.get("next_action") or "核实同标题历史申请，避免重复投递")
            elif row["decision"] == "needs_edits" and not row["interview_approved"]:
                blocker, action = "RESUME_INTERVIEW_REQUIRED", "完成经历访谈并确认英文改动"
            elif row["decision"] == "needs_edits" and not row["resume_pdf"]:
                blocker, action = "TAILORED_PDF_REQUIRED", "按已确认的改动生成并核对岗位 PDF"
            elif not row["resume_pdf"]:
                action = "生成并核对岗位 PDF"
            elif app_state == "ready_for_user" and same_resume and preserved_form:
                action = app.get("next_action") or "核对保留的表单，你点击 Submit"
            elif app_state == "ready_for_user":
                blocker, action = "FORM_REVALIDATION_REQUIRED", "核对并恢复官网表单，保留网页后再交你提交"
            elif app_state == "form_blocked" and same_resume:
                action = app.get("next_action") or "处理表单待办"
            elif not immediate and not route and "requested" in ref_states and now < cutoff:
                action = "等待内推检查"
            else:
                route = route or "direct"
                action = ("立即准备官方申请表，保留网页交你提交" if immediate else
                          "准备官方申请表" if preparation_due(job_by_id[jid], now, timing_policy) else "按交接时间准备官方表单")
            if (app_state in {"ready_for_user", "form_blocked"} and not same_resume and
                    app.get("blocker_code") != "POSSIBLE_DUPLICATE_APPLICATION"):
                stale_forms[jid] = action
            referral_summary = ", ".join(sorted(s for s in ref_states if s)) or "无已确认内推"
            if direct:
                referral_summary = "用户选择直投，不寻找内推；已有记录：" + referral_summary
            elif preserved_form and app.get("route") == "direct" and "link_received" in ref_states:
                referral_summary += "；已保留直投表单，新增内推入口待核对切换"
            queue.append({
                **row, "match_decision": row["decision"], "referral_summary": referral_summary,
                "job_referral_version": ref.get("row_version", 0), "route": route or (app.get("route") if same_resume else None),
                "referral_cutoff_at_pt": cutoff.isoformat() if cutoff else None,
                "handoff_at_pt": handoff.isoformat() if handoff else None,
                "closes_at_pt": row["closes_at"], "next_action": action, "blocker_code": blocker,
            })
        statuses = []
        queue_actions = {row["job_id"]: row["next_action"] for row in queue}
        match_by_id = {row["job_id"]: row for row in matches}
        for app in applications:
            jid = app["job_id"]
            if jid not in visible_ids and not application_needs_tracking(app):
                continue
            stale_form_action = stale_forms.get(jid)
            pause_action = None
            if app["state"] in ready_states:
                if workflow[jid] == "open_status_unverified":
                    pause_action = "核实官网开放状态后继续；暂停使用已保留的表单"
                elif jid in retained_ids:
                    current_match = match_by_id.get(jid)
                    if not current_match:
                        pause_action = "等待当前 JD、基础简历和候选条件重新匹配；暂停使用旧表单"
                    elif current_match["decision"] not in {"no_changes", "needs_edits"}:
                        pause_action = "当前匹配未通过或仍待核实；暂停使用已保留的表单"
            statuses.append({
                **job_by_id[jid], **app, "application_state": "not_started" if stale_form_action else app["state"],
                "application_version": app["row_version"], "next_action": pause_action or stale_form_action or
                (queue_actions.get(jid) if immediate and app["state"] in ready_states else None) or app["next_action"],
                "prepared_at_pt": None if stale_form_action else app["prepared_at"], "submit_clicked_at_pt": app["submit_clicked_at"],
                "submitted_at_pt": app["submitted_at"], "last_event_at_pt": latest.get(jid, app["updated_at"]),
            })
    books = [
        {"path": f"positions/{decision}.xlsx", "sheets": [
            sheet("Matches", title, MATCH, [m for m in matches if m["decision"] == decision])
        ]} for decision, title in [("no_changes", "可直接用基础简历"), ("needs_edits", "需定制简历")]
    ]
    books.append({"path": "applications/tracker.xlsx", "sheets": [
        sheet("Queue", "待准备的申请", "job_id company_key company official_req_id title official_url effective_posted_at_pt freshness_basis discovery_status workflow_admitted_at_pt match_decision resume_pdf referral_summary job_referral_version route referral_cutoff_at_pt handoff_at_pt closes_at_pt next_action blocker_code", queue),
        sheet("Status", "申请状态", "job_id company_key company official_req_id title application_state route prepared_at_pt submit_clicked_at_pt pending_submit_event_id submitted_at_pt last_event_at_pt last_evidence_ref next_action application_version", statuses),
        sheet("Events", "联系、内推与申请事件", "event_id job_id contact_id event_type source_kind source_ref occurred_at_pt evidence_ref applied_row_version", events),
    ]})
    manual = sheet("Updates", "手动更新 岗位和联系人", MANUAL, [])
    books.append({"path": "applications/manual_updates.xlsx", "manual": True, "sheets": [manual]})
    for company in sorted({c["company_key"] for c in contacts}):
        books.append({"path": f"connection/{company}/contacts.xlsx", "sheets": [
            sheet("Contacts", "公司联系人", "company_key contact_id display_name linkedin_url role_title employment_state employment_evidence_ref relationship_kind relationship_evidence_ref outreach_state last_contact_at_pt verified_at_pt draft_path contact_version", [c for c in contacts if c["company_key"] == company]),
            sheet("JobLinks", "联系人与岗位", "company_key job_id official_req_id title contact_id display_name rank referral_state referral_cutoff_at_pt evidence_ref updated_at_pt referral_version", [p for p in links if p["company_key"] == company and p["job_id"] in visible_ids]),
        ]})
    return books


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Private workspace root")
    parser.add_argument("command", nargs="?", choices=("export", "manual"), default="export")
    args = parser.parse_args()
    if args.command == "export":
        print(json.dumps(export(args.root), ensure_ascii=False))
        return
    from jobapply_ledger import Ledger
    ledger = Ledger(root=args.root)
    try:
        feedback = [{"update_id": row["update_id"], **ledger.apply_manual(row)} for row in json.load(sys.stdin)]
        print(json.dumps(feedback, ensure_ascii=False))
    finally:
        ledger.close()


if __name__ == "__main__":
    main()

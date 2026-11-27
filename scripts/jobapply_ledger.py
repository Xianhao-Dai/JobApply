"""SQLite writer for the JobApply v1 task and event contracts."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, time, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from tempfile import NamedTemporaryFile
import unicodedata
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile
from zoneinfo import ZoneInfo

from jobapply_scope import (candidate_context, constraints_match_context, match_context_is_current,
                           match_graduation_month, normalize_employment_type)


ROOT = Path(__file__).resolve().parent.parent
SCHEMA = Path(__file__).with_name("schema.sql")
BASE = "resume/Candidate Resume.docx"
BASE_PDF = "resume/Candidate Resume.pdf"
PT = ZoneInfo("America/Los_Angeles")
STAGES = {"discover", "match", "tailor", "connections", "prepare", "track"}
IDENTITY = ("schema_version", "task_id", "attempt", "stage", "scope_key", "job_id", "input_versions")
INTERVIEW_SCOPE = ("job_id", "jd_revision", "base_resume_id")
FORM_SCOPE = ("jd_revision", "resume_id", "route_fingerprint")
IMMEDIATE_PREPARATION = "immediate_after_qualification"
SCHEDULED_PREPARATION = "scheduled_20_pt"
RESULT_STATES = {
    "succeeded": "succeeded", "retryable_error": "retry_wait", "blocked": "blocked",
    "terminal_error": "failed", "superseded": "superseded",
}
CONTACT_EVENTS = {"connection_invited": "invited", "connection_accepted": "connected", "connection_declined": "declined"}
REFERRAL_EVENTS = {
    "referral_requested": "requested", "referral_link_received": "link_received",
    "referral_submitted_by_employee": "employee_referred", "referral_declined": "declined",
    "referral_wait_expired": "expired",
}
APPLICATION_EVENTS = {
    "application_form_ready": "ready_for_user", "application_form_blocked": "form_blocked",
    "application_submit_clicked": "submission_pending_verification", "application_received": "submitted",
    "application_not_submitted_verified": "not_started", "application_interviewing": "interviewing",
    "application_rejected": "rejected", "application_offer_received": "offer",
    "application_withdrawn": "withdrawn",
}
MANUAL_EVENTS = set(CONTACT_EVENTS) | (set(REFERRAL_EVENTS) - {"referral_wait_expired"}) | {
    "application_submit_clicked", "application_received", "application_not_submitted_verified",
    "application_withdrawn",
}


class ContractError(ValueError):
    pass


class Conflict(ContractError):
    pass


class NeedsReview(ContractError):
    pass


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def digest_file(path):
    with path.open("rb") as stream:
        from hashlib import file_digest
        return file_digest(stream, "sha256").hexdigest()


def time_utc(value=None, local=False):
    if value is None:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if not isinstance(value, str) or not value.strip():
        raise ContractError("时间须为 ISO 文本")
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("时间格式无效") from exc
    if "T" not in value and " " not in value.strip():
        raise ContractError("请填写日期和时分")
    if dt.tzinfo is None:
        if not local:
            raise ContractError("请填写带时区的时间")
        choices = {dt.replace(tzinfo=PT, fold=fold).astimezone(timezone.utc) for fold in (0, 1)}
        choices = {item for item in choices if item.astimezone(PT).replace(tzinfo=None) == dt}
        if len(choices) != 1:
            raise ContractError("夏令时切换窗口请使用显式 UTC 偏移")
        dt = choices.pop()
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_pt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(PT).isoformat() if value else None


def safe_key(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value):
        raise ContractError(f"{label} 格式无效")
    return value


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"缺少 {label}")
    return value.strip()


def company_slug(value):
    name = unicodedata.normalize("NFKD", nonempty(value, "company")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return safe_key(slug, "company_key").lower()


def get_job_id(company_key, req):
    return "j_" + digest(f"{company_key}:{req}")[:20]


def http_url(value, label):
    value = nonempty(value, label)
    fields = urlsplit(value)
    if fields.scheme != "https" or not fields.hostname or fields.username or fields.password:
        raise ContractError(f"{label} 不是有效的 HTTPS 地址")
    return value


def linkedin_url(value):
    fields = urlsplit(http_url(value, "linkedin_url"))
    match = re.fullmatch(r"/in/([A-Za-z0-9%_-]+)/?", fields.path)
    if fields.hostname not in ("www.linkedin.com", "linkedin.com") or match is None:
        raise ContractError("联系人须提供 LinkedIn 个人主页")
    username = unquote(match.group(1)).lower()
    if not re.fullmatch(r"[\w-]+", username):
        raise ContractError("LinkedIn 个人主页路径无效")
    return "https://www.linkedin.com/in/" + match.group(1).lower()


def year_month(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value) is None:
        raise ContractError("毕业月份须使用 YYYY-MM")
    return value


def stable_hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ContractError(f"{label} 须为 SHA-256")
    return value


def interview_setting_key(job_id):
    return f"resume_interview:{job_id}"


def base_resume_input(base):
    """Pin the real canonical artifact; a PDF never implies an editable Word source."""
    base = dict(base)
    return {"base_source_format": "pdf" if base.get("pdf_path") else "docx",
            **{f"base_{key}": base.get(key) for key in (
                "pdf_path", "pdf_sha256", "docx_path", "docx_sha256")}}


def current_interview(db, match):
    if not match or match["decision"] != "needs_edits":
        return None
    record = db.execute("SELECT value_json FROM settings WHERE key=?",
                        (interview_setting_key(match["job_id"]),)).fetchone()
    if not record:
        return None
    approval = json.loads(record[0])
    packet = approval["packet"]
    base = db.execute("SELECT * FROM resumes WHERE resume_id=?", (match["base_resume_id"],)).fetchone()
    if not base:
        return None
    pinned = approval.get("base_artifacts")
    if (pinned is not None or dict(base).get("pdf_path")) and pinned != base_resume_input(base):
        return None
    month = match_graduation_month(db, match)
    if "candidate_constraints_revision" in approval or "graduation_month" in approval:
        if (approval.get("candidate_constraints_revision") != match["candidate_constraints_revision"] or
                approval.get("graduation_month") != month):
            return None
    else:
        # Existing approvals belong to the original full-time profile. They
        # cannot authorize the conditional internship graduation date.
        job = db.execute("SELECT * FROM jobs WHERE job_id=?", (match["job_id"],)).fetchone()
        base = db.execute("SELECT graduation_month FROM resumes WHERE resume_id=?",
                          (match["base_resume_id"],)).fetchone()
        if (not job or candidate_context(db, job)["employment_type"] != "full_time" or
                match["candidate_constraints_revision"] not in (1, 2) or not base or month != base[0]):
            return None
    if (all(packet[key] == match[key] for key in INTERVIEW_SCOPE) and
            approval["interview_sha256"] == digest(json_text(packet))):
        return approval
    return None


def current_job_pdf(db, match, interview, resume_id=None):
    if not match or match["decision"] not in ("needs_edits", "no_changes"):
        return None
    tailored = match["decision"] == "needs_edits"
    if tailored and not interview:
        return None
    interview_hash = interview["interview_sha256"] if tailored else None
    month = match_graduation_month(db, match)
    if not month:
        return None
    return db.execute("""
        SELECT r.* FROM resumes r
        WHERE r.job_id=? AND r.jd_revision=? AND r.base_resume_id=?
          AND r.kind='job' AND r.mode=? AND r.verified_at IS NOT NULL
          AND r.graduation_month=?
          AND (? IS NULL OR r.resume_id=?)
          AND (? IS NULL OR EXISTS (
            SELECT 1 FROM task_attempts a WHERE a.task_id=r.task_id
              AND json_extract(a.task_json, '$.input_versions.interview_sha256')=?
              AND json_extract(a.result_json, '$.status')='succeeded'
              AND json_extract(a.result_json, '$.output.resume_id')=r.resume_id
          ))
        ORDER BY r.verified_at DESC, r.resume_id DESC LIMIT 1
    """, (*(match[key] for key in INTERVIEW_SCOPE), "tailor" if tailored else "export_only", month,
          resume_id, resume_id, interview_hash, interview_hash)).fetchone()


def current_form(application, versions):
    if not application:
        return None
    context = json.loads(application["form_context_json"] or "{}")
    if isinstance(context, dict) and application["form_ref"] and all(context.get(key) == value for key, value in versions.items()):
        return {"form_ref": application["form_ref"], **context}
    return None


def preparation_timing_policy(db):
    """Keep old installations scheduled until the user's policy is registered."""
    row = db.execute("SELECT value_json FROM settings WHERE key='preparation_timing_policy'").fetchone()
    return IMMEDIATE_PREPARATION if row and json.loads(row[0]) == IMMEDIATE_PREPARATION else SCHEDULED_PREPARATION


def current_preparation_form(application, jd_revision, resume_id):
    form = current_form(application, {"jd_revision": jd_revision, "resume_id": resume_id})
    return form if (form and isinstance(form.get("route_fingerprint"), str) and form["route_fingerprint"] and
                    application["route"] in ("direct", "referral_link")) else None


def preparation_due(job, now, policy=SCHEDULED_PREPARATION):
    closes = datetime.fromisoformat(job["closes_at"]) if job.get("closes_at") else None
    if closes and closes <= now:
        return False
    if policy == IMMEDIATE_PREPARATION:
        return True
    local = now.astimezone(PT)
    handoff = (datetime.fromisoformat(job["handoff_at"]) if job.get("handoff_at") else
               datetime.combine(local.date(), time(20), tzinfo=PT))
    return now >= handoff or bool(closes and closes < handoff)


def preparation_route(job, referral_records, now, policy=SCHEDULED_PREPARATION, preference=None):
    """Choose an entry without converting a possible employee submission to direct."""
    employee_referred = any(r.get("referral_state") == "employee_referred" for r in referral_records)
    if policy == IMMEDIATE_PREPARATION and employee_referred:
        return None
    if preference and preference.get("route") == "direct":
        return "direct", job.get("apply_url"), None
    confirmed = next((r for r in referral_records if r.get("referral_state") == "link_received"
                      and r.get("referral_url")), None)
    if confirmed:
        return "referral_link", confirmed["referral_url"], confirmed.get("evidence_ref")
    if employee_referred:
        return None
    local = now.astimezone(PT)
    cutoff = (datetime.fromisoformat(job["referral_cutoff_at"]) if job.get("referral_cutoff_at") else
              datetime.combine(local.date(), time(19, 45), tzinfo=PT))
    if (policy != IMMEDIATE_PREPARATION and now < cutoff and
            any(r.get("referral_state") == "requested" for r in referral_records)):
        return None
    return "direct", job.get("apply_url"), None


def current_reapply_request(db, job):
    """Read a user's request for this exact requisition, without clearing history."""
    row = db.execute("SELECT value_json FROM settings WHERE key=?",
                     ("reapply_request:" + job["job_id"],)).fetchone()
    if not row:
        return None
    request = json.loads(row[0])
    required = ("request_id", "job_id", "official_req_id", "requested_at", "instructions")
    if not isinstance(request, dict) or any(not isinstance(request.get(key), str) or not request[key].strip()
                                            for key in required):
        return None
    if request["job_id"] != job["job_id"] or request["official_req_id"] != job["official_req_id"]:
        return None
    try:
        time_utc(request["requested_at"])
    except ContractError:
        return None
    return request


def current_route_preference(db, job):
    """Return a scoped routing choice; it grants no permission to reapply."""
    row = db.execute("SELECT value_json FROM settings WHERE key=?",
                     ("route_preference:" + job["job_id"],)).fetchone()
    if not row:
        return None
    preference = json.loads(row[0])
    required = ("request_id", "job_id", "official_req_id", "requested_at", "instructions")
    if not isinstance(preference, dict) or any(not isinstance(preference.get(key), str) or not preference[key].strip()
                                               for key in required):
        return None
    if preference["job_id"] != job["job_id"] or preference["official_req_id"] != job["official_req_id"]:
        return None
    if (preference.get("route"), preference.get("seek_referral")) not in (("direct", False), ("auto", True)):
        return None
    if type(preference.get("seek_referral")) is not bool:
        return None
    try:
        time_utc(preference["requested_at"])
    except ContractError:
        return None
    return preference


class Ledger:
    def __init__(self, root=ROOT, db_path=None):
        self.root = Path(root).resolve()
        path = Path(db_path) if db_path is not None else self.root / "state/jobapply.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            self.db.executescript(SCHEMA.read_text(encoding="utf-8"))
            version = 4
        if version == 1:
            with self.transaction():
                self.db.execute("ALTER TABLE job_sources ADD COLUMN reposted_at TEXT")
                self.db.execute("ALTER TABLE job_sources ADD COLUMN reposted_at_basis TEXT "
                                "CHECK (reposted_at_basis IN ('source_exact','source_relative_lower_bound'))")
                self.db.execute("ALTER TABLE job_sources ADD COLUMN repost_observed_at TEXT")
                self.db.execute("ALTER TABLE job_sources ADD COLUMN repost_label TEXT")
                self.db.execute("PRAGMA user_version=2")
            version = 2
        if version == 2:
            with self.transaction():
                self.db.execute("""CREATE TABLE IF NOT EXISTS job_workflow_admissions (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
                    admitted_at TEXT NOT NULL,
                    match_task_id TEXT NOT NULL REFERENCES tasks(task_id),
                    admission_reason TEXT NOT NULL CHECK (admission_reason IN ('matched','policy_transition'))
                ) STRICT""")
                self.db.execute("PRAGMA user_version=3")
            version = 3
        if version == 3:
            self._migrate_pdf_base()
            version = 4
        if version != 4:
            raise ContractError(f"未知 SQLite 版本 {version}")

    def _migrate_pdf_base(self):
        """Rebuild only the resume constraint, preserving rows and foreign-key targets."""
        if self.db.in_transaction:
            raise ContractError("基础简历结构迁移不能在已有事务内运行")
        schema = SCHEMA.read_text(encoding="utf-8")
        statement = re.search(r"CREATE TABLE IF NOT EXISTS resumes \([\s\S]+?\n\) STRICT;", schema)
        if not statement:
            raise ContractError("找不到基础简历结构定义")
        extras = [row[0] for row in self.db.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='resumes' AND type IN ('index','trigger') "
            "AND sql IS NOT NULL")]
        self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            with self.transaction():
                self.db.execute(statement[0].replace("IF NOT EXISTS resumes", "resumes_pdf_migration", 1))
                columns = ",".join(row[1] for row in self.db.execute("PRAGMA table_info(resumes)"))
                self.db.execute(f"INSERT INTO resumes_pdf_migration ({columns}) SELECT {columns} FROM resumes")
                self.db.execute("DROP TABLE resumes")
                self.db.execute("ALTER TABLE resumes_pdf_migration RENAME TO resumes")
                for extra in extras:
                    self.db.execute(extra)
                if self.db.execute("PRAGMA foreign_key_check").fetchone():
                    raise Conflict("简历结构迁移发现外键不一致，已回滚")
                self.db.execute("PRAGMA user_version=4")
        finally:
            self.db.execute("PRAGMA foreign_keys=ON")

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def _insert(self, table, values):
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self.db.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values()))

    def _update(self, table, changes, keys):
        if not changes:
            return
        columns = ",".join(f"{key}=?" for key in changes)
        predicate = " AND ".join(f"{key}=?" for key in keys)
        self.db.execute(f"UPDATE {table} SET {columns} WHERE {predicate}", (*changes.values(), *keys.values()))

    def _setting(self, key, value):
        self.db.execute(
            "INSERT INTO settings (key,value_json) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
            (key, json_text(value)),
        )

    def setting(self, key, default=None):
        record = self._one("SELECT value_json FROM settings WHERE key=?", (key,))
        return json.loads(record["value_json"]) if record else default

    def _record_discovery_candidates(self, task, result, job_ids, strict=False):
        from jobapply_freshness import (discovery_snapshot_admission, official_availability,
                                       workflow_dispositions, utc_timestamp)
        now = datetime.now(timezone.utc)
        source = self._one("SELECT created_at FROM tasks WHERE task_id=?", (task["task_id"],))
        records = []
        for job_id in job_ids:
            row = self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
            proof = (discovery_snapshot_admission(self.db, task, result, dict(row), source["created_at"], now)
                     if row and source else None)
            eligible = bool(proof and official_availability(self.db, [dict(row)], now)[job_id] == "active"
                            and workflow_dispositions(self.db, [dict(row)], now)[job_id] not in ("withdrawn", "tracking"))
            if not eligible:
                if strict:
                    raise Conflict(f"原始发现结果未证明窗口内资格或当前状态阻止匹配：{job_id}")
                continue
            self._setting("discovery_admission:" + job_id, proof)
            records.append(proof)
        return records

    def admit_discovery_candidates(self, source_task_id, job_ids):
        """Explicitly repair only verified candidates in one accepted snapshot.

        No staging-file trust, bulk history backfill, publication refresh, or
        workflow admission: current matching is still required afterwards.
        """
        if (not isinstance(job_ids, (list, tuple)) or not job_ids
                or any(not isinstance(key, str) for key in job_ids) or len(set(job_ids)) != len(job_ids)):
            raise ContractError("发现资格须指定不重复的 job_id 白名单")
        with self.transaction():
            row = self._one("""SELECT t.state,t.stage,a.task_json,a.result_json FROM tasks t
                JOIN task_attempts a ON a.task_id=t.task_id AND a.attempt=t.current_attempt
                WHERE t.task_id=?""", (source_task_id,))
            if not row or row["stage"] != "discover" or row["state"] != "succeeded" or not row["result_json"]:
                raise Conflict("须使用已接受成功的发现任务原始快照")
            return {"admissions": self._record_discovery_candidates(json.loads(row["task_json"]),
                          json.loads(row["result_json"]), job_ids, strict=True)}

    def enable_workflow_retention(self, job_ids, confirmed_at=None):
        """Enable retention and admit only the explicitly confirmed transition list.

        An earlier confirmation time preserves a reviewed admission at a 24h
        boundary; it never overrides a current closure, withdrawal or stale match.
        Schema migration itself deliberately admits no historical jobs.
        """
        from jobapply_freshness import (WORKFLOW_RETENTION_POLICY, dispositions,
                                       official_availability, utc_timestamp)
        if not isinstance(job_ids, (list, tuple)) or any(not isinstance(key, str) for key in job_ids):
            raise ContractError("入队白名单须为 job_id 数组")
        if len(set(job_ids)) != len(job_ids):
            raise ContractError("入队白名单不可重复")
        recorded = time_utc()
        confirmed = time_utc(confirmed_at) if confirmed_at is not None else recorded
        if confirmed > recorded:
            raise ContractError("确认时刻不可晚于当前时刻")
        with self.transaction():
            base_id = self.setting("active_base_resume_id")
            base = self._one("SELECT * FROM resumes WHERE resume_id=?", (base_id,))
            if job_ids and not self._base_is_current(base):
                raise Conflict("当前基础简历未核验")
            admissions = []
            for job_id in job_ids:
                row = self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
                if not row:
                    raise Conflict(f"岗位不存在：{job_id}")
                job = dict(row)
                if dispositions(self.db, [job], utc_timestamp(confirmed))[job_id] != "active":
                    raise Conflict(f"确认时刻不满足新发现资格：{job_id}")
                if official_availability(self.db, [job], utc_timestamp(recorded))[job_id] != "active":
                    raise Conflict(f"岗位当前未确认开放：{job_id}")
                app = self._one("SELECT state FROM applications WHERE job_id=?", (job_id,))
                if app and app["state"] not in ("not_started", "form_blocked", "ready_for_user"):
                    raise Conflict(f"已投或已放弃岗位不可入队：{job_id}")
                match = self._current_match(job_id, job["current_jd_revision"], base_id)
                if (not match or match["decision"] not in ("no_changes", "needs_edits") or
                        match["decided_at"] > confirmed):
                    raise Conflict(f"确认时刻没有当前合格匹配：{job_id}")
                self.db.execute("INSERT OR IGNORE INTO job_workflow_admissions VALUES (?,?,?,?)",
                                (job_id, confirmed, match["task_id"], "policy_transition"))
                admissions.append(dict(self._one("SELECT * FROM job_workflow_admissions WHERE job_id=?", (job_id,))))
            self._setting("workflow_retention_policy", WORKFLOW_RETENTION_POLICY)
            if self.setting("workflow_retention_enabled_at") is None:
                self._setting("workflow_retention_enabled_at", recorded)
        return {"policy": WORKFLOW_RETENTION_POLICY, "admissions": admissions}

    def _path(self, relative):
        relative = Path(nonempty(relative, "artifact path"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ContractError("产物须位于项目内")
        resolved = (self.root / relative).resolve()
        if not resolved.is_relative_to(self.root):
            raise ContractError("产物须位于项目内")
        return resolved

    def _artifact(self, entries, relative, kind=None):
        entry = entries.get(relative)
        if not entry or (kind is not None and entry.get("kind") != kind):
            raise ContractError("产物未在回包中正确登记")
        expected = stable_hash(entry.get("sha256"), "产物哈希")
        path = self._path(relative)
        if not path.is_file() or not path.is_relative_to(self.root / "state/staging"):
            raise ContractError("产物不存在或不在 staging 内")
        if digest_file(path) != expected:
            raise Conflict("产物哈希不符")
        return path, expected

    def _promote(self, entries, relative, destination, promoted, kind=None, replace=False):
        source, checksum = self._artifact(entries, relative, kind)
        target = self._path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        old = target.read_bytes() if target.is_file() else None
        if old is not None and digest(old) == checksum:
            return checksum
        if old is not None and not replace:
            raise Conflict("不可改写已有版本的产物")
        self._put(target, source.read_bytes())
        promoted.append((target, old))
        return checksum

    @staticmethod
    def _put(target, content):
        with NamedTemporaryFile(dir=target.parent, delete=False) as temporary:
            temp_path = Path(temporary.name)
            temporary.write(content)
        try:
            temp_path.replace(target)
        finally:
            temp_path.unlink(missing_ok=True)

    def _base_is_current(self, base, task_input=None):
        if not base:
            return False
        base = dict(base)
        kind = "pdf" if base.get("pdf_path") else "docx"
        relative, expected = base.get(kind + "_path"), base.get(kind + "_sha256")
        if not relative or not expected:
            return False
        path = self._path(relative)
        if not path.is_file() or digest_file(path) != expected:
            return False
        if task_input is not None:
            pinned = base_resume_input(base)
            # Canonical PDF tasks must name that PDF explicitly. Legacy DOCX
            # envelopes without source fields remain readable for old ledgers.
            if kind == "pdf" and any(task_input.get(key) != value for key, value in pinned.items()):
                return False
            if kind == "docx" and any(key in task_input and task_input[key] != value
                                      for key, value in pinned.items()):
                return False
        return True

    def register_base(self, graduation_month, source_path=None,
                      editable_docx_path=None, editable_verification=None):
        graduation_month = year_month(graduation_month)
        relative = source_path or (BASE_PDF if self._path(BASE_PDF).is_file() else BASE)
        path = self._path(relative)
        if not path.is_file():
            raise ContractError("基础简历不存在")
        kind = path.suffix.lower().lstrip(".")
        if kind not in ("pdf", "docx"):
            raise ContractError("基础简历须为 PDF 或 DOCX")
        if kind == "pdf" and path.read_bytes()[:5] != b"%PDF-":
            raise ContractError("基础文件不是有效的 PDF")
        checksum = digest_file(path)
        base_resume_id = "r_base_" + checksum[:20]
        artifacts = {"pdf_path": None, "pdf_sha256": None, "docx_path": None, "docx_sha256": None}
        artifacts.update({kind + "_path": str(path.relative_to(self.root)), kind + "_sha256": checksum})
        if editable_docx_path is not None:
            if kind != "pdf":
                raise ContractError("额外可编辑原稿仅适用于 PDF 基础版")
            editable = self._path(editable_docx_path)
            if not editable.is_file() or editable.suffix.lower() != ".docx":
                raise ContractError("可编辑原稿须为已核对的 DOCX")
            editable_hash = digest_file(editable)
            proof = editable_verification
            if (not isinstance(proof, dict) or proof.get("pdf_sha256") != checksum or
                    proof.get("docx_sha256") != editable_hash or
                    not isinstance(proof.get("evidence_ref"), str) or not proof["evidence_ref"].strip()):
                raise ContractError("关联 Word 前须提供与当前 PDF 和 DOCX 哈希一致的内容核对记录")
            artifacts.update(docx_path=str(editable.relative_to(self.root)), docx_sha256=editable_hash)
        with self.transaction():
            saved = self._one("SELECT * FROM resumes WHERE resume_id=?", (base_resume_id,))
            if saved and (saved["kind"] != "base" or saved["graduation_month"] != graduation_month or
                          saved[kind + "_sha256"] != checksum):
                raise Conflict("基础简历内容不变，不能改记毕业月份或来源格式")
            if saved:
                self._update("resumes", {**artifacts, "verified_at": time_utc()}, {"resume_id": base_resume_id})
            else:
                self._insert("resumes", {"resume_id": base_resume_id, "kind": "base",
                             "graduation_month": graduation_month, **artifacts, "verified_at": time_utc()})
            self._setting("active_base_resume_id", base_resume_id)
            if editable_docx_path is not None:
                self._setting("base_editable_verification:" + base_resume_id, editable_verification)
        return {"base_resume_id": base_resume_id, **base_resume_input(artifacts),
                "candidate_constraints_revision": self.setting("candidate_constraints_revision", 1)}

    def _current_match(self, job_id, jd_revision, base_resume_id):
        job = self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if not job:
            return None
        context = candidate_context(self.db, job)
        match = self._one(
            "SELECT * FROM matches WHERE job_id=? AND jd_revision=? AND base_resume_id=? "
            "AND candidate_constraints_revision=?",
            (job_id, jd_revision, base_resume_id, context["candidate_constraints_revision"]),
        )
        return match if match and match_context_is_current(self.db, match) else None

    def approve_interview(self, packet):
        if not isinstance(packet, dict) or set(packet) != {*INTERVIEW_SCOPE, "approved_edits"}:
            raise ContractError("访谈确认包字段无效")
        job_id = safe_key(packet["job_id"], "job_id")
        base_id = safe_key(packet["base_resume_id"], "base_resume_id")
        jd_revision = packet["jd_revision"]
        if type(jd_revision) is not int or jd_revision < 1:
            raise ContractError("JD 版本须为正整数")
        edits = packet["approved_edits"]
        if not isinstance(edits, list) or not edits:
            raise ContractError("访谈确认包需要至少一条已确认的编辑")
        targets = set()
        for edit in edits:
            if not isinstance(edit, dict) or set(edit) != {
                "base_bullet", "jd_requirement", "user_confirmed_facts", "approved_english_text"
            }:
                raise ContractError("访谈编辑字段无效")
            target = edit["base_bullet"]
            nonempty(target, "base_bullet")
            nonempty(edit["jd_requirement"], "jd_requirement")
            english = edit["approved_english_text"]
            nonempty(english, "approved_english_text")
            if any(character in target or character in english for character in "\r\n"):
                raise ContractError("原文和替换文本须为准确的单段落文本")
            if target.count("\t") != english.count("\t"):
                raise ContractError("访谈编辑须保留原段落的制表符布局")
            if target == english:
                raise ContractError("访谈编辑须有实际文字改动")
            if target in targets:
                raise ContractError("原段落重复")
            targets.add(target)
            facts = edit["user_confirmed_facts"]
            if not isinstance(facts, list) or not facts:
                raise ContractError("访谈编辑需要用户已确认的事实")
            for fact in facts:
                nonempty(fact, "user_confirmed_facts")
        checksum = digest(json_text(packet))
        key = interview_setting_key(job_id)
        with self.transaction():
            job = self._one("SELECT listing_state, current_jd_revision FROM jobs WHERE job_id=?", (job_id,))
            match = self._current_match(job_id, jd_revision, base_id)
            if (not job or job["listing_state"] != "open" or job["current_jd_revision"] != jd_revision or
                    self.setting("active_base_resume_id") != base_id or not match or
                    match["decision"] != "needs_edits"):
                raise Conflict("访谈确认须对应当前开放岗位、JD、基础版和需定制的匹配判断")
            base = self._one("SELECT * FROM resumes WHERE resume_id=?", (base_id,))
            if not self._base_is_current(base):
                raise Conflict("基础简历的版本与登记不一致")
            if not base["docx_path"]:
                raise NeedsReview("当前基础版只有 PDF；定制前需提供并核对匹配的可编辑 Word")
            path = self._path(base["docx_path"])
            if not path.is_file() or base["docx_sha256"] != digest_file(path):
                raise Conflict("基础 Word 的版本与登记不一致")
            self._verify_interview_targets(path, targets)
            approval = {"interview_sha256": checksum, "packet": packet,
                        "candidate_constraints_revision": match["candidate_constraints_revision"],
                        "graduation_month": match_graduation_month(self.db, match),
                        "base_artifacts": base_resume_input(base)}
            outcome = "duplicate" if self.setting(key) == approval else "approved"
            if outcome == "approved":
                self._setting(key, approval)
        return {"job_id": job_id, "interview_sha256": checksum, "outcome": outcome}

    @staticmethod
    def _verify_interview_targets(path, targets):
        word = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
        try:
            with ZipFile(path) as archive:
                doc = ElementTree.fromstring(archive.read("word/document.xml"))
        except (BadZipFile, KeyError, ElementTree.ParseError) as exc:
            raise ContractError("基础 Word 的段落无法读取") from exc
        separators = {f"{word}tab": "\t", f"{word}br": "\n", f"{word}cr": "\n"}
        paragraphs = [
            "".join(node.text or "" if node.tag == f"{word}t" else separators.get(node.tag, "")
                    for run in paragraph.iter(f"{word}r") for node in run)
            for paragraph in doc.findall(f"{word}body/{word}p")[2:]
        ]
        if any(paragraphs.count(target) != 1 for target in targets):
            raise Conflict("访谈确认包须逐字匹配基础 Word 中唯一可编辑的原段落")

    def _check_identity(self, envelope):
        if not isinstance(envelope, dict) or not all(key in envelope for key in IDENTITY):
            raise ContractError("任务或结果信封不完整")
        if envelope["schema_version"] != 1 or envelope["stage"] not in STAGES:
            raise ContractError("未知任务版本或阶段")
        safe_key(envelope["task_id"], "task_id")
        if type(envelope["attempt"]) is not int or envelope["attempt"] < 1:
            raise ContractError("attempt 须从 1 起")
        stage = envelope["stage"]
        job = envelope["job_id"]
        scope = nonempty(envelope["scope_key"], "scope_key")
        if stage == "discover" and job is not None or stage != "discover" and job != scope:
            raise ContractError("无效的岗位或任务范围")
        versions = envelope["input_versions"]
        required_versions = {
            "discover": ("source_config_revision", "scan_window_id"),
            "match": ("jd_revision", "base_resume_id", "candidate_constraints_revision"),
            "tailor": ("jd_revision", "base_resume_id"), "connections": ("jd_revision",),
            "prepare": ("jd_revision", "resume_id", "application_version", "route_fingerprint"),
            "track": ("application_version", "scan_window_id"),
        }
        if not isinstance(versions, dict) or any(key not in versions for key in required_versions[stage]):
            raise ContractError("input_versions 缺少阶段版本")

    def dispatch(self, task):
        self._check_identity(task)
        stage, task_id, attempt, scope = (task[key] for key in ("stage", "task_id", "attempt", "scope_key"))
        if not isinstance(task.get("input"), dict) or not task.get("deadline_at"):
            raise ContractError("任务缺少 input 或截止时间")
        if stage == "discover" and task["input"].get("source_key") != scope:
            raise ContractError("发现任务 source_key 与 scope_key 不符")
        now = time_utc()
        deadline = time_utc(task["deadline_at"])
        stable_input = {key: value for key, value in task["input"].items() if key != "previous_docx_path"}
        dedupe = digest(json_text([stage, scope, task["input_versions"], stable_input]))
        exact = json_text(task)
        with self.transaction():
            existing_attempt = self._one(
                "SELECT task_json, result_id FROM task_attempts WHERE task_id=? AND attempt=?", (task_id, attempt)
            )
            if existing_attempt:
                if existing_attempt["task_json"] != exact:
                    raise Conflict("已有相同 task_id 和 attempt，但输入不同")
                if (not existing_attempt["result_id"] and stage not in ("discover", "track") and
                        not self._input_is_current(task)):
                    return {"task_id": task_id, "attempt": attempt, "outcome": "superseded"}
                return {"task_id": task_id, "attempt": attempt, "outcome": "duplicate"}
            previous = self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
            if deadline < now:
                raise Conflict("任务截止时间已过")
            if stage not in ("discover", "track") and not self._input_is_current(task):
                raise Conflict("岗位阶段输入或工作流资格与当前台账不符")
            if previous:
                if (previous["dedupe_key"] != dedupe or attempt != previous["current_attempt"] + 1
                        or previous["state"] not in ("retry_wait", "blocked")):
                    raise Conflict("不能使用该任务 ID 重试当前阶段或输入")
                self._update("tasks", {"state": "running", "current_attempt": attempt, "run_after_at": None,
                                       "deadline_at": deadline, "updated_at": now}, {"task_id": task_id})
            else:
                if attempt != 1:
                    raise ContractError("新任务必须从第一次尝试开始")
                duplicate = self._one("SELECT task_id FROM tasks WHERE dedupe_key=?", (dedupe,))
                if duplicate:
                    raise Conflict(f"已有相同任务 {duplicate['task_id']}")
                self._insert("tasks", {"task_id": task_id, "stage": stage, "scope_key": scope, "job_id": task["job_id"],
                                       "dedupe_key": dedupe, "state": "running", "current_attempt": attempt,
                                       "deadline_at": deadline, "created_at": now, "updated_at": now})
            self._insert("task_attempts", {"task_id": task_id, "attempt": attempt,
                                           "lease_until_at": deadline, "task_json": exact})
        return {"task_id": task_id, "attempt": attempt, "outcome": "dispatched"}

    def _input_is_current(self, task):
        stage, versions = task["stage"], task["input_versions"]
        if stage in ("discover", "track"):
            return True
        job = self._one("SELECT * FROM jobs WHERE job_id=?", (task["job_id"],))
        if not job or job["listing_state"] != "open" or job["current_jd_revision"] != versions.get("jd_revision"):
            return False
        from jobapply_freshness import workflow_dispositions, match_dispositions
        eligibility = match_dispositions if stage == "match" else workflow_dispositions
        if eligibility(self.db, [dict(job)], datetime.now(timezone.utc))[job["job_id"]] != "active":
            return False
        base_id = self.setting("active_base_resume_id")
        if stage in ("match", "tailor", "connections", "prepare"):
            base = self._one("SELECT * FROM resumes WHERE resume_id=?", (base_id,))
            if (base_id != versions.get("base_resume_id", base_id) or
                    not self._base_is_current(base, task["input"] if stage in ("match", "tailor") else None)):
                return False
        if stage == "match":
            context = candidate_context(self.db, job)
            return (context["candidate_constraints_revision"] == versions.get("candidate_constraints_revision") and
                    constraints_match_context(context, task["input"].get("candidate_constraints", {})))
        if stage == "connections":
            preference = current_route_preference(self.db, job)
            match = self._current_match(task["job_id"], versions["jd_revision"], base_id)
            return bool(match and match["decision"] in ("no_changes", "needs_edits") and
                        (not preference or preference["seek_referral"]))
        if stage not in ("tailor", "prepare"):
            return True
        data = task["input"]
        match = self._current_match(task["job_id"], versions["jd_revision"], base_id)
        interview = current_interview(self.db, match)
        if not match or match["decision"] not in ("no_changes", "needs_edits"):
            return False
        context = candidate_context(self.db, job)
        if ((stage == "tailor" or "graduation_month" in data or context["employment_type"] == "internship") and
                data.get("graduation_month") != match_graduation_month(self.db, match)):
            return False
        if stage == "tailor":
            if match["decision"] == "no_changes":
                return (data.get("mode") == "export_only" and not data.get("edit_plan") and
                        not data.get("approved_edits") and "interview_sha256" not in versions)
            editable = self._path(base["docx_path"]) if base["docx_path"] else None
            return (data.get("mode") == "tailor" and editable is not None and editable.is_file() and
                    digest_file(editable) == base["docx_sha256"] and interview is not None and
                    versions.get("interview_sha256") == interview["interview_sha256"] and
                    data.get("approved_edits") == interview["packet"]["approved_edits"])
        application = self._one("SELECT * FROM applications WHERE job_id=?", (task["job_id"],))
        if preparation_timing_policy(self.db) == IMMEDIATE_PREPARATION:
            if application and application["route"] == "employee_submission":
                return False
            if self._one("""SELECT 1 FROM job_referrals WHERE job_id=? AND referral_state='employee_referred'
                          UNION ALL SELECT 1 FROM job_contacts WHERE job_id=? AND referral_state='employee_referred'""",
                         (task["job_id"], task["job_id"])):
                return False
        preference = current_route_preference(self.db, job)
        if data.get("route_preference") != preference:
            return False
        if preference and preference["route"] == "direct" and data.get("route") != "direct":
            return False
        reapply = current_reapply_request(self.db, job)
        if "reapply_request" in data and data["reapply_request"] != reapply:
            return False
        if application and application["blocker_code"] == "POSSIBLE_DUPLICATE_APPLICATION":
            if not reapply or data.get("reapply_request") != reapply:
                return False
        if application and application["state"] not in ("not_started", "ready_for_user", "form_blocked"):
            return False
        if (application["row_version"] if application else 0) != versions.get("application_version"):
            return False
        if (context["employment_type"] == "internship" and
                (data.get("employment_type") != "internship" or data.get("availability") != context["availability"])):
            return False
        material = current_job_pdf(self.db, match, interview, versions["resume_id"])
        if not material or any(data.get("resume_" + field) != material[field] for field in ("pdf_sha256", "pdf_path")):
            return False
        existing = data.get("existing_form")
        if existing and existing != current_form(application, {key: versions[key] for key in FORM_SCOPE}):
            return False
        if existing and (not application or data.get("route") != application["route"]):
            return False
        if (application and application["state"] == "ready_for_user" and
                current_preparation_form(application, versions["jd_revision"], material["resume_id"])):
            return False
        pdf = self._path(material["pdf_path"])
        return pdf.is_file() and digest_file(pdf) == material["pdf_sha256"]

    def accept(self, result):
        self._check_identity(result)
        result_id = safe_key(result.get("result_id"), "result_id")
        status = result.get("status")
        if status not in RESULT_STATES or not isinstance(result.get("output"), dict):
            raise ContractError("无效的结果状态或 output")
        if status != "succeeded" and not isinstance(result.get("issue"), dict):
            raise ContractError("非成功结果需要 issue")
        if not isinstance(result.get("artifacts"), list) or not isinstance(result.get("evidence"), list):
            raise ContractError("结果需要 artifacts 和 evidence 数组")
        finished = time_utc(nonempty(result.get("completed_at"), "completed_at"))
        exact = json_text(result)
        task_id, attempt = result["task_id"], result["attempt"]
        artifacts = {}
        for entry in result["artifacts"]:
            if not isinstance(entry, dict):
                raise ContractError("无效的产物登记")
            path = nonempty(entry.get("path"), "artifact path")
            if path in artifacts:
                raise ContractError("产物路径重复")
            artifacts[path] = entry
        promoted = []
        try:
            with self.transaction():
                old = self._one("SELECT result_json FROM task_attempts WHERE result_id=?", (result_id,))
                if old:
                    if old["result_json"] != exact:
                        raise Conflict("相同 result_id 的内容不同")
                    return {"task_id": task_id, "outcome": "duplicate"}
                previous = self._one("SELECT * FROM tasks WHERE task_id=?", (task_id,))
                row = self._one("SELECT * FROM task_attempts WHERE task_id=? AND attempt=?", (task_id, attempt))
                if not previous or not row or row["result_id"]:
                    raise Conflict("找不到可接收结果的已派工尝试")
                task = json.loads(row["task_json"])
                if any(task[key] != result[key] for key in IDENTITY):
                    raise Conflict("任务与回包版本、阶段或岗位不一致")
                old_attempt = previous["current_attempt"] != attempt
                expired = finished > previous["deadline_at"] and task["stage"] != "track"
                stale = old_attempt or expired or not self._input_is_current(task)
                state = "superseded" if stale else RESULT_STATES[status]
                detail = {}
                if not stale:
                    detail = self._project(task, result, artifacts, promoted, finished)
                self._update("task_attempts", {"result_id": result_id, "result_json": exact,
                                               "finished_at": finished}, {"task_id": task_id, "attempt": attempt})
                if not old_attempt:
                    retry_at = (result.get("issue") or {}).get("retry_after_at")
                    self._update("tasks", {"state": state,
                                           "run_after_at": time_utc(retry_at) if state == "retry_wait" and retry_at else None,
                                           "updated_at": time_utc()}, {"task_id": task_id})
            return {"task_id": task_id, "outcome": state, **detail}
        except BaseException:
            for path, before in reversed(promoted):
                if before is None:
                    path.unlink(missing_ok=True)
                else:
                    self._put(path, before)
            raise

    def _project(self, task, result, artifacts, promoted, finished):
        stage, status = task["stage"], result["status"]
        output = result["output"]
        if stage == "discover" and status in ("succeeded", "retryable_error", "blocked"):
            return self._discover(task, output, artifacts, promoted, status == "succeeded", result)
        if stage == "prepare" and (status == "succeeded" or
                                   status == "blocked" and output.get("disposition") == "form_blocked"):
            return self._prepare(task, output, result["evidence"], finished)
        if status != "succeeded":
            return {}
        if stage == "match":
            self._match(task, output, finished)
        elif stage == "tailor":
            self._tailor(task, output, artifacts, promoted, finished)
        elif stage == "connections":
            return self._connections(task, output, artifacts, promoted, finished)
        elif stage == "track":
            return self._track(task, output)
        return {}

    def _discover(self, task, output, artifacts, promoted, complete, result=None):
        observations = output.get("observations", [])
        if not isinstance(observations, list):
            raise ContractError("observations 须为数组")
        source_key = task["scope_key"]
        jobs = set()
        for observation in observations:
            source_id = nonempty(observation.get("source_listing_id"), "source_listing_id")
            observed_at = time_utc(nonempty(observation.get("observed_at"), "observed_at"))
            observed_url = http_url(observation.get("observed_url"), "observed_url")
            key = {"source_key": source_key, "source_listing_id": source_id}
            prior = self._one("SELECT * FROM job_sources WHERE source_key=? AND source_listing_id=?",
                              tuple(key.values()))
            if prior and observed_at < prior["last_seen_at"]:
                continue
            req = observation.get("official_req_id")
            official_url = observation.get("official_url")
            job_id = prior["job_id"] if prior else None
            verification = prior["verification_state"] if prior else "unverified"
            if req and official_url:
                new_job = self._official_job(observation, artifacts, promoted, observed_at)
                if job_id and job_id != new_job:
                    raise Conflict("同一来源岗位指向了另一官方岗位")
                job_id, verification = new_job, "verified"
                jobs.add(job_id)
            values = {"job_id": job_id, "observed_url": observed_url, "last_seen_at": observed_at,
                      "verification_state": verification}
            values.update({key: observation.get(key) for key in (
                "company_as_listed", "title_as_listed", "location_as_listed", "published_at_as_listed"
            )})
            from jobapply_freshness import normalize_repost
            try:
                values.update(normalize_repost(observation, observed_at, dict(prior) if prior else None))
            except ValueError as exc:
                raise ContractError(f"无效的重发时间证据: {exc}") from exc
            if prior:
                self._update("job_sources", values, key)
            else:
                self._insert("job_sources", {**key, **values, "first_seen_at": observed_at})
        if complete:
            self._setting("discover_cursor:" + source_key, output.get("next_cursor"))
            if result:
                self._record_discovery_candidates(task, result, sorted(jobs))
        return {"observations": len(observations), "job_ids": sorted(jobs)}

    def _official_job(self, observation, artifacts, promoted, observed_at):
        listed_company = nonempty(observation.get("company_as_listed"), "company_as_listed")
        company = nonempty(observation.get("official_company") or listed_company, "official_company")
        key = observation.get("company_key")
        key = safe_key(key, "company_key").lower() if key else company_slug(company)
        req = nonempty(observation.get("official_req_id"), "official_req_id")
        job_id = get_job_id(key, req)
        official_url = http_url(observation["official_url"], "official_url")
        apply_url = observation.get("apply_url")
        if apply_url:
            http_url(apply_url, "apply_url")
        domain = observation.get("official_domain") or urlsplit(official_url).hostname
        self.db.execute(
            "INSERT OR IGNORE INTO companies (company_key,display_name,official_domain) VALUES (?,?,?)",
            (key, company, domain),
        )
        existing = self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        if existing and existing["last_verified_at"] is not None and observed_at < existing["last_verified_at"]:
            return job_id
        state = observation.get("listing_state") or "unknown"
        published = observation.get("published_at")
        basis = observation.get("published_at_basis")
        if published and basis != "official_date_only":
            published = time_utc(published)
        if not published:
            basis = None
        closes = observation.get("closes_at")
        values = {"title": nonempty(observation.get("official_title") or observation.get("title_as_listed"), "title"),
                  "location": observation.get("official_location") or observation.get("location_as_listed"),
                  "official_url": official_url, "apply_url": apply_url, "listing_state": state,
                  "published_at": published, "published_at_basis": basis, "last_verified_at": observed_at,
                  "closes_at": time_utc(closes) if closes else None}
        if existing:
            if state == "unknown":
                values["listing_state"] = existing["listing_state"]
                values["last_verified_at"] = existing["last_verified_at"]
            for field in ("published_at", "published_at_basis", "apply_url", "location", "closes_at"):
                if values[field] is None:
                    values[field] = existing[field]
            # A new board posting for the same requisition cannot replace its
            # earliest trusted first publication. Reposts need source evidence.
            if existing["published_at_basis"] == "official_first_published" and (
                    basis != "official_first_published" or
                    (existing["published_at"] and published and time_utc(existing["published_at"]) <= published)):
                values["published_at"] = existing["published_at"]
                values["published_at_basis"] = existing["published_at_basis"]
            changes = {name: value for name, value in values.items() if existing[name] != value}
            if changes:
                self._update("jobs", {**changes, "row_version": existing["row_version"] + 1}, {"job_id": job_id})
        else:
            self._insert("jobs", {"job_id": job_id, "company_key": key, "official_req_id": req,
                                  "first_seen_at": observed_at, **values})
        if "official_employment_type" in observation or "employment_type" in observation:
            supplied = observation.get("official_employment_type", observation.get("employment_type"))
            kind = normalize_employment_type(supplied) or "unknown"
            scope_key = "job_employment_type:" + job_id
            prior_scope = self.setting(scope_key)
            # Explicit manual classifications take precedence over later feeds.
            if prior_scope is None or (isinstance(prior_scope, dict) and
                                       prior_scope.get("source_kind") == "official_discovery"):
                prior_kind = normalize_employment_type(prior_scope)
                self._setting(scope_key, {"employment_type": kind if prior_kind in (None, kind) else "unknown",
                                         "source_kind": "official_discovery", "observed_at": observed_at,
                                         "evidence_ref": observation.get("employment_type_evidence_ref") or official_url})
        jd_path = observation.get("jd_path")
        if jd_path:
            _, checksum = self._artifact(artifacts, jd_path)
            version = self._one("SELECT jd_revision FROM job_jd_versions WHERE job_id=? AND sha256=?",
                                (job_id, checksum))
            if version:
                revision = version["jd_revision"]
            else:
                revision = self._one("SELECT COALESCE(MAX(jd_revision),0)+1 AS n FROM job_jd_versions WHERE job_id=?",
                                     (job_id,))["n"]
                destination = f"positions/jd/{job_id}/{revision}.txt"
                self._promote(artifacts, jd_path, destination, promoted)
                self._insert("job_jd_versions", {"job_id": job_id, "jd_revision": revision, "sha256": checksum,
                                                 "file_path": destination, "captured_at": observed_at})
            self.db.execute("UPDATE jobs SET current_jd_revision=?,row_version=row_version+1 "
                            "WHERE job_id=? AND current_jd_revision IS NOT ?", (revision, job_id, revision))
        return job_id

    def _match(self, task, output, finished):
        versions = task["input_versions"]
        base = self._one("SELECT graduation_month FROM resumes WHERE resume_id=?", (versions["base_resume_id"],))
        month = task["input"].get("candidate_constraints", {}).get("graduation_month")
        if output.get("decision") == "no_changes" and month and month != base["graduation_month"]:
            raise ContractError("毕业月份不同于基础版，匹配须列为 needs_edits 并准备已确认的日期修改")
        fields = {}
        for key in ("hr_findings", "hm_findings", "edit_plan", "open_questions"):
            items = output.get(key)
            if not isinstance(items, list):
                raise ContractError(f"{key} 须为数组")
            if key.endswith("findings") and any(
                not isinstance(item, dict) or not all(k in item for k in ("requirement", "finding", "evidence_ref"))
                or item["finding"] not in ("met", "unmet", "unknown", "partial") for item in items
            ):
                raise ContractError("匹配判断记录无效")
            fields[f"{key}_json"] = json_text(items)
        self._insert("matches", {"job_id": task["job_id"], "jd_revision": versions["jd_revision"],
                                 "base_resume_id": versions["base_resume_id"],
                                 "candidate_constraints_revision": versions["candidate_constraints_revision"],
                                 "decision": output.get("decision"), **fields, "decided_at": finished,
                                 "task_id": task["task_id"]})
        from jobapply_freshness import match_dispositions, workflow_policy_enabled
        job = dict(self._one("SELECT * FROM jobs WHERE job_id=?", (task["job_id"],)))
        if (output.get("decision") in ("no_changes", "needs_edits") and workflow_policy_enabled(self.db) and
                match_dispositions(self.db, [job], datetime.now(timezone.utc))[task["job_id"]] == "active"):
            self.db.execute("INSERT OR IGNORE INTO job_workflow_admissions VALUES (?,?,?,?)",
                            (task["job_id"], finished, task["task_id"], "matched"))

    def _tailor(self, task, output, artifacts, promoted, finished):
        input_data = task["input"]
        versions = task["input_versions"]
        mode = input_data.get("mode")
        if mode not in ("export_only", "tailor") or output.get("pdf_verified") is not True:
            raise ContractError("岗位 PDF 未通过核对或制作模式无效")
        job_id = task["job_id"]
        resume_id = safe_key(output.get("resume_id"), "resume_id")
        graduation = year_month(output.get("graduation_month"))
        if graduation != input_data.get("graduation_month"):
            raise Conflict("岗位简历与已派工的毕业月份不符")
        base = self._one("SELECT * FROM resumes WHERE resume_id=?", (versions["base_resume_id"],))
        if mode == "export_only" and graduation != base["graduation_month"]:
            raise Conflict("直接导出的毕业月份与基础版不符")
        expected = {"pdf": "Candidate Resume.pdf"}
        if mode == "tailor":
            expected.update(docx="Candidate Resume.docx", changes="changes.md")
        by_kind = {entry.get("kind"): path for path, entry in artifacts.items()}
        if set(by_kind) != set(expected) or len(by_kind) != len(artifacts):
            raise ContractError("岗位材料不符合制作模式")
        pdf, _ = self._artifact(artifacts, by_kind["pdf"], "pdf")
        if mode == "export_only" and base["pdf_path"] and digest_file(pdf) != base["pdf_sha256"]:
            raise Conflict("PDF 基础版无需编辑时须原样复用，不可重新排版或转换")
        with pdf.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ContractError("产物不是有效的 PDF")
        paths = {}
        for kind, name in expected.items():
            path = f"resume/{job_id}/{resume_id}/{name}"
            checksum = self._promote(artifacts, by_kind[kind], path, promoted, kind)
            paths[kind + "_path"] = path
            if kind in ("pdf", "docx"):
                paths[kind + "_sha256"] = checksum
        self._insert("resumes", {"resume_id": resume_id, "kind": "job", "mode": mode, "job_id": job_id,
                                 "jd_revision": versions["jd_revision"], "base_resume_id": versions["base_resume_id"],
                                 "graduation_month": graduation, "verified_at": finished, "task_id": task["task_id"],
                                 **paths})

    def _connections(self, task, output, artifacts, promoted, finished):
        candidates = output.get("candidates")
        if not isinstance(candidates, list):
            raise ContractError("candidates 须为数组")
        job_id = task["job_id"]
        company = self._one("SELECT company_key FROM jobs WHERE job_id=?", (job_id,))["company_key"]
        if company != task["input"].get("company_key"):
            raise Conflict("联系人任务公司与岗位不符")
        contact_ids = set()
        for candidate in candidates:
            url = linkedin_url(candidate.get("linkedin_url"))
            contact = self._one(
                "SELECT * FROM contacts WHERE linkedin_url=? OR contact_id IN "
                "(SELECT contact_id FROM contact_aliases WHERE linkedin_url=?)", (url, url)
            )
            if candidate.get("contact_id") and (not contact or candidate["contact_id"] != contact["contact_id"]):
                raise Conflict("联系人 ID 与个人主页不符")
            contact_id = contact["contact_id"] if contact else "c_" + digest(url)[:20]
            contact_ids.add(contact_id)
            if not contact:
                self._insert("contacts", {"contact_id": contact_id, "display_name": nonempty(
                    candidate.get("display_name"), "display_name"),
                    "linkedin_url": url, "relationship_kind": candidate.get("relationship_kind"),
                    "relationship_evidence_ref": candidate.get("relationship_evidence_ref")})
            self.db.execute("INSERT OR IGNORE INTO contact_aliases (linkedin_url,contact_id,verified_at) VALUES (?,?,?)",
                            (url, contact_id, finished))
            draft = candidate.get("draft_path")
            key = {"company_key": company, "contact_id": contact_id}
            saved = self._one("SELECT * FROM company_contacts WHERE company_key=? AND contact_id=?",
                              tuple(key.values()))
            changes = {"employment_state": candidate.get("employment_state"), "role_title": candidate.get("role_title"),
                       "employment_evidence_ref": candidate.get("employment_evidence_ref"), "verified_at": finished}
            if draft:
                path = f"connection/{company}/drafts/{contact_id}/connect.md"
                if not saved or saved["draft_path"] != draft:
                    checksum = self._promote(artifacts, draft, path, promoted, replace=True)
                    changes.update(draft_path=path, draft_sha256=checksum)
                else:
                    file = self._path(draft)
                    if not file.is_file() or digest_file(file) != saved["draft_sha256"]:
                        raise Conflict("复用联系草稿与台账版本不符")
            if saved:
                self._update("company_contacts", changes, key)
            else:
                self._insert("company_contacts", {**key, **changes})
            rank = candidate.get("rank")
            self.db.execute(
                "INSERT INTO job_contacts (job_id,company_key,contact_id,rank,updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(job_id,contact_id) DO UPDATE SET rank=excluded.rank, updated_at=excluded.updated_at,"
                " row_version=job_contacts.row_version+1 WHERE job_contacts.rank<>excluded.rank",
                (job_id, company, contact_id, rank, finished),
            )
        return {"contact_ids": sorted(contact_ids)}

    def _prepare(self, task, output, evidence, finished):
        job_id, data, versions = task["job_id"], task["input"], task["input_versions"]
        disposition = output.get("disposition")
        if disposition in ("existing_application", "listing_closed") and not evidence:
            raise NeedsReview("已有申请或岗位关闭需要官网证据")
        if disposition == "listing_closed":
            self.db.execute("UPDATE jobs SET listing_state='closed', last_verified_at=?, row_version=row_version+1 "
                            "WHERE job_id=? AND (last_verified_at IS NULL OR last_verified_at<=?)",
                            (finished, job_id, finished))
            return {"application_disposition": disposition}
        observed = evidence[0] if evidence else {}
        event = {"job_id": job_id, "source_kind": "orchestrator",
                 "source_ref": f"{task['task_id']}/{task['attempt']}", "occurred_at": finished,
                 "evidence_kind": observed.get("kind"), "evidence_ref": observed.get("ref")}
        if disposition == "existing_application":
            event["event_type"] = "application_received"
        elif disposition in ("ready_for_user", "form_blocked"):
            if data.get("route") not in ("direct", "referral_link"):
                raise ContractError("可填写的表单需要直申或核实内推入口")
            resume = self._one("SELECT graduation_month,pdf_path,pdf_sha256 FROM resumes WHERE resume_id=?",
                               (versions["resume_id"],))
            if (data.get("graduation_month") != resume["graduation_month"] or
                    data.get("resume_pdf_path") != resume["pdf_path"] or
                    data.get("resume_pdf_sha256") != resume["pdf_sha256"]):
                raise Conflict("投递表单与已核对的简历不符")
            event.update(
                event_type="application_form_ready" if disposition == "ready_for_user" else "application_form_blocked",
                route=data["route"], resume_id=versions["resume_id"],
                form_ref=output.get("form_ref") or (data.get("existing_form") or {}).get("form_ref"),
                form_context={key: versions[key] for key in FORM_SCOPE},
                blocker_code=output.get("blocker_code"), next_action=output.get("next_action"),
            )
            if disposition == "ready_for_user" and not event["form_ref"]:
                raise ContractError("待提交表单需要表单引用")
            if disposition == "form_blocked" and not output.get("blocker_code"):
                raise ContractError("表单阻塞需要原因代码")
        else:
            raise ContractError("未知申请准备结果")
        return {"application_disposition": disposition, **self._apply_event(event)}

    def _track(self, task, output):
        proposals = output.get("proposed_events")
        unmatched = output.get("unmatched_evidence")
        if not isinstance(proposals, list) or not isinstance(unmatched, list):
            raise ContractError("跟踪结果必须包含已匹配和未匹配数组")
        changed = []
        problems = []
        for index, event in enumerate(proposals):
            if (not isinstance(event, dict) or event.get("job_id") != task["job_id"] or
                    event.get("source_kind") not in ("gmail", "linkedin", "ats") or
                    event.get("event_type") in ("application_form_ready", "application_form_blocked",
                                                "application_submit_clicked", "referral_wait_expired")):
                problems.append({"index": index, "reason": "事件不属于该跟踪任务"})
                continue
            self.db.execute("SAVEPOINT track_event")
            try:
                changed.append(self._apply_event(event))
            except (ContractError, sqlite3.IntegrityError) as exc:
                self.db.execute("ROLLBACK TO track_event")
                problems.append({"index": index, "reason": str(exc)})
            finally:
                self.db.execute("RELEASE track_event")
        self._setting("track_cursor:" + task["job_id"], output.get("next_cursor"))
        return {"events": changed, "event_errors": problems, "unmatched": len(unmatched)}

    def _event_target(self, event):
        event_type = event.get("event_type")
        job_id = event.get("job_id")
        contact_id = event.get("contact_id")
        job = self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,)) if job_id else None
        expected_company = event.get("company_key")
        if expected_company and job and job["company_key"] != expected_company:
            raise NeedsReview("岗位与公司不一致")
        if event_type in CONTACT_EVENTS:
            if not contact_id:
                raise ContractError("联系更新需要 contact_id")
            company = expected_company or (job["company_key"] if job else None)
            if not company or not self._one(
                "SELECT 1 FROM company_contacts WHERE company_key=? AND contact_id=?", (company, contact_id)
            ):
                raise NeedsReview("联系人与公司不匹配")
            return "contact", contact_id, job
        if not job:
            if not job_id:
                raise ContractError("更新需要 job_id")
            raise NeedsReview("岗位未在台账中核实")
        if event_type in REFERRAL_EVENTS:
            if expected_company is None and event["source_kind"] == "manual":
                raise ContractError("内推更新需要 company_key")
            if contact_id:
                if not self._one("SELECT 1 FROM job_contacts WHERE job_id=? AND contact_id=?", (job_id, contact_id)):
                    raise NeedsReview("联系人尚未和该岗位关联")
                return "job_contact", f"{job_id}/{contact_id}", job
            if event_type in ("referral_requested", "referral_declined"):
                raise ContractError("该内推更新需要 contact_id")
            return "job_referral", job_id, job
        if event_type in APPLICATION_EVENTS:
            if contact_id:
                raise ContractError("申请事件应按岗位登记")
            return "application", job_id, job
        raise ContractError("未知事件类型")

    def _target_row(self, target_kind, target_key):
        if target_kind == "contact":
            table, key = "contacts", {"contact_id": target_key}
        elif target_kind == "job_contact":
            job, contact = target_key.split("/", 1)
            table, key = "job_contacts", {"job_id": job, "contact_id": contact}
        else:
            table = "job_referrals" if target_kind == "job_referral" else "applications"
            key = {"job_id": target_key}
        where = " AND ".join(f"{column}=?" for column in key)
        return table, key, self._one(f"SELECT * FROM {table} WHERE {where}", tuple(key.values()))

    def apply_event(self, event):
        """Apply one independently verified event without a task envelope."""
        with self.transaction():
            return self._apply_event(event)

    def _apply_event(self, event):
        if not isinstance(event, dict) or event.get("source_kind") not in ("gmail", "linkedin", "ats", "manual", "orchestrator"):
            raise ContractError("无效事件来源")
        source = event["source_kind"]
        source_ref = nonempty(event.get("source_ref"), "source_ref")
        occurred = time_utc(nonempty(event.get("occurred_at"), "occurred_at"))
        event_type = nonempty(event.get("event_type"), "event_type")
        kind, key, job = self._event_target(event)
        event_id = "e_" + digest(json_text([source, source_ref, kind, key, event_type]))[:24]
        existing = self._one("SELECT * FROM events WHERE event_id=?", (event_id,))
        if existing:
            if existing["payload_json"] != json_text(event):
                raise Conflict("相同来源事件的内容不同")
            return {"event_id": event_id, "outcome": "duplicate", "row_version": existing["applied_row_version"]}
        table, identity, current = self._target_row(kind, key)
        older = self._one("SELECT MAX(occurred_at) AS last FROM events WHERE target_kind=? AND target_key=? "
                          "AND applied_row_version IS NOT NULL", (kind, key))["last"]
        if kind == "contact":
            changes = self._contact_changes(current, event_type, occurred, older)
        elif kind in ("job_contact", "job_referral"):
            changes = self._referral_changes(current, event, job, occurred, older)
        else:
            changes = self._application_changes(current, event, event_id, occurred, older)
        version = (current["row_version"] + 1 if current else 1) if changes else None
        self._insert("events", {"event_id": event_id, "event_type": event_type, "target_kind": kind,
                                "target_key": key, "job_id": event.get("job_id"), "contact_id": event.get("contact_id"),
                                "source_kind": source, "source_ref": source_ref, "evidence_kind": event.get("evidence_kind"),
                                "evidence_ref": event.get("evidence_ref"), "occurred_at": occurred, "recorded_at": time_utc(),
                                "payload_json": json_text(event), "applied_row_version": version})
        if changes:
            changes["row_version"] = version
            if current:
                self._update(table, changes, identity)
            else:
                self._insert(table, {**identity, **changes})
        return {"event_id": event_id, "outcome": "applied" if changes else "recorded", "row_version": version}

    @staticmethod
    def _contact_changes(current, event_type, occurred, older):
        state = CONTACT_EVENTS[event_type]
        if (older and occurred < older) or current["outreach_state"] == state:
            return {}
        if current["outreach_state"] in ("connected", "declined") and state == "invited":
            return {}
        if current["outreach_state"] == "connected" and state == "declined":
            return {}
        return {"outreach_state": state, "last_contact_at": occurred}

    @staticmethod
    def _validate_referral_link(link, job):
        parsed = urlsplit(http_url(link, "referral_url"))
        known_hosts = {urlsplit(job[field]).hostname for field in ("official_url", "apply_url") if job[field]}
        if parsed.hostname not in known_hosts:
            raise NeedsReview("内推入口域名未和岗位官方入口核实")
        req = str(job["official_req_id"])
        if link not in (job["official_url"], job["apply_url"]) and not re.search(
            rf"(?<![A-Za-z0-9]){re.escape(req)}(?![A-Za-z0-9])", unquote(parsed.path + "?" + parsed.query),
            re.IGNORECASE,
        ):
            raise NeedsReview("内推入口无法确认官方岗位编号")

    def _referral_changes(self, current, event, job, occurred, older):
        state = REFERRAL_EVENTS[event["event_type"]]
        previous = current["referral_state"] if current else "not_requested"
        evidence = event.get("evidence_ref")
        link = event.get("referral_url")
        if state in ("link_received", "employee_referred", "declined"):
            nonempty(evidence, "evidence_ref")
        if state == "link_received":
            self._validate_referral_link(link, job)
        if older and occurred < older and not (previous == "expired" and state in ("link_received", "employee_referred")):
            return {}
        if state in ("requested", "expired") and previous in ("link_received", "employee_referred", "declined"):
            return {}
        if state == previous and ((current and current["referral_url"]) == link):
            return {}
        values = {"referral_state": state, "updated_at": time_utc()}
        if link:
            values["referral_url"] = link
        if evidence:
            values["evidence_ref"] = evidence
        return values

    @staticmethod
    def _application_changes(current, event, event_id, occurred, older):
        event_type = event["event_type"]
        target = APPLICATION_EVENTS[event_type]
        state = current["state"] if current else "not_started"
        evidence = event.get("evidence_ref")
        if event_type not in ("application_form_ready", "application_form_blocked", "application_submit_clicked"):
            nonempty(evidence, "evidence_ref")
        if event_type == "application_received" and event.get("submitted_at"):
            submitted = time_utc(event["submitted_at"])
            if submitted > occurred:
                raise NeedsReview("报告的提交时间晚于回执")
        else:
            submitted = None
        late = bool(older and occurred < older)
        early_states = {"not_started", "form_blocked", "ready_for_user", "submission_pending_verification"}
        values = {"state": target, "updated_at": time_utc()}
        if event_type in ("application_form_ready", "application_form_blocked"):
            if state not in (early_states - {"submission_pending_verification"}) or late:
                return {}
            values.update(route=event.get("route"), resume_id=event.get("resume_id"),
                          form_ref=event.get("form_ref"), form_context_json=json_text(event.get("form_context")),
                          blocker_code=event.get("blocker_code"), next_action=event.get("next_action"),
                          prepared_at=occurred if target == "ready_for_user" else None)
        elif event_type == "application_submit_clicked":
            if state not in early_states or late or (current and current["submit_clicked_at"] == occurred):
                return {}
            values.update(submit_clicked_at=occurred, pending_submit_event_id=event_id,
                          blocker_code=None, next_action="等待 ATS 或邮件回执")
        elif event_type == "application_not_submitted_verified":
            if (not current or state != "submission_pending_verification" or
                    event.get("pending_submit_event_id") != current["pending_submit_event_id"] or
                    occurred <= current["submit_clicked_at"]):
                raise NeedsReview("无提交证据未对应当前点击")
            values.update(pending_submit_event_id=None, form_ref=None, form_context_json=None,
                          blocker_code=None, next_action="可重新准备申请表")
        elif event_type == "application_received" and (late and state not in early_states or
                                                       state in ("interviewing", "rejected", "offer", "withdrawn", "submitted")):
            if submitted and not current["submitted_at"]:
                values = {"submitted_at": submitted, "updated_at": time_utc()}
            else:
                return {}
        elif late and state not in early_states:
            return {}
        else:
            values.update(pending_submit_event_id=None, blocker_code=None, next_action=None)
            if submitted:
                values["submitted_at"] = submitted
        if evidence:
            values["last_evidence_ref"] = evidence
        return values

    def apply_manual(self, update):
        try:
            return self._apply_manual(update)
        except NeedsReview as exc:
            return self._manual_feedback("待核实", str(exc))
        except Conflict as exc:
            return self._manual_feedback("冲突", str(exc))
        except (ContractError, sqlite3.IntegrityError) as exc:
            return self._manual_feedback("格式错误", str(exc))

    @staticmethod
    def _manual_feedback(status, message, imported=None, event_id=None):
        return {"import_status": status, "feedback": message, "imported_at_pt": iso_pt(imported),
                "event_id": event_id}

    def _apply_manual(self, update):
        if not isinstance(update, dict):
            raise ContractError("人工更新须为对象")
        update_id = safe_key(update.get("update_id"), "update_id")
        action = nonempty(update.get("action_code"), "action_code")
        if action not in MANUAL_EVENTS:
            raise ContractError("无法识别该人工操作")
        occurred = time_utc(nonempty(update.get("occurred_at_pt"), "occurred_at_pt"), local=True)
        event = {"event_type": action, "occurred_at": occurred, "source_kind": "manual", "source_ref": update_id}
        for field in ("company_key", "job_id", "contact_id", "evidence_kind", "evidence_ref",
                      "referral_url", "pending_submit_event_id", "note"):
            value = update.get(field)
            if value is not None and str(value).strip():
                if not isinstance(value, str):
                    raise ContractError(f"{field} 须为文本")
                event[field] = value.strip()
        submit_time = update.get("submitted_at_pt")
        if submit_time:
            event["submitted_at"] = time_utc(submit_time, local=True)
        evidence_kind = event.get("evidence_kind")
        if evidence_kind not in ("user_report", "linkedin_message", "gmail_message", "ats_receipt", "ats_status"):
            raise ContractError("请选择有效的 evidence_kind")
        if action in ("application_received", "application_not_submitted_verified") and evidence_kind not in (
            "gmail_message", "ats_receipt", "ats_status"
        ):
            raise NeedsReview("申请确认需要邮件或 ATS 凭证")
        expected = update.get("expected_version")
        if expected is not None and expected != "":
            try:
                numeric_expected = float(expected)
                if isinstance(expected, bool) or not numeric_expected.is_integer() or numeric_expected < 0:
                    raise ValueError("not a nonnegative integer")
                expected = int(numeric_expected)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ContractError("expected_version 须为非负整数") from exc
        else:
            expected = None
        payload_hash = digest(json_text([event, expected]))
        with self.transaction():
            original = self._one("SELECT * FROM manual_updates WHERE update_id=?", (update_id,))
            if original:
                if original["payload_sha256"] != payload_hash:
                    raise Conflict("已导入的 update_id 内容有更改，请用新行更正")
                return self._manual_feedback("已处理过", "该更新已入账", original["imported_at"], original["event_id"])
            target_kind, target_key, _ = self._event_target(event)
            fact = digest(json_text([
                target_kind, target_key, action, occurred, event.get("evidence_ref"),
                event.get("referral_url"), event.get("pending_submit_event_id"), event.get("submitted_at"),
            ]))
            same = self._one("SELECT event_id FROM manual_updates WHERE fact_key=?", (fact,))
            imported = time_utc()
            if same:
                self._insert("manual_updates", {"update_id": update_id, "payload_sha256": payload_hash,
                                                "fact_key": fact, "outcome": "duplicate", "event_id": same["event_id"],
                                                "imported_at": imported})
                return self._manual_feedback("已处理过", "同一事实由其他行入账", imported, same["event_id"])
            _, _, current = self._target_row(target_kind, target_key)
            version = current["row_version"] if current else 0
            if expected is None and not current:
                expected = 0
            if expected != version:
                if expected is None:
                    raise NeedsReview(f"请填入当前记录版本 {version}")
                raise Conflict(f"当前记录版本是 {version}，本行预期 {expected}")
            result = self._apply_event(event)
            self._insert("manual_updates", {"update_id": update_id, "payload_sha256": payload_hash,
                                            "fact_key": fact, "outcome": "applied",
                                            "event_id": result["event_id"], "imported_at": imported})
        explanation = ("已附证据，当前状态未改变" if result["outcome"] == "recorded"
                       else f"已保存事件，记录版本为 {result['row_version']}")
        return self._manual_feedback("已入账", explanation, imported, result["event_id"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=ROOT, type=Path)
    parser.add_argument("--db", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize an empty ledger")
    registration = commands.add_parser("register-base", help="Register a local canonical document")
    registration.add_argument("--graduation-month", required=True)
    registration.add_argument("--source-path", help="Workspace-relative canonical PDF or DOCX")
    for command in ("dispatch", "accept", "manual", "approve-interview"):
        commands.add_parser(command).add_argument("payload", type=Path)
    args = parser.parse_args()
    ledger = None
    try:
        ledger = Ledger(args.root, args.db)
        if args.command == "init":
            result = {"schema_version": ledger.db.execute("PRAGMA user_version").fetchone()[0]}
        elif args.command == "register-base":
            result = ledger.register_base(args.graduation_month, source_path=args.source_path)
        else:
            payload = json.loads(args.payload.read_text(encoding="utf-8"))
            worker = {"dispatch": ledger.dispatch, "accept": ledger.accept,
                      "manual": ledger.apply_manual, "approve-interview": ledger.approve_interview}
            result = worker[args.command](payload)
        print(json_text(result))
    except (ContractError, sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"{exc}\n")
    finally:
        if ledger is not None:
            ledger.close()


if __name__ == "__main__":
    main()

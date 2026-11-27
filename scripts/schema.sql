BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK (json_valid(value_json))
) STRICT;

CREATE TABLE IF NOT EXISTS companies (
    company_key TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    official_domain TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    company_key TEXT NOT NULL REFERENCES companies(company_key),
    official_req_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    official_url TEXT NOT NULL,
    apply_url TEXT,
    listing_state TEXT NOT NULL CHECK (listing_state IN ('open', 'closed', 'unknown')),
    published_at TEXT,
    published_at_basis TEXT CHECK (published_at_basis IN ('official_first_published', 'official_last_published', 'official_date_only')),
    first_seen_at TEXT NOT NULL,
    last_verified_at TEXT,
    closes_at TEXT,
    current_jd_revision INTEGER,
    referral_cutoff_at TEXT,
    handoff_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    UNIQUE (company_key, official_req_id),
    UNIQUE (job_id, company_key),
    FOREIGN KEY (job_id, current_jd_revision) REFERENCES job_jd_versions(job_id, jd_revision)
) STRICT;

CREATE TABLE IF NOT EXISTS job_sources (
    source_key TEXT NOT NULL,
    source_listing_id TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(job_id),
    observed_url TEXT NOT NULL,
    company_as_listed TEXT,
    title_as_listed TEXT,
    location_as_listed TEXT,
    published_at_as_listed TEXT,
    reposted_at TEXT,
    reposted_at_basis TEXT CHECK (reposted_at_basis IN ('source_exact', 'source_relative_lower_bound')),
    repost_observed_at TEXT,
    repost_label TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    verification_state TEXT NOT NULL CHECK (verification_state IN ('unverified', 'verified', 'unavailable')),
    PRIMARY KEY (source_key, source_listing_id)
) STRICT;

CREATE TABLE IF NOT EXISTS job_jd_versions (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    jd_revision INTEGER NOT NULL CHECK (jd_revision >= 1),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    file_path TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY (job_id, jd_revision),
    UNIQUE (job_id, sha256)
) STRICT;

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL CHECK (stage IN ('discover', 'match', 'tailor', 'connections', 'prepare', 'track')),
    scope_key TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(job_id),
    dedupe_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'retry_wait', 'blocked', 'succeeded', 'failed', 'superseded')),
    current_attempt INTEGER NOT NULL CHECK (current_attempt >= 1),
    run_after_at TEXT,
    deadline_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK ((stage = 'discover') = (job_id IS NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS task_attempts (
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    attempt INTEGER NOT NULL CHECK (attempt >= 1),
    lease_until_at TEXT,
    task_json TEXT NOT NULL CHECK (json_valid(task_json)),
    result_id TEXT UNIQUE,
    result_json TEXT CHECK (json_valid(result_json)),
    finished_at TEXT,
    PRIMARY KEY (task_id, attempt)
) STRICT;

CREATE TABLE IF NOT EXISTS resumes (
    resume_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('base', 'job')),
    mode TEXT CHECK (mode IN ('export_only', 'tailor')),
    job_id TEXT REFERENCES jobs(job_id),
    jd_revision INTEGER,
    base_resume_id TEXT REFERENCES resumes(resume_id),
    graduation_month TEXT NOT NULL CHECK (graduation_month GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]'),
    docx_path TEXT,
    docx_sha256 TEXT CHECK (length(docx_sha256) = 64),
    pdf_path TEXT,
    pdf_sha256 TEXT CHECK (length(pdf_sha256) = 64),
    changes_path TEXT,
    verified_at TEXT,
    task_id TEXT REFERENCES tasks(task_id),
    FOREIGN KEY (job_id, jd_revision) REFERENCES job_jd_versions(job_id, jd_revision),
    CHECK ((docx_path IS NULL) = (docx_sha256 IS NULL)),
    CHECK ((pdf_path IS NULL) = (pdf_sha256 IS NULL)),
    CHECK (
        (kind = 'base' AND mode IS NULL AND job_id IS NULL AND jd_revision IS NULL
          AND base_resume_id IS NULL AND (pdf_path IS NOT NULL OR docx_path IS NOT NULL))
        OR
        (kind = 'job' AND mode IS NOT NULL AND job_id IS NOT NULL AND jd_revision IS NOT NULL
          AND base_resume_id IS NOT NULL AND pdf_path IS NOT NULL AND verified_at IS NOT NULL
          AND (mode = 'export_only' OR (docx_path IS NOT NULL AND changes_path IS NOT NULL)))
    )
) STRICT;

CREATE TABLE IF NOT EXISTS matches (
    job_id TEXT NOT NULL,
    jd_revision INTEGER NOT NULL,
    base_resume_id TEXT NOT NULL REFERENCES resumes(resume_id),
    candidate_constraints_revision INTEGER NOT NULL CHECK (candidate_constraints_revision >= 1),
    decision TEXT NOT NULL CHECK (decision IN ('no_changes', 'needs_edits', 'needs_review', 'not_fit')),
    hr_findings_json TEXT NOT NULL CHECK (json_valid(hr_findings_json)),
    hm_findings_json TEXT NOT NULL CHECK (json_valid(hm_findings_json)),
    edit_plan_json TEXT NOT NULL CHECK (json_valid(edit_plan_json)),
    open_questions_json TEXT NOT NULL CHECK (json_valid(open_questions_json)),
    decided_at TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    PRIMARY KEY (job_id, jd_revision, base_resume_id, candidate_constraints_revision),
    FOREIGN KEY (job_id, jd_revision) REFERENCES job_jd_versions(job_id, jd_revision)
) STRICT;

CREATE TABLE IF NOT EXISTS contacts (
    contact_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    linkedin_url TEXT NOT NULL UNIQUE,
    relationship_kind TEXT NOT NULL CHECK (relationship_kind IN ('high_school', 'university', 'former_coworker', 'graduate_school', 'other')),
    relationship_evidence_ref TEXT,
    outreach_state TEXT NOT NULL DEFAULT 'not_contacted' CHECK (outreach_state IN ('not_contacted', 'invited', 'connected', 'declined')),
    last_contact_at TEXT,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1)
) STRICT;

CREATE TABLE IF NOT EXISTS contact_aliases (
    linkedin_url TEXT PRIMARY KEY,
    contact_id TEXT NOT NULL REFERENCES contacts(contact_id),
    verified_at TEXT NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS company_contacts (
    company_key TEXT NOT NULL REFERENCES companies(company_key),
    contact_id TEXT NOT NULL REFERENCES contacts(contact_id),
    employment_state TEXT NOT NULL CHECK (employment_state IN ('current', 'former', 'unverified')),
    role_title TEXT,
    employment_evidence_ref TEXT,
    verified_at TEXT,
    draft_path TEXT,
    draft_sha256 TEXT CHECK (length(draft_sha256) = 64),
    PRIMARY KEY (company_key, contact_id),
    CHECK ((draft_path IS NULL) = (draft_sha256 IS NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS job_contacts (
    job_id TEXT NOT NULL,
    company_key TEXT NOT NULL,
    contact_id TEXT NOT NULL,
    rank INTEGER NOT NULL CHECK (rank >= 1),
    referral_state TEXT NOT NULL DEFAULT 'not_requested' CHECK (referral_state IN ('not_requested', 'requested', 'link_received', 'employee_referred', 'declined', 'expired')),
    referral_url TEXT,
    evidence_ref TEXT,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    PRIMARY KEY (job_id, contact_id),
    FOREIGN KEY (job_id, company_key) REFERENCES jobs(job_id, company_key),
    FOREIGN KEY (company_key, contact_id) REFERENCES company_contacts(company_key, contact_id)
) STRICT;

CREATE TABLE IF NOT EXISTS job_referrals (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    referral_state TEXT NOT NULL CHECK (referral_state IN ('not_requested', 'requested', 'link_received', 'employee_referred', 'declined', 'expired')),
    referral_url TEXT,
    evidence_ref TEXT,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1)
) STRICT;

CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    state TEXT NOT NULL CHECK (state IN ('not_started', 'ready_for_user', 'submission_pending_verification', 'submitted', 'form_blocked', 'interviewing', 'rejected', 'offer', 'withdrawn')),
    route TEXT CHECK (route IN ('direct', 'referral_link', 'employee_submission')),
    resume_id TEXT REFERENCES resumes(resume_id),
    form_ref TEXT,
    form_context_json TEXT CHECK (json_valid(form_context_json)),
    prepared_at TEXT,
    submit_clicked_at TEXT,
    pending_submit_event_id TEXT REFERENCES events(event_id) DEFERRABLE INITIALLY DEFERRED,
    submitted_at TEXT,
    last_evidence_ref TEXT,
    blocker_code TEXT,
    next_action TEXT,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1 CHECK (row_version >= 1),
    CHECK ((state = 'submission_pending_verification') = (pending_submit_event_id IS NOT NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('contact', 'job_contact', 'job_referral', 'application')),
    target_key TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(job_id),
    contact_id TEXT REFERENCES contacts(contact_id),
    source_kind TEXT NOT NULL CHECK (source_kind IN ('gmail', 'linkedin', 'ats', 'manual', 'orchestrator')),
    source_ref TEXT NOT NULL,
    evidence_kind TEXT,
    evidence_ref TEXT,
    occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
    applied_row_version INTEGER,
    UNIQUE (source_kind, source_ref, target_kind, target_key, event_type)
) STRICT;

CREATE TABLE IF NOT EXISTS manual_updates (
    update_id TEXT PRIMARY KEY,
    payload_sha256 TEXT NOT NULL CHECK (length(payload_sha256) = 64),
    fact_key TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('applied', 'duplicate')),
    event_id TEXT NOT NULL REFERENCES events(event_id),
    imported_at TEXT NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS events_target_time ON events(target_kind, target_key, occurred_at);
CREATE INDEX IF NOT EXISTS sources_job ON job_sources(job_id);
CREATE INDEX IF NOT EXISTS resumes_job ON resumes(job_id, jd_revision, base_resume_id, verified_at);
CREATE INDEX IF NOT EXISTS manual_updates_fact ON manual_updates(fact_key);
INSERT OR IGNORE INTO settings (key, value_json) VALUES ('candidate_constraints_revision', '1');
CREATE TABLE IF NOT EXISTS job_workflow_admissions (
    job_id TEXT PRIMARY KEY REFERENCES jobs(job_id),
    admitted_at TEXT NOT NULL,
    match_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    admission_reason TEXT NOT NULL CHECK (admission_reason IN ('matched', 'policy_transition'))
) STRICT;
PRAGMA user_version = 4;
COMMIT;

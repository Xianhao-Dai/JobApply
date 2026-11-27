---
name: jobapply-prepare-application
description: Execute a dispatched JobApply prepare task by checking the official role, verified resume and candidate answers, then preparing an application for the applicant to review and submit.
---

# Prepare an application

Use this skill for an authorized `stage="prepare"` task. Read the [worker contract](../../../docs/worker-contract.md) for the v1 envelope, execution controls, staged artifacts, and coordinator ownership. Skill authoring, an offline demo, and synthetic fixtures authorize no live application actions.

Before external actions, check the configured private execution controls and the current authorization supplied by the coordinator. A newer applicant pause takes precedence over a queued or retrying task. Preserve existing state and handoffs when paused.

## Bind the task

Require `job_id == scope_key` and preserve the complete task identity. `input_versions` pins `jd_revision`, `resume_id`, `application_version`, and `route_fingerprint`.

Read these `input` fields as dispatched:

- `official_req_id`, `apply_url`, `route`, and optional `referral_url`.
- `resume_pdf_path`, `resume_pdf_sha256`, `graduation_month`, and `candidate_answers_ref`.
- `current_application_state` and optional `existing_form`.
- When supplied, `employment_type`, `availability`, `preparation_timing_policy`, `route_preference`, and `reapply_request`.

Resolve the generated resume and answer reference only in the configured private workspace. Verify the PDF SHA-256 and its registered role, revision, and graduation month before uploading. Use the current official JD and current match through the coordinator's read-only context. The task contains no mandatory `workflow_context` object. Return a specific input problem if the necessary pinned context cannot be established.

Use only verified local answers and the approved resume. Keep role-specific availability, enrollment, graduation, work authorization, and sponsorship answers consistent with their actual question. A planned date or arrangement does not prove institutional approval. An unknown required answer produces a handoff, with the exact missing field identified. Demo answers are not applicant facts.

## Preserve the application and route

Verify the official employer, requisition, entry route, and current open state. An inaccessible page leaves the official status unverified. It does not establish closure. Return `form_blocked` with `OFFICIAL_STATUS_UNVERIFIED` when this prevents preparation.

Locate an existing live form before opening another. `existing_form` carries `form_ref`, `jd_revision`, `resume_id`, and `route_fingerprint`. Resume it only when these match the task and actual page. Preserve a valid draft and current Review/Submit handoff without refreshing, closing, or navigating it to another role. A late referral does not replace valid form progress automatically.

Confirmed submission means `existing_application`, supported by a receipt or official application status uniquely tied to this role. A pending submission needs verification before another application. A same-title historical receipt without a unique requisition association leaves a possible duplicate, reported as `POSSIBLE_DUPLICATE_APPLICATION`. An applicable `reapply_request` permits only the coordinator-authorized retry and does not erase history or bypass an explicit existing-application state. Honor a recorded withdrawal.

Use the dispatched `direct` or `referral_link` route and its fingerprint. A referral entry must be an employer-recognized route for the same requisition. Respect a supplied direct-route preference. Missing outreach or a future referral is not a reason to delay an otherwise authorized direct preparation. An employee referral is not proof of a complete application. `employee_submission` is not a fillable form route in the current Ledger contract.

## Prepare and hand off

Use an approved authenticated browser or supported official tool. Reuse the appropriate existing account identity. Account creation or authentication assistance requires the user's authorization and the current tool's approval rules. A missing session, verification challenge, or unavailable capability should produce a recoverable handoff. Do not switch identities to evade a duplicate application.

Keep credentials, authentication codes, session tokens, and secret verification links out of artifacts, results, logs, and messages. If authorized verification is needed, restrict reads to that operation's official sender, recipient, purpose, and recent time range. Record only a safe message reference and verification outcome. Authentication success is separate from application receipt.

Fill known fields and attach the verified generated PDF within the authorized scope. Stop for new eligibility conflicts or required facts that the local answers do not establish. Follow current tool confirmation rules for binding agreements and other consequential actions.

The applicant performs signatures, required personal attestations, and every actual final submission action. Determine the effect of the next action, including a last Save/Continue, profile completion, or registration that automatically applies. Do not click a potentially final action to discover its effect. Describe the observed boundary and leave the page recoverable.

Keep current pages that need applicant review, submission, login, verification, signatures, or required answers. Also retain valid drafts whose progress would be lost on close. Re-mark an existing handoff when required by the browser tool. Close workflow-owned research, JD, duplicate, obsolete, and safely recoverable inactive pages after use. Leave unrelated user tabs alone.

## Return the v1 result

Write the result in the dispatched attempt directory using the shared envelope. `output` contains exactly these stage fields:

- `disposition` is `ready_for_user`, `form_blocked`, `existing_application`, or `listing_closed`.
- `form_ref` is a safe official role/application URL, or `null` when unavailable. A tab ID is not a form reference.
- `blocker_code` is a concrete code for `form_blocked`, otherwise `null` when inapplicable.
- `next_action` describes the actual remaining action or recovery step, or is `null` when unnecessary.

Use `succeeded` for a verified `ready_for_user`, `existing_application`, or `listing_closed` outcome. Existing applications and closure require supporting evidence. Place the relevant receipt or official-status evidence first because the current Ledger projects that entry into the application event. A ready form requires a nonempty `form_ref`.

Use `blocked` with `disposition="form_blocked"` and a complete `issue` when user action or an external condition is required. Temporary technical failures use `retryable_error` with the recoverable location and complete `issue`. Preserve task identity and versions on every outcome. Never report submission merely because a form is ready.

An optional private `browser_handoff.json` can record the job, safe URL, browser/tab identifiers, pinned versions, and next action. Register it in `artifacts` with `kind="browser_handoff"`, its workspace-relative `path`, and SHA-256. It supplements `form_ref` and must contain no credentials or secret URL parameters. The coordinator applies results and writes the Ledger.

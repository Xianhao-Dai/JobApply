---
name: jobapply-track
description: Execute a dispatched JobApply track task by reading authorized referral, receipt, and recruiting-status evidence and returning idempotent event proposals with honest coverage.
---

# Track applications and referrals

Use this skill for an authorized `stage="track"` task. Read the [worker contract](../../../docs/worker-contract.md) for the v1 envelope, execution controls, evidence, and coordinator ownership. Access to a fixture, an offline demo, or this skill alone grants no live mailbox, messaging, or ATS access.

Before reading external sources, check the configured private execution controls and the current authorization supplied by the coordinator. A newer applicant pause takes precedence over a scheduled or retrying task. Preserve history and successful checkpoints when paused.

## Bind the scope

Require `job_id == scope_key`. Preserve `input_versions.application_version` and `input_versions.scan_window_id` even when evidence arrives after dispatch.

Read `input.official_req_id`, `company_name`, `role_title`, `since_at`, `known_message_refs`, `job_contacts`, and `current_application_state`. Optional `submit_clicked_at`, `pending_submit_event_id`, and `cursor` identify the submission and previous scan progress. Each `job_contacts` item supplies `contact_id` and `display_name`.

Use only the deployment's configured and authorized sources, account identities, and read-only capabilities. Associate each read with the actual application identity. A new contact address does not transfer old receipts to another inbox. Record inaccessible identities and unread ranges as coverage gaps. Preserve the previous successful checkpoint for an unfinished range.

Read only relevant messages and official status pages. Do not send or modify messages, invitations, applications, or account settings. Use an approved authenticated browser only when the required official evidence is unavailable through a suitable connector. Preserve active application handoffs, close your research pages, and leave unrelated user tabs alone.

Do not expose or persist authentication codes, passwords, session tokens, or secret verification links. A safe message ID and verification outcome are enough to identify an authentication message that does not prove submission.

## Establish the fact and role

Match the official requisition or other evidence that uniquely associates the fact with this job. A shared employer or title alone is insufficient. Keep uncertain associations in `unmatched_evidence` rather than turning them into a submission or clearing duplicate guards.

Distinguish a referral link, an employee-entered referral, and an employer-received complete application. A referral link must resolve to an employer-recognized route for the same requisition. An employee's referral confirmation supports `referral_submitted_by_employee`. It supports `application_received` only with independent evidence of a complete application. If the role is known but the referring person is not, omit `contact_id` so the coordinator can record a job-level referral.

An exact official receipt or ATS status can establish `application_received`, including when no prior click event was recorded. Registration, login, verification messages, an uploaded resume, and a prepared form do not establish receipt. No email found does not establish non-submission.

For `application_not_submitted_verified`, require affirmative evidence that the current click did not submit, a later `occurred_at` than `submit_clicked_at`, and the same `pending_submit_event_id` from the task. A timeout or missing receipt is insufficient.

Use the fact's actual event time as `occurred_at`. Receipt delivery time can date a receipt event, but it is not the application submission time. Include `submitted_at` only when evidence explicitly establishes that separate time and it is no later than the receipt/event. Leave it absent when the employer confirms receipt without an exact submission time.

Preserve existing history and identifiers through reposts, file relocation, changing availability, and late evidence. Discovery freshness limits new admission and does not end tracking of an existing application or resolve a duplicate. An inaccessible official page does not close a role. Silence does not prove rejection, withdrawal, failed submission, or a declined referral.

## Return event proposals

Write the shared v1 result in the dispatched attempt directory. `output` contains `proposed_events`, `unmatched_evidence`, and `next_cursor`.

Each proposed event contains `event_type`, this task's `job_id`, `source_kind`, `source_ref`, `occurred_at`, `evidence_kind`, and `evidence_ref`. Connection events, `referral_requested`, and `referral_declined` require an associated `contact_id`. Other referral events can omit it for job-level evidence. Application events must omit `contact_id`. Add `referral_url`, `pending_submit_event_id`, or `submitted_at` only when applicable and evidenced.

The current public Ledger accepts worker `source_kind` values `gmail`, `linkedin`, and `ats`. Use the actual source. A different provider must not be mislabeled to fit this enum. Return unsupported source evidence for coordinator review instead of inventing a provider enum or a false event provenance. Evidence kinds should describe the actual evidence, such as `gmail_message`, `linkedin_message`, `ats_receipt`, or `ats_status`.

Use these existing event names when the fact supports them:

- `connection_invited`, `connection_accepted`, `connection_declined`.
- `referral_requested`, `referral_link_received`, `referral_submitted_by_employee`, `referral_declined`.
- `application_received`, `application_not_submitted_verified`, `application_interviewing`, `application_rejected`, `application_offer_received`, `application_withdrawn`.

Do not propose `application_form_ready`, `application_form_blocked`, `application_submit_clicked`, or `referral_wait_expired` from this worker. Those describe coordinator, preparation, or applicant actions outside this stage. Reading a connection invitation does not authorize sending one, and a local withdrawal preference does not prove withdrawal at the employer.

Use a stable message, conversation, receipt, or official-status identifier for `source_ref`, with a concise nonsecret `evidence_ref`. Preserve the original time and evidence on retries. The Ledger deduplicates by source, reference, target, and event type, and treats changed content for the same identity as a conflict. A new read time does not create a new underlying event. `known_message_refs` supports avoiding redundant work without discarding independently new facts.

Each unmatched item carries `source_kind`, `source_ref`, `occurred_at` when known, and a specific `reason`. Preserve only the evidence needed to resolve the association. Avoid full unrelated message bodies and applicant data in results.

Return `succeeded` after the authorized range is actually read, using empty arrays when there are no new facts. Advance `next_cursor` only for completed coverage, preserving pending pagination and original evidence times. A temporary read failure uses `retryable_error`; missing access or a required identity decision uses `blocked`. Include a complete shared `issue` and explain uncovered ranges. Do not claim successful whole-scope coverage from a partial read. Preserve any verified partial findings for recovery.

Workers return proposals and staged evidence. The coordinator owns Ledger writes, reconciliation against the current application version, accepted event transitions, and report exports.

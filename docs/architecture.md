# Architecture

## Coordinator and workers

Discovery, matching, résumé verification, contacts, preparation, and tracking use the same v1 envelope. Workers return evidence and proposed changes. The coordinator checks identity, current inputs, and hashes before applying a result. SQLite is the system of record, and CSV and worksheet views are projections.

| Field | Role |
| --- | --- |
| `task_id`, `stage`, `scope_key`, `job_id` | Identify the work and the affected source or role. |
| `attempt`, `deadline_at` | Bind a lease and distinguish retries of the same work. |
| `input_versions` | Pin the relevant scan window, JD revision, base résumé, application version, and route. |
| `input` | Give the worker the scoped task data. |
| `result_id`, `status`, `output` | Name an idempotent result and carry its structured findings or proposed state change. |
| `artifacts`, `evidence` | Identify staged files by SHA-256 and preserve the provenance of a result. |

An accepted result is tied to a particular attempt. Replaying that result is idempotent. A result from a stale attempt or a mismatched revision cannot overwrite current work. Artifact paths are resolved within the workspace. The writer promotes validated artifacts and keeps ledger updates in a transaction.

## Discovery and preparation

A rolling discovery window applies to initial admission. Posting evidence and explicit repost evidence are stored separately. Two listings with the same employer and official requisition share one job, including its accumulated history. A qualifying match admits the job to the ongoing workflow. The job can still move forward after the source window expires while the official listing, application state, and material versions remain valid.

The preparation stage can select a verified referral route or proceed with direct preparation. Existing valid form progress takes precedence over starting another form for the same position. A user-input blocker reserves the handoff for the applicant. A transient technical failure can be retried under the same task and a new attempt.

## Application states

```text
not_started ──prepare──> ready_for_user ──user records click──> submission_pending_verification
     │                         ▲                                    │
     └──────> form_blocked ────┘                                    ├── confirmed receipt ──> submitted
                                                                    └── verified failure ──> not_started
```

A receipt can also verify a submission when the applicant previously clicked Submit outside the coordinator. Receipts and manual updates carry separate timestamps, source references, and evidence. The ledger can accept late evidence without moving a newer state backwards.

## Source layout

| File | Responsibility |
| --- | --- |
| `scripts/schema.sql` | SQLite tables, indexes, and constraints. |
| `scripts/jobapply_ledger.py` | Task dispatch, result acceptance, artifact promotion, and event transitions. |
| `scripts/jobapply_freshness.py` | Publication evidence, rolling windows, and durable admissions. |
| `scripts/jobapply_scope.py` | Role-specific candidate contexts and version checks. |
| `scripts/jobapply_batch.py` | Draft and collect stage envelopes. |
| `scripts/jobapply_intake.py` | Normalize manual leads and attach them to verified jobs. |
| `scripts/jobapply_views.py` | CSV and worksheet views. |
| `demo.py` | Offline replay using synthetic fixtures and the same ledger. |

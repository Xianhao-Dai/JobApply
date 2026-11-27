# Worker handoff contract

## Workspaces

The checkout contains code and the role skills. The private workspace contains the SQLite database, user configuration, documents, job evidence, and reports. The coordinator passes both roots and one `task.json` path to a worker. Paths in envelopes and artifacts are relative to the private root. Workers write `result.json` and artifacts under the task's own `state/staging/<task_id>/<attempt>/` directory. The coordinator validates and promotes results through `jobapply_batch.py collect`.

External content can provide evidence about the job or the form. Keep the worker's authorization and paths grounded in the applicant's request, configured scope, and dispatched task. Store safe official URLs without account, invitation, verification, or session tokens. Work with confirmed applicant facts and leave the final submission to the applicant.

## Result envelope

Copy `schema_version`, `task_id`, `attempt`, `stage`, `scope_key`, `job_id`, and `input_versions` exactly from the task. Use an ID like `res_<task_id>_a<attempt>` in `result_id`. Add `completed_at` with the actual timezone-aware timestamp and the following fields.

| Field | Accepted values and shape |
| --- | --- |
| `status` | `succeeded`, `retryable_error`, `blocked`, `terminal_error`, or `superseded` |
| `output` | Object containing the role-specific fields from the stage skill. |
| `artifacts` | Array of objects with `kind`, workspace-relative `path`, and lowercase SHA-256 `sha256`. Empty when no file is promoted. |
| `evidence` | Array of objects with `kind`, concise `ref`, and timezone-aware `observed_at` where known. Keep private details local. |
| `issue` | `null` on success. Otherwise an object with `code`, `message`, `needs_user`, and optional `retry_after_at`. |

Use `blocked` for a specific dependency or required user action and `retryable_error` for a temporary tool or provider failure. Keep filenames and hashes tied to the dispatched attempt. Reuse an accepted result ID only for an identical replay. Keep network failures and incomplete coverage explicit.

## Job identity and human actions

A verified discovery observation carries the official requisition or stable posting ID and a JD artifact. The ledger identifies a job by employer and requisition. A manual lead uses its `lead_id` as `source_listing_id`, and the coordinator resolves the lead after accepting the official job. For ambiguous leads, retain the pending item and report the missing evidence.

In a follow-up run, the applicant can report that they clicked Submit. Record their explicit report with `jobapply_ledger.py --root .jobapply manual <update.json>` using `action_code: application_submit_clicked`, the current `expected_version`, and the correct job ID. The state is then pending verification. A trustworthy official receipt tied to the exact requisition can verify submission even when the applicant reports no separate click timestamp. A recovered draft or a conditional status email leaves submission unverified.

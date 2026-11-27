---
name: jobapply-discover
description: Execute a supplied JobApply discover task by checking its named job source or manual lead, verifying employer postings, and returning v1 observations with posting evidence and staged JDs.
---

# Discover and verify jobs

Use this skill for a supplied `stage=discover` task. Read the [worker contract](../../../docs/worker-contract.md). The coordinator supplies the repository root, private workspace root, and `task.json`. Work within that task's scope and attempt directory, return `result.json`, and leave ledger writes to the coordinator.

## Scope and evidence

- Check `job_id=null`, `scope_key=input.source_key`, `input_versions.source_config_revision`, and `scan_window_id`. Read `since_at`, `until_at`, `target_filters`, and any cursor or lead in `input`. Keep the supplied identity and window throughout the run.
- Supported named sources are `linkedin:jobs`, `newgrad-jobs:us`, `speedyapply:2027-us-newgrad`, and `glassdoor:jobs`. Use the specific source entry, alert, or query supplied by the coordinator. For `manual:joblead`, verify the supplied URL or company-and-role lead. Use `input.lead_id` as the manual observation’s `source_listing_id`. A verified manual posting can proceed even if its publication time is unknown or outside the automated scan window.
- Honor any task-supplied row limit and the coordinator’s channel permissions. Use available authorized source access. Public employer career pages and official ATS postings establish the requisition, hiring employer, title, location, employment type, requirements, and listing state. When source access requires unavailable credentials or a human action, report the concrete gap. A partial scan describes the portion actually covered and retains the prior cursor.
- Apply the task's role and location filters to the official JD. Read required experience and internship conditions rather than deciding level from a title alone. Preserve ambiguous leads for verification instead of inventing a qualification or requisition ID.
- Merge evidence conceptually by the verified employer and official requisition. Tracking parameters or identical titles do not establish that two listings are the same role. Keep the source's displayed company separate from a different verified hiring employer.
- Use original publication evidence and explicit repost evidence for the task's admission window. Preserve original dates. A feed update, alert delivery, page refresh, or new local observation does not establish a repost. For a relative age, preserve the raw label and observation time and use its conservative lower time bound. Re-reading the same label during a retry must not refresh its anchor.
- When a later observation supplies explicit repost evidence, use that actual observation as the row timestamp and keep the earlier source observation in evidence. `repost_observed_at` must be at or before the row `observed_at`.
- Set `closed` only from official closure or deadline evidence. An access failure yields an unverified observation or `unknown` official state. Save a verified JD as UTF-8 text containing the posting body. Put capture timestamps and access notes in `evidence` so they do not create artificial JD revisions.

## Return v1 output

`output` contains `observations` and `next_cursor`. It may include `coverage` explaining completed pages, remaining work, and access gaps. A completed empty scan returns `observations: []`. Return the prior cursor for a single-role check or incomplete scan. Only a completed scan advances its source cursor.

Each observation carries `source_listing_id`, `observed_url`, `company_as_listed`, `title_as_listed`, `location_as_listed`, `published_at_as_listed`, and `observed_at`. Add verified official fields when available:

- `company_key`, `official_company`, `official_domain`, `official_req_id`, `official_title`, and `official_location`.
- `official_url`, `apply_url`, `listing_state` (`open`, `closed`, or `unknown`), `closes_at`, and `jd_path`.
- `published_at` and `published_at_basis`, using `official_first_published`, `official_last_published`, or `official_date_only`. Unknown dates and their basis remain `null`.
- `reposted_at`, `reposted_at_basis` (`source_exact` or `source_relative_lower_bound`), `repost_observed_at`, and `repost_label` when supported by actual repost evidence.
- `official_employment_type` and `employment_type_evidence_ref` when the official posting establishes the type.

For each staged JD, add an artifact with `kind: "jd"`, its workspace-relative `path`, and SHA-256. The observation's `jd_path` must equal that path. Evidence entries should identify the source or official page, the observation time, and the fact verified. The coordinator assigns canonical job IDs and JD revisions.

Use `succeeded` for completed coverage, `retryable_error` for a temporary access failure, or `blocked` for missing access or source information. Include safely verified partial observations when useful and an `issue` for every non-success result. Follow the shared contract for deadlines and superseded tasks.

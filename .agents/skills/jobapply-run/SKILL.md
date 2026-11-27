---
name: jobapply-run
description: Run the local JobApply offline demo or coordinate one explicitly requested live job-search cycle in Codex. Use for official job leads, staged résumé matching, applicant review handoffs, or verified status updates.
---

# Run JobApply

Start in the repository root. For a demo, run `python3 demo.py`, optionally with `--with-receipt`. Use live mode when the user requests it and has set up the private configuration described in the [README](../../../README.md). Configured capabilities limit a run. Use the user's current request to authorize each source, form, or mailbox scope.

## Start one live cycle

1. Run `python3 scripts/jobapply_start.py --config .jobapply/config.json`. Add `--lead '<official HTTPS job URL>'` when the user supplies a specific posting. Read the returned workspace, limits, and task paths. If setup is incomplete, report the missing fact or resource. Preserve the approved PDF, existing state, and private workspace.
2. Open the [worker contract](../../../docs/worker-contract.md). Dispatch only tasks whose `execution_authorized` flag is true and whose source is covered by the user’s request. Give the worker its attempt path, the repository and private workspace roots, and the relevant capability scope. Ask it to read the corresponding role skill. Delegate independent tasks when the host exposes subagents. Otherwise run the same role steps sequentially and use the same file handoff. For browser work, ensure the worker can access the user's approved session. If the coordinator holds that session, it can perform the browser step using the preparation skill.
3. Collect finished results individually or in a small batch. Use only results from the emitted attempt directories and inspect the accepted outcome. For a verified manual lead, link the returned `job_id` to its `lead_id` using the intake command. A provisional or ambiguous lead stays pending. Deduplicate accepted job IDs before requesting matching. An already verified supplied lead may use its linked job ID for the same bounded run after the official listing is rechecked. Process the run's explicit candidate set and honor both limits and current qualification.
4. For a current `no_changes` match, request `tailor` to reuse the verified PDF. For `needs_edits`, preserve the concrete edit plan and proceed when the applicant has approved facts and wording and the editable source is verified. `needs_review` and `not_fit` return to the applicant as evidence-based decisions. Contact research, when enabled, can run in parallel. Use an existing verified referral entry when available, or continue with direct review.
5. Request `prepare` for qualified jobs with a current verified PDF when the applicant has authorized form preparation. For an existing user-input blocker, wait for the applicant’s response or explicit request to resume that job before targeting it again. Stop before the action that submits the application. Preserve a recoverable official URL and, when available, the active review tab. Request `track` only for exact roles and authorized receipt sources. Distinguish a form being ready, the applicant reporting a click, and an employer receipt.
6. Export reports and show the applicant the job IDs, current statuses, usable URLs, and required next actions. Close only temporary research tabs opened for the cycle. Preserve unresolved handoffs. End after this cycle.

| Task stage | Worker skill |
| --- | --- |
| `discover` | [jobapply-discover](../jobapply-discover/SKILL.md) |
| `match` | [jobapply-match](../jobapply-match/SKILL.md) |
| `tailor` | [jobapply-tailor](../jobapply-tailor/SKILL.md) |
| `connections` | [jobapply-connections](../jobapply-connections/SKILL.md) |
| `prepare` | [jobapply-prepare-application](../jobapply-prepare-application/SKILL.md) |
| `track` | [jobapply-track](../jobapply-track/SKILL.md) |

## Coordinator commands

Use the private root from the launcher in these commands. Result arguments are file paths from the command's current directory. Use actual IDs and paths from the accepted run.

```bash
python3 scripts/jobapply_batch.py --root .jobapply collect .jobapply/state/staging/TASK_ID/1/result.json
python3 scripts/jobapply_intake.py --root .jobapply resolve LEAD_ID --job-id JOB_ID
python3 scripts/jobapply_batch.py --root .jobapply tasks match --limit 1 JOB_ID
python3 scripts/jobapply_batch.py --root .jobapply tasks tailor --limit 1 JOB_ID
python3 scripts/jobapply_views.py --root .jobapply export > .jobapply/reports.json
```

The `tasks` command also accepts `connections`, `prepare`, and `track`. An empty output means the ledger has no eligible new task for the requested scope. Inspect the existing status or blocker before retrying. If the operator asks to stop, end the run and retain existing data for a future explicit request.

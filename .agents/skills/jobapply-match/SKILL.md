---
name: jobapply-match
description: Evaluate one JobApply match task against its pinned official JD, canonical resume, and candidate constraints, returning evidence-based HR and hiring-manager findings and a v1 decision.
---

# Match a resume to a job

Use this skill for a supplied `stage=match` task. Read the [worker contract](../../../docs/worker-contract.md). Resolve task input paths against the supplied private workspace root. Write `result.json` in the task attempt directory and leave ledger writes and downstream dispatch to the coordinator.

## Evaluate the supplied versions

- Check `scope_key=job_id` and preserve `jd_revision`, `base_resume_id`, and `candidate_constraints_revision` from `input_versions`.
- Read `input.jd_path`, `candidate_constraints`, and the canonical source selected by `base_source_format`. Verify the supplied PDF and/or DOCX hashes before using their content. A PDF source supports matching without an editable Word file. Missing, changed, or unreadable input needs a concrete issue rather than a guessed assessment.
- Assess the task's employment type, graduation and availability facts, location scope, sponsorship facts, and experience constraints. For an internship, examine enrollment, return-to-study, duration, hours, and any required work authorization against the supplied facts. Conditional plans remain conditional.
- Record HR findings for explicit required qualifications and hiring constraints. Record hiring-manager findings for the responsibilities and technical depth demonstrated by specific resume evidence. Distinguish required conditions from preferences. Count professional, full-time, internship, and relevant experience according to the JD's wording and actual dates.
- A language listed on the resume can support an unquantified proficiency requirement. Missing project detail can leave depth uncertain without proving absence of the skill. Explicit experience thresholds and contradictory facts need their own findings.
- `no_changes` means the current canonical material can be used as supplied. Use `needs_edits` when existing supported facts need better selection, wording, ordering, or an approved task-specific date change. A graduation month differing from the base requires `needs_edits`.
- Use `needs_review` when an unresolved fact or policy materially affects eligibility. Use `not_fit` for a demonstrated conflict or a required qualification the supplied evidence cannot support. State the exact gap. Ordinary questions about sponsorship or availability do not by themselves establish an employer prohibition.
- If the task contains a coordinator-validated `candidate_constraints.application_directive`, honor its exact scope while keeping the accepted gap visible as `unmet`. That instruction does not create experience or change other qualifications.
- Evaluate a valid dispatched task against its preserved discovery evidence. The coordinator handles admission windows and current versions. Do not change dates to keep a job eligible or reject it solely because the discovery window elapsed during this assessment.

## Return v1 output

Return these exact `output` fields:

```json
{
  "decision": "no_changes",
  "hr_findings": [],
  "hm_findings": [],
  "edit_plan": [],
  "open_questions": []
}
```

`decision` is `no_changes`, `needs_edits`, `needs_review`, or `not_fit`. Each entry in either findings array is `{ "requirement": "...", "finding": "met", "evidence_ref": "..." }`, with `finding` chosen from `met`, `unmet`, `unknown`, or `partial`. Reference the relevant JD passage and canonical resume section or supplied constraint. An edit-plan item identifies the existing evidence and proposed change. Open questions identify the fact needed and how it affects the decision.

A completed assessment returns technical status `succeeded`, including decisions of `not_fit` or `needs_review`. Temporary input access failures use `retryable_error`. Missing required material or candidate facts needed to perform the assessment use `blocked`. Populate `issue` for non-success results and preserve every identity field. This stage returns assessment and suggestions for the coordinator to use in later material preparation.

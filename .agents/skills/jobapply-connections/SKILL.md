---
name: jobapply-connections
description: Execute a supplied JobApply connections task by verifying company contacts and shared background, reusing known contacts, and drafting personalized outreach as v1 candidates without sending it.
---

# Find relevant company contacts

Use this skill for a supplied `stage=connections` task. Read the [worker contract](../../../docs/worker-contract.md). The coordinator supplies repository and private workspace roots, the scoped `task.json`, and the attempt directory for draft artifacts and `result.json`. The coordinator owns contact identity assignment, state changes, and ledger writes.

## Research and draft

- Check `scope_key=job_id`, `input_versions.jd_revision`, `company_key`, `company_name`, `role_title`, `relationship_priority`, and `existing_contacts`. Use the task's candidate-background facts and authorized sources when available. A direct-application preference with `seek_referral=false` returns to the coordinator without additional outreach research.
- Reuse known contacts and verified aliases before finding additional people. Confirm current employment and the claimed shared background separately using public professional profiles, employer pages, or other primary professional evidence. Preserve uncertainty in `employment_state` rather than presenting an old role as current.
- Follow the supplied relationship priorities. The public relationship kinds are `high_school`, `university`, `former_coworker`, `graduate_school`, and `other`. Prioritize a relevant current team within a relationship tier when the evidence supports it. A name alone establishes neither nationality nor personal connection.
- Identify a person by a verified LinkedIn profile URL and coordinator-provided aliases. Copy `contact_id` only for a confirmed existing contact. Leave the identifier absent or `null` for a new candidate.
- Write a concise personalized draft grounded in the confirmed common background, the role, and supplied candidate facts. Use the language supported by the task or the person's public professional profile. Save drafts locally for the applicant to review and send. Sending invitations, messages, or email is outside this worker's scope.
- Contact discovery runs alongside resume and application preparation. Return a verified referral lead or an indication of possible employee submission to the coordinator for verification. A draft, accepted invitation, or willingness to help does not establish a completed referral or application.

## Return v1 output

`output` contains a `candidates` array. Each candidate includes:

```json
{
  "contact_id": null,
  "display_name": "...",
  "linkedin_url": "...",
  "role_title": "...",
  "employment_state": "current",
  "employment_evidence_ref": "...",
  "relationship_kind": "university",
  "relationship_evidence_ref": "...",
  "rank": 1,
  "draft_path": null
}
```

`linkedin_url` is a verified HTTPS LinkedIn `/in/` profile. `employment_state` is `current`, `former`, or `unverified`. Use positive integer ranks. Evidence references identify the public fact and its source. Include a draft when the evidence supports an accurate introduction.

For a new draft, set `draft_path` to a file inside the task attempt directory and register an artifact with `kind: "connect_draft"`, the same workspace-relative path, and SHA-256. To reuse a supplied existing draft, return its given path after verifying its content and registered hash. The coordinator retains its established company directory and merges repeated people.

Completed research with no reliable candidates returns `succeeded` and `candidates: []`. A temporary source failure uses `retryable_error`. Missing access or candidate facts that prevent meaningful research use `blocked` with a specific `issue`. Preserve task identity and report the actual coverage so the coordinator can continue other stages.

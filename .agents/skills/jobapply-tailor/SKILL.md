---
name: jobapply-tailor
description: Execute one JobApply tailor task by verifying and copying its canonical resume PDF or applying supplied approved edits to a synchronized Word source, then returning verified v1 artifacts or a material blocker.
---

# Prepare a job-specific resume

Use this skill for a supplied `stage=tailor` task. Read the [worker contract](../../../docs/worker-contract.md). Read pinned inputs from the supplied private workspace and create the output files and `result.json` within the supplied task attempt directory. The coordinator promotes accepted artifacts and writes the ledger.

## Select the task's material route

Check `job_id`, `scope_key`, `jd_revision`, `base_resume_id`, `input.mode`, `jd_path`, `graduation_month`, and the canonical source paths and hashes. The task declares `base_source_format` and any `base_pdf_path`, `base_pdf_sha256`, `base_docx_path`, and `base_docx_sha256`. Verify the files actually supplied. A similarly named document does not establish an editable source for the registered PDF.

- For `mode=export_only` with a PDF base, copy its original bytes to the staged PDF. Its SHA-256 must equal `base_pdf_sha256`. Confirm the selected graduation month and content agree with the task. This route produces only a PDF artifact.
- For `export_only` with a DOCX-only base, export the pinned source using available document tooling and verify the resulting PDF. Preserve the source content and layout.
- For `mode=tailor`, use the supplied synchronized Word source and `approved_edits`, bound by `input_versions.interview_sha256`. Apply only those approved changes to a copy. The coordinator's approval records connect each original passage, JD requirement, confirmed fact, and approved replacement text. Keep the task's exact graduation month consistent in Word and PDF.
- When the desired improvement requires additional facts, wording approval, or an updated editable source, return `blocked` with the specific missing material and proposed next action. The coordinator can obtain a fact-and-edit confirmation packet and dispatch a new valid task. Do not turn an edit suggestion into an invented accomplishment, metric, qualification, date, or approval.

## Verify the artifacts

Use available Word/PDF tooling. Keep the source typography, margins, bullets, tabs, section order, and page layout unless the supplied edits authorize a layout change. Inspect extracted text and links, render and visually review every page, and check for clipping, font substitution, broken bullets, and unintended pagination. A visual preview alone does not establish that text remains selectable or correctly encoded.

Save the verified PDF in the attempt directory. A tailored result also includes the edited DOCX and a `changes.md` tying each applied change to its supplied approval and JD requirement. Register hashes only after the final files are saved. If export or verification fails, preserve useful intermediate files inside the attempt directory and report the exact failed step for a scoped retry.

## Return v1 output

On success, `output` contains `resume_id`, `graduation_month`, and `pdf_verified: true`. Use a unique safe identifier for `resume_id`. An optional `changes_path` points to the staged change record for `tailor` and is `null` for `export_only`.

- `export_only` has exactly one artifact, with `kind: "pdf"`.
- `tailor` has exactly three artifacts, with kinds `pdf`, `docx`, and `changes`.

Each artifact has its workspace-relative `path` and SHA-256. Use `succeeded` only after content and visual verification pass. A temporary export-tool failure is `retryable_error`. Missing synchronized source material, unapproved edits, conflicting facts, or failed verification that needs human action is `blocked`. Non-success results include `issue`; a saved intermediate DOCX may be referenced as `output.previous_docx_path` and registered as an artifact for the coordinator to consider during retry. Report the files actually created rather than claiming a usable final PDF.

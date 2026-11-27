# JobApply repository guidance

- For a requested job-search run, use [jobapply-run](.agents/skills/jobapply-run/SKILL.md). A demo or general project discussion uses synthetic data. A live run requires a user request and a valid private local configuration.
- Keep the repository root as the code workspace and `.jobapply/` as the private data workspace. Run coordinator commands from the code workspace and pass the private root explicitly. Keep applicant files, evidence, credentials, and runtime outputs out of Git.
- Use the appropriate stage skill for each dispatched task. Workers return files in the attempt directory. The coordinator accepts results and owns all ledger writes.
- The applicant performs final submission and required personal attestations. Save contact drafts for the applicant to send. Continue with other eligible candidates when a source or step is blocked.
- For repository changes, run `python3 -m unittest discover -s tests` and both modes of `demo.py`. Use only synthetic or explicitly authorized public data for validation.

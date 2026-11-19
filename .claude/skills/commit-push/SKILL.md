---
name: commit-push
description: Commit all working-tree changes in the expense tracker and push them to GitHub (heptile7/expense-tracker, branch master). Runs the tests and lint first, makes sure nothing private (.env, data/, statements, logs) is staged, writes a commit message in the project's style, pushes and verifies the remote. Use when the user asks to commit and push, "ship it", "save and push", or to push the latest work.
---

# Commit and push

Work from the project root (`E:\Fun\expense-tracker`). Stop and tell the user at the first step that fails; never work around a failure silently.

## 1. Pre-flight

- Tests: `.venv\Scripts\python.exe -m pytest -q` (all must pass; about 40 s, the net banking tests drive a headless Edge).
- Lint: `.venv\Scripts\ruff.exe check .` (must say "All checks passed!").
- Skip either only if the user explicitly says so in this conversation, and say so in the report.

## 2. Stage and check what is going in

- `git add -A`, then `git status --short` and `git diff --cached --stat`.
- Nothing staged: tell the user there is nothing to commit and stop.
- These must never be committed. If any show up, unstage them (`git restore --staged <path>`), fix `.gitignore`, and tell the user:
  `.env`, anything under `data/` (database, backups, statements, inbox, logs), `credentials.json`, `.claude/logs/`, `.claude/settings.local.json`, `*.db`, and any `.pdf`, `.xls`, `.xlsx` or `.csv` outside `tests/`.
- Scan the staged diff for secrets:
  `git diff --cached | Select-String -Pattern 'sk-ant-[A-Za-z0-9]|ANTHROPIC_API_KEY=\S|BEGIN [A-Z ]*PRIVATE KEY|password\s*[=:]\s*\S{4,}'`
  On a match, look at the matched lines yourself. Known false positives: this skill's own pattern above, and the deliberately fake values in `tests/test_hooks.py` (e.g. `sk-ant-api03-abcdefghijklmnop`, `hunter22`) that test log redaction. If every match is clearly a placeholder, continue and mention it in the report. If any value could be real, stop and tell the user which files matched (not the matched text).

## 3. Write the commit message

Match the existing style (`git log -5 --format=%B` shows it):

- Subject: imperative, at most 72 characters, says what changed for the user (e.g. "Add sub-categories, richer dashboard statistics and a money plan").
- Body: a blank line, then short paragraphs or bullets grouped by area (categorization, dashboard, net banking, plan, tooling...). Say what changed and why, not file-by-file. Finish with the test count ("Tests: N passing.").
- Last line: the attribution trailer this session asks for (for example `Co-Authored-By: Claude ... <noreply@anthropic.com>`).

Write the message to a file in the session's scratchpad directory and commit with `git commit -q -F <file>`. Do not pass a multi-line message with `-m` or a PowerShell here-string: PowerShell splits it into separate arguments and git fails with "pathspec ... did not match".

## 4. Push

- `git push origin master`. Never force-push, never push other branches, never rewrite published history.
- If the push is rejected (remote has new commits), stop and ask the user before pulling or rebasing.
- The project's post-push hook (`scripts/hooks/post_push_cleanup.py`) then clears caches and upload leftovers in the background; its summary goes to `.claude/logs/cleanup.log`.

## 5. Verify

- `git ls-remote origin refs/heads/master` must print the same hash as `git rev-parse HEAD`.
- `git status -sb` must show `## master...origin/master` with nothing ahead and no changes.

## 6. Report

In a few lines: the short hash and subject, the link https://github.com/heptile7/expense-tracker, the test result, and anything you left out of the commit and why.

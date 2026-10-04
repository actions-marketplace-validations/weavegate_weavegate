---
name: github-workflow
description: "Manage weavegate's public GitHub delivery lifecycle using repository-specific conventions: create a missing public issue, prepare an issue-numbered branch, validate and publish implemented changes as a PR, update an existing PR after review or CI fixes, and merge only when explicitly requested. Use for weavegate issue/branch/commit/push/PR/update/merge work; inspect actual changes before writing PR content and keep all public artifacts self-contained."
---

# GitHub Workflow

Turn weavegate work into public, reviewable GitHub artifacts with traceability from issue to branch, commits, PR and merge. `AGENTS.md` and `CONTRIBUTING.md` are the canonical rules; this skill only adds how to apply them at each stage.

## Ground rules

- Inspect local state (`git status --short --branch`, `git log --oneline -5`) before any mutation. Preserve unrelated changes and stage only confirmed paths.
- Never create, push, update or merge a remote artifact without explicit user authorization for that stage. Do not reply to reviews, resolve threads or delete branches unless asked.
- Gitignored planning material is private input only. Never mention or link it in an issue or PR.
- When supplying a body through `gh` or an app, reproduce the repository template (`.github/ISSUE_TEMPLATE/issue.md`, `.github/pull_request_template.md`); GitHub does not merge templates into supplied bodies.

## Naming

- Issue and PR title: `Type: title` in English, only the first letter of `Type` capitalized, no issue/PR/sequence numbers.
- Branch: `<type><issue-number>/<short-kebab-summary>`, lowercase.
- Commit: `<type>(<scope>): <summary> #<issue-number>`, issue number last.

## Issues

- Reuse an existing issue for the same delivery unit; never create a duplicate or a replacement to improve wording. Edit an existing issue only when asked.
- Use `issue.md` order `Summary` → `TODO` → `Validation` → optional `Notes`. `bug_report.md` and `fixture_contribution.md` keep their own order.
- **Write requirements only.** `TODO` items are observable behaviors or user-visible contracts; `Validation` items are acceptance signals. Leave files, functions, scripts, commands, step order and other implementation or procedure choices to the implementing agent. Point to an ADR instead of restating its contract, and do not copy the `AGENTS.md` check list into an issue.
- Use only public issues, code, tests and docs as references.

## Pull requests

Before drafting, read the linked issue, the full base-to-head diff, the branch's commits, the validation actually run, and any known follow-up or risk. Use headings in this order: `Related Issues`, `Summary`, `Description`, `Review Points`, `Docs`, `Notes`, `Validation`.

- `Related Issues`: `Closes #N` for issues this PR completes; `Refs #N` when the issue needs evidence that only exists later (for example after a release tag).
- `Summary`: one or two headline lines.
- `Description`: how the capability now works at the behavior or design level, in implemented tense. Not a file inventory.
- `Review Points`: concrete decisions, invariants, failure modes and tradeoffs from this diff — not a generic checklist.
- `Docs`: check exactly one box. A user-visible contract change also adds its `CHANGELOG.md` `Unreleased` entry.
- `Notes`: follow-ups, limitations or tradeoffs; `- None.` when there are none.
- `Validation`: last. Only checks actually run, as `PASS`/`FAIL`/`SKIP` with reasons and evidence.

## Lifecycle modes

Pick the smallest mode for the current stage.

- **Prepare:** resolve the issue and base, confirm no equivalent branch exists, create the issue-numbered branch, return it. No implementation or push.
- **Publish:** after implementation, inspect the diff and history, run the applicable `AGENTS.md` checks plus `git diff --check`, push the branch, and open the PR — or switch to Update if one exists. Return issue, branch, commits, PR URL and validation results.
- **Update:** after review or CI fixes already made, map each change to the concern it addresses, re-run relevant checks, commit with the issue number, push, and update the PR body when its content materially changed. Never open a second PR.
- **Merge:** only on explicit request. Confirm required checks and reviews, report anything failing or pending first, use the requested or unambiguous default method (merge commit when the PR pins its own commit, per `CONTRIBUTING.md`). After merging, confirm that `Closes` issues actually closed.

Report only the artifacts the selected mode touched, and separate completed work from anything blocked or unauthorized.

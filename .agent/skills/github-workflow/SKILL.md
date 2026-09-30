---
name: github-workflow
description: "Manage weavegate's public GitHub delivery lifecycle using repository-specific conventions: create a missing public issue for an explicitly requested end-to-end flow, prepare an issue-numbered branch, validate and publish implemented changes as a PR, update an existing PR after review or CI fixes, and merge only when explicitly requested. Use for weavegate issue/branch/commit/push/PR/update/merge work; inspect actual changes before writing PR content and keep all public artifacts self-contained."
---

# GitHub Workflow

## Purpose and Authority

Turn weavegate work into public, reviewable GitHub artifacts while preserving traceability from issue to branch, commits, PR, review updates, and merge.

Use this skill's repository-specific branch, commit, and PR conventions instead of generic publish-skill conventions when working in weavegate. Prefer the connected GitHub app for structured issue and PR operations; use local `git` for checkout state and `gh` where connector coverage is insufficient.

## Preconditions

1. Inspect local state before any mutation:
   - `git status --short --branch`
   - `git remote -v`
   - `git log --oneline -5`
2. Preserve unrelated user changes. Stage only confirmed paths.
3. Confirm `gh auth status` before CLI-backed remote work.
4. Never create, push, update, or merge a remote artifact without explicit user authorization for that stage.
5. Treat private or gitignored planning artifacts as local reasoning inputs only. Never mention or link them in public issues or PRs.

## Canonical Public Artifacts

Repository templates are the canonical public structures:

- Issues: `.github/ISSUE_TEMPLATE/issue.md`
- Pull requests: `.github/pull_request_template.md`

When supplying a body explicitly through an app or CLI, reproduce the corresponding template structure because GitHub does not merge the repository template into an explicitly supplied body.

### Issue Rules

- Reuse an existing related issue when one is supplied or already exists. Never create a duplicate issue for the same delivery unit.
- When a dedicated issue-writing workflow supplies a reviewed draft or created issue, validate it against the public template and reuse it instead of generating competing content.
- If the user explicitly requests an end-to-end public workflow and no issue exists, write and create one from public repository context using the canonical template.
- Use heading order `Summary` → `TODO` → `Validation` → optional `Notes`. This order applies to issues created from `.github/ISSUE_TEMPLATE/issue.md`. `bug_report.md` and `fixture_contribution.md` are separate entry points with their own section order — do not force their content into the `issue.md` order.
- Write the issue title in English as `Type: title`. Capitalize only the first letter of `Type`, keep the title after the colon lowercase, and use wording that covers the full delivery scope.
- State what capability will be implemented and why it matters, not a file/function-level implementation plan.
- Keep the title conventional and omit issue, PR, sequence, and private feature numbers.
- Use only public issues, code, tests, and docs as references.

### Pull Request Rules

Before drafting a PR, inspect all of the following:

- the linked public issue and its intended outcome
- the full base-to-head diff
- commits included in the branch
- validation commands and their actual results
- any known follow-up, tradeoff, limitation, or residual risk

Use the linked issue title unchanged for the PR title so both artifacts share the same English `Type: title` wording. Do not append an issue or PR number.

Use headings in exactly this order:

1. `Related Issues`
2. `Summary`
3. `Description`
4. `Review Points`
5. `Docs`
6. `Notes`
7. `Validation`

Write the PR as implementation evidence, not as a file inventory or implementation diary:

- `Related Issues`: use `Closes #<issue-number>` for issues completed by the PR and public links for related but non-closing issues.
- `Summary`: one or two lines stating what the PR does, so a reader knows the change at a glance without reading further. This is not a shortened `Description` — it is the headline.
- `Description`: explain how the issue's intended capability was implemented at the behavior, data-flow, coordination, or design level; state what purpose or invariant the implementation now achieves. Use past or present-perfect implementation language, not the issue's future-tense TODO wording. Add a table, diagram, or chart here when it makes the implementation easier to follow.
- `Review Points`: derive concrete review targets from the actual diff—important decisions, invariants, failure modes, concurrency behavior, compatibility, operational risk, or deliberately chosen tradeoffs. Do not paste a generic checklist.
- `Docs`: check exactly one of the two boxes the template provides — documentation was updated in this PR, or the change does not alter a user-visible contract. Never leave both unchecked. A change to a user-visible contract also adds its `CHANGELOG.md` `Unreleased` entry in the same PR.
- `Notes`: record concise follow-up work, limitations, migration considerations, or tradeoffs. Use `- None.` when there is nothing material so the required heading order remains stable.
- `Validation`: report only checks actually run, using `PASS`, `FAIL`, or `SKIP` with an honest reason and relevant observable evidence. Keep this section last.

Do not describe the PR as a list of files added, functions created, or line-by-line edits unless a public API or artifact name is itself essential to understanding the behavior. A reviewer should understand how the implementation satisfies the issue and what deserves scrutiny without needing private context.

## Resolve the Public Issue

Resolve the issue once before Prepare or Publish:

1. If the user supplies an existing issue, verify that it matches the delivery scope and reuse it.
2. If the current branch or PR already links an issue, reuse that issue unless the user identifies a different target.
3. If no issue exists and the user explicitly requests a complete end-to-end public workflow, draft and create one using the canonical issue template and public repository context.
4. If the user requests issue creation only, create the issue and stop after returning its number and URL.
5. Never create a replacement merely to improve wording; update the existing public issue only when explicitly requested.

## Lifecycle Modes

Select the smallest mode that matches the user's current stage. Do not rerun earlier remote stages unnecessarily.

### Prepare

Use after a public issue exists and before implementation begins.

1. Resolve the issue number and intended base branch.
2. Confirm no equivalent working branch already exists locally.
3. Create the issue-numbered branch from the intended base.
4. Return the branch name and issue link. Do not implement, commit, push, or open a PR in this mode unless separately requested.

### Implementation Handoff

Core feature implementation is not a separate GitHub lifecycle mode. After Prepare, the active implementation agent follows the approved scope, runs its commit-level gates, and creates commits using the public issue number. Return to Publish only after implementation and the feature-completion gate are done.

### Publish

Use after implementation and commit-level verification are complete.

1. Resolve the existing public issue and current branch.
2. Inspect status, base-to-head diff, and commit history; confirm the exact delivery scope.
3. Run repository-appropriate validation. Do not hide or rewrite failures.
4. Stage only intended remaining changes and create any requested final commit using the repository commit convention.
5. Push the issue-numbered branch.
6. Detect whether the branch already has a PR.
   - If no PR exists, inspect the actual changes, draft the canonical PR body, and open the PR.
   - If a PR exists, switch to Update behavior instead of opening a duplicate.
7. Return the issue, branch, commits, PR URL, and validation results.

### Update

Use after review comments, requested changes, or CI fixes modify an existing PR branch.

This mode publishes fixes already made by a review-comment, CI-fix, or implementation workflow. It does not diagnose or implement the fix unless the user separately requests that work.

1. Resolve the existing PR, linked issue, and current branch.
2. Inspect the new diff and identify which review or CI concern each change addresses.
3. Run relevant validation.
4. Stage only the intended follow-up changes, commit with the linked issue number, and push the existing branch.
5. Update the PR body when the summary, description, review points, docs decision, notes, or validation evidence materially changed. Preserve the canonical heading order.
6. Do not open a new PR. Do not reply to or resolve review threads unless explicitly requested.

### Merge

Use only when the user explicitly requests merging a specific PR.

1. Resolve the PR and confirm its base/head branches.
2. Confirm required checks are successful, required reviews are satisfied, and no unresolved requested changes remain.
3. Report any failing, pending, external, or unavailable checks before merging.
4. Use the repository-supported merge method requested by the user or the repository default when unambiguous.
5. Do not delete local or remote branches unless the user explicitly requests deletion.
6. Return the merged PR URL and resulting merge commit or squash commit identifier.

## Required Validation

Always run:

- `git diff --check`

Then select checks from the actual change:

- Markdown, skills, templates: review links, public-facing clarity, stale phase language, private-source leakage, and template heading order. Validate changed `SKILL.md` directories with `quick_validate.py` when available.
- Go code: `go test ./...`; add `go test -race ./...` for concurrency, orchestration, sync-point, adapter, or fixture behavior.
- Testcontainers, MySQL, fixtures, or CI demos: run the documented narrow experiment or fixture command when available; state Docker or Actions limitations explicitly.
- Java/Spring adapter: run the adapter's native test command.
- GitHub Actions: check YAML, action paths, artifact names, permissions, and expected exit codes.
- Release changes: run supported dry-run validation without publishing a release.

Record the same actual results in the PR `Validation` section. Never mark an unexecuted check as passed.

## Naming Conventions

- Issue and PR title: `Type: title`. Capitalize only the first letter of `Type`, keep the English title after the colon lowercase while covering the full delivery scope, use the exact same title for the linked issue and PR, and do not append an issue, PR, sequence, or private feature number.
- Branch: `<type><issue-number>/<short-kebab-summary>`.
- Commit: `<type>(<scope>): <summary> #<issue-number>`, with the issue number as the final token.
- Keep branch names lowercase and short.

Examples:

- Issue and PR: `Feat: add sync-point runtime`
- `feat17/syncpoint-runtime`
- `fix28/blocked-worker-timeout`
- `docs42/diagnostic-reference`
- `feat(syncpoint): add arrive and release runtime #17`

## Write Safety

- Do not use `git add -A` in a mixed worktree without explicit confirmation.
- Do not rewrite, clean, or stage unrelated changes.
- Do not publish an issue or PR body that depends on private context.
- Do not use a generic publish skill's branch or commit naming when it conflicts with this repository's conventions.
- Do not reply to reviews, resolve threads, merge, or delete branches without explicit authorization.

## Final Handoff

Report only artifacts affected by the selected mode:

- issue number and URL
- branch name
- commit SHA and message
- PR number and URL
- validation results
- merge result, when explicitly performed

Separate completed local work from blocked or unauthorized remote work and state the exact blocker.

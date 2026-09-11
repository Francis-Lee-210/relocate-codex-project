---
name: relocate-codex-project
description: Rename or move a local Codex project folder while preserving its project identity, existing tasks, and file access. Use for physical folder path changes and recovery or verification of a move recorded by this skill; skip sidebar-only renaming and ordinary file organization.
---

# Relocate Codex Project

Treat rename, location move, and both together as one operation: **old absolute path → new absolute path**. Preserve the same project and task IDs, unrelated project roots, archive status, permissions, and history.

The helper moves the filesystem and audits Codex through read-only snapshots. Update application settings through the desktop's **Edit project** flow. Never write Codex's SQLite, desktop JSON, or session JSONL directly. The directory move and application update are separate recoverable stages; success requires checking both.

## 1. Establish the paths and plan

Resolve the exact source, destination, and local Codex home from the request and current environment. A display-name change alone uses the app's ordinary rename control and needs no folder relocation.

Use `scripts/relocate.py plan` and save its JSON in a private, durable file outside both paths and Codex home. Use a new filename; retain the original plan through recovery and verification.

```bash
python3 "<skill-dir>/scripts/relocate.py" plan "<old-absolute-path>" "<new-absolute-path>" > "<record-directory>/relocation.plan.json"
```

The helper requires Python 3.10+, `lsof`, and a supported macOS/Linux no-replace rename primitive. It supports same-filesystem directory moves with an old-to-new compatibility symlink. Cross-volume moves, case/Unicode-only renames, nested destinations, linked Git worktrees/submodules, repositories managing external worktrees, and relative symlinks whose destination would change need a separate procedure. A blocked plan is not permission to substitute a recursive copy or a plain overwriting `mv`.

Review `status`, blockers, filesystem identity, affected projects/tasks (including archived tasks), ordered `native_steps`, and compatibility dependencies. Read [storage-contract.md](references/storage-contract.md) when interpreting identity mappings, permissions, or API behavior. Unknown schemas or missing catalog evidence stop the automatic move; a migration number alone is not compatibility proof.

Proceed when the plan is `ready`, the paths match the user's request, and existing authorization covers this move. Ask only for missing choices or authorization; do not repeat approval already given for these exact paths. The plan checksum detects accidental edits, not user consent or a frozen content snapshot.

## 2. Move the folder

Stop affected turns, terminals, development servers, and scheduled work that can use the source. Run the helper from a neutral working directory outside the moved tree. If this task itself keeps the source busy, prepare the exact command and arrange execution from outside it; use a user-run Terminal step only when the available tools cannot do that safely. The helper's `lsof` check is a point-in-time check, so keep that work stopped through the application update.

```bash
python3 "<skill-dir>/scripts/relocate.py" apply --plan "<absolute-plan-path>"
```

`apply` rechecks affected state and directory identity, saves a receipt beside the plan, uses an atomic no-replace rename, and creates the compatibility symlink. Keep the plan, receipt, and link. Its successful result is **filesystem-moved-native-update-pending**; it does not mean the project migration is complete.

For any interruption, unexpected path state, or recovery request, read [recovery.md](references/recovery.md) before further mutations. Do not generate a replacement plan to adopt a directory that has already moved.

## 3. Update the same Codex project

Use the native desktop **Edit project** flow for every project listed in `native_steps`. Add the replacement folder before removing the old folder if the UI requires at least one root. Preserve the complete intended root order and primary root, substituting only old-path roots and descendants. Keep the existing project ID; creating a new project does not preserve the original association.

Read back the saved roots and original task grouping. The new path must appear in both the desktop project and the native project catalog. A label change or successful root update does not establish that existing tasks changed their cwd.

When a verified control channel to the current desktop/host supports task settings, follow [storage-contract.md](references/storage-contract.md) for current APIs and persistence checks. Otherwise retain compatibility for existing tasks and report it. Starting a separate app-server against the real Codex home is not a verified attachment to the desktop. Do not broaden a permission profile or unarchive all tasks to force a direct migration.

## 4. Verify continuity

```bash
python3 "<skill-dir>/scripts/relocate.py" verify --plan "<absolute-plan-path>"
```

Resolve every reported conflict and pending project update before runtime acceptance. `ready-for-runtime-check` means the filesystem/catalog audit passed; the helper does not execute tasks or certify effective permission enforcement.

Verify in the desktop that the same project contains its expected tasks. Continue representative existing tasks through their normal controls, including each distinct affected permission configuration, and observe their effective cwd and intended read access. Where the profile permits writing, exercise an authorized reversible write within the workspace; for read-only tasks, verify the expected write restriction. Check required read-only carveouts and unrelated roots remain intact. Do not create replacement tasks as evidence that the old ones work.

Report the result per affected task:

- **Direct:** persisted settings and a subsequent resume use the new path, with actual intended access verified.
- **Compatibility-dependent:** effective settings still use the old path through the verified link; state whether continuation was exercised.
- **Unverified:** runtime access or continuation was not exercised, including untouched archived tasks.

Preserve archived status and historical JSONL. Keep the compatibility link while any dependency or unverified task remains; the helper deliberately has no link-removal option. Removal needs a separate, fully evidenced cleanup decision.

Deliver the old/new paths, same-project association result, filesystem/catalog status, tested task IDs and access results, remaining compatibility dependencies, and plan/receipt locations. State any outstanding runtime check explicitly; do not claim complete migration from an exit code alone.

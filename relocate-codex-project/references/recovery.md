# Recover an interrupted folder move

Retain the original v2 plan and its adjacent receipt. The plan binds the source directory's device/inode, both parents, affected project/task baseline, and intended root changes. It is an identity record, not a backup of project contents. The helper never restores or rewrites Codex metadata.

## Inspect before mutating

Stop affected work and run from a neutral directory:

```bash
python3 "<skill-dir>/scripts/relocate.py" verify --plan "<absolute-plan-path>"
```

Inspect the actual paths, source identity, conflicts, and pending updates. A receipt is evidence of progress, not proof that its last recorded stage finished: a process can stop between an operating-system change and the next receipt write.

| Observed state | Action |
| --- | --- |
| Old directory present, destination absent, identity unchanged | If the affected catalog still matches, retry `apply` using the original plan. Regenerate a plan only while the original directory is still at the original path. |
| Old path absent, destination has the original directory identity | `recover` completes the compatibility link and durability checks. |
| Old path is the intended link, destination has the original identity | `recover` is idempotent. Continue the pending native project edit and verification. |
| Wrong link, changed directory identity, both paths occupied, missing destination, or conflicting application association | Stop. Preserve both paths and the records; resolve the specific conflict before retrying. |

```bash
python3 "<skill-dir>/scripts/relocate.py" recover --plan "<absolute-plan-path>"
```

Recovery uses the same writer check and no-replace filesystem primitives. Before creating the compatibility link, it rechecks the tree at its current location, including links inside real `.git` directories and external link chains. The audit must match the saved plan by content; entry order does not matter. Added, removed, or changed compatibility dependencies stop recovery even if each current link would otherwise be allowed. Investigate the mismatch against the original plan before retrying. A saved plan or receipt does not bypass this check. Verification also reports this mismatch and current link conflicts, even if the filesystem stage already completed.

The audit compares link resolution before and after the move, including the effect of the old-path alias on `..`. Changed targets, cycles, missing intermediate directories, and ambiguous case or Unicode spellings of either relocation root block progress. Safe internal links, stable external links, and dangling final targets remain supported. Resolve the reported relationship before retrying; preserve an existing compatibility link while investigating.

Recovery may finish the filesystem stage; it never calls native APIs or certifies task execution. Existing tasks with old cwd/permission paths remain dependent on the link.

## When the native edit is incomplete

Use **Edit project** on the original project ID and apply the complete ordered root list in `native_steps`. Rerun `verify` after both desktop and native catalog readback agree. An update to only one store remains pending. Restore unintended root/association changes through native controls; do not hand-edit a store to satisfy the audit.

For task settings, permission profiles, archive behavior, and readback delays, follow [storage-contract.md](storage-contract.md). A failed or unavailable settings call does not justify a broader permission policy. Preserve the link and report unresolved tasks.

## Reverse the move only when needed

Forward recovery is usually simpler. If reversal is required, prepare a concrete reverse plan from the recorded before/after state and the current identity, then use existing authorization or obtain authorization for the changed outcome. This helper has no automatic rollback command.

First stop affected work and restore changed project roots and task settings through native controls while both paths remain accessible. Then verify that the old path is exactly the compatibility symlink and the destination is still the original directory. Remove only that verified symlink, use an exclusive rename to return the directory to its original path, and sync the parent directories. Account for the interruption window between unlink and rename; retain the identity record to complete the reverse rename if interrupted. Never recursively remove a directory or overwrite an occupied old path. Verify original project association, access, and archive state after reversal.

## Missing plan or an externally moved folder

Do not fabricate a v2 plan, reuse a legacy token, or infer identity from matching filenames. Inventory both paths, preserved migration evidence, project IDs, roots, task associations, and permissions read-only. Recover trustworthy source identity and authorization before proposing a specific repair; otherwise report the evidence gap. Native UI edits may be appropriate after that review, but the helper cannot certify an unrecorded move.

## Command results

| Exit | Meaning |
| --- | --- |
| `0` | Inspect JSON status: `ready` authorizes no move by itself; `filesystem-moved-native-update-pending` requires application updates; `ready-for-runtime-check` requires desktop and task acceptance. |
| `2` | Refused, blocked, or incomplete audit. The command did not begin a project-directory move, although it may have created a record/lock. Resolve the cause before retrying. |
| `3` | A filesystem-stage or durability failure may have left a partial move. Use actual identity and paths to select recovery. |
| `4` | Verification has conflicts or pending work. Read its findings before further mutations. |

Legacy `--token`, `repair`, and `--without-link` commands are retired. Their metadata-writing workflow is not compatible with v2.

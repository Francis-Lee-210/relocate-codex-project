# Recover a partial relocation

Use this reference only after exit `3`, interruption, or an earlier manual move.

## Diagnose first

Run `verify` before another mutation. Classify the disk state:

- `initial`: source directory exists and destination is absent;
- `destination-only-unverified`: destination exists and the old path is absent;
- `linked`: old path is a symlink resolving exactly to the destination;
- ambiguous: both real directories exist, a link points elsewhere, or either path is an unexpected node.

Keep ambiguous states unchanged. Never merge, overwrite, delete, or infer identity from matching names.

## Resume with proof

Reuse the original `apply_token` when the directory moved but link creation or parent-directory durability sync did not finish. The token contains the planned directory identity; `apply` re-syncs the proven rename parents, creates and syncs only a missing compatibility link, and only then continues metadata repair.

When the original token is unavailable, the script cannot prove that a destination-only directory is the source. Obtain independent evidence from the user before running metadata-only `repair`, and verify with `--without-link`.

## Metadata partial state

Open the reported backup directory and read `manifest.json`. The backup was prepared before the filesystem rename; it lists every planned field, completed count, error, original file, and backup. Run a fresh read-only `repair` plan to learn which allowlisted fields remain.

Use the fresh plan to continue when the current files still match it. Preserve the backup and surface a conflict if another process changed a file. Do not restore over third-party changes.

## Completion criterion

Recovery is complete only when `verify` reports a complete filesystem state, a complete metadata audit, zero structured metadata changes, SQLite `quick_check = ok`, and the user confirms the project and expected tasks in the restarted Codex UI.

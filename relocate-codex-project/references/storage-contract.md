# Codex storage contract

Use this reference only to interpret or apply the metadata branch. These are private Codex implementation details observed on macOS on 2026-07-14; treat a schema mismatch as a stop signal.

## Active stores

`~/.codex/state_5.sqlite` is the current thread catalog. The tested schema ends at `_sqlx_migrations.version = 40` and exposes:

- `threads.cwd`: current task working directory;
- `threads.sandbox_policy`: JSON containing typed filesystem entries;
- `threads.rollout_path`: session JSONL location, not a project path to rewrite. For every related task, it must resolve to a real contained JSONL whose first-record thread ID matches.

`~/.codex/.codex-global-state.json` holds independent desktop and permission state.

Session JSONL files under `sessions/` and `archived_sessions/` begin with `session_meta`; `session_meta.payload.cwd` is the only historical record this skill updates.

`session_index.jsonl` contains task names rather than project paths. The nested `~/.codex/sqlite/state_5.sqlite`, backups, temporary files, logs, shell snapshots, and memories are audit-only.

## Write allowlist

Update a path only when it equals the literal old path or begins with `old + os.sep` inside one of these structured fields:

- `threads.cwd`;
- `threads.sandbox_policy.file_system.entries[*].path.path` when `path.type == "path"`;
- global-state workspace-root lists, project ordering/pinning, workspace labels, thread root hints, thread writable roots, and heartbeat sandbox writable roots;
- the first JSONL record's `session_meta.payload.cwd`.

Preserve relative suffixes such as `.git`, `.agents`, and `.codex`. Keep unrelated writable roots and pre-existing duplicates. Remove only a collision introduced when an allowlisted old path transforms into a new path that is already present, and report that removal as its own planned action.

## Historical boundary

Treat `world_state`, `turn_context`, `thread_settings`, messages, tool inputs and outputs, and other event records as historical snapshots. They may truthfully mention the old path, and their values remain unchanged. A residual textual match outside the allowlist is not a repair instruction.

Session JSONL editing is surgical: read at most the 8 MiB first-record limit, rewrite only `session_meta.payload.cwd`, and stream-copy every later byte unchanged. An oversized first record blocks the audit. Global state and sandbox policies are parsed with duplicate-key, non-standard-number, non-finite-number, and precision-losing-number rejection, then normalized with ASCII-safe JSON escapes; non-allowlisted JSON values remain semantically unchanged, but whitespace and escape spelling are not a byte-for-byte contract. Valid escaped lone surrogates remain escaped rather than becoming invalid UTF-8.

## Transaction boundary

Use the root `state_5.sqlite` when present; treat a distinct nested copy as legacy. `--state-db` may only confirm the same file identity as the automatically selected root or legacy database; it cannot override that selection. A candidate path occupied by a directory or other non-file node is ambiguous and blocks the operation. Require the active database, global state, sessions, and SQLite sidecars to be real files contained in a real `CODEX_HOME`; refuse symlinks and external explicit database paths.

Treat missing or malformed active stores as an incomplete audit. A malformed unrelated session first record may remain a warning only when it does not contain the literal or JSON-escaped old path. Do not create an apply token until the audit is complete and migration 40 is confirmed.

Read planning and verification data from a stable temporary copy of the database plus WAL so SQLite never creates or updates the real SHM. Normalize that temporary copy to standalone DELETE-journal mode. A present rollback `-journal` blocks planning because its recovery state is ambiguous; bind its absent state into the token so one cannot appear before mutation. Back up SQLite from the same kind of snapshot with its backup API so WAL content is consistent, and complete all backups before the project directory is renamed. Check open handles on the database, WAL, SHM, and any rollback journal fail-closed. Update only the exact token-approved row and field action set with parameterized SQL in one transaction and require `integrity_check = ok`.

Write ordinary files through a same-directory temporary file, `fsync`, and atomic replace. Fsync every backup file, the backup directory tree, and the parent directory entries before renaming the project. Fsync both rename parents and the compatibility-link parent; a failed sync is a recoverable partial state, and token replay must retry those syncs before metadata can commit. Bind metadata-only repair approval to the destination device/inode and filesystem state. Recheck that binding after backup, immediately before metadata writes, during multi-store repair, and again before reporting completion; use the normal apply token's source identity for the same guards after a move. Recheck each metadata fingerprint immediately before replacement, require the applied count to equal the planned count, and finish with a clean full audit. Keep a manifest and SHA-256 for every backup. A running process holding the database or any sidecar makes the transaction unsafe; quit Codex and create a fresh token.

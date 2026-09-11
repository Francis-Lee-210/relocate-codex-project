# Codex integration contract

Read this reference when interpreting the metadata audit, updating project associations, or deciding whether the compatibility link can be removed.

## Ownership and supported scope

The helper performs physical folder renames and moves on the same filesystem. Its `plan` and `verify` commands inspect Codex metadata through stable, read-only snapshots; they never write Codex databases, global state, permission profiles, or session history. Filesystem rollback restores the directory/link state, not application changes made afterward.

Use the desktop's **Edit project** flow to attach the new path to the existing project. Preserve its canonical project ID, the ordered root list, and which root is primary. Replace the affected root without recreating the project or changing unrelated roots. The desktop updates its project storage and cache together. **Make primary** changes the default working directory for new tasks; it is not evidence that historical task settings changed. [Official project guidance](https://learn.chatgpt.com/docs/projects#use-local-projects-for-folders-and-codebases).

Keep the default old-path compatibility link until the acceptance checks below pass. Stop affected running tasks and processes before the physical move, as required by the main workflow.

## Read-only metadata audit

Discover the active state schema and available fields rather than assuming a historical migration number grants write compatibility. Modern state includes:

- `projects` and ordered `project_roots`: canonical identity and attached directories;
- `threads.project_id`, `threads.cwd`, `threads.archived`, and `threads.sandbox_policy`: association, current working directory, archive status, and effective stored permissions;
- `threads.rollout_path`: the history location, not a project path to relocate;
- `.codex-global-state.json`: desktop `local-projects`, legacy/canonical identity mappings, and independent root or permission hints.

Inspect all affected active and archived tasks. Match the exact old path or a descendant separated by a path boundary; preserve unrelated paths. Treat malformed, ambiguous, unsupported, or changing inputs as an incomplete audit. Read a stable temporary copy of the database and WAL so SQLite cannot alter the real SHM or recover the real store. A raw text search is not an effective-settings audit.

A stale desktop cache and an updated database can coexist. Local source inspection found that the desktop's project update calls both `project/update` and its cache updater. Startup loads server projects into identity maps; it does not establish that an external native update will refresh `local-projects`. Verify both representations after desktop editing.

## Native API boundary

A schema describes a capability, not an authorized connection to the user's desktop. Use native methods only through a channel already verified to target the current host, its active server, and the intended project/task IDs. Keep the desktop/cache synchronization requirement even when a native method succeeds.

Do not launch a separate app-server against the real Codex home to perform offline updates. Do not invent a socket path or treat `app-server proxy` as a universal attachment mechanism. Its `--sock` option targets a running control socket; attachment to this desktop's existing server was not verified. When a suitable channel is unavailable, use desktop editing and retain compatibility support.

Generate version-specific protocol schemas in a temporary directory when needed. Isolated experiments may use a temporary Codex home and synthetic tasks; they are not a production migration route. [Official App Server schema and transport guidance](https://learn.chatgpt.com/docs/app-server#message-schema).

| Method | Verified meaning and boundary |
| --- | --- |
| `project/update {projectId, roots}` | Replaces the project's roots and persists them. Does not update task cwd. Supply the complete intended ordered roots; preserve the existing ID. |
| `thread/metadata/update {threadId, projectId}` | Updates task association. Omission leaves it unchanged; an empty string clears it. Verified for stored, unloaded and archived tasks, preserving archive status. |
| `thread/settings/update {threadId, cwd}` | Updates subsequent-turn settings for a loaded task. An unloaded task returns `thread not found`; it must first be resumed through the verified host channel. |
| `thread/resume {threadId, cwd, runtimeWorkspaceRoots}` | Loads an existing task and accepts cwd and absolute runtime-root overrides. Use the existing thread ID, not replacement history. |
| Archived-task resume | Fails until the task is unarchived. Preserve archive status; do not silently unarchive every historical task to force a migration. |
| Active tasks | A running turn is not a migration target. Finish or stop the affected work through the normal task controls before changing its filesystem or settings. |

## Permissions and history

Keep managed permission profiles in their native representation. The observed store uses `type: managed` with typed filesystem entries, network restrictions, and read carveouts for `.git`, `.agents`, and `.codex`. The RPC `SandboxPolicy` legacy union is not a lossless representation. Never convert managed permissions to a broad legacy workspace/full-access policy to make an update succeed.

The built-in `:workspace` profile passed one isolated test: changing only cwd preserved its managed restrictions and moved the write root and read carveouts to the new path. This does not prove preservation of arbitrary custom entries or profiles. Verify their effective roots and restrictions individually; retain the link when exact preservation is unresolved.

A successful settings response can precede persistence. Require stored readback and the next resume to show the intended cwd, runtime roots, project association, and permissions before marking a task direct. A `{}` response, notification, sidebar label, or visible task alone is insufficient.

Historical `session_meta.cwd`, `turn_context`, environment messages, and tool output may correctly contain the old path. Native settings updates append `event_msg.thread_settings_applied`; they do not need to rewrite old records. Leave all JSONL history intact. Inspect effective settings and subsequent resume behavior instead of replacing historical strings.

## Acceptance and compatibility

Record the project association and each affected task separately:

- **Direct:** the saved project uses the intended new roots, and the task's persisted settings plus next resume use the new path with the intended permissions. Verify actual access from that resumed task.
- **Compatibility-dependent:** the old literal path still appears in effective settings but resolves through the verified old-to-new link. This may keep the task working; record that dependency explicitly and retain the link.
- **Unverified or blocked:** association, root resolution, permissions, or task continuation lacks evidence. Do not promote this to direct because the folder exists or the sidebar looks correct.

Remove the link only when every affected dependency, including archived tasks and permission hints, has been resolved and direct access verified. An archived task that has not been resumed remains unverified for continuation. Preserve the link and report that state rather than rewriting history or relaxing permissions.

## Evidence baseline

Observed on macOS on **2026-09-11**, desktop **26.903.71938**, bundled CLI **0.153.4**. These are tested versions, not a promise about later releases.

An isolated temporary Codex home used `thread/start` and `thread/inject_items` to create a durable synthetic task, without `turn/start`, a model request, or copying real credentials. The experiment physically renamed its temporary folder and established:

1. Project root updates survived a fresh app-server process; the task cwd stayed unchanged until separately updated.
2. Loaded-task cwd updates persisted through an appended settings event; immediate readback could still be stale.
3. A later process resumed without overrides and returned the new cwd, runtime roots, and preserved built-in managed profile.
4. Original history retained the old cwd; the current database and resumed task used the new cwd.
5. Archived metadata updates succeeded without unarchiving; archived resume failed explicitly.

No real-project relocation, live desktop cache synchronization, custom-profile migration, or resumed model/tool execution was exercised by that experiment. Those remain per-migration acceptance checks.

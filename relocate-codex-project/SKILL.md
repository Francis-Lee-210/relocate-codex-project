---
name: relocate-codex-project
description: Relocate local Codex project folders transactionally. Use when a user wants to rename or move a project created from an existing folder, repair missing or mis-grouped tasks after its path changed, or verify a completed relocation.
---

# Relocate Codex Project

Treat the filesystem path, Codex thread catalog, desktop state, and session metadata as one transaction. Use `scripts/relocate.py` for every inspection and mutation.

## Guardrails

- Keep `plan` and `verify` read-only; run them inside Codex when useful.
- Run `apply` and mutating `repair` only from Terminal after Codex has quit. The script must report no process holding the active state database.
- Preserve the old path with the default compatibility symlink until end-to-end verification passes.
- Match literal stored paths while auditing metadata; resolve filesystem paths separately.
- Modify only the script's allowlisted structured fields. Preserve historical message and snapshot values; preserve every session JSONL byte after the first `session_meta` record. Global-state JSON may be normalized and reserialized, but non-allowlisted values must remain semantically unchanged.
- Stop on `blocked`, `unsafe-refused`, an incomplete audit, an untested schema, ambiguous directories, an occupied destination, or a stale token. Resolve the reported condition and create a fresh plan.

## 1. Establish the transaction

Identify the literal old absolute path and the intended new absolute path. Run:

```bash
python3 <skill-dir>/scripts/relocate.py plan "<old>" "<new>"
```

Read the filesystem state, every planned metadata change, all warnings, and the `apply_token`. Explain them to the user before requesting approval.

When metadata changes are listed, read `references/storage-contract.md` completely before interpreting them.

Complete this step only when both paths are exact, the state is `ready` or `repair-only`, and every warning has a stated disposition.

## 2. Select the branch

- `initial`: continue with a normal relocation.
- `linked`: use metadata repair if changes remain; otherwise verify.
- `destination-only-unverified`: prefer the original `apply_token` to resume. Without that proof, repair metadata only after the user independently confirms the destination identity, then verify with `--without-link`.
- `unsafe-refused` or any other state: keep the filesystem unchanged and resolve the blocker.

Complete this step only when exactly one branch applies.

## 3. Execute a normal relocation

Obtain explicit approval for the exact old path, new path, compatibility-link policy, and listed metadata changes. Then instruct the user to:

1. Quit every Codex window.
2. Open Terminal.
3. Rerun `plan` to obtain a fresh token after shutdown.
4. Run:

```bash
python3 <skill-dir>/scripts/relocate.py apply "<old>" "<new>" --token "<fresh-apply-token>"
```

Use `--without-link` in both commands only when the user explicitly accepts losing old-path compatibility.

The script must finish and validate the metadata backup before it renames the project directory. A backup-preparation failure is a stop condition and must leave the project at the old path.

Complete this step only when `apply` reports `completed` or `already-complete`. For `partial-recoverable`, read `references/recovery.md` completely and resume from the diagnosed disk state.

## 4. Repair an earlier relocation

Run a read-only repair plan:

```bash
python3 <skill-dir>/scripts/relocate.py repair "<old>" "<new>"
```

Before interpreting or applying metadata changes, read `references/storage-contract.md` completely. Show the user every file, thread ID, JSON pointer, old value, and new value. Record the top-level `repair_apply_token`, which binds both the metadata snapshot and destination directory identity. After approval, have the user quit Codex, rerun the repair plan from Terminal, and apply its fresh token:

```bash
python3 <skill-dir>/scripts/relocate.py repair "<old>" "<new>" --token "<fresh-repair-apply-token>"
```

Complete this step only when repair reports `completed` or `already-complete`, or a fresh repair plan reports `clean`, and a backup location is recorded for every actual mutation.

## 5. Verify end to end

After Codex restarts, run:

```bash
python3 <skill-dir>/scripts/relocate.py verify "<old>" "<new>" --token "<apply-token>"
```

Use `--without-link` only for the explicitly linkless branch. Confirm all of the following:

- filesystem state is complete;
- the token verifies the destination identity when available;
- structured metadata has zero remaining changes;
- SQLite `quick_check` is `ok`;
- Codex shows the new project and all expected tasks.

Disk verification cannot prove that the UI reloaded. Complete the relocation only after the user confirms the final Codex view.

## Status handling

- Exit `0`: the command produced a normal result; always inspect JSON `status`. `ready` and `repair-ready` require a later approved mutation, while `blocked` must stop even though the read-only command exited `0`. Only `completed`, `clean`, `already-complete`, or `verified` represent their corresponding completed operation.
- Exit `2`: unsafe or stale; no project or Codex metadata mutation should have started.
- Exit `3`: recoverable partial state; read `references/recovery.md` before retrying.
- Exit `4`: verification failed; inspect the reported filesystem and metadata sections.

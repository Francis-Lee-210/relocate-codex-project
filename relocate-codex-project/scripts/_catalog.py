"""Read-only Codex catalog snapshots. No database or session write operations."""
from __future__ import annotations

import copy
from contextlib import closing, contextmanager
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import Any, Iterable

from _filesystem import Refusal, path_kind, is_descendant, rewrite_structured_path


def path_map(value: str, old: str, new: str) -> str:
    return rewrite_structured_path(value, old, new) or value


def policy_paths(policy: dict) -> list[str]:
    """Read path fields only, preserving the rest of the effective policy."""
    if not isinstance(policy, dict):
        raise Refusal("A stored sandbox policy is not an object.")
    paths = []
    filesystem = policy.get("file_system", {})
    if not isinstance(filesystem, dict) or not isinstance(filesystem.get("entries", []), list):
        raise Refusal("A stored filesystem policy has an unsupported structure.")
    for entry in filesystem.get("entries", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), dict):
            raise Refusal("A stored permission entry is not a typed path object.")
        path = entry["path"]
        if path.get("type") == "path":
            paths.append(path["path"])
        elif path.get("type") != "special":
            raise Refusal("A stored permission entry has an unsupported path type.")
    for key in ("writable_roots", "writableRoots"):
        if key in policy:
            if not isinstance(policy[key], list):
                raise Refusal("Stored writable roots must be an array.")
            paths.extend(policy[key])
    if not all(isinstance(p, str) for p in paths):
        raise Refusal("A stored permission path is not text.")
    return paths


def mapped_policy(policy: dict, old: str, new: str) -> dict:
    """Comparison only. Never serialize this value back into a Codex store."""
    result = copy.deepcopy(policy)
    policy_paths(result)
    filesystem = result.get("file_system", {})
    if isinstance(filesystem, dict):
        for entry in filesystem.get("entries", []):
            path = entry.get("path", {})
            if path.get("type") == "path":
                path["path"] = path_map(path["path"], old, new)
    for key in ("writable_roots", "writableRoots"):
        if key in result:
            result[key] = [path_map(p, old, new) for p in result[key]]
    return result


def read_catalog(home: Path, old: str, new: str, baseline: dict | None = None) -> dict:
    """Inventory affected objects, including archived tasks, without opening live SQLite."""
    ensure_real_directory(home, "Codex home")
    database, warnings = choose_state_db(home, None)
    if database is None:
        raise Refusal("No Codex state database is available; the project/task audit is incomplete.")
    global_path = home / ".codex-global-state.json"
    ensure_trusted_file(global_path, home, "Codex desktop state")
    before = file_fingerprint(global_path)
    desktop = strict_json_loads(global_path.read_bytes(), "Codex desktop state")
    if before != file_fingerprint(global_path):
        raise Refusal("Desktop state changed during the read; repeat the read-only plan.")
    if not isinstance(desktop, dict):
        raise Refusal("Desktop state must be a JSON object.")
    with sqlite_read_snapshot(database) as temporary:
        with closing(sqlite3.connect(temporary.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            required = {
                "threads": {"id", "cwd", "rollout_path", "sandbox_policy", "project_id", "archived"},
                "projects": {"id", "name"}, "project_roots": {"project_id", "position", "path"},
            }
            for table, columns in required.items():
                found = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
                if not columns <= found:
                    raise Refusal(f"Unsupported read schema: {table} lacks {sorted(columns - found)}. No files were moved.")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise Refusal("The temporary catalog snapshot failed SQLite quick_check.")
            migrations = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            version = connection.execute("SELECT MAX(version) FROM _sqlx_migrations WHERE success=1").fetchone()[0] if "_sqlx_migrations" in migrations else None
            native = {row["id"]: {"name": row["name"], "roots": []} for row in connection.execute("SELECT id,name FROM projects")}
            for row in connection.execute("SELECT project_id,path FROM project_roots ORDER BY project_id,position"):
                if row["project_id"] not in native or not isinstance(row["path"], str):
                    raise Refusal("Invalid project-root catalog entry.")
                native[row["project_id"]]["roots"].append(row["path"])
            rows = [dict(row) for row in connection.execute("SELECT id,cwd,rollout_path,sandbox_policy,project_id,archived FROM threads ORDER BY id")]
    if before != file_fingerprint(global_path):
        raise Refusal("Desktop state changed during the database snapshot; repeat the audit.")
    projects = desktop.get("local-projects")
    if not isinstance(projects, dict):
        raise Refusal("The desktop has no supported local-projects catalog. Use the app to finish its project migration first.")
    mapping = desktop.get("app-server-project-id-by-legacy-project-id-by-host", {}).get(f"local:{home}", {})
    assignments = desktop.get("thread-project-assignments", {})
    saved_projects = {p["id"] for p in (baseline or {}).get("projects", [])}
    saved_threads = {t["id"] for t in (baseline or {}).get("threads", [])}
    affected_projects = []
    blockers = []
    for identifier, project in sorted(projects.items()):
        roots = project.get("rootPaths")
        if not isinstance(roots, list) or not all(isinstance(p, str) for p in roots):
            raise Refusal("A desktop project has an unsupported root list.")
        server_id = mapping.get(identifier)
        server = native.get(server_id)
        if identifier not in saved_projects and not any(path_map(p, old, new) != p for p in roots + (server or {}).get("roots", [])):
            continue
        if server is None:
            blockers.append(f"Project {identifier} has no verified native identity mapping.")
        affected_projects.append({"id": identifier, "native_id": server_id, "name": project.get("name"),
                                  "roots": roots, "native_roots": None if server is None else server["roots"]})
    mapped_ids = {p["native_id"] for p in affected_projects if p["native_id"] is not None}
    for identifier, project in native.items():
        if identifier not in mapped_ids and any(path_map(p, old, new) != p for p in project["roots"]):
            blockers.append(f"Native project {identifier} has no matching desktop project in this audit.")
    for identifier in saved_projects - {p["id"] for p in affected_projects}:
        blockers.append(f"Expected desktop project {identifier} is missing.")
    affected_ids = {p["id"] for p in affected_projects}
    threads = []
    for row in rows:
        if not isinstance(row["cwd"], str) or not isinstance(row["id"], str):
            raise Refusal("Invalid task identity or working directory in the catalog.")
        policy = strict_json_loads(row.pop("sandbox_policy"), f"Task {row['id']} permissions")
        assignment = assignments.get(row["id"])
        assigned = assignment.get("projectId") if isinstance(assignment, dict) and assignment.get("projectKind") == "local" else None
        paths = [row["cwd"], *policy_paths(policy)]
        if row["id"] not in saved_threads and row["project_id"] not in mapped_ids and assigned not in affected_ids and not any(path_map(p, old, new) != p for p in paths):
            continue
        rollout = Path(row["rollout_path"])
        ensure_trusted_file(rollout, home, f"Task {row['id']} history")
        with rollout.open("rb") as handle:
            first = handle.readline(8 * 1024 * 1024 + 1)
        if len(first) > 8 * 1024 * 1024:
            raise Refusal("A task history header exceeds the audit limit.")
        meta = strict_json_loads(first, "Task history header")
        if (not isinstance(meta, dict) or meta.get("type") != "session_meta" or
                not isinstance(meta.get("payload"), dict) or meta["payload"].get("id") != row["id"]):
            raise Refusal(f"Task {row['id']} does not match its history file.")
        if row["project_id"] is not None and assigned is not None and mapping.get(assigned) != row["project_id"]:
            blockers.append(f"Task {row['id']} has conflicting native and desktop project assignments.")
        row.update({"policy": policy, "desktop_project_id": assigned, "archived": bool(row["archived"])})
        threads.append(row)
    for identifier in saved_threads - {t["id"] for t in threads}:
        blockers.append(f"Expected task {identifier} is missing.")
    dependencies = []
    for field in ("thread-writable-roots", "thread-workspace-root-hints"):
        container = desktop.get(field, {})
        if not isinstance(container, dict):
            raise Refusal(f"Unsupported desktop {field} format.")
        for identifier, value in sorted(container.items()):
            paths = value if isinstance(value, list) else [value]
            if not all(isinstance(p, str) for p in paths):
                raise Refusal(f"Unsupported desktop paths in {field}.")
            if identifier in {t["id"] for t in threads} or any(path_map(p, old, new) != p for p in paths):
                dependencies.append({"field": field, "owner": identifier, "paths": paths})
    heartbeat = desktop.get("electron-persisted-atom-state", {}).get("heartbeat-thread-permissions-by-id", {})
    for identifier, permission in sorted(heartbeat.items()):
        roots = permission.get("sandboxPolicy", {}).get("writableRoots", [])
        if any(path_map(p, old, new) != p for p in roots):
            dependencies.append({"field": "heartbeat-writable-roots", "owner": identifier, "paths": roots})
    return {"schema_migration": version, "projects": affected_projects, "threads": threads,
            "dependencies": dependencies, "blockers": blockers, "warnings": warnings}


def logical_state(catalog: dict) -> dict:
    return {key: catalog[key] for key in ("projects", "threads", "dependencies", "blockers")}

def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def strict_json_loads(raw: str | bytes, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise Refusal(f"{label} contains a duplicate JSON key: {key!r}.")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise Refusal(f"{label} contains a non-standard JSON number: {value}.")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise Refusal(f"{label} contains a non-finite JSON number: {value}.")
        if Decimal(value) != Decimal(str(parsed)):
            raise Refusal(
                f"{label} contains a JSON number that cannot be serialized without precision loss: {value}."
            )
        return parsed

    return json.loads(
        raw,
        object_pairs_hook=object_pairs,
        parse_constant=reject_constant,
        parse_float=finite_float,
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise Refusal(
                "A metadata path is not a real regular file.",
                details={"path": str(path)},
            )
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise Refusal(
                "A metadata file changed while it was read.",
                details={"path": str(path)},
            )
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def file_fingerprint(path: Path) -> dict[str, Any]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise Refusal("A metadata path is not a real regular file.", details={"path": str(path)})
    digest = file_sha256(path)
    after = path.lstat()
    if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise Refusal("A metadata file changed while it was fingerprinted.", details={"path": str(path)})
    return {
        "path": str(path),
        "exists": True,
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "size": int(info.st_size),
        "mtime_ns": int(info.st_mtime_ns),
        "sha256": digest,
    }


def path_state_fingerprint(path: Path) -> dict[str, Any]:
    if path_kind(path) == "absent":
        return {"path": str(path), "exists": False}
    return file_fingerprint(path)


@contextmanager
def sqlite_read_snapshot(path: Path) -> Iterable[Path]:
    """Read SQLite through a stable temporary copy so plan/verify never touch WAL/SHM."""
    wal = Path(str(path) + "-wal")
    rollback_journal = Path(str(path) + "-journal")
    source_paths = (path, wal, rollback_journal)
    before = [path_state_fingerprint(candidate) for candidate in source_paths]
    if before[2]["exists"]:
        raise Refusal(
            "A rollback journal exists beside the state database; safe recovery state is ambiguous.",
            details={"path": str(rollback_journal)},
        )
    with tempfile.TemporaryDirectory(prefix="codex-relocate-sqlite-") as temporary:
        snapshot = Path(temporary) / path.name
        shutil.copy2(path, snapshot, follow_symlinks=False)
        if before[1]["exists"]:
            shutil.copy2(wal, Path(str(snapshot) + "-wal"), follow_symlinks=False)
        after = [path_state_fingerprint(candidate) for candidate in source_paths]
        if canonical_json(before) != canonical_json(after):
            raise Refusal("The state database changed while a read-only snapshot was copied.")
        try:
            normalizer = sqlite3.connect(str(snapshot))
            try:
                normalizer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                journal = normalizer.execute("PRAGMA journal_mode=DELETE").fetchone()
                if journal is None or str(journal[0]).lower() != "delete":
                    raise Refusal(
                        "The temporary SQLite snapshot could not enter standalone DELETE mode."
                    )
            finally:
                normalizer.close()
        except sqlite3.DatabaseError as exc:
            raise Refusal(f"Cannot normalize the temporary SQLite snapshot: {exc}") from exc
        yield snapshot


def ensure_real_directory(path: Path, label: str) -> None:
    if path_kind(path) != "directory" or Path(os.path.realpath(path)) != path:
        raise Refusal(f"{label} must be a real directory without symlink traversal.")


def ensure_trusted_file(
    path: Path,
    codex_home: Path,
    label: str,
    *,
    allow_absent: bool = False,
) -> bool:
    ensure_real_directory(codex_home, "CODEX_HOME")
    try:
        path.relative_to(codex_home)
    except ValueError as exc:
        raise Refusal(f"{label} is outside CODEX_HOME.", details={"path": str(path)}) from exc
    kind = path_kind(path)
    if kind == "absent" and allow_absent:
        if Path(os.path.realpath(path.parent)) != path.parent:
            raise Refusal(f"{label} would traverse a symlinked directory.")
        return False
    if kind == "symlink":
        raise Refusal(f"{label} must not be a symlink.", details={"path": str(path)})
    if kind != "file":
        raise Refusal(f"{label} is not a real regular file.", details={"path": str(path)})
    if Path(os.path.realpath(path)) != path:
        raise Refusal(f"{label} traverses a symlinked directory.", details={"path": str(path)})
    return True


def choose_state_db(codex_home: Path, explicit: Path | None) -> tuple[Path | None, list[str]]:
    warnings: list[str] = []
    root = codex_home / "state_5.sqlite"
    legacy = codex_home / "sqlite" / "state_5.sqlite"
    root_kind = path_kind(root)
    legacy_kind = path_kind(legacy)
    if root_kind == "symlink" or legacy_kind == "symlink":
        raise Refusal("A candidate state database must not be a symlink.")
    if root_kind not in {"absent", "file"}:
        raise Refusal(
            "The canonical root state database path is occupied by a non-file node.",
            details={"path": str(root), "kind": root_kind},
        )
    if legacy_kind not in {"absent", "file"}:
        raise Refusal(
            "The legacy state database path is occupied by a non-file node.",
            details={"path": str(legacy), "kind": legacy_kind},
        )
    selected: Path | None = None
    if root_kind == "file":
        ensure_trusted_file(root, codex_home, "The root state database")
        if legacy_kind == "file" and not os.path.samefile(root, legacy):
            ensure_trusted_file(legacy, codex_home, "The legacy state database")
            warnings.append("Ignored the legacy nested state_5.sqlite; the root database is active.")
        selected = root
    elif legacy_kind == "file":
        ensure_trusted_file(legacy, codex_home, "The legacy state database")
        warnings.append("Using the legacy nested state_5.sqlite because the root database is absent.")
        selected = legacy
    else:
        warnings.append("No state_5.sqlite was found; database metadata cannot be audited.")

    if explicit is not None:
        ensure_trusted_file(explicit, codex_home, "The explicit state database")
        if selected is None or not os.path.samefile(explicit, selected):
            raise Refusal(
                "The explicit state database is not the canonical active state_5.sqlite.",
                details={
                    "explicit": str(explicit),
                    "canonical": str(selected) if selected is not None else None,
                },
            )
    return selected, warnings

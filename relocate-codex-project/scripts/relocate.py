#!/usr/bin/env python3
"""Move project folders and audit Codex with read-only snapshots.

Codex owns application metadata. This helper never writes its stores.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import sqlite3
import subprocess
import sys
import tempfile

from _filesystem import (
    Refusal, PartialFailure, normalize_path, path_kind, same_object_identity,
    is_descendant, inspect_filesystem, validate_static_paths, apply_filesystem,
    fsync_directory, tree_risks,
)
from _catalog import (
    canonical_json, strict_json_loads, read_catalog, logical_state,
    path_map, mapped_policy, policy_paths,
)

FORMAT = "codex-folder-relocation/v2"


def digest(value: dict) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def absolute(raw: str) -> Path:
    if not os.path.isabs(os.path.expanduser(raw)):
        raise Refusal("Use explicit absolute paths for source, destination, and records.")
    return normalize_path(raw)


def make_plan(old: Path, new: Path, home: Path) -> dict:
    warnings = validate_static_paths(old, new, home)
    filesystem = inspect_filesystem(old, new, compatibility_link=True)
    if filesystem["state"] != "initial":
        raise Refusal("A new plan requires the source directory and an absent destination. For an interrupted move, use its original plan with recover/verify.")
    risks = tree_risks(old, new)
    catalog = read_catalog(home, str(old), str(new))
    blockers = [f"{r['kind']}: {r['path']}" for r in risks if r["kind"] != "absolute-link-needs-compatibility"]
    blockers += catalog["blockers"]
    if not catalog["projects"]:
        blockers.append("No saved Codex project refers to this directory. Resolve project identity in the app before moving it.")
    for project in catalog["projects"]:
        if project["roots"] != project["native_roots"]:
            blockers.append(f"Desktop/native roots disagree for project {project['id']}.")
        roots = [path_map(p, str(old), str(new)) for p in project["roots"]]
        if len(set(roots)) != len(set(project["roots"])):
            blockers.append(f"The move would collapse roots in project {project['id']}.")
    plan = {
        "format": FORMAT, "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "status": "blocked" if blockers else "ready", "old": str(old), "new": str(new),
        "codex_home": str(home), "compatibility_link": True, "filesystem": filesystem,
        "tree_risks": risks, "catalog": catalog, "blockers": blockers,
        "warnings": warnings + catalog["warnings"],
        "native_steps": [{"project_id": p["id"], "native_id": p["native_id"],
                          "before_roots": p["roots"],
                          "after_roots": [path_map(r, str(old), str(new)) for r in p["roots"]]}
                         for p in catalog["projects"]],
        "next": "Review the plan and existing authorization. Stop affected work; run apply from outside both project paths. Then edit the SAME project in the desktop and run verify.",
    }
    plan["plan_id"] = digest(plan)
    return plan


def outside_record(path: Path, plan: dict) -> None:
    if Path(os.path.realpath(path.parent)) != path.parent or not path.parent.is_dir():
        raise Refusal("The record parent must be an existing real directory.")
    for raw in (plan["old"], plan["new"], plan["codex_home"]):
        root = Path(raw)
        if path == root or is_descendant(path, root):
            raise Refusal("Keep the plan and receipt outside the moved directories and Codex home.")


def load_plan(path: Path) -> dict:
    if path_kind(path) != "file" or Path(os.path.realpath(path)) != path:
        raise Refusal("The plan must be a real JSON file without symlink traversal.")
    if path.stat().st_size > 32 * 1024 * 1024:
        raise Refusal("The plan exceeds the supported size.")
    plan = strict_json_loads(path.read_bytes(), "Relocation plan")
    if not isinstance(plan, dict) or plan.get("format") != FORMAT:
        raise Refusal("This is not a v2 relocation plan. Legacy apply tokens cannot be reused.")
    saved = plan.pop("plan_id", None)
    if not isinstance(saved, str) or saved != digest(plan):
        raise Refusal("The plan changed after it was generated; produce a fresh plan.")
    plan["plan_id"] = saved
    if not all(isinstance(plan.get(key), dict) for key in ("filesystem", "catalog")):
        raise Refusal("The plan has an invalid filesystem or catalog structure.")
    for key in ("old", "new", "codex_home"):
        if str(absolute(plan[key])) != plan[key]:
            raise Refusal("The plan contains non-normalized paths.")
    outside_record(path, plan)
    if plan.get("status") != "ready" or plan.get("blockers") or plan.get("compatibility_link") is not True:
        raise Refusal("The plan is blocked or does not preserve the compatibility link.")
    return plan


def parse_writers(output: str) -> list[dict]:
    results, process, descriptor = [], {}, {}

    def flush() -> None:
        if process.get("pid") != os.getpid() and (descriptor.get("fd") == "cwd" or descriptor.get("access") in {"w", "u"}):
            results.append({**process, **descriptor})

    for line in output.splitlines():
        if not line:
            continue
        field, value = line[0], line[1:]
        if field in {"p", "f"}:
            flush()
            descriptor = {}
        if field == "p":
            process = {"pid": int(value)}
        elif field == "c":
            process["command"] = value
        elif field == "f":
            descriptor["fd"] = value
        elif field == "a":
            descriptor["access"] = value
        elif field == "n":
            descriptor["path"] = value
    flush()
    return results


def require_quiet_tree(root: Path) -> None:
    cwd = Path.cwd().resolve()
    if cwd == root or is_descendant(cwd, root):
        raise Refusal("Run apply/recover from a neutral directory outside the project being moved.")
    try:
        result = subprocess.run(["lsof", "-Fpcfan", "+D", str(root)], capture_output=True,
                                text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal(f"Cannot check project writers with lsof: {error}") from error
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise Refusal("The project writer scan was incomplete.", details={"stderr": result.stderr[-2000:]})
    writers = parse_writers(result.stdout)
    if writers:
        raise Refusal("Processes still use this project as cwd or hold writable files. Stop them and retry.", details={"writers": writers})


@contextmanager
def record_lock(path: Path, plan: dict):
    lock = path.with_name(path.name + ".lock")
    outside_record(lock, plan)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refusal("The record lock is not a regular file.")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Refusal("Another operation holds this relocation record.") from error
        yield
    finally:
        os.close(fd)


def write_receipt(path: Path, plan: dict, state: str, **details) -> None:
    outside_record(path, plan)
    if path_kind(path) != "absent":
        if path_kind(path) != "file":
            raise Refusal("The receipt path is occupied by an unexpected node.")
        current = strict_json_loads(path.read_bytes(), "Relocation receipt")
        if current.get("plan_id") != plan["plan_id"]:
            raise Refusal("The receipt belongs to a different plan.")
    data = {"format": FORMAT, "plan_id": plan["plan_id"], "old": plan["old"], "new": plan["new"],
            "state": state, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), **details}
    fd, temp = tempfile.mkstemp(prefix=".relocation-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical_json(data) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def sync_plan(path: Path, plan: dict) -> None:
    """Persist the original recovery evidence before changing a directory entry."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
            raise Refusal("The recovery plan is no longer a supported regular file.")
        if strict_json_loads(handle.read(), "Recovery plan") != plan:
            raise Refusal("The recovery plan changed while preparing the move.")
        os.fsync(handle.fileno())
        current = path.lstat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise Refusal("The recovery plan was replaced while preparing the move.")
    fsync_directory(path.parent)


def compare_catalog(plan: dict, current: dict) -> dict:
    old, new = plan["old"], plan["new"]
    result = {"projects": [], "threads": [], "compatibility_dependencies": [],
              "conflicts": list(current["blockers"]), "pending": []}
    projects = {p["id"]: p for p in current["projects"]}
    threads = {t["id"]: t for t in current["threads"]}
    for original in plan["catalog"]["projects"]:
        actual = projects.get(original["id"])
        desired = [path_map(p, old, new) for p in original["roots"]]
        if actual is None or actual["native_id"] != original["native_id"]:
            result["conflicts"].append(f"Project {original['id']} is missing or has a different identity.")
            continue
        states = []
        for key in ("roots", "native_roots"):
            if actual[key] == desired:
                states.append("direct")
            elif actual[key] == original[key]:
                states.append("pending")
            else:
                states.append("conflict")
        state = "conflict" if "conflict" in states else "pending" if "pending" in states else "direct"
        result["projects"].append({"id": original["id"], "state": state, "expected_roots": desired})
        if state != "direct":
            result["conflicts" if state == "conflict" else "pending"].append(f"Project {original['id']}: {state}; update the same project through Edit project.")
    for original in plan["catalog"]["threads"]:
        actual = threads.get(original["id"])
        if actual is None:
            result["conflicts"].append(f"Task {original['id']} is missing.")
            continue
        expected_native = next((p["native_id"] for p in plan["catalog"]["projects"] if p["id"] == original["desktop_project_id"]), original["project_id"])
        if (actual["desktop_project_id"] != original["desktop_project_id"] or
                actual["project_id"] not in {original["project_id"], expected_native} or
                actual["archived"] != original["archived"] or actual["rollout_path"] != original["rollout_path"]):
            result["conflicts"].append(f"Task {original['id']} association, archive status, or history location changed.")
        desired_cwd = path_map(original["cwd"], old, new)
        desired_policy = mapped_policy(original["policy"], old, new)
        if actual["cwd"] not in {original["cwd"], desired_cwd} or mapped_policy(actual["policy"], old, new) != desired_policy:
            result["conflicts"].append(f"Task {original['id']} cwd or permissions diverged from the plan.")
            continue
        alias_paths = [p for p in [actual["cwd"], *policy_paths(actual["policy"])] if path_map(p, old, new) != p]
        resolved = str(Path(actual["cwd"]).resolve())
        expected_resolved = str(Path(desired_cwd).resolve())
        if resolved != expected_resolved or not Path(desired_cwd).is_dir():
            result["pending"].append(f"Task {original['id']} cwd does not resolve to an existing intended directory.")
        state = "compatibility-dependent" if alias_paths else "direct-settings"
        result["threads"].append({"id": original["id"], "state": state, "archived": actual["archived"],
                                  "cwd": actual["cwd"], "expected_cwd": desired_cwd,
                                  "resolved_cwd": resolved, "runtime_check": "not-performed-by-helper"})
        result["compatibility_dependencies"].extend({"owner": original["id"], "path": p} for p in alias_paths)
    for identifier in threads.keys() - {t["id"] for t in plan["catalog"]["threads"]}:
        result["conflicts"].append(f"New affected task {identifier} appeared after planning.")
    for dependency in current["dependencies"]:
        result["compatibility_dependencies"].extend({"owner": dependency["owner"], "field": dependency["field"], "path": p}
                                                   for p in dependency["paths"] if path_map(p, old, new) != p)
    for risk in plan["tree_risks"]:
        if risk["kind"] == "absolute-link-needs-compatibility":
            result["compatibility_dependencies"].append({"owner": "project symlink", "path": risk["path"], "target": risk["target"]})
    return result


def same_tree_risks(current: list[dict], planned: list[dict]) -> bool:
    """Compare audit contents without os.walk's directory/file entry ordering.

    Before the compatibility alias exists, an unchanged directory symlink can
    be dangling and consequently appear among files instead of directories.
    """
    return sorted(map(canonical_json, current)) == sorted(map(canonical_json, planned))


def inspect_plan(plan: dict) -> dict:
    old, new, home = (Path(plan[k]) for k in ("old", "new", "codex_home"))
    validate_static_paths(old, new, home)
    state = inspect_filesystem(old, new, compatibility_link=True)
    reference = new if state["state"] != "initial" else old
    if not same_object_identity(reference, plan["filesystem"]["source_identity"]):
        raise Refusal("The directory identity no longer matches the original plan.")
    risks = tree_risks(old, new, tree_root=reference)
    catalog = read_catalog(home, str(old), str(new), plan["catalog"])
    audit = compare_catalog({**plan, "tree_risks": risks}, catalog)
    audit["conflicts"].extend(f"{r['kind']}: {r['path']}" for r in risks
                              if r["kind"] != "absolute-link-needs-compatibility")
    if not same_tree_risks(risks, plan["tree_risks"]):
        audit["conflicts"].append("The project's link or Git relationships changed after planning.")
    complete = state["state"] == "linked" and not audit["conflicts"] and not audit["pending"]
    return {"status": "ready-for-runtime-check" if complete else "verification-pending",
            "filesystem": state, "audit": audit, "tree_risks": risks,
            "acceptance": "Verify in the desktop and resume representative existing tasks. This helper does not certify UI reload, actual task execution, or permission enforcement.",
            "keep_compatibility_link": True}


def apply_plan(plan_path: Path, *, recovering: bool = False) -> dict:
    plan = load_plan(plan_path)
    old, new, home = (Path(plan[k]) for k in ("old", "new", "codex_home"))
    receipt = plan_path.with_name(plan_path.stem + ".receipt.json")
    with record_lock(plan_path, plan):
        validate_static_paths(old, new, home)
        state = inspect_filesystem(old, new, compatibility_link=True)
        if state["state"] != "initial" and not recovering:
            raise Refusal("The filesystem is already moved. Use recover or verify with this plan.")
        root = old if state["state"] == "initial" else new
        if not same_object_identity(root, plan["filesystem"]["source_identity"]):
            raise Refusal("The directory identity no longer matches the original plan.")
        require_quiet_tree(root)
        risks = tree_risks(old, new, tree_root=root)
        if any(r["kind"] != "absolute-link-needs-compatibility" for r in risks):
            raise Refusal("The move changes link or Git relationships, or they cannot be resolved.",
                          details={"tree_risks": risks})
        if not same_tree_risks(risks, plan["tree_risks"]):
            raise Refusal("The project's link or Git relationships changed after planning.")
        # Snapshot after the potentially slow process/tree scans, as close as
        # possible to rename. The caller must keep affected work stopped.
        current = read_catalog(home, str(old), str(new), plan["catalog"])
        if state["state"] == "initial":
            if logical_state(current) != logical_state(plan["catalog"]):
                raise Refusal("Affected Codex objects changed after planning; generate a fresh plan before moving.")
        else:
            conflicts = compare_catalog(plan, current)["conflicts"]
            if conflicts:
                raise Refusal("Application state conflicts with the recovery plan.", details={"conflicts": conflicts})
            if not same_object_identity(new, plan["filesystem"]["source_identity"]):
                raise Refusal("The destination is a different directory; recovery cannot adopt it.")
        sync_plan(plan_path, plan)
        write_receipt(receipt, plan, "prepared")
        token = {key: plan["filesystem"][key] for key in ("source_identity", "source_parent_identity", "destination_parent_identity")}
        try:
            moved = apply_filesystem(old, new, token, compatibility_link=True)
            write_receipt(receipt, plan, "filesystem-moved-native-update-pending", filesystem=moved)
        except (Exception, KeyboardInterrupt) as error:
            raise PartialFailure("The filesystem operation stopped. Keep the plan and inspect it with verify before recover.",
                                 details={"receipt": str(receipt), "cause": str(error)}) from error
    return {"status": "filesystem-moved-native-update-pending", "receipt": str(receipt),
            "filesystem": moved, "native_steps": plan["native_steps"],
            "next": "Use Edit project for each SAME project ID and intended ordered roots; then verify. Codex metadata has not been modified by this helper."}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan", help="Read-only preflight; redirect JSON to a durable file outside both paths")
    plan.add_argument("old")
    plan.add_argument("new")
    plan.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    for name in ("apply", "recover", "verify"):
        command = sub.add_parser(name)
        command.add_argument("--plan", required=True, help="Absolute path to the original v2 plan JSON")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    code = 0
    try:
        if args.command == "plan":
            output = make_plan(absolute(args.old), absolute(args.new), absolute(args.codex_home))
            code = 2 if output["status"] == "blocked" else 0
        elif args.command == "verify":
            output = inspect_plan(load_plan(absolute(args.plan)))
            code = 0 if output["status"] == "ready-for-runtime-check" else 4
        else:
            output = apply_plan(absolute(args.plan), recovering=args.command == "recover")
    except Refusal as error:
        output, code = {"status": "refused", "error": str(error), "details": error.details}, 2
    except (PartialFailure, KeyboardInterrupt) as error:
        output, code = {"status": "partial-recoverable", "error": str(error), "details": getattr(error, "details", {})}, 3
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.DatabaseError) as error:
        output, code = {"status": "audit-incomplete", "error": str(error)}, 2
    print(json.dumps(output, ensure_ascii=True, indent=2, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Plan, apply, repair, and verify a local Codex project relocation.

The script is intentionally conservative.  Read-only commands may run while Codex is
open.  Mutating commands require an exact plan token and refuse to touch an active
Codex state database.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import ctypes
import datetime as dt
from decimal import Decimal
import errno
import hashlib
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unicodedata
from typing import Any, Iterable


EXIT_OK = 0
EXIT_UNSAFE = 2
EXIT_PARTIAL = 3
EXIT_VERIFY_FAILED = 4

TOKEN_VERSION = 1
SUPPORTED_STATE_MIGRATION = 40
MAX_SESSION_META_BYTES = 8 * 1024 * 1024


class Refusal(RuntimeError):
    """A fail-closed condition detected before mutation."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


class PartialFailure(RuntimeError):
    """A recoverable failure after at least one mutation."""

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.details = details or {}


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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def emit(payload: dict[str, Any], exit_code: int = EXIT_OK) -> int:
    print(
        json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    )
    return exit_code


def normalize_path(raw: str) -> Path:
    if any(ord(char) < 32 or ord(char) == 127 for char in raw):
        raise Refusal("Paths containing control characters are unsupported.")
    if any(0xD800 <= ord(char) <= 0xDFFF for char in raw):
        raise Refusal("Paths containing Unicode surrogate code points are unsupported.")
    expanded = os.path.expanduser(raw)
    return Path(os.path.abspath(expanded))


def path_kind(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def identity(path: Path, *, follow_symlinks: bool = True) -> dict[str, int]:
    info = path.stat() if follow_symlinks else path.lstat()
    return {
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
        "mtime_ns": int(info.st_mtime_ns),
    }


def same_identity(path: Path, expected: dict[str, Any]) -> bool:
    try:
        current = identity(path)
    except OSError:
        return False
    return all(current[key] == int(expected[key]) for key in ("device", "inode", "mtime_ns"))


def same_object_identity(path: Path, expected: dict[str, Any]) -> bool:
    """Compare stable object identity without treating later directory use as replacement."""
    try:
        current = identity(path)
    except OSError:
        return False
    return all(current[key] == int(expected[key]) for key in ("device", "inode"))


def is_descendant(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
        return candidate != parent
    except ValueError:
        return False


def rewrite_structured_path(value: str, old: str, new: str) -> str | None:
    if value == old:
        return new
    prefix = old + os.sep
    if value.startswith(prefix):
        return new + value[len(old) :]
    return None


def resolved_symlink_target(link: Path) -> Path:
    target = os.readlink(link)
    target_path = Path(target)
    if not target_path.is_absolute():
        target_path = link.parent / target_path
    return Path(os.path.realpath(target_path))


def name_collision(parent: Path, name: str, *, ignore_name: str | None = None) -> str | None:
    wanted = unicodedata.normalize("NFC", name).casefold()
    try:
        names = os.listdir(parent)
    except OSError as exc:
        raise Refusal(f"Cannot list destination parent: {exc}") from exc
    for existing in names:
        if ignore_name is not None and existing == ignore_name:
            continue
        if unicodedata.normalize("NFC", existing).casefold() == wanted:
            return existing
    return None


def dangerous_roots(codex_home: Path) -> set[Path]:
    return {Path("/"), Path.home(), codex_home}


def validate_static_paths(old: Path, new: Path, codex_home: Path) -> list[str]:
    warnings: list[str] = []
    if old == new:
        raise Refusal("Old and new paths are identical.")
    if old in dangerous_roots(codex_home):
        raise Refusal("The source is a protected root.", details={"source": str(old)})
    if any(
        (
            candidate == codex_home
            or is_descendant(candidate, codex_home)
            or is_descendant(codex_home, candidate)
        )
        for candidate in (old, new)
    ):
        raise Refusal(
            "Project paths and CODEX_HOME must not contain one another.",
            details={
                "source": str(old),
                "destination": str(new),
                "codex_home": str(codex_home),
            },
        )
    if os.path.ismount(old):
        raise Refusal("Moving a mount point is unsupported.")
    if old.parent == old or new.parent == new:
        raise Refusal("Moving a filesystem root is unsupported.")
    if path_kind(old.parent) != "directory" or old.parent.is_symlink():
        raise Refusal("The source parent must be a real directory.")
    if path_kind(new.parent) != "directory" or new.parent.is_symlink():
        raise Refusal("The destination parent must already exist as a real directory.")
    if Path(os.path.realpath(old.parent)) != old.parent:
        raise Refusal("The source path traverses a symlinked ancestor.")
    if Path(os.path.realpath(new.parent)) != new.parent:
        raise Refusal("The destination path traverses a symlinked ancestor.")
    if is_descendant(new, old) or is_descendant(old, new):
        raise Refusal("Nested source and destination paths are unsupported.")
    old_real = Path(os.path.realpath(old)) if path_kind(old) != "absent" else old
    new_parent_real = Path(os.path.realpath(new.parent))
    new_real = new_parent_real / new.name
    if is_descendant(new_real, old_real) or is_descendant(old_real, new_real):
        raise Refusal("Resolved source and destination paths are nested.")
    if not os.access(new.parent, os.W_OK | os.X_OK):
        warnings.append(
            "The current environment cannot write the destination parent; apply must recheck from Terminal."
        )
    if not os.access(old.parent, os.W_OK | os.X_OK):
        warnings.append(
            "The current environment cannot write the source parent; apply must recheck from Terminal."
        )
    if old.parent == new.parent:
        collision = name_collision(new.parent, new.name, ignore_name=new.name)
        if collision is not None and collision == old.name:
            raise Refusal(
                "Case-only or Unicode-normalization-only renames are unsupported.",
                details={"colliding_name": collision},
            )
    return warnings


def inspect_filesystem(old: Path, new: Path, *, compatibility_link: bool) -> dict[str, Any]:
    old_kind = path_kind(old)
    new_kind = path_kind(new)
    result: dict[str, Any] = {
        "compatibility_link": compatibility_link,
        "old_kind": old_kind,
        "new_kind": new_kind,
        "state": "ambiguous",
        "warnings": [],
    }

    if old_kind == "directory" and new_kind == "absent":
        if (old / ".git").is_file():
            raise Refusal("Linked Git worktrees and submodules require a dedicated move procedure.")
        source_identity = identity(old)
        destination_parent_identity = identity(new.parent)
        source_parent_identity = identity(old.parent)
        if source_identity["device"] != destination_parent_identity["device"]:
            raise Refusal("Cross-volume relocation is unsupported.")
        collision = name_collision(
            new.parent,
            new.name,
            ignore_name=old.name if old.parent == new.parent else None,
        )
        if collision is not None:
            raise Refusal(
                "The destination name collides on this filesystem.",
                details={"colliding_name": collision},
            )
        result.update(
            {
                "state": "initial",
                "source_identity": source_identity,
                "source_parent_identity": source_parent_identity,
                "destination_parent_identity": destination_parent_identity,
                "same_device": True,
            }
        )
        return result

    if old_kind == "symlink" and new_kind == "directory":
        if not compatibility_link:
            raise Refusal(
                "The old compatibility link exists, but --without-link requires it to be absent."
            )
        try:
            target = resolved_symlink_target(old)
        except OSError as exc:
            raise Refusal(f"Cannot resolve the compatibility link: {exc}") from exc
        if target != Path(os.path.realpath(new)):
            raise Refusal(
                "The old path is a symlink to a different target.",
                details={"actual_target": str(target)},
            )
        result.update(
            {
                "state": "linked",
                "source_identity": identity(new),
                "source_parent_identity": identity(old.parent),
                "destination_parent_identity": identity(new.parent),
                "link_target": os.readlink(old),
            }
        )
        return result

    if old_kind == "absent" and new_kind == "directory":
        result.update(
            {
                "state": "destination-only-unverified",
                "source_identity": identity(new),
                "source_parent_identity": identity(old.parent),
                "destination_parent_identity": identity(new.parent),
                "warnings": [
                    "The destination exists but no compatibility link proves it came from the source."
                ],
            }
        )
        return result

    if old_kind == "absent" and new_kind == "absent":
        raise Refusal("Neither source nor destination exists.")
    if old_kind == "directory" and new_kind == "directory":
        raise Refusal("Both source and destination are real directories; refusing to merge.")
    if new_kind not in {"absent", "directory"}:
        raise Refusal("The destination is occupied by a non-directory node.")
    if old_kind == "symlink":
        raise Refusal("The source is an unrecognized symlink.")
    raise Refusal(
        "The filesystem state is unsupported.",
        details={"old_kind": old_kind, "new_kind": new_kind},
    )


def encode_apply_token(payload: dict[str, Any]) -> str:
    raw = canonical_json(payload)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"a{TOKEN_VERSION}.{encoded}.{sha256_bytes(raw)}"


def decode_apply_token(token: str) -> dict[str, Any]:
    if len(token) > 8192:
        raise Refusal("The apply token is invalid or corrupted.")
    try:
        prefix, encoded, digest = token.split(".", 2)
        if prefix != f"a{TOKEN_VERSION}":
            raise ValueError("version")
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            (encoded + padding).encode("ascii"), altchars=b"-_", validate=True
        )
        if sha256_bytes(raw) != digest:
            raise ValueError("checksum")
        value = strict_json_loads(raw, "The apply token")
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Refusal("The apply token is invalid or corrupted.") from exc
    if not isinstance(value, dict):
        raise Refusal("The apply token payload is invalid.")
    if canonical_json(value) != raw:
        raise Refusal("The apply token payload is not canonically encoded.")
    expected_keys = {
        "version",
        "old",
        "new",
        "compatibility_link",
        "source_identity",
        "source_parent_identity",
        "destination_parent_identity",
        "metadata_token",
    }
    if set(value) != expected_keys:
        raise Refusal("The apply token payload has an unexpected schema.")
    if type(value["version"]) is not int or value["version"] != TOKEN_VERSION:
        raise Refusal("The apply token payload has an unsupported version.")
    if type(value["compatibility_link"]) is not bool:
        raise Refusal("The apply token compatibility-link policy is invalid.")
    if not all(isinstance(value[field], str) for field in ("old", "new", "metadata_token")):
        raise Refusal("The apply token path or metadata fields are invalid.")
    if str(normalize_path(value["old"])) != value["old"] or str(
        normalize_path(value["new"])
    ) != value["new"]:
        raise Refusal("The apply token paths are not canonical absolute paths.")
    if not re.fullmatch(rf"r{TOKEN_VERSION}\.[0-9a-f]{{64}}", value["metadata_token"]):
        raise Refusal("The apply token metadata snapshot is invalid.")
    for field in (
        "source_identity",
        "source_parent_identity",
        "destination_parent_identity",
    ):
        candidate = value[field]
        if not isinstance(candidate, dict) or set(candidate) != {
            "device",
            "inode",
            "mtime_ns",
        }:
            raise Refusal("The apply token filesystem identity is invalid.")
        if any(type(candidate[key]) is not int or candidate[key] < 0 for key in candidate):
            raise Refusal("The apply token filesystem identity is invalid.")
    return value


def encode_repair_apply_token(payload: dict[str, Any]) -> str:
    raw = canonical_json(payload)
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"p{TOKEN_VERSION}.{encoded}.{sha256_bytes(raw)}"


def decode_repair_apply_token(token: str) -> dict[str, Any]:
    if len(token) > 8192:
        raise Refusal("The repair apply token is invalid or corrupted.")
    try:
        prefix, encoded, digest = token.split(".", 2)
        if prefix != f"p{TOKEN_VERSION}":
            raise ValueError("version")
        padding = "=" * (-len(encoded) % 4)
        raw = base64.b64decode(
            (encoded + padding).encode("ascii"), altchars=b"-_", validate=True
        )
        if sha256_bytes(raw) != digest:
            raise ValueError("checksum")
        value = strict_json_loads(raw, "The repair apply token")
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise Refusal("The repair apply token is invalid or corrupted.") from exc
    if not isinstance(value, dict) or canonical_json(value) != raw:
        raise Refusal("The repair apply token payload is invalid.")
    expected_keys = {
        "version",
        "old",
        "new",
        "filesystem_state",
        "destination_identity",
        "metadata_token",
    }
    if set(value) != expected_keys:
        raise Refusal("The repair apply token payload has an unexpected schema.")
    if type(value["version"]) is not int or value["version"] != TOKEN_VERSION:
        raise Refusal("The repair apply token has an unsupported version.")
    if not all(isinstance(value[field], str) for field in ("old", "new", "metadata_token")):
        raise Refusal("The repair apply token path or metadata fields are invalid.")
    if str(normalize_path(value["old"])) != value["old"] or str(
        normalize_path(value["new"])
    ) != value["new"]:
        raise Refusal("The repair apply token paths are not canonical absolute paths.")
    if not isinstance(value["filesystem_state"], str) or value[
        "filesystem_state"
    ] not in {"linked", "destination-only-unverified"}:
        raise Refusal("The repair apply token filesystem state is invalid.")
    if not re.fullmatch(rf"r{TOKEN_VERSION}\.[0-9a-f]{{64}}", value["metadata_token"]):
        raise Refusal("The repair apply token metadata snapshot is invalid.")
    destination_identity = value["destination_identity"]
    if not isinstance(destination_identity, dict) or set(destination_identity) != {
        "device",
        "inode",
    }:
        raise Refusal("The repair apply token destination identity is invalid.")
    if any(
        type(destination_identity[key]) is not int or destination_identity[key] < 0
        for key in destination_identity
    ):
        raise Refusal("The repair apply token destination identity is invalid.")
    return value


def repair_apply_token_for(
    old: Path,
    new: Path,
    filesystem: dict[str, Any],
    metadata_token: str,
) -> str:
    source_identity = filesystem["source_identity"]
    return encode_repair_apply_token(
        {
            "version": TOKEN_VERSION,
            "old": str(old),
            "new": str(new),
            "filesystem_state": filesystem["state"],
            "destination_identity": {
                "device": int(source_identity["device"]),
                "inode": int(source_identity["inode"]),
            },
            "metadata_token": metadata_token,
        }
    )


def assert_destination_binding(
    old: Path,
    new: Path,
    *,
    expected_state: str,
    expected_identity: dict[str, Any],
    compatibility_link: bool,
) -> None:
    current = inspect_filesystem(
        old,
        new,
        compatibility_link=compatibility_link,
    )
    if current["state"] != expected_state:
        raise Refusal(
            "The destination filesystem state changed after approval.",
            details={"expected": expected_state, "actual": current["state"]},
        )
    if not same_object_identity(new, expected_identity):
        raise Refusal(
            "The destination directory identity changed after approval.",
            details={"destination": str(new)},
        )


def apply_token_for(
    old: Path,
    new: Path,
    filesystem: dict[str, Any],
    metadata_token: str,
    *,
    compatibility_link: bool,
) -> str | None:
    if filesystem["state"] not in {"initial", "linked"}:
        return None
    payload = {
        "version": TOKEN_VERSION,
        "old": str(old),
        "new": str(new),
        "compatibility_link": compatibility_link,
        "source_identity": filesystem["source_identity"],
        "source_parent_identity": filesystem["source_parent_identity"],
        "destination_parent_identity": filesystem["destination_parent_identity"],
        "metadata_token": metadata_token,
    }
    return encode_apply_token(payload)


def stat_matches_identity(info: os.stat_result, expected: dict[str, Any]) -> bool:
    return all(
        int(getattr(info, field)) == int(expected[key])
        for field, key in (("st_dev", "device"), ("st_ino", "inode"), ("st_mtime_ns", "mtime_ns"))
    )


def stat_matches_object(info: os.stat_result, expected: dict[str, Any]) -> bool:
    return int(info.st_dev) == int(expected["device"]) and int(info.st_ino) == int(
        expected["inode"]
    )


def fsync_regular_file(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise Refusal(
                "A backup path is not a real regular file.",
                details={"path": str(path)},
            )
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def fsync_directory_tree(root: Path) -> None:
    def walk_error(exc: OSError) -> None:
        raise Refusal(f"Cannot enumerate the generated backup tree: {exc}") from exc

    directories: list[Path] = []
    for current_raw, directory_names, _ in os.walk(
        root,
        followlinks=False,
        onerror=walk_error,
    ):
        current = Path(current_raw)
        directories.append(current)
        for name in directory_names:
            child = current / name
            if path_kind(child) != "directory" or child.is_symlink():
                raise Refusal(
                    "The generated backup tree contains a non-directory or symlink.",
                    details={"path": str(child)},
                )
    for directory in sorted(
        directories,
        key=lambda candidate: len(candidate.parts),
        reverse=True,
    ):
        fsync_directory(directory)


def fsync_proven_parent_directories(
    old: Path,
    new: Path,
    *,
    source_parent_identity: dict[str, Any],
    destination_parent_identity: dict[str, Any],
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    source_descriptor = os.open(old.parent, flags)
    destination_descriptor = os.open(new.parent, flags)
    try:
        source_info = os.fstat(source_descriptor)
        destination_info = os.fstat(destination_descriptor)
        if not stat_matches_object(source_info, source_parent_identity):
            raise Refusal("The source parent object changed during relocation recovery.")
        if not stat_matches_object(destination_info, destination_parent_identity):
            raise Refusal("The destination parent object changed during relocation recovery.")
        try:
            os.fsync(destination_descriptor)
            if (source_info.st_dev, source_info.st_ino) != (
                destination_info.st_dev,
                destination_info.st_ino,
            ):
                os.fsync(source_descriptor)
        except OSError as exc:
            raise PartialFailure(
                "The relocated directory entries could not be durably synced.",
                details={"old": str(old), "new": str(new), "error": str(exc)},
            ) from exc
    finally:
        os.close(destination_descriptor)
        os.close(source_descriptor)


def create_compatibility_link(
    old: Path,
    new: Path,
    *,
    source_identity: dict[str, Any],
    source_parent_identity: dict[str, Any],
    destination_parent_identity: dict[str, Any],
) -> None:
    directory_flags = (
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    )
    source_fd = os.open(old.parent, directory_flags)
    destination_fd = os.open(new.parent, directory_flags)
    try:
        if not stat_matches_object(os.fstat(source_fd), source_parent_identity):
            raise Refusal("The source parent object changed before link creation.")
        if not stat_matches_object(os.fstat(destination_fd), destination_parent_identity):
            raise Refusal("The destination parent object changed before link creation.")
        destination = os.stat(new.name, dir_fd=destination_fd, follow_symlinks=False)
        if not stat.S_ISDIR(destination.st_mode) or not stat_matches_object(
            destination, source_identity
        ):
            raise Refusal("The destination object changed before link creation.")
        try:
            os.stat(old.name, dir_fd=source_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise Refusal("The old path appeared before compatibility-link creation.")
        os.symlink(
            str(new),
            old.name,
            target_is_directory=True,
            dir_fd=source_fd,
        )
        try:
            os.fsync(source_fd)
        except OSError as exc:
            raise PartialFailure(
                "The compatibility link was created, but its parent directory could not be durably synced.",
                details={"link": str(old), "error": str(exc)},
            ) from exc
    finally:
        for descriptor in (destination_fd, source_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def atomic_exclusive_rename(
    old: Path,
    new: Path,
    *,
    source_identity: dict[str, Any],
    source_parent_identity: dict[str, Any],
    destination_parent_identity: dict[str, Any],
) -> None:
    """Rename without replacing a racing destination."""
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    source_fd = os.open(old.parent, directory_flags)
    destination_fd = os.open(new.parent, directory_flags)
    try:
        if not stat_matches_identity(os.fstat(source_fd), source_parent_identity):
            raise Refusal("The source parent changed before the atomic rename.")
        if not stat_matches_identity(os.fstat(destination_fd), destination_parent_identity):
            raise Refusal("The destination parent changed before the atomic rename.")
        source_info = os.stat(old.name, dir_fd=source_fd, follow_symlinks=False)
        if not stat.S_ISDIR(source_info.st_mode) or not stat_matches_identity(
            source_info, source_identity
        ):
            raise Refusal("The source directory changed before the atomic rename.")

        libc = ctypes.CDLL(None, use_errno=True)
        if sys.platform == "darwin":
            function = getattr(libc, "renameatx_np", None)
            flag = 0x00000004  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            function = getattr(libc, "renameat2", None)
            flag = 1  # RENAME_NOREPLACE
        else:
            function = None
            flag = 0
        if function is None:
            raise Refusal("Atomic no-replace rename is unavailable on this platform.")
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(
            source_fd,
            os.fsencode(old.name),
            destination_fd,
            os.fsencode(new.name),
            flag,
        )
        if result != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(new))
        try:
            os.fsync(destination_fd)
            source_parent = os.fstat(source_fd)
            destination_parent = os.fstat(destination_fd)
            if (source_parent.st_dev, source_parent.st_ino) != (
                destination_parent.st_dev,
                destination_parent.st_ino,
            ):
                os.fsync(source_fd)
        except OSError as exc:
            raise PartialFailure(
                "The directory was renamed, but its parent directories could not be durably synced.",
                details={"new": str(new), "error": str(exc)},
            ) from exc
    finally:
        for descriptor in (destination_fd, source_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


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


def migration_version(connection: sqlite3.Connection) -> int | None:
    try:
        row = connection.execute(
            "SELECT MAX(version) FROM _sqlx_migrations WHERE success = 1"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise Refusal(f"Cannot audit the state database migrations: {exc}") from exc
    if row is None or row[0] is None:
        return None
    return int(row[0])


def state_schema(connection: sqlite3.Connection) -> dict[str, Any]:
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(threads)").fetchall()
        }
    except sqlite3.DatabaseError as exc:
        raise Refusal(f"Cannot audit the state database threads schema: {exc}") from exc
    required = {"id", "cwd", "rollout_path", "sandbox_policy"}
    maximum = migration_version(connection)
    return {
        "columns": sorted(columns),
        "max_migration": maximum,
        "supported": required.issubset(columns) and maximum == SUPPORTED_STATE_MIGRATION,
    }


def matching_path_pointers(value: Any, old: str, pointer: str = "") -> list[str]:
    matches: list[str] = []
    if isinstance(value, str):
        if rewrite_structured_path(value, old, old) is not None:
            matches.append(pointer or "/")
        return matches
    if isinstance(value, list):
        for index, item in enumerate(value):
            matches.extend(matching_path_pointers(item, old, f"{pointer}/{index}"))
        return matches
    if isinstance(value, dict):
        for key, item in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            key_pointer = f"{pointer}/{escaped}"
            if isinstance(key, str) and rewrite_structured_path(key, old, old) is not None:
                matches.append(f"{key_pointer}#key")
            matches.extend(matching_path_pointers(item, old, key_pointer))
    return matches


def policy_rewrites(policy_text: str, old: str, new: str) -> tuple[str | None, list[dict[str, Any]]]:
    try:
        policy = strict_json_loads(policy_text, "A sandbox_policy")
    except json.JSONDecodeError as exc:
        raise Refusal("A sandbox_policy is not valid JSON; its paths cannot be audited.") from exc
    entries: list[Any] = []
    if isinstance(policy, dict):
        file_system = policy.get("file_system")
        if isinstance(file_system, dict):
            candidate_entries = file_system.get("entries", [])
            if isinstance(candidate_entries, list):
                entries = candidate_entries
    actions: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        path_node = entry.get("path")
        if not isinstance(path_node, dict) or path_node.get("type") != "path":
            continue
        value = path_node.get("path")
        if not isinstance(value, str):
            continue
        rewritten = rewrite_structured_path(value, old, new)
        if rewritten is None:
            continue
        path_node["path"] = rewritten
        actions.append(
            {
                "pointer": f"/file_system/entries/{index}/path/path",
                "old": value,
                "new": rewritten,
            }
        )
    recognized = {
        action["pointer"] for action in actions
    }
    unknown = sorted(set(matching_path_pointers(policy, old)) - recognized)
    if unknown:
        raise Refusal(
            "A sandbox_policy contains old paths outside the write allowlist.",
            details={"pointers": unknown},
        )
    if not actions:
        return None, []
    return json.dumps(
        policy,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ), actions


def collect_state_db_changes(
    connection: sqlite3.Connection, path: Path, old: str, new: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    changes: list[dict[str, Any]] = []
    row_updates: list[dict[str, Any]] = []
    related_sessions: list[dict[str, str]] = []
    rows = connection.execute(
        "SELECT id, cwd, rollout_path, sandbox_policy FROM threads ORDER BY id"
    ).fetchall()
    for thread_id, cwd, rollout_path, policy_text in rows:
        if not all(
            isinstance(value, str)
            for value in (thread_id, cwd, rollout_path, policy_text)
        ):
            raise Refusal(
                "A thread row contains non-text identity, cwd, rollout_path, or sandbox_policy data."
            )
        new_cwd = rewrite_structured_path(cwd, old, new)
        new_policy, policy_actions = policy_rewrites(policy_text, old, new)
        policy = strict_json_loads(policy_text, "A sandbox_policy")
        related = any(
            rewrite_structured_path(cwd, base, base) is not None
            or bool(matching_path_pointers(policy, base))
            for base in (old, new)
        )
        if related:
            related_sessions.append(
                {"thread_id": thread_id, "rollout_path": rollout_path}
            )
        if new_cwd is not None:
            changes.append(
                {
                    "store": "state_db",
                    "kind": "thread_cwd",
                    "thread_id": thread_id,
                    "pointer": "/threads/cwd",
                    "old": cwd,
                    "new": new_cwd,
                    "file": str(path),
                }
            )
        for action in policy_actions:
            changes.append(
                {
                    "store": "state_db",
                    "kind": "sandbox_path",
                    "thread_id": thread_id,
                    "pointer": f"/threads/sandbox_policy{action['pointer']}",
                    "old": action["old"],
                    "new": action["new"],
                    "file": str(path),
                }
            )
        if new_cwd is not None or policy_actions:
            row_updates.append(
                {
                    "thread_id": thread_id,
                    "old_cwd": cwd,
                    "old_policy": policy_text,
                    "new_cwd": new_cwd if new_cwd is not None else cwd,
                    "new_policy": new_policy if new_policy is not None else policy_text,
                }
            )
    return changes, row_updates, related_sessions


def scan_state_db(
    path: Path, old: str, new: str
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    list[str],
    list[dict[str, str]],
]:
    warnings: list[str] = []
    with sqlite_read_snapshot(path) as snapshot_path:
        uri = snapshot_path.resolve().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.DatabaseError as exc:
            raise Refusal(f"Cannot open the state database read-only: {exc}") from exc
        try:
            schema = state_schema(connection)
            required = {"id", "cwd", "rollout_path", "sandbox_policy"}
            if not required.issubset(set(schema["columns"])):
                warnings.append("The threads table lacks required columns; database repair is disabled.")
                return [], schema, warnings, []
            changes, _, related_sessions = collect_state_db_changes(
                connection, path, old, new
            )
        finally:
            connection.close()
    if not schema["supported"]:
        warnings.append(
            f"State schema migration {schema['max_migration']!r} is not the tested migration "
            f"{SUPPORTED_STATE_MIGRATION}; mutation is disabled."
        )
    return changes, schema, warnings, related_sessions


def transform_list(
    values: Any,
    old: str,
    new: str,
    *,
    pointer: str,
    actions: list[dict[str, Any]],
) -> None:
    if not isinstance(values, list):
        raise Refusal(f"Global-state field {pointer} is not a list.")
    if any(not isinstance(value, str) for value in values):
        raise Refusal(f"Global-state field {pointer} contains a non-string path.")
    original = list(values)
    rewritten_indices: set[int] = set()
    for index, value in enumerate(original):
        if not isinstance(value, str):
            continue
        rewritten = rewrite_structured_path(value, old, new)
        if rewritten is None:
            continue
        values[index] = rewritten
        rewritten_indices.add(index)
        actions.append(
            {
                "pointer": f"{pointer}/{index}",
                "old": value,
                "new": rewritten,
            }
        )
    unchanged_values = {
        values[index]
        for index in range(len(values))
        if index not in rewritten_indices and isinstance(values[index], str)
    }
    remove_indices = {
        index for index in rewritten_indices if values[index] in unchanged_values
    }
    for index in sorted(remove_indices):
        actions.append(
            {
                "pointer": f"{pointer}/{index}",
                "operation": "remove_rewrite_collision",
                "old": values[index],
                "new": None,
            }
        )
    if remove_indices:
        values[:] = [
            value for index, value in enumerate(values) if index not in remove_indices
        ]


def transform_global_state(data: Any, old: str, new: str) -> tuple[Any, list[dict[str, Any]]]:
    if not isinstance(data, dict):
        raise Refusal("The Codex global state is not a JSON object.")
    result = copy.deepcopy(data)
    actions: list[dict[str, Any]] = []
    for field in (
        "electron-saved-workspace-roots",
        "active-workspace-roots",
        "project-order",
        "pinned-project-ids",
    ):
        if field in result:
            transform_list(result[field], old, new, pointer=f"/{field}", actions=actions)

    labels = result.get("electron-workspace-root-labels")
    if "electron-workspace-root-labels" in result:
        if not isinstance(labels, dict):
            raise Refusal("Global-state workspace-root labels are not an object.")
        for key in list(labels):
            if not isinstance(key, str):
                raise Refusal("A global-state workspace-root label key is not text.")
            rewritten = rewrite_structured_path(key, old, new)
            if rewritten is None:
                continue
            if rewritten in labels and rewritten != key:
                raise Refusal("Global-state label keys would collide after relocation.")
            labels[rewritten] = labels.pop(key)
            actions.append(
                {
                    "pointer": "/electron-workspace-root-labels",
                    "operation": "rename_key",
                    "old": key,
                    "new": rewritten,
                }
            )

    hints = result.get("thread-workspace-root-hints")
    if "thread-workspace-root-hints" in result:
        if not isinstance(hints, dict):
            raise Refusal("Global-state thread workspace-root hints are not an object.")
        for thread_id, value in list(hints.items()):
            if not isinstance(value, str):
                raise Refusal("A global-state thread workspace-root hint is not text.")
            rewritten = rewrite_structured_path(value, old, new)
            if rewritten is not None:
                hints[thread_id] = rewritten
                actions.append(
                    {
                        "pointer": f"/thread-workspace-root-hints/{thread_id}",
                        "old": value,
                        "new": rewritten,
                    }
                )

    roots = result.get("thread-writable-roots")
    if "thread-writable-roots" in result:
        if not isinstance(roots, dict):
            raise Refusal("Global-state thread writable roots are not an object.")
        for thread_id, values in roots.items():
            transform_list(
                values,
                old,
                new,
                pointer=f"/thread-writable-roots/{thread_id}",
                actions=actions,
            )

    persisted = result.get("electron-persisted-atom-state")
    if "electron-persisted-atom-state" in result and not isinstance(persisted, dict):
        raise Refusal("Global-state persisted atom state is not an object.")
    heartbeat = persisted.get("heartbeat-thread-permissions-by-id") if persisted else None
    if (
        isinstance(persisted, dict)
        and "heartbeat-thread-permissions-by-id" in persisted
        and not isinstance(heartbeat, dict)
    ):
        raise Refusal("Global-state heartbeat thread permissions are not an object.")
    if isinstance(heartbeat, dict):
        for thread_id, permission in heartbeat.items():
            if not isinstance(permission, dict):
                raise Refusal("A heartbeat thread permission is not an object.")
            if "sandboxPolicy" not in permission:
                continue
            policy = permission["sandboxPolicy"]
            if not isinstance(policy, dict):
                raise Refusal("A heartbeat sandboxPolicy is not an object.")
            if "writableRoots" not in policy:
                continue
            values = policy["writableRoots"]
            transform_list(
                values,
                old,
                new,
                pointer=(
                    "/electron-persisted-atom-state/heartbeat-thread-permissions-by-id/"
                    f"{thread_id}/sandboxPolicy/writableRoots"
                ),
                actions=actions,
            )

    residuals: list[str] = []
    for field in (
        "electron-saved-workspace-roots",
        "active-workspace-roots",
        "project-order",
        "pinned-project-ids",
        "thread-workspace-root-hints",
        "thread-writable-roots",
    ):
        if field in result:
            residuals.extend(
                f"/{field}{pointer if pointer != '/' else ''}"
                for pointer in matching_path_pointers(result[field], old)
            )
    if isinstance(labels, dict):
        residuals.extend(
            f"/electron-workspace-root-labels/{key}"
            for key in labels
            if rewrite_structured_path(key, old, old) is not None
        )
    if isinstance(heartbeat, dict):
        for thread_id, permission in heartbeat.items():
            policy = permission.get("sandboxPolicy")
            if isinstance(policy, dict) and "writableRoots" in policy:
                residuals.extend(
                    "/electron-persisted-atom-state/heartbeat-thread-permissions-by-id/"
                    f"{thread_id}/sandboxPolicy/writableRoots{pointer if pointer != '/' else ''}"
                    for pointer in matching_path_pointers(policy["writableRoots"], old)
                )
    if residuals:
        raise Refusal(
            "Global-state allowlisted fields still contain old paths after transformation.",
            details={"pointers": sorted(residuals)},
        )
    return result, actions


def scan_global_state(
    path: Path, codex_home: Path, old: str, new: str
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    if path_kind(path) == "absent":
        return [], warnings, ["The Codex global-state JSON was not found."]
    ensure_trusted_file(path, codex_home, "The Codex global-state JSON")
    try:
        data = strict_json_loads(
            path.read_text(encoding="utf-8"), "The Codex global-state JSON"
        )
    except Refusal as exc:
        return [], warnings, [str(exc)]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [], warnings, [f"Cannot parse the Codex global-state JSON: {exc}"]
    try:
        _, raw_actions = transform_global_state(data, old, new)
    except Refusal as exc:
        return [], warnings, [f"Cannot safely transform the Codex global-state JSON: {exc}"]
    changes = [
        {
            "store": "global_state",
            "kind": "allowlisted_path",
            "file": str(path),
            **action,
        }
        for action in raw_actions
    ]
    return changes, warnings, errors


def session_files(codex_home: Path) -> Iterable[Path]:
    def walk_error(exc: OSError) -> None:
        raise Refusal(f"Cannot enumerate the Codex session tree: {exc}") from exc

    for root in (codex_home / "sessions", codex_home / "archived_sessions"):
        kind = path_kind(root)
        if kind == "absent":
            continue
        ensure_real_directory(root, f"The session root {root}")
        discovered: list[Path] = []
        for current_raw, directory_names, file_names in os.walk(
            root, followlinks=False, onerror=walk_error
        ):
            current = Path(current_raw)
            ensure_real_directory(current, f"The session directory {current}")
            for name in directory_names:
                child = current / name
                if path_kind(child) == "symlink":
                    raise Refusal(
                        "A Codex session directory tree contains a symlink.",
                        details={"path": str(child)},
                    )
                ensure_real_directory(child, f"The session directory {child}")
            for name in file_names:
                if name.endswith(".jsonl"):
                    discovered.append(current / name)
        for path in sorted(discovered):
            ensure_trusted_file(path, codex_home, "A Codex session JSONL")
            yield path


def read_session_meta_line(handle: Any, path: Path) -> bytes:
    first_line = handle.readline(MAX_SESSION_META_BYTES + 1)
    if len(first_line) > MAX_SESSION_META_BYTES:
        raise Refusal(
            "A session_meta record exceeds the supported size limit.",
            details={"path": str(path), "limit_bytes": MAX_SESSION_META_BYTES},
        )
    return first_line


def scan_sessions(
    codex_home: Path, old: str, new: str
) -> tuple[
    list[dict[str, Any]],
    list[str],
    list[str],
    dict[str, list[str]],
]:
    changes: list[dict[str, Any]] = []
    warnings: list[str] = []
    errors: list[str] = []
    inventory: dict[str, list[str]] = {}
    literal_markers = {
        old.encode("utf-8"),
        json.dumps(old, ensure_ascii=True)[1:-1].encode("utf-8"),
    }
    for path in session_files(codex_home):
        try:
            with path.open("rb") as handle:
                first_line = read_session_meta_line(handle, path)
        except Refusal as exc:
            errors.append(str(exc))
            continue
        except OSError as exc:
            errors.append(f"Cannot read {path}: {exc}")
            continue
        try:
            meta = strict_json_loads(
                first_line.decode("utf-8"), f"The first record of {path}"
            )
        except Refusal as exc:
            errors.append(str(exc))
            continue
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = f"Cannot parse the first record of {path}: {exc}"
            if any(marker in first_line for marker in literal_markers):
                errors.append(message)
            else:
                warnings.append(f"Ignored {path}: {exc}")
            continue
        if not isinstance(meta, dict) or meta.get("type") != "session_meta":
            warnings.append(f"Ignored {path}: first JSONL record is not session_meta")
            continue
        payload = meta.get("payload")
        thread_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(thread_id, str) or not thread_id:
            errors.append(f"The session_meta in {path} has no valid thread id.")
            continue
        inventory.setdefault(thread_id, []).append(str(path))
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        if not isinstance(cwd, str):
            errors.append(f"The session_meta in {path} has no valid cwd path.")
            continue
        rewritten = rewrite_structured_path(cwd, old, new)
        if rewritten is None:
            continue
        replace_session_meta_cwd(first_line, old, new)
        changes.append(
            {
                "store": "session",
                "kind": "session_meta_cwd",
                "file": str(path),
                "thread_id": thread_id,
                "pointer": "/session_meta/payload/cwd",
                "old": cwd,
                "new": rewritten,
            }
        )
    return changes, warnings, errors, inventory


def audit_related_sessions(
    related_sessions: list[dict[str, str]],
    inventory: dict[str, list[str]],
    codex_home: Path,
) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    errors: list[str] = []
    for record in related_sessions:
        thread_id = record["thread_id"]
        raw_rollout = record["rollout_path"]
        rollout = Path(raw_rollout)
        if not rollout.is_absolute() or str(normalize_path(raw_rollout)) != raw_rollout:
            errors.append(f"Thread {thread_id} has a non-canonical rollout_path: {raw_rollout}")
            continue
        try:
            rollout.relative_to(codex_home)
        except ValueError:
            errors.append(f"Thread {thread_id} has a rollout_path outside CODEX_HOME: {raw_rollout}")
            continue
        known_paths = inventory.get(thread_id, [])
        kind = path_kind(rollout)
        if kind == "absent":
            errors.append(
                f"Thread {thread_id} rollout_path is absent: {raw_rollout}; alternate sessions: {known_paths}"
            )
            continue
        try:
            ensure_trusted_file(rollout, codex_home, f"Thread {thread_id} rollout_path")
        except Refusal as exc:
            errors.append(str(exc))
            continue
        if raw_rollout not in known_paths:
            errors.append(
                f"Thread {thread_id} rollout_path does not contain a matching session_meta: {raw_rollout}"
            )
    return warnings, errors


def metadata_snapshot(
    codex_home: Path,
    old: str,
    new: str,
    *,
    explicit_state_db: Path | None,
) -> dict[str, Any]:
    changes: list[dict[str, Any]] = []
    warnings: list[str] = []
    audit_errors: list[str] = []
    ensure_real_directory(codex_home, "CODEX_HOME")
    state_db, db_warnings = choose_state_db(codex_home, explicit_state_db)
    warnings.extend(db_warnings)
    schema: dict[str, Any] | None = None
    related_sessions: list[dict[str, str]] = []
    if state_db is not None:
        db_changes, schema, db_scan_warnings, related_sessions = scan_state_db(
            state_db, old, new
        )
        changes.extend(db_changes)
        warnings.extend(db_scan_warnings)
    else:
        audit_errors.append("No state_5.sqlite was found; database metadata cannot be audited.")
    global_path = codex_home / ".codex-global-state.json"
    global_changes, global_warnings, global_errors = scan_global_state(
        global_path, codex_home, old, new
    )
    changes.extend(global_changes)
    warnings.extend(global_warnings)
    audit_errors.extend(global_errors)
    session_changes, session_warnings, session_errors, session_inventory = scan_sessions(
        codex_home, old, new
    )
    changes.extend(session_changes)
    warnings.extend(session_warnings)
    audit_errors.extend(session_errors)
    related_warnings, related_errors = audit_related_sessions(
        related_sessions, session_inventory, codex_home
    )
    warnings.extend(related_warnings)
    audit_errors.extend(related_errors)

    affected_files = sorted({change["file"] for change in changes})
    fingerprints: list[dict[str, Any]] = []
    for raw_path in affected_files:
        path = Path(raw_path)
        if path_kind(path) == "file":
            ensure_trusted_file(path, codex_home, "An affected metadata file")
            fingerprints.append(file_fingerprint(path))
        else:
            audit_errors.append(f"An affected file disappeared during planning: {path}")
    if state_db is not None:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(str(state_db) + suffix)
            if path_kind(sidecar) != "absent":
                ensure_trusted_file(sidecar, codex_home, "A SQLite sidecar")
            if str(sidecar) not in affected_files:
                fingerprints.append(path_state_fingerprint(sidecar))

    changes.sort(key=lambda item: canonical_json(item))
    fingerprints.sort(key=lambda item: item["path"])
    token_payload = {
        "version": TOKEN_VERSION,
        "old": old,
        "new": new,
        "state_db": str(state_db) if state_db else None,
        "schema": schema,
        "changes": changes,
        "fingerprints": fingerprints,
        "audit_complete": not audit_errors,
        "audit_errors": audit_errors,
    }
    repair_token = f"r{TOKEN_VERSION}.{sha256_bytes(canonical_json(token_payload))}"
    repair_supported = (
        not audit_errors
        and state_db is not None
        and schema is not None
        and bool(schema.get("supported"))
    )
    return {
        "changes": changes,
        "change_count": len(changes),
        "affected_files": affected_files,
        "fingerprints": fingerprints,
        "audit_complete": not audit_errors,
        "audit_errors": audit_errors,
        "repair_supported": repair_supported,
        "repair_token": repair_token,
        "schema": schema,
        "state_db": str(state_db) if state_db else None,
        "warnings": warnings,
    }


def database_users(path: Path) -> list[int] | None:
    try:
        result = subprocess.run(
            ["lsof", "-t", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if result.returncode not in {0, 1} or result.stderr.strip():
        return None
    users: list[int] = []
    for line in result.stdout.splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            return None
        if pid != os.getpid():
            users.append(pid)
    return sorted(set(users))


def ensure_codex_offline(snapshot: dict[str, Any]) -> None:
    raw = snapshot.get("state_db")
    if not raw:
        raise Refusal("No state database is available for a consistent metadata transaction.")
    paths = [Path(raw)]
    paths.extend(
        sidecar
        for sidecar in (
            Path(raw + "-wal"),
            Path(raw + "-shm"),
            Path(raw + "-journal"),
        )
        if path_kind(sidecar) != "absent"
    )
    active: dict[str, list[int]] = {}
    for path in paths:
        users = database_users(path)
        if users is None:
            raise Refusal(
                "Cannot prove that Codex metadata is offline because lsof failed.",
                details={"path": str(path)},
            )
        if users:
            active[str(path)] = users
    if active:
        raise Refusal(
            "Codex is still using the state database or a SQLite sidecar. Quit Codex and rerun from Terminal.",
            details={"active_paths": active},
        )


def assert_snapshot_fresh(snapshot: dict[str, Any]) -> None:
    for expected in snapshot["fingerprints"]:
        path = Path(expected["path"])
        current_exists = path_kind(path) != "absent"
        if bool(expected.get("exists")) != current_exists:
            raise Refusal(
                "A SQLite sidecar or planned metadata file changed existence before mutation.",
                details={"path": str(path)},
            )
        if not current_exists:
            continue
        if path_kind(path) != "file":
            raise Refusal(
                "A planned metadata path is no longer a real regular file.",
                details={"path": str(path)},
            )
        current = file_fingerprint(path)
        for field in ("device", "inode", "size", "mtime_ns", "sha256"):
            if current[field] != expected[field]:
                raise Refusal(
                    "A planned metadata file changed before mutation; create a fresh plan.",
                    details={"path": str(path), "field": field},
                )


def fingerprint_for(snapshot: dict[str, Any], path: Path) -> dict[str, Any]:
    for fingerprint in snapshot["fingerprints"]:
        if fingerprint["path"] == str(path) and fingerprint.get("exists"):
            return fingerprint
    raise Refusal(
        "The approved snapshot does not contain a fingerprint for an affected file.",
        details={"path": str(path)},
    )


def assert_file_matches(path: Path, expected: dict[str, Any]) -> None:
    current = file_fingerprint(path)
    for field in ("device", "inode", "size", "mtime_ns", "sha256"):
        if current[field] != expected[field]:
            raise Refusal(
                "A metadata file changed after planning.",
                details={"path": str(path), "field": field},
            )


def sorted_actions(actions: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(actions, key=canonical_json)


def require_exact_actions(
    actual: Iterable[dict[str, Any]],
    expected: Iterable[dict[str, Any]],
    store: str,
) -> None:
    actual_sorted = sorted_actions(actual)
    expected_sorted = sorted_actions(expected)
    if canonical_json(actual_sorted) != canonical_json(expected_sorted):
        raise Refusal(
            f"The {store} action set changed after planning.",
            details={"actual": actual_sorted, "expected": expected_sorted},
        )


def create_backup(
    snapshot: dict[str, Any], codex_home: Path, old: str, new: str
) -> tuple[Path, dict[str, Any]]:
    token = snapshot["repair_token"].split(".", 1)[1][:12]
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_parent = codex_home / "relocation-backups"
    parent_created = False
    parent_kind = path_kind(backup_parent)
    if parent_kind == "absent":
        try:
            backup_parent.mkdir(mode=0o700)
            parent_created = True
        except OSError as exc:
            raise Refusal(f"Cannot create the metadata backup directory: {exc}") from exc
    elif parent_kind != "directory" or Path(os.path.realpath(backup_parent)) != backup_parent:
        raise Refusal("The metadata backup location is not a real directory.")
    root = backup_parent / f"{stamp}-{token}"
    try:
        root.mkdir(mode=0o700, exist_ok=False)
    except OSError as exc:
        if parent_created:
            try:
                backup_parent.rmdir()
            except OSError:
                pass
        raise Refusal(f"Cannot create the metadata backup directory: {exc}") from exc
    try:
        os.chmod(root, 0o700)
        assert_snapshot_fresh(snapshot)
        manifest: dict[str, Any] = {
            "version": TOKEN_VERSION,
            "created_at": stamp,
            "old": old,
            "new": new,
            "repair_token": snapshot["repair_token"],
            "status": "backed-up",
            "changes": snapshot["changes"],
            "files": [],
        }
        state_db = Path(snapshot["state_db"]) if snapshot.get("state_db") else None
        for raw in snapshot["affected_files"]:
            source = Path(raw)
            ensure_trusted_file(source, codex_home, "An affected metadata file")
            expected = fingerprint_for(snapshot, source)
            assert_file_matches(source, expected)
            relative = source.relative_to(codex_home)
            destination = root / "files" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if state_db is not None and source == state_db:
                with sqlite_read_snapshot(source) as snapshot_path:
                    uri = snapshot_path.resolve().as_uri() + "?mode=ro"
                    try:
                        source_connection = sqlite3.connect(uri, uri=True)
                    except sqlite3.DatabaseError as exc:
                        raise Refusal(
                            f"Cannot open the temporary SQLite snapshot for backup: {exc}"
                        ) from exc
                    try:
                        destination_connection = sqlite3.connect(str(destination))
                    except sqlite3.DatabaseError as exc:
                        source_connection.close()
                        raise Refusal(
                            f"Cannot create the SQLite backup destination: {exc}"
                        ) from exc
                    try:
                        try:
                            source_connection.backup(destination_connection)
                        except sqlite3.DatabaseError as exc:
                            raise Refusal(f"SQLite backup API failed: {exc}") from exc
                        try:
                            journal = destination_connection.execute(
                                "PRAGMA journal_mode=DELETE"
                            ).fetchone()
                        except sqlite3.DatabaseError as exc:
                            raise Refusal(
                                f"Cannot make the SQLite backup standalone: {exc}"
                            ) from exc
                        if journal is None or str(journal[0]).lower() != "delete":
                            raise Refusal(
                                "The SQLite backup did not switch to standalone DELETE journal mode."
                            )
                    finally:
                        destination_connection.close()
                        source_connection.close()
                shutil.copymode(source, destination)
                try:
                    check_connection = sqlite3.connect(
                        destination.resolve().as_uri() + "?mode=ro", uri=True
                    )
                except sqlite3.DatabaseError as exc:
                    raise Refusal(
                        f"Cannot reopen the SQLite backup for quick_check: {exc}"
                    ) from exc
                try:
                    try:
                        row = check_connection.execute("PRAGMA quick_check").fetchone()
                    except sqlite3.DatabaseError as exc:
                        raise Refusal(f"SQLite backup quick_check failed: {exc}") from exc
                finally:
                    check_connection.close()
                if row is None or row[0] != "ok":
                    raise Refusal("The SQLite backup failed its quick_check.")
                backup_kind = "sqlite-backup-api"
            else:
                shutil.copy2(source, destination, follow_symlinks=False)
                backup_kind = "copy2"
            assert_file_matches(source, expected)
            source_hash = file_sha256(source)
            backup_hash = file_sha256(destination)
            if backup_kind == "copy2" and source_hash != backup_hash:
                raise Refusal("A copied metadata backup does not match its source.")
            fsync_regular_file(destination)
            manifest["files"].append(
                {
                    "source": str(source),
                    "backup": str(destination),
                    "kind": backup_kind,
                    "source_sha256": source_hash,
                    "backup_sha256": backup_hash,
                }
            )
        assert_snapshot_fresh(snapshot)
        manifest_path = root / "manifest.json"
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(
                manifest,
                handle,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                indent=2,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        fsync_directory_tree(root)
        fsync_directory(backup_parent)
        if parent_created:
            fsync_directory(codex_home)
        return root, manifest
    except Exception as exc:
        try:
            shutil.rmtree(root)
            if parent_created:
                backup_parent.rmdir()
        except OSError:
            pass
        if isinstance(exc, Refusal):
            raise
        raise Refusal(f"Cannot create a complete metadata backup: {exc}") from exc


def discard_backup(root: Path | None) -> None:
    if root is None or path_kind(root) == "absent":
        return
    parent = root.parent
    shutil.rmtree(root)
    try:
        parent.rmdir()
    except OSError:
        pass


def completed_backup_exists(codex_home: Path, token: str, old: str, new: str) -> bool:
    backup_parent = codex_home / "relocation-backups"
    kind = path_kind(backup_parent)
    if kind == "absent":
        return False
    ensure_real_directory(backup_parent, "The metadata backup location")
    for candidate in sorted(backup_parent.iterdir()):
        if path_kind(candidate) != "directory" or Path(os.path.realpath(candidate)) != candidate:
            continue
        manifest_path = candidate / "manifest.json"
        if path_kind(manifest_path) != "file" or Path(os.path.realpath(manifest_path)) != manifest_path:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (
            isinstance(manifest, dict)
            and manifest.get("repair_token") == token
            and manifest.get("old") == old
            and manifest.get("new") == new
            and manifest.get("status") == "completed"
        ):
            return True
    return False


def atomic_write(
    path: Path, data: bytes, *, expected_fingerprint: dict[str, Any] | None = None
) -> None:
    if path_kind(path) != "file" or Path(os.path.realpath(path)) != path:
        raise Refusal(
            "An atomic-write target is not a real regular file.",
            details={"path": str(path)},
        )
    if expected_fingerprint is not None:
        assert_file_matches(path, expected_fingerprint)
    original = path.lstat()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.relocate-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, stat.S_IMODE(original.st_mode))
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected_fingerprint is not None:
            assert_file_matches(path, expected_fingerprint)
        elif path_kind(path) != "file" or Path(os.path.realpath(path)) != path:
            raise Refusal("The atomic-write target changed before replacement.")
        directory_fd = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            current = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ) != (
                original.st_dev,
                original.st_ino,
                original.st_size,
                original.st_mtime_ns,
            ):
                raise Refusal("The atomic-write target changed before replacement.")
            os.replace(
                temporary_path.name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


JSON_STRING = re.compile(r'"(?:\\.|[^"\\])*"')


def replace_session_meta_cwd(raw: bytes, old: str, new: str) -> bytes:
    line_end = raw.find(b"\n")
    if line_end == -1:
        first_raw, remainder = raw, b""
    else:
        first_raw, remainder = raw[: line_end + 1], raw[line_end + 1 :]
    first_text = first_raw.decode("utf-8")
    parsed = strict_json_loads(first_text, "A session_meta record")
    payload = parsed.get("payload") if isinstance(parsed, dict) else None
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    rewritten = rewrite_structured_path(cwd, old, new) if isinstance(cwd, str) else None
    if rewritten is None:
        raise Refusal("The session metadata no longer contains the planned old cwd.")

    matches: list[re.Match[str]] = []
    for key_match in re.finditer(r'"cwd"\s*:\s*', first_text):
        value_match = JSON_STRING.match(first_text, key_match.end())
        if value_match is None:
            continue
        try:
            value = json.loads(value_match.group(0))
        except json.JSONDecodeError:
            continue
        if value == cwd:
            matches.append(value_match)
    if len(matches) != 1:
        raise Refusal("The session_meta cwd token is not uniquely identifiable.")
    match = matches[0]
    replacement = json.dumps(rewritten, ensure_ascii=True, allow_nan=False)
    updated_first = first_text[: match.start()] + replacement + first_text[match.end() :]
    updated_parsed = strict_json_loads(updated_first, "An updated session_meta record")
    expected = copy.deepcopy(parsed)
    expected["payload"]["cwd"] = rewritten
    if updated_parsed != expected:
        raise Refusal("Session metadata validation detected an unintended change.")
    return updated_first.encode("utf-8") + remainder


def session_changes_from_first_line(
    path: Path, first_line: bytes, old: str, new: str
) -> list[dict[str, Any]]:
    if not first_line:
        return []
    try:
        meta = strict_json_loads(
            first_line.decode("utf-8"), "A planned session_meta record"
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("A planned session first record is no longer valid JSON.") from exc
    if not isinstance(meta, dict) or meta.get("type") != "session_meta":
        return []
    payload = meta.get("payload")
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    rewritten = rewrite_structured_path(cwd, old, new) if isinstance(cwd, str) else None
    if rewritten is None:
        return []
    replace_session_meta_cwd(first_line, old, new)
    return [
        {
            "store": "session",
            "kind": "session_meta_cwd",
            "file": str(path),
            "thread_id": payload.get("id"),
            "pointer": "/session_meta/payload/cwd",
            "old": cwd,
            "new": rewritten,
        }
    ]


def stat_matches_fingerprint(
    info: os.stat_result, expected: dict[str, Any]
) -> bool:
    return stat.S_ISREG(info.st_mode) and all(
        int(actual) == int(expected[field])
        for actual, field in (
            (info.st_dev, "device"),
            (info.st_ino, "inode"),
            (info.st_size, "size"),
            (info.st_mtime_ns, "mtime_ns"),
        )
    )


def rewrite_session_file(
    path: Path,
    old: str,
    new: str,
    planned_changes: list[dict[str, Any]],
    expected_fingerprint: dict[str, Any],
) -> int:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source_descriptor = os.open(path, flags)
    temporary_descriptor = -1
    temporary_path: Path | None = None
    try:
        original = os.fstat(source_descriptor)
        if not stat_matches_fingerprint(original, expected_fingerprint):
            raise Refusal(
                "A session metadata file changed after planning.",
                details={"path": str(path)},
            )
        temporary_descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.relocate-",
            dir=path.parent,
        )
        temporary_path = Path(temporary)
        os.fchmod(temporary_descriptor, stat.S_IMODE(original.st_mode))

        with os.fdopen(source_descriptor, "rb", closefd=True) as source:
            source_descriptor = -1
            first_line = read_session_meta_line(source, path)
            actual = session_changes_from_first_line(path, first_line, old, new)
            require_exact_actions(actual, planned_changes, "session")
            updated_first_line = replace_session_meta_cwd(first_line, old, new)
            source_digest = hashlib.sha256()
            source_digest.update(first_line)

            with os.fdopen(temporary_descriptor, "wb", closefd=True) as destination:
                temporary_descriptor = -1
                destination.write(updated_first_line)
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    source_digest.update(chunk)
                    destination.write(chunk)
                source_after = os.fstat(source.fileno())
                if not stat_matches_fingerprint(
                    source_after, expected_fingerprint
                ) or source_digest.hexdigest() != expected_fingerprint["sha256"]:
                    raise Refusal(
                        "A session metadata file changed while it was streamed.",
                        details={"path": str(path)},
                    )
                destination.flush()
                os.fsync(destination.fileno())

            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
            )
            directory_descriptor = os.open(path.parent, directory_flags)
            try:
                current = os.stat(
                    path.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                if not stat_matches_fingerprint(current, expected_fingerprint):
                    raise Refusal(
                        "A session metadata file changed before replacement.",
                        details={"path": str(path)},
                    )
                os.replace(
                    temporary_path.name,
                    path.name,
                    src_dir_fd=directory_descriptor,
                    dst_dir_fd=directory_descriptor,
                )
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
        return len(planned_changes)
    except Exception:
        if source_descriptor >= 0:
            try:
                os.close(source_descriptor)
            except OSError:
                pass
        if temporary_descriptor >= 0:
            try:
                os.close(temporary_descriptor)
            except OSError:
                pass
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def global_changes_from_bytes(
    path: Path, raw: bytes, old: str, new: str
) -> tuple[Any, list[dict[str, Any]]]:
    try:
        data = strict_json_loads(raw.decode("utf-8"), "The Codex global-state JSON")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Refusal("The Codex global-state JSON changed into invalid data.") from exc
    transformed, actions = transform_global_state(data, old, new)
    changes = [
        {
            "store": "global_state",
            "kind": "allowlisted_path",
            "file": str(path),
            **action,
        }
        for action in actions
    ]
    return transformed, changes


def apply_database_changes(
    path: Path,
    old: str,
    new: str,
    planned_changes: list[dict[str, Any]],
) -> int:
    connection = sqlite3.connect(str(path), timeout=5)
    try:
        connection.execute("BEGIN IMMEDIATE")
        schema = state_schema(connection)
        if not schema["supported"]:
            raise Refusal("The state database schema is no longer the tested schema.")
        actual_changes, row_updates, _ = collect_state_db_changes(
            connection, path, old, new
        )
        require_exact_actions(actual_changes, planned_changes, "state database")
        for update in row_updates:
            cursor = connection.execute(
                "UPDATE threads SET cwd = ?, sandbox_policy = ? "
                "WHERE id = ? AND cwd = ? AND sandbox_policy = ?",
                (
                    update["new_cwd"],
                    update["new_policy"],
                    update["thread_id"],
                    update["old_cwd"],
                    update["old_policy"],
                ),
            )
            if cursor.rowcount != 1:
                raise Refusal(
                    "A thread row changed concurrently during repair.",
                    details={"thread_id": update["thread_id"]},
                )
        remaining, _, _ = collect_state_db_changes(connection, path, old, new)
        if remaining:
            raise Refusal(
                "The state database still contains structured old-path actions after repair.",
                details={"remaining": remaining},
            )
        check = connection.execute("PRAGMA integrity_check").fetchone()
        if check is None or check[0] != "ok":
            raise Refusal("SQLite integrity_check failed before commit.")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return len(planned_changes)


def update_manifest(root: Path, manifest: dict[str, Any], status_value: str, **extra: Any) -> None:
    manifest = copy.deepcopy(manifest)
    manifest["status"] = status_value
    manifest.update(extra)
    path = root / "manifest.json"
    data = json.dumps(
        manifest,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"
    atomic_write(path, data)


def prepare_metadata_backup(
    snapshot: dict[str, Any], codex_home: Path, old: str, new: str
) -> tuple[Path, dict[str, Any]] | None:
    if not snapshot["repair_supported"]:
        raise Refusal("Metadata mutation is disabled because the audit or schema is incomplete.")
    ensure_codex_offline(snapshot)
    assert_snapshot_fresh(snapshot)
    prepared = (
        create_backup(snapshot, codex_home, old, new)
        if snapshot["changes"]
        else None
    )
    try:
        ensure_codex_offline(snapshot)
        assert_snapshot_fresh(snapshot)
        refreshed = metadata_snapshot(
            codex_home,
            old,
            new,
            explicit_state_db=Path(snapshot["state_db"]),
        )
        if refreshed["repair_token"] != snapshot["repair_token"]:
            raise Refusal("Codex metadata changed while the backup was prepared.")
    except Exception:
        discard_backup(prepared[0] if prepared is not None else None)
        raise
    return prepared


def apply_metadata_snapshot(
    snapshot: dict[str, Any],
    codex_home: Path,
    old: str,
    new: str,
    prepared_backup: tuple[Path, dict[str, Any]] | None,
    destination_guard: Any | None = None,
) -> dict[str, Any]:
    if not snapshot["changes"]:
        try:
            if destination_guard is not None:
                destination_guard()
            ensure_codex_offline(snapshot)
            post = metadata_snapshot(
                codex_home,
                old,
                new,
                explicit_state_db=Path(snapshot["state_db"]),
            )
            if (
                not post["audit_complete"]
                or not post["repair_supported"]
                or post["changes"]
            ):
                raise Refusal(
                    "The post-relocation metadata audit is not clean.",
                    details={
                        "audit_errors": post["audit_errors"],
                        "remaining_changes": post["changes"],
                    },
                )
            if destination_guard is not None:
                destination_guard()
        except Exception as exc:
            raise PartialFailure(
                "The filesystem relocation completed, but the final metadata audit failed.",
                details={"backup": None, "error": str(exc)},
            ) from exc
        return {"status": "clean", "backup": None, "changes_applied": 0}
    if not snapshot["repair_supported"]:
        raise Refusal("Metadata mutation is disabled because the audit or schema is incomplete.")
    if prepared_backup is None:
        raise Refusal("A complete metadata backup was not prepared before mutation.")
    root, manifest = prepared_backup
    applied = 0
    try:
        ensure_codex_offline(snapshot)
        assert_snapshot_fresh(snapshot)
        refreshed = metadata_snapshot(
            codex_home,
            old,
            new,
            explicit_state_db=Path(snapshot["state_db"]),
        )
        if refreshed["repair_token"] != snapshot["repair_token"]:
            raise Refusal("Codex metadata changed after the backup was prepared.")
        if destination_guard is not None:
            destination_guard()

        by_store: dict[str, list[dict[str, Any]]] = {}
        for change in snapshot["changes"]:
            by_store.setdefault(change["store"], []).append(change)

        if by_store.get("state_db"):
            applied += apply_database_changes(
                Path(snapshot["state_db"]),
                old,
                new,
                by_store["state_db"],
            )

        session_paths = sorted({change["file"] for change in by_store.get("session", [])})
        for raw_path in session_paths:
            if destination_guard is not None:
                destination_guard()
            path = Path(raw_path)
            expected_fingerprint = fingerprint_for(snapshot, path)
            planned = [
                change for change in by_store["session"] if change["file"] == raw_path
            ]
            applied += rewrite_session_file(
                path,
                old,
                new,
                planned,
                expected_fingerprint,
            )

        if by_store.get("global_state"):
            if destination_guard is not None:
                destination_guard()
            path = Path(by_store["global_state"][0]["file"])
            expected_fingerprint = fingerprint_for(snapshot, path)
            assert_file_matches(path, expected_fingerprint)
            before = path.read_bytes()
            transformed, actual = global_changes_from_bytes(path, before, old, new)
            require_exact_actions(actual, by_store["global_state"], "global-state")
            trailing_newline = b"\n" if before.endswith(b"\n") else b""
            after = json.dumps(
                transformed,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8") + trailing_newline
            atomic_write(path, after, expected_fingerprint=expected_fingerprint)
            applied += len(by_store["global_state"])

        if applied != len(snapshot["changes"]):
            raise Refusal(
                "The applied action count differs from the approved plan.",
                details={"applied": applied, "planned": len(snapshot["changes"])},
            )
        post = metadata_snapshot(
            codex_home,
            old,
            new,
            explicit_state_db=Path(snapshot["state_db"]),
        )
        if not post["audit_complete"] or not post["repair_supported"] or post["changes"]:
            raise Refusal(
                "The post-repair metadata audit did not reach a clean state.",
                details={
                    "audit_errors": post["audit_errors"],
                    "remaining_changes": post["changes"],
                },
            )
        if destination_guard is not None:
            destination_guard()

        update_manifest(root, manifest, "completed", changes_applied=applied)
        return {"status": "repaired", "backup": str(root), "changes_applied": applied}
    except Exception as exc:
        try:
            update_manifest(root, manifest, "partial", error=str(exc), changes_applied=applied)
        except Exception:
            pass
        raise PartialFailure(
            "Metadata repair stopped in a recoverable partial state.",
            details={"backup": str(root), "changes_applied": applied, "error": str(exc)},
        ) from exc


def apply_filesystem(
    old: Path,
    new: Path,
    token_payload: dict[str, Any],
    *,
    compatibility_link: bool,
) -> dict[str, Any]:
    if not os.access(new.parent, os.W_OK | os.X_OK):
        raise Refusal("The destination parent is not writable in the applying environment.")
    if not os.access(old.parent, os.W_OK | os.X_OK):
        raise Refusal("The source parent is not writable in the applying environment.")
    expected = token_payload["source_identity"]
    old_kind = path_kind(old)
    new_kind = path_kind(new)

    if old_kind == "symlink" and new_kind == "directory":
        if not compatibility_link:
            raise Refusal(
                "The old compatibility link exists, but --without-link requires it to be absent."
            )
        if resolved_symlink_target(old) != Path(os.path.realpath(new)):
            raise Refusal("The compatibility link now points somewhere else.")
        if not same_object_identity(new, expected):
            raise Refusal("The destination identity no longer matches the plan.")
        fsync_proven_parent_directories(
            old,
            new,
            source_parent_identity=token_payload["source_parent_identity"],
            destination_parent_identity=token_payload["destination_parent_identity"],
        )
        return {"status": "already-complete", "link_created": False}

    if old_kind == "absent" and new_kind == "directory":
        if not same_object_identity(new, expected):
            raise Refusal("The destination cannot be proven to be the planned source directory.")
        fsync_proven_parent_directories(
            old,
            new,
            source_parent_identity=token_payload["source_parent_identity"],
            destination_parent_identity=token_payload["destination_parent_identity"],
        )
        if compatibility_link:
            try:
                create_compatibility_link(
                    old,
                    new,
                    source_identity=expected,
                    source_parent_identity=token_payload["source_parent_identity"],
                    destination_parent_identity=token_payload["destination_parent_identity"],
                )
            except (OSError, Refusal) as exc:
                raise PartialFailure(
                    "The directory was moved, but the compatibility link is still missing.",
                    details={"new": str(new), "error": str(exc)},
                ) from exc
            return {"status": "resumed", "link_created": True}
        return {"status": "already-complete", "link_created": False}

    if old_kind != "directory" or new_kind != "absent":
        raise Refusal(
            "Filesystem state changed after planning.",
            details={"old_kind": old_kind, "new_kind": new_kind},
        )
    if not same_identity(old, expected):
        raise Refusal("The source identity changed after planning.")
    if not same_identity(old.parent, token_payload["source_parent_identity"]):
        raise Refusal("The source parent identity changed after planning.")
    if not same_identity(new.parent, token_payload["destination_parent_identity"]):
        raise Refusal("The destination parent identity changed after planning.")
    if old.stat().st_dev != new.parent.stat().st_dev:
        raise Refusal("The relocation became cross-volume after planning.")
    if path_kind(new) != "absent":
        raise Refusal("The destination appeared after planning.")

    try:
        atomic_exclusive_rename(
            old,
            new,
            source_identity=expected,
            source_parent_identity=token_payload["source_parent_identity"],
            destination_parent_identity=token_payload["destination_parent_identity"],
        )
    except OSError as exc:
        if exc.errno in {errno.EEXIST, errno.ENOTEMPTY}:
            raise Refusal("The destination appeared during the atomic rename.") from exc
        raise Refusal(f"The atomic rename failed before completion: {exc}") from exc

    if not same_object_identity(new, expected):
        raise PartialFailure(
            "The move completed, but destination identity verification failed.",
            details={"new": str(new)},
        )
    if compatibility_link:
        try:
            create_compatibility_link(
                old,
                new,
                source_identity=expected,
                source_parent_identity=token_payload["source_parent_identity"],
                destination_parent_identity=token_payload["destination_parent_identity"],
            )
        except (OSError, Refusal) as exc:
            raise PartialFailure(
                "The directory moved successfully, but compatibility-link creation failed.",
                details={"new": str(new), "error": str(exc)},
            ) from exc
    return {"status": "moved", "link_created": compatibility_link}


def plan_command(args: argparse.Namespace) -> int:
    old = normalize_path(args.old)
    new = normalize_path(args.new)
    codex_home = normalize_path(args.codex_home)
    explicit_db = normalize_path(args.state_db) if args.state_db else None
    static_warnings = validate_static_paths(old, new, codex_home)
    filesystem = inspect_filesystem(old, new, compatibility_link=not args.without_link)
    filesystem["warnings"] = static_warnings + filesystem["warnings"]
    metadata = metadata_snapshot(
        codex_home, str(old), str(new), explicit_state_db=explicit_db
    )
    token = None
    if metadata["repair_supported"]:
        token = apply_token_for(
            old,
            new,
            filesystem,
            metadata["repair_token"],
            compatibility_link=not args.without_link,
        )
    actions: list[dict[str, Any]] = []
    if filesystem["state"] == "initial":
        actions.append({"operation": "atomic_rename", "old": str(old), "new": str(new)})
        if not args.without_link:
            actions.append(
                {"operation": "create_compatibility_link", "link": str(old), "target": str(new)}
            )
    elif filesystem["state"] == "linked":
        actions.append({"operation": "filesystem_noop", "reason": "already linked"})
    if not metadata["repair_supported"]:
        status = "blocked"
    elif token:
        status = "ready"
    else:
        status = "repair-only"
    return emit(
        {
            "command": "plan",
            "status": status,
            "old": str(old),
            "new": str(new),
            "filesystem": filesystem,
            "filesystem_actions": actions,
            "metadata": metadata,
            "apply_token": token,
            "next": (
                "Quit Codex, rerun plan from Terminal, then pass its fresh apply_token to apply."
                if token
                else (
                    "Resolve every metadata audit or schema error before relocation."
                    if status == "blocked"
                    else "Use repair for metadata-only recovery; filesystem apply is not proven safe."
                )
            ),
        }
    )


def apply_command(args: argparse.Namespace) -> int:
    old = normalize_path(args.old)
    new = normalize_path(args.new)
    codex_home = normalize_path(args.codex_home)
    explicit_db = normalize_path(args.state_db) if args.state_db else None
    validate_static_paths(old, new, codex_home)
    token = decode_apply_token(args.token)
    if token.get("old") != str(old) or token.get("new") != str(new):
        raise Refusal("The apply token names different paths.")
    if bool(token.get("compatibility_link")) != (not args.without_link):
        raise Refusal("The compatibility-link policy differs from the plan.")
    metadata = metadata_snapshot(
        codex_home, str(old), str(new), explicit_state_db=explicit_db
    )
    if metadata["repair_token"] != token.get("metadata_token"):
        already_complete = False
        if (
            metadata["repair_supported"]
            and not metadata["changes"]
            and path_kind(new) == "directory"
            and same_object_identity(new, token["source_identity"])
        ):
            if args.without_link:
                if path_kind(old) == "absent":
                    already_complete = True
            elif path_kind(old) == "symlink":
                try:
                    already_complete = resolved_symlink_target(old) == Path(os.path.realpath(new))
                except OSError:
                    already_complete = False
        if already_complete:
            return emit(
                {
                    "command": "apply",
                    "status": "already-complete",
                    "old": str(old),
                    "new": str(new),
                    "filesystem": {"status": "already-complete", "link_created": False},
                    "metadata": {"status": "clean", "backup": None, "changes_applied": 0},
                    "next": "No changes were made; run verify if UI state is uncertain.",
                }
            )
        raise Refusal("Codex metadata changed after planning; create a fresh plan while Codex is closed.")
    if not metadata["repair_supported"]:
        raise Refusal("The Codex metadata audit or schema is incomplete; refusing before filesystem mutation.")
    prepared = prepare_metadata_backup(metadata, codex_home, str(old), str(new))
    backup_root = prepared[0] if prepared is not None else None
    try:
        filesystem_result = apply_filesystem(
            old,
            new,
            token,
            compatibility_link=not args.without_link,
        )
    except Refusal:
        discard_backup(backup_root)
        raise
    except PartialFailure as exc:
        raise PartialFailure(
            str(exc),
            details={**exc.details, "backup": str(backup_root) if backup_root else None},
        ) from exc
    except Exception as exc:
        raise PartialFailure(
            "Filesystem relocation stopped in an uncertain state.",
            details={"backup": str(backup_root) if backup_root else None, "error": str(exc)},
        ) from exc

    def destination_guard() -> None:
        assert_destination_binding(
            old,
            new,
            expected_state=(
                "linked" if not args.without_link else "destination-only-unverified"
            ),
            expected_identity=token["source_identity"],
            compatibility_link=not args.without_link,
        )

    try:
        metadata_result = apply_metadata_snapshot(
            metadata,
            codex_home,
            str(old),
            str(new),
            prepared,
            destination_guard,
        )
        destination_guard()
    except PartialFailure as exc:
        raise PartialFailure(
            "Filesystem relocation completed, but metadata repair is partial.",
            details={"filesystem": filesystem_result, **exc.details},
        ) from exc
    except Refusal as exc:
        raise PartialFailure(
            "Filesystem relocation completed, but metadata repair was safely refused.",
            details={"filesystem": filesystem_result, "error": str(exc), **exc.details},
        ) from exc
    return emit(
        {
            "command": "apply",
            "status": "completed",
            "old": str(old),
            "new": str(new),
            "filesystem": filesystem_result,
            "metadata": metadata_result,
            "next": "Restart Codex, register the new folder if needed, then run verify.",
        }
    )


def repair_command(args: argparse.Namespace) -> int:
    old = normalize_path(args.old)
    new = normalize_path(args.new)
    codex_home = normalize_path(args.codex_home)
    explicit_db = normalize_path(args.state_db) if args.state_db else None
    validate_static_paths(old, new, codex_home)
    filesystem = inspect_filesystem(old, new, compatibility_link=True)
    if filesystem["state"] not in {"linked", "destination-only-unverified"}:
        raise Refusal("Metadata-only repair requires the project to exist at the destination.")
    repair_payload: dict[str, Any] | None = None
    if args.token is not None:
        repair_payload = decode_repair_apply_token(args.token)
        if repair_payload["old"] != str(old) or repair_payload["new"] != str(new):
            raise Refusal("The repair apply token names different paths.")
        assert_destination_binding(
            old,
            new,
            expected_state=repair_payload["filesystem_state"],
            expected_identity=repair_payload["destination_identity"],
            compatibility_link=True,
        )
    snapshot = metadata_snapshot(
        codex_home, str(old), str(new), explicit_state_db=explicit_db
    )
    if args.token is None:
        if not snapshot["repair_supported"]:
            status = "blocked"
        elif not snapshot["changes"]:
            status = "clean"
        else:
            status = "repair-ready"
        repair_apply_token = (
            repair_apply_token_for(
                old,
                new,
                filesystem,
                snapshot["repair_token"],
            )
            if status == "repair-ready"
            else None
        )
        return emit(
            {
                "command": "repair",
                "status": status,
                "old": str(old),
                "new": str(new),
                "metadata": snapshot,
                "repair_apply_token": repair_apply_token,
                "next": (
                    "Resolve every metadata audit or schema error before repair."
                    if status == "blocked"
                    else (
                        "No structured metadata repair is needed."
                        if status == "clean"
                        else "Quit Codex, rerun repair from Terminal, and pass its fresh repair_apply_token with --token."
                    )
                ),
            }
        )
    if repair_payload is None:
        raise Refusal("A repair apply token is required for metadata mutation.")
    if repair_payload["metadata_token"] != snapshot["repair_token"]:
        if (
            snapshot["repair_supported"]
            and not snapshot["changes"]
            and completed_backup_exists(
                codex_home,
                repair_payload["metadata_token"],
                str(old),
                str(new),
            )
        ):
            return emit(
                {
                    "command": "repair",
                    "status": "already-complete",
                    "old": str(old),
                    "new": str(new),
                    "metadata": {
                        "status": "clean",
                        "backup": None,
                        "changes_applied": 0,
                    },
                    "next": "No changes were made; the exact completed repair token was replayed.",
                }
            )
        raise Refusal("Metadata changed after planning; create a fresh repair plan.")
    prepared = prepare_metadata_backup(snapshot, codex_home, str(old), str(new))

    def destination_guard() -> None:
        assert_destination_binding(
            old,
            new,
            expected_state=repair_payload["filesystem_state"],
            expected_identity=repair_payload["destination_identity"],
            compatibility_link=True,
        )

    try:
        destination_guard()
    except Exception:
        discard_backup(prepared[0] if prepared is not None else None)
        raise
    try:
        result = apply_metadata_snapshot(
            snapshot,
            codex_home,
            str(old),
            str(new),
            prepared,
            destination_guard,
        )
        destination_guard()
    except PartialFailure:
        raise
    except Refusal as exc:
        raise PartialFailure(
            "Metadata repair completed, but the approved destination changed before completion.",
            details={
                "backup": str(prepared[0]) if prepared is not None else None,
                "error": str(exc),
            },
        ) from exc
    return emit(
        {
            "command": "repair",
            "status": "completed",
            "old": str(old),
            "new": str(new),
            "metadata": result,
            "next": "Restart Codex and run verify.",
        }
    )


def verify_command(args: argparse.Namespace) -> int:
    old = normalize_path(args.old)
    new = normalize_path(args.new)
    codex_home = normalize_path(args.codex_home)
    explicit_db = normalize_path(args.state_db) if args.state_db else None
    validate_static_paths(old, new, codex_home)
    filesystem = inspect_filesystem(old, new, compatibility_link=not args.without_link)
    metadata = metadata_snapshot(
        codex_home, str(old), str(new), explicit_state_db=explicit_db
    )
    identity_verified: bool | None = None
    if args.token:
        token = decode_apply_token(args.token)
        if token.get("old") != str(old) or token.get("new") != str(new):
            raise Refusal("The verification token names different paths.")
        if token["compatibility_link"] != (not args.without_link):
            raise Refusal("The verification token uses a different compatibility-link policy.")
        if path_kind(new) == "directory":
            identity_verified = same_object_identity(new, token["source_identity"])
        else:
            identity_verified = False
    filesystem_complete = (
        filesystem["state"] == "destination-only-unverified"
        if args.without_link
        else filesystem["state"] == "linked"
    )
    metadata_clean = (
        metadata["audit_complete"]
        and metadata["repair_supported"]
        and not metadata["changes"]
    )
    database_integrity: str | None = None
    if metadata.get("state_db"):
        connection: sqlite3.Connection | None = None
        try:
            with sqlite_read_snapshot(Path(metadata["state_db"])) as snapshot_path:
                uri = snapshot_path.resolve().as_uri() + "?mode=ro"
                connection = sqlite3.connect(uri, uri=True)
                try:
                    row = connection.execute("PRAGMA quick_check").fetchone()
                    database_integrity = row[0] if row else None
                finally:
                    connection.close()
                    connection = None
        except sqlite3.DatabaseError as exc:
            database_integrity = f"error: {exc}"
        finally:
            if connection is not None:
                connection.close()
    token_identity_ok = identity_verified is not False
    success = (
        filesystem_complete
        and metadata_clean
        and database_integrity == "ok"
        and token_identity_ok
    )
    return emit(
        {
            "command": "verify",
            "status": "verified" if success else "verification-failed",
            "old": str(old),
            "new": str(new),
            "filesystem": filesystem,
            "identity_verified_by_token": identity_verified,
            "metadata": metadata,
            "database_quick_check": database_integrity,
            "ui_state": "Restart Codex and confirm the new project and its tasks are visible; disk checks cannot prove UI reload.",
        },
        EXIT_OK if success else EXIT_VERIFY_FAILED,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely relocate a local Codex project and its structured path metadata."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def common(name: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name)
        sub.add_argument("old", help="Literal old absolute project path")
        sub.add_argument("new", help="Literal new absolute project path")
        sub.add_argument(
            "--codex-home",
            default=str(Path.home() / ".codex"),
            help="Codex data directory (default: ~/.codex)",
        )
        sub.add_argument("--state-db", help="Explicit active state_5.sqlite path")
        return sub

    plan = common("plan")
    plan.add_argument("--without-link", action="store_true", help="Do not preserve the old path")
    plan.set_defaults(handler=plan_command)

    apply = common("apply")
    apply.add_argument("--token", required=True, help="Fresh apply_token emitted by plan")
    apply.add_argument("--without-link", action="store_true", help="Must match the plan")
    apply.set_defaults(handler=apply_command)

    repair = common("repair")
    repair.add_argument(
        "--token",
        help="Fresh repair_apply_token; omit for a read-only repair plan",
    )
    repair.set_defaults(handler=repair_command)

    verify = common("verify")
    verify.add_argument("--token", help="Optional apply_token for directory identity verification")
    verify.add_argument("--without-link", action="store_true", help="Expect no compatibility link")
    verify.set_defaults(handler=verify_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except PartialFailure as exc:
        return emit(
            {
                "command": args.command,
                "status": "partial-recoverable",
                "error": str(exc),
                "details": exc.details,
            },
            EXIT_PARTIAL,
        )
    except Refusal as exc:
        return emit(
            {
                "command": args.command,
                "status": "unsafe-refused",
                "error": str(exc),
                "details": exc.details,
            },
            EXIT_UNSAFE,
        )
    except KeyboardInterrupt:
        return emit(
            {
                "command": args.command,
                "status": "interrupted",
                "error": "Interrupted; run verify before retrying.",
            },
            EXIT_PARTIAL,
        )
    except Exception as exc:
        return emit(
            {
                "command": args.command,
                "status": "partial-recoverable",
                "error": "An unexpected failure occurred; run verify before retrying.",
                "details": {"exception": type(exc).__name__, "message": str(exc)},
            },
            EXIT_PARTIAL,
        )


if __name__ == "__main__":
    raise SystemExit(main())

"""Filesystem-only relocation primitives. Never read or write Codex stores."""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import stat
import sys
import unicodedata
from typing import Any

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
            "The current environment cannot write the destination parent; apply needs an environment with that write access."
        )
    if not os.access(old.parent, os.W_OK | os.X_OK):
        warnings.append(
            "The current environment cannot write the source parent; apply needs an environment with that write access."
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


def stat_matches_identity(info: os.stat_result, expected: dict[str, Any]) -> bool:
    return all(
        int(getattr(info, field)) == int(expected[key])
        for field, key in (("st_dev", "device"), ("st_ino", "inode"), ("st_mtime_ns", "mtime_ns"))
    )


def stat_matches_object(info: os.stat_result, expected: dict[str, Any]) -> bool:
    return int(info.st_dev) == int(expected["device"]) and int(info.st_ino) == int(
        expected["inode"]
    )


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
        if not stat_matches_object(os.fstat(source_fd), source_parent_identity):
            raise Refusal("The source parent changed before the atomic rename.")
        if not stat_matches_object(os.fstat(destination_fd), destination_parent_identity):
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
    if not same_object_identity(old.parent, token_payload["source_parent_identity"]):
        raise Refusal("The source parent identity changed after planning.")
    if not same_object_identity(new.parent, token_payload["destination_parent_identity"]):
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


def parent_traversal_after_link(link: Path, target: Path, old: Path) -> bool:
    """Reject '..' after a symlink, including the future old-path alias.

    Lexical normalization cannot model those targets. Ordinary relative
    links such as '../sibling' remain supported when no symlink is crossed.
    """
    node = Path(target.anchor) if target.is_absolute() else link.parent
    crossed_link = False
    for part in target.parts[1:] if target.is_absolute() else target.parts:
        if part == "..":
            if crossed_link:
                return True
            node = node.parent
        else:
            node = node / part
            if node.is_symlink() or (target.is_absolute() and node == old):
                crossed_link = True
    return False


def tree_risks(old: Path, new: Path) -> list[dict[str, str]]:
    """Describe links/Git relationships a directory rename does not repair."""
    risks: list[dict[str, str]] = []

    def walk_error(error: OSError) -> None:
        raise Refusal(f"Cannot inspect the source tree: {error}") from error

    for current, directories, files in os.walk(old, followlinks=False, onerror=walk_error):
        parent = Path(current)
        # A .git directory can own worktrees outside the directory being moved.
        if ".git" in directories:
            git = parent / ".git"
            if git.is_symlink():
                risks.append({"kind": "git-linked-worktree", "path": str(git)})
            elif (git / "worktrees").is_dir() and any((git / "worktrees").iterdir()):
                risks.append({"kind": "git-external-worktrees", "path": str(git)})
            directories.remove(".git")
        if ".git" in files:
            risks.append({"kind": "git-linked-worktree", "path": str(parent / ".git")})
        for name in directories + files:
            link = parent / name
            if not link.is_symlink():
                continue
            raw = os.readlink(link)
            target = Path(raw)
            moved_link = new / link.relative_to(old)
            if parent_traversal_after_link(link, target, old):
                risks.append({"kind": "symlink-parent-traversal", "path": str(link), "target": raw})
                continue
            if target.is_absolute():
                if any(rewrite_structured_path(p, str(old), str(new)) is not None
                       for p in (str(target), os.path.realpath(target))):
                    risks.append({"kind": "absolute-link-needs-compatibility", "path": str(link), "target": raw})
                continue
            before = Path(os.path.abspath(link.parent / target))
            after = Path(os.path.abspath(moved_link.parent / target))
            expected = Path(rewrite_structured_path(str(before), str(old), str(new)) or str(before))
            if after != expected:
                risks.append({"kind": "relative-link-changes-target", "path": str(link), "target": raw,
                              "before": str(before), "after": str(after)})
    return risks

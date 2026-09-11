"""Exercise directory relocation only, without opening a real Codex store."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "relocate-codex-project" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import _filesystem as fs


class FilesystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="codex-relocate-filesystem-", dir="/private/tmp"
        )
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.codex_home = self.root / "unused-codex-home"
        self.old, self.new = self.make_tree("default")

    def make_tree(self, name: str, *, rename: bool = True, move: bool = True):
        base = self.root / name
        source_parent = base / "from"
        destination_parent = base / "to" if move else source_parent
        source_parent.mkdir(parents=True)
        destination_parent.mkdir(parents=True, exist_ok=True)
        old = source_parent / "Project 旧"
        new = destination_parent / ("Renamed 新" if rename else old.name)
        old.mkdir()
        (old / "nested").mkdir()
        (old / "nested" / "payload.bin").write_bytes(b"project\x00contents\xff\n")
        (old / "nested" / "payload.bin").chmod(0o640)
        (old / ".hidden").write_text("keep hidden files", encoding="utf-8")
        (old / "relative-link").symlink_to("nested/payload.bin")
        return old, new

    def plan(self, old: Path | None = None, new: Path | None = None, *, link=True):
        old = old or self.old
        new = new or self.new
        fs.validate_static_paths(old, new, self.codex_home)
        return fs.inspect_filesystem(old, new, compatibility_link=link)

    def apply(self, snapshot, old: Path | None = None, new: Path | None = None, *, link=True):
        return fs.apply_filesystem(
            old or self.old, new or self.new, snapshot, compatibility_link=link
        )

    def test_rename_move_and_combined_relocation_preserve_tree(self) -> None:
        for label, rename, move in (
            ("rename", True, False),
            ("move", False, True),
            ("rename-and-move", True, True),
        ):
            with self.subTest(operation=label):
                old, new = self.make_tree(label, rename=rename, move=move)
                directory_identity = fs.identity(old)
                original_file = (old / "nested" / "payload.bin").stat()
                snapshot = self.plan(old, new)
                result = self.apply(snapshot, old, new)

                self.assertEqual(result["status"], "moved")
                self.assertTrue(fs.same_object_identity(new, directory_identity))
                moved_file = (new / "nested" / "payload.bin").stat()
                self.assertEqual(
                    (moved_file.st_dev, moved_file.st_ino, moved_file.st_mode),
                    (original_file.st_dev, original_file.st_ino, original_file.st_mode),
                )
                self.assertEqual((new / "relative-link").read_bytes(), b"project\x00contents\xff\n")
                self.assertEqual(os.readlink(new / "relative-link"), "nested/payload.bin")
                self.assertEqual((new / ".hidden").read_text(), "keep hidden files")
                self.assertTrue(old.is_symlink())
                self.assertEqual(fs.resolved_symlink_target(old), new)
                self.assertEqual((old / "relative-link").read_bytes(), b"project\x00contents\xff\n")

    def test_linkless_move_is_idempotent(self) -> None:
        snapshot = self.plan(link=False)
        self.assertEqual(self.apply(snapshot, link=False)["status"], "moved")
        moved_identity = fs.identity(self.new)

        self.assertEqual(self.apply(snapshot, link=False)["status"], "already-complete")
        self.assertEqual(fs.path_kind(self.old), "absent")
        self.assertEqual(fs.identity(self.new), moved_identity)

    def test_resume_after_move_before_link_uses_original_identity(self) -> None:
        snapshot = self.plan()
        self.old.rename(self.new)
        self.assertEqual(self.plan()["state"], "destination-only-unverified")

        self.assertEqual(self.apply(snapshot)["status"], "resumed")
        self.assertEqual(fs.resolved_symlink_target(self.old), self.new)
        self.assertTrue(fs.same_object_identity(self.new, snapshot["source_identity"]))
        self.assertEqual(self.apply(snapshot)["status"], "already-complete")

    def test_occupied_destination_is_never_overwritten(self) -> None:
        snapshot = self.plan()
        self.new.mkdir()
        (self.new / "keep.txt").write_text("unrelated", encoding="utf-8")
        destination_identity = fs.identity(self.new)

        with self.assertRaises(fs.Refusal):
            self.plan()
        with self.assertRaises(fs.Refusal):
            self.apply(snapshot)
        # Give the primitive current identities to exercise the OS no-replace
        # operation itself, after the higher-level stale-plan checks above.
        with self.assertRaises(OSError) as failure:
            fs.atomic_exclusive_rename(
                self.old,
                self.new,
                source_identity=fs.identity(self.old),
                source_parent_identity=fs.identity(self.old.parent),
                destination_parent_identity=fs.identity(self.new.parent),
            )
        self.assertIn(failure.exception.errno, (errno.EEXIST, errno.ENOTEMPTY))
        self.assertTrue(fs.same_object_identity(self.new, destination_identity))
        self.assertEqual((self.new / "keep.txt").read_text(), "unrelated")
        self.assertTrue(self.old.is_dir())

    def test_stale_source_or_parent_refuses_before_move(self) -> None:
        for changed in ("source", "source-parent", "destination-parent"):
            with self.subTest(changed=changed):
                old, new = self.make_tree(changed)
                snapshot = self.plan(old, new)
                path = {
                    "source": old,
                    "source-parent": old.parent,
                    "destination-parent": new.parent,
                }[changed]
                if changed == "source":
                    info = path.stat()
                    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                else:
                    parked = path.with_name(path.name + "-original")
                    path.rename(parked)
                    path.mkdir()
                    if changed == "source-parent":
                        (parked / old.name).rename(old)
                with self.assertRaises(fs.Refusal):
                    self.apply(snapshot, old, new)
                self.assertTrue(old.is_dir())
                self.assertFalse(new.exists())

    def test_replaced_destination_and_wrong_link_refuse_recovery(self) -> None:
        for changed in ("destination", "link"):
            with self.subTest(changed=changed):
                old, new = self.make_tree(changed)
                snapshot = self.plan(old, new)
                old.rename(new)
                if changed == "destination":
                    new.rename(new.with_name("original-directory"))
                    new.mkdir()
                else:
                    unrelated = new.parent / "unrelated"
                    unrelated.mkdir()
                    old.symlink_to(unrelated, target_is_directory=True)
                with self.assertRaises(fs.Refusal):
                    self.apply(snapshot, old, new)
                self.assertTrue(new.is_dir())
                if changed == "link":
                    self.assertEqual(fs.resolved_symlink_target(old), unrelated)
                else:
                    self.assertEqual(fs.path_kind(old), "absent")

    def test_symlink_audit_distinguishes_targets_affected_by_move(self) -> None:
        (self.old.parent / "shared").write_text("external", encoding="utf-8")
        (self.old / "relative-external").symlink_to("../shared")
        (self.old / "absolute-internal").symlink_to(self.old / "nested" / "payload.bin")
        (self.old / "absolute-external").symlink_to(self.old.parent / "shared")
        before = fs.identity(self.old)

        risks = fs.tree_risks(self.old, self.new)

        self.assertCountEqual(
            [risk["kind"] for risk in risks],
            ["relative-link-changes-target", "absolute-link-needs-compatibility"],
        )
        self.assertEqual(fs.identity(self.old), before)
        self.assertFalse(self.new.exists())

    def test_git_audit_catches_linked_and_external_worktrees(self) -> None:
        (self.old / ".git").write_text("gitdir: /outside/main/.git/worktrees/linked\n")
        self.assertIn(
            "git-linked-worktree",
            [risk["kind"] for risk in fs.tree_risks(self.old, self.new)],
        )
        with self.assertRaises(fs.Refusal):
            self.plan()

        (self.old / ".git").unlink()
        registry = self.old / ".git" / "worktrees" / "external"
        registry.mkdir(parents=True)
        (registry / "gitdir").write_text("/outside/external/.git\n")
        self.assertIn(
            "git-external-worktrees",
            [risk["kind"] for risk in fs.tree_risks(self.old, self.new)],
        )
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_unsupported_paths_refuse_without_mutation(self) -> None:
        with self.subTest(operation="case-only"):
            with self.assertRaises(fs.Refusal):
                self.plan(self.old, self.old.with_name("project 旧"))
        with self.subTest(operation="nested-destination"):
            with self.assertRaises(fs.Refusal):
                self.plan(self.old, self.old / "nested-destination")
        with self.subTest(operation="cross-volume"):
            actual_identity = fs.identity

            def other_device(path: Path, **kwargs):
                value = actual_identity(path, **kwargs)
                if path == self.new.parent:
                    value["device"] += 1
                return value

            with mock.patch.object(fs, "identity", side_effect=other_device):
                with self.assertRaises(fs.Refusal):
                    self.plan()
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_parent_sync_failure_can_resume_without_moving_twice(self) -> None:
        snapshot = self.plan()
        with mock.patch.object(fs.os, "fsync", side_effect=OSError("sync failed")):
            with self.assertRaises(fs.PartialFailure):
                self.apply(snapshot)
        self.assertEqual(fs.path_kind(self.old), "absent")
        self.assertTrue(fs.same_object_identity(self.new, snapshot["source_identity"]))

        self.assertEqual(self.apply(snapshot)["status"], "resumed")
        self.assertEqual(fs.resolved_symlink_target(self.old), self.new)
        self.assertTrue(fs.same_object_identity(self.new, snapshot["source_identity"]))


if __name__ == "__main__":
    unittest.main()

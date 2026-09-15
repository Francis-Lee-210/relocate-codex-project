"""Relocation link regressions against isolated filesystem and Codex fixtures."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from unittest import mock

from test_relocate import RelocateFixture, filesystem, relocate


class SymlinkAuditTests(RelocateFixture):
    def differing_external_files(self) -> None:
        (self.old.parent / "settings.txt").write_text("original settings")
        (self.new.parent / "settings.txt").write_text("different settings")

    def assert_blocked(self, link: Path) -> None:
        plan = relocate.make_plan(self.old, self.new, self.codex_home)
        self.assertEqual(plan["status"], "blocked", plan["tree_risks"])
        self.assertTrue(any(r["path"] == str(link) and
                            r["kind"] != "absolute-link-needs-compatibility"
                            for r in plan["tree_risks"]), plan["tree_risks"])
        self.assertFalse(self.new.exists())
        self.assertFalse(self.old.is_symlink())

    def test_relative_reentry_and_external_alias_chains_are_blocked(self) -> None:
        self.differing_external_files()
        alias = self.root / "external-alias"
        alias.symlink_to(str(self.old) + "/../settings.txt")
        second = self.root / "second-alias"
        second.symlink_to("external-alias")
        link = self.old / "settings-link"
        for target in ("../../from/旧 Project/../settings.txt", str(alias), str(second)):
            with self.subTest(target=target):
                link.symlink_to(target)
                try:
                    self.assertEqual(link.read_text(), "original settings")
                    self.assert_blocked(link)
                finally:
                    link.unlink()

    def test_git_hooks_config_and_nested_links_are_audited(self) -> None:
        for parent in (self.old.parent, self.new.parent):
            (parent / "shared-hooks").mkdir()
            (parent / "shared-hooks" / "pre-commit").write_text(parent.name)
        self.differing_external_files()
        for relative, target in ((".git/hooks", "shared-hooks"),
                                 (".git/config", "settings.txt"),
                                 (".git/nested/config", "settings.txt")):
            with self.subTest(relative=relative):
                link = self.old / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(os.path.relpath(self.old.parent / target, link.parent))
                try:
                    self.assert_blocked(link)
                finally:
                    link.unlink()

    def test_old_ready_plans_cannot_bypass_apply_or_initial_recovery(self) -> None:
        self.differing_external_files()
        for relative, target in (("settings-link", "../../from/旧 Project/../settings.txt"),
                                 (".git/config", "../../settings.txt")):
            with self.subTest(relative=relative):
                link = self.old / relative
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
                # A checksummed ready plan produced by the earlier, incomplete audit.
                with mock.patch.object(relocate, "tree_risks", return_value=[]):
                    self.save_plan()
                before = self.store_snapshot()
                for recovering in (False, True):
                    with self.assertRaises(filesystem.Refusal):
                        self.apply_quiet(recovering=recovering)
                    self.assertTrue(self.old.is_dir())
                    self.assertFalse(self.new.exists())
                    self.assertEqual(self.store_snapshot(), before)
                link.unlink()

    def test_changed_external_alias_is_rechecked_before_apply(self) -> None:
        self.differing_external_files()
        alias = self.root / "external-alias"
        alias.symlink_to(self.old / "payload.txt")
        (self.old / "settings-link").symlink_to(alias)
        self.save_plan()
        alias.unlink()
        alias.symlink_to(str(self.old) + "/../settings.txt")
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet()
        self.assertFalse(self.new.exists())

    def test_destination_only_recovery_rechecks_links_before_creating_alias(self) -> None:
        self.differing_external_files()
        alias = self.root / "external-alias"
        alias.symlink_to(self.old / "payload.txt")
        (self.old / ".git").mkdir()
        (self.old / ".git" / "config").symlink_to(alias)
        self.save_plan()
        self.old.rename(self.new)  # Simulate interruption before compatibility-link creation.
        alias.unlink()
        alias.symlink_to(str(self.old) + "/../settings.txt")
        before = self.store_snapshot()
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet(recovering=True)
        self.assertFalse(os.path.lexists(self.old))
        self.assertTrue(self.new.is_dir())
        self.assertEqual(self.store_snapshot(), before)

    def assert_recovery_preserves_paths_on_audit_drift(self) -> None:
        before = self.store_snapshot()
        plan_bytes = self.plan_path.read_bytes()
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet(recovering=True)
        self.assertFalse(os.path.lexists(self.old))
        self.assertTrue(self.new.is_dir())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.plan_path.read_bytes(), plan_bytes)
        self.assertEqual(self.store_snapshot(), before)

    def test_recovery_refuses_changed_compatible_target(self) -> None:
        (self.old / "config-link").symlink_to(self.old / "payload.txt")
        self.save_plan()
        self.old.rename(self.new)
        (self.new / "config-link").unlink()
        (self.new / "config-link").symlink_to(self.old / "alternate")
        self.assert_recovery_preserves_paths_on_audit_drift()

    def test_recovery_refuses_added_compatibility_dependency(self) -> None:
        self.save_plan()
        self.old.rename(self.new)
        (self.new / "config-link").symlink_to(self.old / "payload.txt")
        self.assert_recovery_preserves_paths_on_audit_drift()

    def test_recovery_refuses_removed_compatibility_dependency(self) -> None:
        (self.old / "config-link").symlink_to(self.old / "payload.txt")
        self.save_plan()
        self.old.rename(self.new)
        (self.new / "config-link").unlink()
        self.assert_recovery_preserves_paths_on_audit_drift()

    def test_recovery_refuses_retargeted_external_compatibility_alias(self) -> None:
        (self.old / "alternate.txt").write_text("different contents")
        alias = self.root / "external-alias"
        alias.symlink_to(self.old / "payload.txt")
        (self.old / "config-link").symlink_to(alias)
        self.save_plan()
        self.old.rename(self.new)
        alias.unlink()
        alias.symlink_to(self.old / "alternate.txt")
        self.assert_recovery_preserves_paths_on_audit_drift()

    def test_older_plan_without_resolved_targets_requires_current_evidence(self) -> None:
        (self.old / "config-link").symlink_to(self.old / "payload.txt")
        legacy = self.save_plan()
        current_plan_bytes = self.plan_path.read_bytes()
        for risk in legacy["tree_risks"]:
            risk.pop("before", None)
            risk.pop("after", None)
        legacy.pop("plan_id")
        legacy["plan_id"] = relocate.digest(legacy)
        self.plan_path.write_bytes(relocate.canonical_json(legacy) + b"\n")
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet()
        self.assertFalse(self.new.exists())
        self.old.rename(self.new)
        self.assert_recovery_preserves_paths_on_audit_drift()
        # Restore evidence saved before the move, never infer historical targets.
        self.plan_path.write_bytes(current_plan_bytes)
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "resumed")

    def test_verify_and_linked_recovery_report_saved_audit_drift(self) -> None:
        plan = self.moved_plan()
        self.native_edit_projects()
        (self.new / "config-link").symlink_to(self.old / "payload.txt")
        before = self.store_snapshot()
        receipt_bytes = self.receipt.read_bytes()
        result = relocate.inspect_plan(plan)
        self.assertEqual(result["status"], "verification-pending")
        self.assertTrue(result["audit"]["conflicts"])
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet(recovering=True)
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.receipt.read_bytes(), receipt_bytes)
        self.assertEqual(self.store_snapshot(), before)
        (self.new / "config-link").unlink()
        self.assertEqual(relocate.inspect_plan(plan)["status"], "ready-for-runtime-check")
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "already-complete")

    def test_recovery_accepts_unchanged_audit_with_different_entry_order(self) -> None:
        (self.old / "a-file-link").symlink_to(self.old / "payload.txt")
        (self.old / "z-directory-link").symlink_to(self.old / "src", target_is_directory=True)
        plan = self.save_plan()
        self.old.rename(self.new)
        current = filesystem.tree_risks(self.old, self.new, tree_root=self.new)
        self.assertNotEqual(current, plan["tree_risks"])
        before = self.store_snapshot()
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "resumed")
        self.assertEqual((self.new / "a-file-link").read_bytes(), b"keep project contents\n")
        self.assertTrue((self.new / "z-directory-link").samefile(self.new / "src"))
        self.assertEqual(self.store_snapshot(), before)
        self.native_edit_projects()
        after_native_edit = self.store_snapshot()
        self.assertEqual(relocate.inspect_plan(plan)["status"], "ready-for-runtime-check")
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "already-complete")
        self.assertEqual(self.store_snapshot(), after_native_edit)

    def test_verify_and_linked_recovery_report_current_link_conflicts(self) -> None:
        self.differing_external_files()
        alias = self.root / "external-alias"
        alias.symlink_to(self.old / "payload.txt")
        (self.old / "settings-link").symlink_to(alias)
        plan = self.moved_plan()
        self.native_edit_projects()
        alias.unlink()
        alias.symlink_to(str(self.old) + "/../settings.txt")
        result = relocate.inspect_plan(plan)
        self.assertEqual(result["status"], "verification-pending")
        self.assertTrue(result["audit"]["conflicts"])
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet(recovering=True)
        self.assertTrue(self.old.is_symlink())

    def test_safe_links_keep_contents_through_move_and_recovery(self) -> None:
        shared_file = self.shared / "settings.txt"
        shared_file.write_text("stable external settings")
        (self.old / ".git").mkdir()
        (self.old / "src" / "sibling").symlink_to("../payload.txt")
        (self.old / "local-dir").symlink_to("src")
        (self.old / "through-dir").symlink_to("local-dir/../payload.txt")
        (self.old / "absolute").symlink_to(self.old / "payload.txt")
        (self.old / "relative-reentry").symlink_to("../../from/旧 Project/payload.txt")
        alias = self.root / "stable-alias"
        alias.symlink_to(shared_file)
        (self.old / ".git" / "config").symlink_to(alias)
        (self.old / "dangling").symlink_to("future-file")
        links = ("src/sibling", "through-dir", "absolute", "relative-reentry", ".git/config")
        before = {name: (self.old / name).read_bytes() for name in links}
        plan = self.save_plan()
        self.old.rename(self.new)
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "resumed")
        self.assertEqual({name: (self.new / name).read_bytes() for name in links}, before)
        self.assertFalse((self.new / "dangling").exists())
        self.native_edit_projects()
        self.assertEqual(relocate.inspect_plan(plan)["status"], "ready-for-runtime-check")
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "already-complete")

    def test_unresolvable_links_fail_closed(self) -> None:
        link = self.old / "broken-link"
        for target in ("broken-link", "missing/../payload.txt", "payload.txt/../payload.txt"):
            with self.subTest(target=target):
                link.symlink_to(target)
                try:
                    self.assert_blocked(link)
                finally:
                    link.unlink()

    def test_link_to_future_destination_cannot_change_from_missing_to_existing(self) -> None:
        link = self.old / "future-link"
        link.symlink_to(self.new / "payload.txt")
        self.assertFalse(link.exists())
        self.assert_blocked(link)

    def test_alternate_root_spelling_does_not_hide_compatibility_alias(self) -> None:
        self.differing_external_files()
        variant = self.old.with_name(self.old.name.swapcase())
        link = self.old / "case-alias"
        link.symlink_to(str(variant) + "/../settings.txt")
        self.assert_blocked(link)

    def test_compatibility_alias_cannot_exceed_the_platform_link_limit(self) -> None:
        limit = 32 if sys.platform == "darwin" else 40
        chain = [self.root / f"hop-{i}" for i in range(limit - 1)]
        for index, hop in enumerate(chain):
            hop.symlink_to(chain[index + 1] if index + 1 < len(chain) else self.old / "payload.txt")
        link = self.old / "long-chain"
        link.symlink_to(chain[0])
        self.assertEqual(link.read_bytes(), b"keep project contents\n")
        self.assert_blocked(link)

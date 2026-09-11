"""V2 relocation acceptance tests against isolated, modern Codex fixtures."""
from __future__ import annotations

from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "relocate-codex-project" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import _catalog as catalog
import _filesystem as filesystem
import relocate


class RelocateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="codex-relocate-v2-", dir="/private/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.old = self.root / "from" / "旧 Project"
        self.new = self.root / "to" / "New Project"
        self.old.mkdir(parents=True)
        self.new.parent.mkdir()
        (self.old / "src").mkdir()
        (self.old / "alternate").mkdir()
        (self.old / "payload.txt").write_bytes(b"keep project contents\n")
        self.shared = self.root / "shared-root"
        self.unrelated = self.root / "unrelated-root"
        self.shared.mkdir()
        self.unrelated.mkdir()
        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.state_db = self.codex_home / "state_5.sqlite"
        self.global_state = self.codex_home / ".codex-global-state.json"
        self.records = self.root / "records"
        self.records.mkdir()
        self.plan_path = self.records / "move.json"
        self.receipt = self.records / "move.receipt.json"
        self.desktop_id = "desktop-project-1"
        self.native_id = "native-project-1"
        self.sessions: dict[str, Path] = {}
        with closing(sqlite3.connect(self.state_db)) as db, db:
            db.executescript("""
                CREATE TABLE _sqlx_migrations(version INTEGER PRIMARY KEY, success INTEGER NOT NULL);
                INSERT INTO _sqlx_migrations VALUES(52, 1);
                CREATE TABLE projects(id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE project_roots(project_id TEXT, position INTEGER, path TEXT);
                CREATE TABLE threads(
                    id TEXT PRIMARY KEY, cwd TEXT NOT NULL, rollout_path TEXT NOT NULL,
                    sandbox_policy TEXT NOT NULL, project_id TEXT, archived INTEGER NOT NULL,
                    name TEXT
                );
            """)
            db.executemany("INSERT INTO projects VALUES(?,?)", [
                (self.native_id, "Project"), ("native-other", "Other")])
            db.executemany("INSERT INTO project_roots VALUES(?,?,?)", [
                (self.native_id, 0, str(self.old)), (self.native_id, 1, str(self.shared)),
                ("native-other", 0, str(self.unrelated))])
        self.desktop = {
            "local-projects": {
                self.desktop_id: {"name": "Project", "rootPaths": [str(self.old), str(self.shared)]},
                "desktop-other": {"name": "Other", "rootPaths": [str(self.unrelated)]},
            },
            "app-server-project-id-by-legacy-project-id-by-host": {
                f"local:{self.codex_home}": {self.desktop_id: self.native_id, "desktop-other": "native-other"}
            },
            "thread-project-assignments": {},
            "thread-writable-roots": {"active": [str(self.old), str(self.shared)]},
            "thread-workspace-root-hints": {"active": str(self.old / "src")},
            "electron-persisted-atom-state": {"heartbeat-thread-permissions-by-id": {
                "active": {"sandboxPolicy": {"writableRoots": [str(self.old), str(self.shared)]}}
            }},
            "unrelated-ui-setting": {"keep": "original"},
        }
        self.add_thread("active", cwd=self.old / "src")
        self.add_thread("archived", cwd=self.old, archived=True)
        self.add_thread("unrelated", cwd=self.unrelated, affected=False)
        self.save_desktop()

    def policy(self, root: Path | None = None) -> dict:
        root = root or self.old
        return {
            "type": "managed",
            "file_system": {"type": "restricted", "entries": [
                {"access": "read", "path": {"type": "special", "value": "root"}},
                {"access": "write", "path": {"type": "path", "path": str(root)}},
                *[{"access": "read", "path": {"type": "path", "path": str(root / leaf)}}
                  for leaf in (".git", ".agents", ".codex")],
                {"access": "write", "path": {"type": "path", "path": str(self.shared)}},
            ]},
            "network": {"mode": "restricted"},
        }

    def add_thread(self, identifier: str, *, cwd: Path, archived=False, affected=True) -> None:
        session = self.codex_home / ("archived_sessions" if archived else "sessions") / "2026" / f"{identifier}.jsonl"
        session.parent.mkdir(parents=True, exist_ok=True)
        head = {"type": "session_meta", "payload": {"id": identifier, "cwd": str(cwd)}}
        history = (json.dumps(head) + "\n").encode() + (
            '{"type":"turn_context","payload":{"cwd":' + json.dumps(str(cwd)) + '}}\n'
            '{"type":"response_item","payload":{"content":"historical bytes stay unchanged"}}\n'
        ).encode()
        session.write_bytes(history)
        self.sessions[identifier] = session
        with closing(sqlite3.connect(self.state_db)) as db, db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (
                identifier, str(cwd), str(session), json.dumps(self.policy(self.old if affected else self.unrelated)),
                self.native_id if affected else "native-other", int(archived), identifier))
        self.desktop["thread-project-assignments"][identifier] = {
            "projectKind": "local", "projectId": self.desktop_id if affected else "desktop-other"}
        self.save_desktop()

    def save_desktop(self) -> None:
        self.global_state.write_text(json.dumps(self.desktop), encoding="utf-8")

    def query(self, sql: str, values=()) -> list:
        with closing(sqlite3.connect(self.state_db)) as db, db:
            return db.execute(sql, values).fetchall()

    def sql(self, sql: str, values=()) -> None:
        with closing(sqlite3.connect(self.state_db)) as db, db:
            db.execute(sql, values)

    def store_snapshot(self) -> dict:
        result = {}
        for path in self.codex_home.rglob("*"):
            if path.is_file():
                info = path.stat()
                result[str(path.relative_to(self.codex_home))] = (
                    info.st_ino, info.st_size, info.st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        return result

    def save_plan(self) -> dict:
        plan = relocate.make_plan(self.old, self.new, self.codex_home)
        self.assertEqual(plan["status"], "ready", plan.get("blockers"))
        self.plan_path.write_bytes(catalog.canonical_json(plan) + b"\n")
        return plan

    def apply_quiet(self, *, recovering=False) -> dict:
        with mock.patch.object(relocate, "require_quiet_tree"):
            return relocate.apply_plan(self.plan_path, recovering=recovering)

    def moved_plan(self) -> dict:
        plan = self.save_plan()
        self.apply_quiet()
        return plan

    def native_edit_projects(self, *, desktop=True, native=True) -> None:
        """Simulate the app's writes to this fixture; production never performs them."""
        self.desktop["local-projects"][self.desktop_id]["rootPaths"] = [
            str(self.new if desktop else self.old), str(self.shared)]
        self.save_desktop()
        self.sql("UPDATE project_roots SET path=? WHERE project_id=? AND position=0",
                 (str(self.new if native else self.old), self.native_id))

    def native_edit_thread_settings(self) -> None:
        for identifier, cwd in (("active", self.new / "src"), ("archived", self.new)):
            self.sql("UPDATE threads SET cwd=?, sandbox_policy=? WHERE id=?",
                     (str(cwd), json.dumps(self.policy(self.new)), identifier))
        self.desktop["thread-writable-roots"]["active"] = [str(self.new), str(self.shared)]
        self.desktop["thread-workspace-root-hints"]["active"] = str(self.new / "src")
        self.desktop["electron-persisted-atom-state"]["heartbeat-thread-permissions-by-id"]["active"]["sandboxPolicy"]["writableRoots"] = [str(self.new), str(self.shared)]
        self.save_desktop()

    def run_cli(self, *arguments: str) -> tuple[int, dict]:
        env = {**os.environ, "CODEX_HOME": str(self.codex_home), "PYTHONDONTWRITEBYTECODE": "1"}
        result = subprocess.run([sys.executable, str(SCRIPTS / "relocate.py"), *arguments],
                                cwd=self.records, env=env, text=True, capture_output=True, timeout=40)
        self.assertNotIn("Traceback", result.stderr, result.stderr)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)


class CatalogAndFlowTests(RelocateFixture):
    def test_read_only_plan_inventories_modern_ids_archives_and_permissions(self) -> None:
        before = self.store_snapshot()
        plan = self.save_plan()
        self.assertEqual(self.store_snapshot(), before)
        self.assertEqual(plan["catalog"]["schema_migration"], 52)
        self.assertEqual(plan["native_steps"], [{"project_id": self.desktop_id, "native_id": self.native_id,
            "before_roots": [str(self.old), str(self.shared)],
            "after_roots": [str(self.new), str(self.shared)]}])
        self.assertEqual({t["id"] for t in plan["catalog"]["threads"]}, {"active", "archived"})
        self.assertTrue(next(t for t in plan["catalog"]["threads"] if t["id"] == "archived")["archived"])
        self.assertEqual(plan["catalog"]["threads"][0]["policy"], self.policy())
        self.assertFalse(self.new.exists())

    def test_wal_snapshot_reads_committed_changes_without_touching_live_sidecars(self) -> None:
        connection = sqlite3.connect(self.state_db)
        self.addCleanup(connection.close)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        policy = self.policy()
        policy["wal_only_marker"] = "committed in WAL"
        connection.execute("UPDATE threads SET sandbox_policy=? WHERE id='active'", (json.dumps(policy),))
        connection.commit()
        self.assertGreater(Path(str(self.state_db) + "-wal").stat().st_size, 0)
        before = self.store_snapshot()

        plan = self.save_plan()

        active = next(t for t in plan["catalog"]["threads"] if t["id"] == "active")
        self.assertEqual(active["policy"]["wal_only_marker"], "committed in WAL")
        self.assertEqual(self.store_snapshot(), before)

    def test_cli_plan_apply_recover_and_verify_preserve_all_codex_stores(self) -> None:
        before = self.store_snapshot()
        project_inode = self.old.stat().st_ino
        code, plan = self.run_cli("plan", str(self.old), str(self.new), "--codex-home", str(self.codex_home))
        self.assertEqual(code, 0, plan)
        self.plan_path.write_bytes(catalog.canonical_json(plan) + b"\n")
        code, applied = self.run_cli("apply", "--plan", str(self.plan_path))
        self.assertEqual(code, 0, applied)
        self.assertEqual(applied["status"], "filesystem-moved-native-update-pending")
        self.assertEqual(self.new.stat().st_ino, project_inode)
        self.assertEqual((self.new / "payload.txt").read_bytes(), b"keep project contents\n")
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.store_snapshot(), before)
        code, pending = self.run_cli("verify", "--plan", str(self.plan_path))
        self.assertEqual(code, 4, pending)
        self.assertEqual(pending["status"], "verification-pending")
        code, recovered = self.run_cli("recover", "--plan", str(self.plan_path))
        self.assertEqual(code, 0, recovered)
        self.assertEqual(recovered["filesystem"]["status"], "already-complete")
        self.assertEqual(self.store_snapshot(), before)
        self.native_edit_projects()
        metadata_after_app = self.store_snapshot()
        code, verified = self.run_cli("verify", "--plan", str(self.plan_path))
        self.assertEqual(code, 0, verified)
        self.assertEqual(verified["status"], "ready-for-runtime-check")
        self.assertEqual({p["id"] for p in verified["audit"]["projects"]}, {self.desktop_id})
        self.assertEqual({t["id"] for t in verified["audit"]["threads"]}, {"active", "archived"})
        self.assertEqual({t["state"] for t in verified["audit"]["threads"]}, {"compatibility-dependent"})
        self.assertTrue(verified["keep_compatibility_link"])
        self.assertEqual(self.store_snapshot(), metadata_after_app)

    def test_one_sided_project_updates_remain_pending(self) -> None:
        plan = self.moved_plan()
        for desktop, native in ((True, False), (False, True)):
            with self.subTest(desktop=desktop, native=native):
                self.native_edit_projects(desktop=desktop, native=native)
                audit = relocate.inspect_plan(plan)
                self.assertEqual(audit["status"], "verification-pending")
                self.assertTrue(audit["audit"]["pending"])
                self.assertFalse(audit["audit"]["conflicts"])

    def test_direct_settings_preserve_history_but_do_not_certify_runtime(self) -> None:
        plan = self.moved_plan()
        histories = {key: path.read_bytes() for key, path in self.sessions.items()}
        self.native_edit_projects()
        self.native_edit_thread_settings()
        result = relocate.inspect_plan(plan)
        self.assertEqual(result["status"], "ready-for-runtime-check")
        self.assertEqual({t["state"] for t in result["audit"]["threads"]}, {"direct-settings"})
        self.assertEqual({t["runtime_check"] for t in result["audit"]["threads"]}, {"not-performed-by-helper"})
        self.assertEqual(result["audit"]["compatibility_dependencies"], [])
        self.assertTrue(result["keep_compatibility_link"])
        self.assertEqual({key: path.read_bytes() for key, path in self.sessions.items()}, histories)

    def test_permission_broadening_carveout_loss_and_root_loss_are_conflicts(self) -> None:
        plan = self.moved_plan()
        self.native_edit_projects()
        self.native_edit_thread_settings()
        for change in ("broaden", "remove-carveout", "remove-shared-root"):
            with self.subTest(change=change):
                policy = self.policy(self.new)
                entries = policy["file_system"]["entries"]
                if change == "broaden":
                    entries[2]["access"] = "write"
                elif change == "remove-carveout":
                    entries.pop(2)
                else:
                    entries.pop()
                self.sql("UPDATE threads SET sandbox_policy=? WHERE id='active'", (json.dumps(policy),))
                audit = relocate.inspect_plan(plan)
                self.assertEqual(audit["status"], "verification-pending")
                self.assertTrue(any("permissions" in c for c in audit["audit"]["conflicts"]))

    def test_project_root_order_loss_and_identity_changes_are_conflicts(self) -> None:
        plan = self.moved_plan()
        self.native_edit_projects()
        for roots in ([str(self.new)], [str(self.shared), str(self.new)]):
            with self.subTest(roots=roots):
                self.desktop["local-projects"][self.desktop_id]["rootPaths"] = roots
                self.save_desktop()
                self.assertTrue(relocate.inspect_plan(plan)["audit"]["conflicts"])
        self.native_edit_projects()
        mapping = self.desktop["app-server-project-id-by-legacy-project-id-by-host"][f"local:{self.codex_home}"]
        mapping[self.desktop_id] = "native-other"
        self.save_desktop()
        self.assertTrue(relocate.inspect_plan(plan)["audit"]["conflicts"])

    def test_missing_or_new_affected_tasks_are_conflicts(self) -> None:
        plan = self.moved_plan()
        self.native_edit_projects()
        self.add_thread("new-affected", cwd=self.new / "src")
        audit = relocate.inspect_plan(plan)
        self.assertTrue(any("New affected task" in c for c in audit["audit"]["conflicts"]))
        self.sql("DELETE FROM threads WHERE id='new-affected'")
        self.sql("DELETE FROM threads WHERE id='archived'")
        audit = relocate.inspect_plan(plan)
        self.assertTrue(any("archived" in c and "missing" in c for c in audit["audit"]["conflicts"]))

    def test_affected_changes_stale_plan_but_unrelated_changes_do_not(self) -> None:
        self.save_plan()
        self.sql("UPDATE threads SET cwd=? WHERE id='active'", (str(self.old / "alternate"),))
        before = self.store_snapshot()
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet()
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertEqual(self.store_snapshot(), before)
        self.sql("UPDATE threads SET cwd=? WHERE id='active'", (str(self.old / "src"),))
        self.add_thread("new-unrelated", cwd=self.unrelated, affected=False)
        self.desktop["unrelated-ui-setting"] = {"keep": "changed independently"}
        self.save_desktop()
        before = self.store_snapshot()
        self.assertEqual(self.apply_quiet()["status"], "filesystem-moved-native-update-pending")
        self.assertEqual(self.store_snapshot(), before)

    def test_metadata_change_during_writer_scan_refuses_before_rename(self) -> None:
        self.save_plan()

        def concurrent_change(_root):
            self.sql("UPDATE threads SET cwd=? WHERE id='active'", (str(self.old / "alternate"),))

        with mock.patch.object(relocate, "require_quiet_tree", side_effect=concurrent_change):
            with self.assertRaises((filesystem.Refusal, filesystem.PartialFailure)):
                relocate.apply_plan(self.plan_path)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.old.is_symlink())
        self.assertFalse(self.new.exists())

    def test_absolute_and_chained_parent_traversal_links_block_before_rename(self) -> None:
        (self.old.parent / "shared").mkdir()
        for target in (str(self.old) + "/../shared", "alias/../shared"):
            with self.subTest(target=target):
                if target.startswith("alias/"):
                    (self.old / "alias").symlink_to(self.old, target_is_directory=True)
                (self.old / "risky-link").symlink_to(target, target_is_directory=True)
                plan = relocate.make_plan(self.old, self.new, self.codex_home)
                self.assertEqual(plan["status"], "blocked")
                self.assertTrue(any(r["kind"] != "absolute-link-needs-compatibility" for r in plan["tree_risks"]))
                self.assertFalse(self.new.exists())
                (self.old / "risky-link").unlink()

    def test_external_alias_into_old_tree_remains_compatibility_dependency(self) -> None:
        alias = self.root / "external-alias"
        alias.symlink_to(self.old, target_is_directory=True)
        (self.old / "indirect-absolute-link").symlink_to(alias / "payload.txt")
        plan = self.moved_plan()
        self.native_edit_projects()
        self.native_edit_thread_settings()

        audit = relocate.inspect_plan(plan)

        self.assertEqual(audit["status"], "ready-for-runtime-check")
        self.assertTrue(any(item["owner"] == "project symlink"
                            for item in audit["audit"]["compatibility_dependencies"]))
        self.assertEqual((self.new / "indirect-absolute-link").read_bytes(), b"keep project contents\n")


class InvalidInputTests(RelocateFixture):
    def test_missing_native_mapping_and_collapsing_roots_block_planning(self) -> None:
        host_map = self.desktop["app-server-project-id-by-legacy-project-id-by-host"][f"local:{self.codex_home}"]
        del host_map[self.desktop_id]
        self.save_desktop()
        self.assertEqual(relocate.make_plan(self.old, self.new, self.codex_home)["status"], "blocked")
        host_map[self.desktop_id] = self.native_id
        self.desktop["local-projects"][self.desktop_id]["rootPaths"] = [str(self.old), str(self.new)]
        self.save_desktop()
        self.sql("UPDATE project_roots SET path=? WHERE project_id=? AND position=1", (str(self.new), self.native_id))
        plan = relocate.make_plan(self.old, self.new, self.codex_home)
        self.assertEqual(plan["status"], "blocked")
        self.assertTrue(any("collapse" in b for b in plan["blockers"]))

    def test_tampered_malformed_or_structurally_invalid_plans_fail_closed(self) -> None:
        plan = self.save_plan()
        tampered = copy.deepcopy(plan)
        tampered["new"] = str(self.root / "unapproved-destination")
        invalid_structure = copy.deepcopy(plan)
        invalid_structure["catalog"] = ["invalid-container"]
        invalid_structure.pop("plan_id")
        invalid_structure["plan_id"] = relocate.digest(invalid_structure)
        for raw in (b"{not-json", b"[]", catalog.canonical_json(tampered), catalog.canonical_json(invalid_structure)):
            with self.subTest(raw=raw[:45]):
                self.plan_path.write_bytes(raw)
                before = self.store_snapshot()
                code, result = self.run_cli("apply", "--plan", str(self.plan_path))
                self.assertEqual(code, 2, result)
                self.assertEqual(self.store_snapshot(), before)
                self.assertTrue(self.old.is_dir())
                self.assertFalse(self.new.exists())

    def test_symlinked_or_in_project_plan_is_refused(self) -> None:
        plan = self.save_plan()
        linked = self.records / "linked-plan.json"
        linked.symlink_to(self.plan_path)
        with self.assertRaises(filesystem.Refusal):
            relocate.load_plan(linked)
        inside = self.old / "plan.json"
        inside.write_bytes(catalog.canonical_json(plan))
        with self.assertRaises(filesystem.Refusal):
            relocate.load_plan(inside)
        self.assertFalse(self.new.exists())

    def test_missing_mismatched_external_and_symlinked_histories_are_refused(self) -> None:
        original = self.sessions["active"]
        original_bytes = original.read_bytes()
        outside = self.root / "external-history.jsonl"
        outside.write_bytes(original_bytes)
        for kind in ("missing", "wrong-id", "external", "symlink"):
            with self.subTest(kind=kind):
                if original.is_symlink():
                    original.unlink()
                original.write_bytes(original_bytes)
                self.sql("UPDATE threads SET rollout_path=? WHERE id='active'", (str(original),))
                if kind == "missing":
                    original.unlink()
                elif kind == "wrong-id":
                    original.write_text('{"type":"session_meta","payload":{"id":"other"}}\n')
                elif kind == "external":
                    self.sql("UPDATE threads SET rollout_path=? WHERE id='active'", (str(outside),))
                else:
                    original.unlink()
                    original.symlink_to(outside)
                with self.assertRaises(filesystem.Refusal):
                    relocate.make_plan(self.old, self.new, self.codex_home)
                self.assertTrue(self.old.is_dir())
                self.assertFalse(self.new.exists())

    def test_malformed_history_and_permission_shapes_return_structured_refusals(self) -> None:
        original = self.sessions["active"].read_bytes()
        for header in (b"[]\n", b'{"type":"session_meta","payload":[]}\n', b'{"type":"session_meta","payload":{"id":"active","id":"other"}}\n'):
            with self.subTest(header=header):
                self.sessions["active"].write_bytes(header)
                code, result = self.run_cli("plan", str(self.old), str(self.new), "--codex-home", str(self.codex_home))
                self.assertEqual(code, 2, result)
                self.assertFalse(self.new.exists())
        self.sessions["active"].write_bytes(original)
        for policy in ({"file_system": {"entries": ["invalid"]}}, {"file_system": {"entries": None}}):
            with self.subTest(policy=policy):
                self.sql("UPDATE threads SET sandbox_policy=? WHERE id='active'", (json.dumps(policy),))
                code, result = self.run_cli("plan", str(self.old), str(self.new), "--codex-home", str(self.codex_home))
                self.assertEqual(code, 2, result)
                self.assertFalse(self.new.exists())

    def test_unreadable_or_untrusted_catalog_is_never_repaired_by_helper(self) -> None:
        before = self.store_snapshot()
        self.global_state.write_text('{"local-projects":{},"local-projects":{}}')
        with self.assertRaises(filesystem.Refusal):
            relocate.make_plan(self.old, self.new, self.codex_home)
        self.save_desktop()
        external = self.root / "external-db.sqlite"
        self.state_db.rename(external)
        self.state_db.symlink_to(external)
        with self.assertRaises(filesystem.Refusal):
            relocate.make_plan(self.old, self.new, self.codex_home)
        self.assertEqual(hashlib.sha256(external.read_bytes()).hexdigest(), before["state_5.sqlite"][3])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())


class WriterAndRecoveryTests(RelocateFixture):
    def test_plan_durability_failure_stops_before_move(self) -> None:
        self.save_plan()
        before = self.store_snapshot()
        with mock.patch.object(relocate.os, "fsync", side_effect=OSError("plan sync failed")):
            with self.assertRaises(OSError):
                self.apply_quiet()
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.store_snapshot(), before)

    def test_plan_replacement_during_preparation_stops_before_move(self) -> None:
        self.save_plan()

        def replace_plan(_root):
            self.plan_path.write_text('{}')

        with mock.patch.object(relocate, "require_quiet_tree", side_effect=replace_plan):
            with self.assertRaises(filesystem.Refusal):
                relocate.apply_plan(self.plan_path)
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_writer_parser_includes_cwd_and_writable_handles_only(self) -> None:
        other_pid = os.getpid() + 10000
        output = (f"p{other_pid}\nceditor\nfcwd\nn{self.old}\nf8\naw\nn{self.old}/write file\n"
                  f"f9\nar\nn{self.old}/read file\nf10\nau\nn{self.old}/read-write\n"
                  f"p{os.getpid()}\ncself\nfcwd\nn{self.old}\nf7\naw\nn{self.old}/self-write\n")
        writers = relocate.parse_writers(output)
        self.assertEqual([(entry["pid"], entry["fd"]) for entry in writers],
                         [(other_pid, "cwd"), (other_pid, "8"), (other_pid, "10")])
        self.assertEqual(writers[1]["path"], str(self.old) + "/write file")

    def test_writer_scan_missing_tool_incomplete_scan_and_active_writer_refuse(self) -> None:
        for outcome in (FileNotFoundError("lsof missing"),
                        subprocess.CompletedProcess([], 1, "", "warning: incomplete"),
                        subprocess.CompletedProcess([], 0, f"p{os.getpid()+10000}\nceditor\nf4\naw\nn{self.old}/payload.txt\n", "")):
            with self.subTest(outcome=type(outcome).__name__):
                kwargs = {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
                with mock.patch.object(relocate.subprocess, "run", **kwargs):
                    with self.assertRaises(filesystem.Refusal):
                        relocate.require_quiet_tree(self.old)
        with mock.patch.object(relocate.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            relocate.require_quiet_tree(self.old)
        with mock.patch.object(relocate.Path, "cwd", return_value=self.old / "src"):
            with self.assertRaises(filesystem.Refusal):
                relocate.require_quiet_tree(self.old)

    def test_interruption_after_rename_recovers_original_directory_and_preserves_metadata(self) -> None:
        plan = self.save_plan()
        before = self.store_snapshot()
        real_link = filesystem.create_compatibility_link
        with mock.patch.object(filesystem, "create_compatibility_link", side_effect=OSError("interrupted before link")):
            with self.assertRaises(filesystem.PartialFailure):
                self.apply_quiet()
        self.assertEqual(filesystem.path_kind(self.old), "absent")
        self.assertTrue(filesystem.same_object_identity(self.new, plan["filesystem"]["source_identity"]))
        self.assertEqual(json.loads(self.receipt.read_text())["state"], "prepared")
        self.assertEqual(self.store_snapshot(), before)
        self.assertEqual(filesystem.create_compatibility_link, real_link)
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "resumed")
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.store_snapshot(), before)
        self.assertEqual(self.apply_quiet(recovering=True)["filesystem"]["status"], "already-complete")

    def test_recovery_wont_adopt_replacement_or_conflicting_receipt(self) -> None:
        plan = self.save_plan()
        self.receipt.write_text(json.dumps({"plan_id": "another-plan"}))
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet()
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.receipt.unlink()
        self.old.rename(self.new)
        parked = self.new.with_name("original-directory")
        self.new.rename(parked)
        self.new.mkdir()
        with self.assertRaises(filesystem.Refusal):
            self.apply_quiet(recovering=True)
        self.assertTrue(filesystem.same_object_identity(parked, plan["filesystem"]["source_identity"]))
        self.assertFalse(self.old.exists())


if __name__ == "__main__":
    unittest.main()

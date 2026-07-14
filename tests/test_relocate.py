from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "relocate-codex-project" / "scripts" / "relocate.py"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RelocateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.old = self.root / "旧 Project"
        self.new = self.root / "New Project"
        self.old.mkdir()
        (self.old / "payload.txt").write_text("unchanged\n", encoding="utf-8")

        self.codex_home = self.root / "codex-home"
        self.codex_home.mkdir()
        self.state_db = self.codex_home / "state_5.sqlite"
        self.session = (
            self.codex_home
            / "sessions"
            / "2026"
            / "07"
            / "14"
            / "rollout-test.jsonl"
        )
        self.session.parent.mkdir(parents=True)
        self.global_state = self.codex_home / ".codex-global-state.json"
        self._create_metadata(migration=40)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _create_metadata(self, *, migration: int) -> None:
        connection = sqlite3.connect(self.state_db)
        connection.executescript(
            """
            CREATE TABLE _sqlx_migrations (
                version INTEGER PRIMARY KEY,
                description TEXT NOT NULL,
                installed_on TEXT,
                success INTEGER NOT NULL,
                checksum BLOB,
                execution_time INTEGER
            );
            CREATE TABLE threads (
                id TEXT PRIMARY KEY,
                cwd TEXT NOT NULL,
                rollout_path TEXT NOT NULL,
                sandbox_policy TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO _sqlx_migrations(version, description, success) VALUES (?, 'fixture', 1)",
            (migration,),
        )
        policy = {
            "type": "managed",
            "file_system": {
                "type": "restricted",
                "entries": [
                    {
                        "path": {"type": "path", "path": str(self.old)},
                        "access": "write",
                    },
                    {
                        "path": {"type": "path", "path": str(self.old / ".git")},
                        "access": "read",
                    },
                    {
                        "path": {"type": "path", "path": str(self.root / "unrelated")},
                        "access": "read",
                    },
                ],
            },
            "network": "restricted",
        }
        connection.execute(
            "INSERT INTO threads(id, cwd, rollout_path, sandbox_policy) VALUES (?, ?, ?, ?)",
            ("thread-1", str(self.old), str(self.session), json.dumps(policy, ensure_ascii=False)),
        )
        connection.commit()
        connection.close()

        meta = {
            "timestamp": "2026-07-14T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "thread-1",
                "cwd": str(self.old),
                "source": "fixture",
            },
        }
        history = {
            "type": "response_item",
            "payload": {
                "type": "message",
                "content": f"Historical prose keeps {self.old} unchanged",
            },
        }
        self.history_line = json.dumps(history, ensure_ascii=False, separators=(",", ":")) + "\n"
        self.session.write_text(
            json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + self.history_line,
            encoding="utf-8",
        )

        state = {
            "electron-saved-workspace-roots": [str(self.old)],
            "active-workspace-roots": [str(self.old)],
            "thread-writable-roots": {
                "thread-1": [str(self.old), str(self.root / "extra")]
            },
            "prompt-history": {
                "thread-1": [f"Historical prose keeps {self.old} unchanged"]
            },
        }
        self.global_state.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def make_metadata_clean_for_destination(self) -> None:
        connection = sqlite3.connect(self.state_db)
        policy_text = connection.execute(
            "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()[0]
        policy = json.loads(policy_text)
        for entry in policy["file_system"]["entries"]:
            value = entry["path"].get("path")
            if isinstance(value, str) and value.startswith(str(self.old)):
                entry["path"]["path"] = str(self.new) + value[len(str(self.old)) :]
        connection.execute(
            "UPDATE threads SET cwd = ?, sandbox_policy = ? WHERE id = 'thread-1'",
            (
                str(self.new),
                json.dumps(policy, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        connection.commit()
        connection.close()

        lines = self.session.read_text(encoding="utf-8").splitlines(keepends=True)
        meta = json.loads(lines[0])
        meta["payload"]["cwd"] = str(self.new)
        self.session.write_text(
            json.dumps(meta, ensure_ascii=False, separators=(",", ":"))
            + "\n"
            + "".join(lines[1:]),
            encoding="utf-8",
        )
        state = json.loads(self.global_state.read_text(encoding="utf-8"))
        state["electron-saved-workspace-roots"] = [str(self.new)]
        state["active-workspace-roots"] = [str(self.new)]
        state["thread-writable-roots"]["thread-1"][0] = str(self.new)
        self.global_state.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def run_script(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], dict]:
        command = [
            sys.executable,
            str(SCRIPT),
            *arguments,
            "--codex-home",
            str(self.codex_home),
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"Invalid JSON output.\nstdout={result.stdout}\nstderr={result.stderr}\n{exc}")
        return result, payload

    def plan(self) -> tuple[subprocess.CompletedProcess[str], dict]:
        return self.run_script("plan", str(self.old), str(self.new))


class ReadOnlyAndSafetyTests(RelocateFixture):
    def test_plan_is_read_only_and_reports_structured_changes(self) -> None:
        before = {
            "db": digest(self.state_db),
            "session": digest(self.session),
            "global": digest(self.global_state),
            "payload": digest(self.old / "payload.txt"),
        }
        result, payload = self.plan()
        self.assertEqual(
            result.returncode,
            0,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(payload["status"], "ready")
        self.assertIsNotNone(payload["apply_token"])
        self.assertGreaterEqual(payload["metadata"]["change_count"], 7)
        self.assertEqual(before["db"], digest(self.state_db))
        self.assertEqual(before["session"], digest(self.session))
        self.assertEqual(before["global"], digest(self.global_state))
        self.assertEqual(before["payload"], digest(self.old / "payload.txt"))
        self.assertFalse((self.codex_home / "relocation-backups").exists())

    def test_existing_destination_is_refused_without_changes(self) -> None:
        self.new.mkdir()
        result, payload = self.plan()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertTrue(self.old.is_dir())
        self.assertTrue(self.new.is_dir())

    def test_nested_destination_is_refused(self) -> None:
        nested = self.old / "nested"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "plan",
                str(self.old),
                str(nested),
                "--codex-home",
                str(self.codex_home),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.old.is_dir())

    def test_stale_source_identity_is_refused(self) -> None:
        _, planned = self.plan()
        (self.old / "new-entry.txt").write_text("changes directory mtime", encoding="utf-8")
        result, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_case_only_destination_is_refused(self) -> None:
        case_alias = self.root / "旧 project"
        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "plan",
                str(self.old),
                str(case_alias),
                "--codex-home",
                str(self.codex_home),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertTrue(self.old.is_dir())

    def test_wrong_existing_symlink_is_refused(self) -> None:
        other = self.root / "other"
        other.mkdir()
        os.rename(self.old, self.new)
        os.symlink(str(other), str(self.old), target_is_directory=True)
        result, payload = self.run_script("repair", str(self.old), str(self.new))
        self.assertEqual(result.returncode, 2)
        self.assertIn("different target", payload["error"])

    def test_reserved_characters_in_codex_home_uri(self) -> None:
        special_home = self.root / "codex #home?"
        os.rename(self.codex_home, special_home)
        self.codex_home = special_home
        self.state_db = special_home / "state_5.sqlite"
        self.session = special_home / self.session.relative_to(self.root / "codex-home")
        self.global_state = special_home / ".codex-global-state.json"
        connection = sqlite3.connect(self.state_db)
        connection.execute(
            "UPDATE threads SET rollout_path = ? WHERE id = 'thread-1'",
            (str(self.session),),
        )
        connection.commit()
        connection.close()
        result, payload = self.plan()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "ready")

    def test_malformed_global_state_blocks_plan(self) -> None:
        encoded_old = json.dumps(str(self.old), ensure_ascii=False)
        self.global_state.write_text(f'{{"broken":{encoded_old}\n', encoding="utf-8")

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["apply_token"])
        self.assertFalse(payload["metadata"]["audit_complete"])
        self.assertFalse(payload["metadata"]["repair_supported"])

    def test_malformed_session_containing_old_path_blocks_plan(self) -> None:
        broken = self.session.with_name("rollout-broken.jsonl")
        encoded_old = json.dumps(str(self.old), ensure_ascii=False)
        broken.write_text(
            f'{{"type":"session_meta","payload":{{"cwd":{encoded_old}\n',
            encoding="utf-8",
        )

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["apply_token"])
        self.assertFalse(payload["metadata"]["audit_complete"])

    def test_malformed_session_without_old_path_is_only_a_warning(self) -> None:
        broken = self.session.with_name("rollout-unrelated-broken.jsonl")
        broken.write_text('{"unfinished":\n', encoding="utf-8")

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["metadata"]["audit_complete"])
        self.assertTrue(
            any(str(broken) in warning for warning in payload["metadata"]["warnings"])
        )

    def test_explicit_state_database_outside_codex_home_is_refused(self) -> None:
        outside = self.root / "external-state.sqlite"
        outside.write_bytes(self.state_db.read_bytes())

        result, payload = self.run_script(
            "plan",
            str(self.old),
            str(self.new),
            "--state-db",
            str(outside),
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("outside", payload["error"].lower())
        self.assertTrue(self.old.is_dir())

    def test_distinct_internal_explicit_state_database_is_refused(self) -> None:
        alternate = self.codex_home / "other.sqlite"
        alternate.write_bytes(self.state_db.read_bytes())

        result, payload = self.run_script(
            "plan",
            str(self.old),
            str(self.new),
            "--state-db",
            str(alternate),
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("canonical active", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_symlink_state_database_is_refused(self) -> None:
        real = self.codex_home / "real-state.sqlite"
        self.state_db.rename(real)
        self.state_db.symlink_to(real.name)

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("symlink", payload["error"].lower())
        self.assertTrue(real.is_file())

    def test_non_file_root_state_path_does_not_fall_back_to_legacy(self) -> None:
        legacy = self.codex_home / "sqlite" / "state_5.sqlite"
        legacy.parent.mkdir()
        self.state_db.rename(legacy)
        self.state_db.mkdir()

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("non-file node", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_rollback_journal_blocks_planning(self) -> None:
        rollback_journal = Path(str(self.state_db) + "-journal")
        rollback_journal.write_bytes(b"ambiguous rollback state")

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("rollback journal", payload["error"].lower())
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_oversized_session_meta_blocks_planning(self) -> None:
        self.session.write_bytes(b"{" + b" " * (8 * 1024 * 1024) + b"}")

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["apply_token"])
        self.assertIn("size limit", " ".join(payload["metadata"]["audit_errors"]))
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_unallowlisted_sandbox_path_is_refused(self) -> None:
        connection = sqlite3.connect(self.state_db)
        policy_text = connection.execute(
            "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()[0]
        policy = json.loads(policy_text)
        policy["network"] = {"unexpectedRoot": str(self.old)}
        connection.execute(
            "UPDATE threads SET sandbox_policy = ? WHERE id = 'thread-1'",
            (json.dumps(policy, ensure_ascii=False, separators=(",", ":")),),
        )
        connection.commit()
        connection.close()

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("outside the write allowlist", payload["error"])
        self.assertIn(
            "/network/unexpectedRoot",
            json.dumps(payload["details"], ensure_ascii=False),
        )

    def test_malformed_policy_container_cannot_hide_an_old_path(self) -> None:
        connection = sqlite3.connect(self.state_db)
        policy_text = connection.execute(
            "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()[0]
        policy = json.loads(policy_text)
        policy["file_system"]["entries"] = {
            "hidden": {"path": {"type": "path", "path": str(self.old)}}
        }
        connection.execute(
            "UPDATE threads SET sandbox_policy = ? WHERE id = 'thread-1'",
            (json.dumps(policy, ensure_ascii=False, separators=(",", ":")),),
        )
        connection.commit()
        connection.close()

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("outside the write allowlist", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_policy_object_key_cannot_hide_an_old_path(self) -> None:
        connection = sqlite3.connect(self.state_db)
        policy_text = connection.execute(
            "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()[0]
        policy = json.loads(policy_text)
        policy["future_path_map"] = {str(self.old): {"access": "write"}}
        connection.execute(
            "UPDATE threads SET sandbox_policy = ? WHERE id = 'thread-1'",
            (json.dumps(policy, ensure_ascii=False, separators=(",", ":")),),
        )
        connection.commit()
        connection.close()

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("outside the write allowlist", payload["error"])
        self.assertIn("#key", json.dumps(payload["details"]))
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_nonfinite_global_number_blocks_before_mutation(self) -> None:
        raw = self.global_state.read_text(encoding="utf-8")
        self.global_state.write_text(
            raw[:-1] + ',"overflow":1e999}',
            encoding="utf-8",
        )

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["apply_token"])
        self.assertIn("non-finite JSON number", " ".join(payload["metadata"]["audit_errors"]))
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_precision_losing_global_number_blocks_before_mutation(self) -> None:
        raw = self.global_state.read_text(encoding="utf-8")
        self.global_state.write_text(
            raw[:-1] + ',"precise":0.123456789012345678901234567890}',
            encoding="utf-8",
        )

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["apply_token"])
        self.assertIn("precision loss", " ".join(payload["metadata"]["audit_errors"]))
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_nonfinite_policy_number_is_refused_before_mutation(self) -> None:
        connection = sqlite3.connect(self.state_db)
        policy_text = connection.execute(
            "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE threads SET sandbox_policy = ? WHERE id = 'thread-1'",
            (policy_text[:-1] + ',"overflow":1e999}',),
        )
        connection.commit()
        connection.close()

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("non-finite JSON number", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_precision_losing_policy_number_is_refused_before_mutation(self) -> None:
        connection = sqlite3.connect(self.state_db)
        policy_text = connection.execute(
            "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()[0]
        connection.execute(
            "UPDATE threads SET sandbox_policy = ? WHERE id = 'thread-1'",
            (
                policy_text[:-1]
                + ',"precise":0.123456789012345678901234567890}',
            ),
        )
        connection.commit()
        connection.close()

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("precision loss", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())


class TransactionTests(RelocateFixture):
    def test_large_session_history_is_streamed_and_preserved(self) -> None:
        history_chunk = b'{"type":"response_item","payload":"history"}\n' * 25000
        expected_history = hashlib.sha256()
        expected_history.update(self.history_line.encode("utf-8"))
        with self.session.open("ab") as handle:
            for _ in range(12):
                handle.write(history_chunk)
                expected_history.update(history_chunk)

        _, planned = self.plan()
        spec = importlib.util.spec_from_file_location("relocate_stream_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_read_bytes = Path.read_bytes

        def guarded_read_bytes(candidate: Path) -> bytes:
            if candidate == self.session:
                raise AssertionError("session JSONL must not be loaded with read_bytes")
            return original_read_bytes(candidate)

        args = SimpleNamespace(
            old=str(self.old),
            new=str(self.new),
            codex_home=str(self.codex_home),
            state_db=None,
            token=planned["apply_token"],
            without_link=False,
        )
        with mock.patch.object(Path, "read_bytes", guarded_read_bytes):
            with contextlib.redirect_stdout(io.StringIO()):
                result = module.apply_command(args)

        self.assertEqual(result, 0)
        actual_history = hashlib.sha256()
        with self.session.open("rb") as handle:
            handle.readline()
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                actual_history.update(chunk)
        self.assertEqual(actual_history.hexdigest(), expected_history.hexdigest())
        self.assertTrue(self.old.is_symlink())
        self.assertTrue(self.new.is_dir())

    def test_parent_fsync_failure_is_retried_before_metadata_commit(self) -> None:
        _, planned = self.plan()
        spec = importlib.util.spec_from_file_location("relocate_fsync_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_rename = module.atomic_exclusive_rename
        original_recovery_sync = module.fsync_proven_parent_directories
        real_fsync = os.fsync
        context = {"rename": False, "recovery": False}
        failures = {"rename": True, "recovery": True}

        def flaky_fsync(descriptor: int) -> None:
            for phase in ("rename", "recovery"):
                if context[phase] and failures[phase]:
                    failures[phase] = False
                    raise OSError(f"simulated {phase} directory fsync failure")
            real_fsync(descriptor)

        def wrapped_rename(*args, **kwargs):
            context["rename"] = True
            try:
                return original_rename(*args, **kwargs)
            finally:
                context["rename"] = False

        def wrapped_recovery_sync(*args, **kwargs):
            context["recovery"] = True
            try:
                return original_recovery_sync(*args, **kwargs)
            finally:
                context["recovery"] = False

        args = SimpleNamespace(
            old=str(self.old),
            new=str(self.new),
            codex_home=str(self.codex_home),
            state_db=None,
            token=planned["apply_token"],
            without_link=False,
        )
        with mock.patch.object(module.os, "fsync", side_effect=flaky_fsync):
            with mock.patch.object(
                module,
                "atomic_exclusive_rename",
                side_effect=wrapped_rename,
            ):
                with mock.patch.object(
                    module,
                    "fsync_proven_parent_directories",
                    side_effect=wrapped_recovery_sync,
                ):
                    with self.assertRaises(module.PartialFailure):
                        module.apply_command(args)
                    self.assertFalse(self.old.exists())
                    self.assertTrue(self.new.is_dir())

                    with self.assertRaises(module.PartialFailure):
                        module.apply_command(args)
                    connection = sqlite3.connect(self.state_db)
                    cwd = connection.execute(
                        "SELECT cwd FROM threads WHERE id = 'thread-1'"
                    ).fetchone()[0]
                    connection.close()
                    self.assertEqual(cwd, str(self.old))
                    self.assertFalse(self.old.exists())

                    with contextlib.redirect_stdout(io.StringIO()):
                        result = module.apply_command(args)

        self.assertEqual(result, 0)
        self.assertTrue(self.old.is_symlink())
        self.assertTrue(self.new.is_dir())

    def test_escaped_lone_surrogates_are_preserved_without_partial_failure(self) -> None:
        state = json.loads(self.global_state.read_text(encoding="utf-8"))
        state["unrelated-surrogate"] = "\ud800"
        self.global_state.write_text(
            json.dumps(state, ensure_ascii=True, separators=(",", ":")),
            encoding="utf-8",
        )

        connection = sqlite3.connect(self.state_db)
        policy_text = connection.execute(
            "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()[0]
        policy = json.loads(policy_text)
        policy["unrelated-surrogate"] = "\ud800"
        connection.execute(
            "UPDATE threads SET sandbox_policy = ? WHERE id = 'thread-1'",
            (json.dumps(policy, ensure_ascii=True, separators=(",", ":")),),
        )
        connection.commit()
        connection.close()

        planned_result, planned = self.plan()
        self.assertEqual(planned_result.returncode, 0)
        self.assertEqual(planned["status"], "ready")

        result, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "completed")
        repaired_state = json.loads(self.global_state.read_text(encoding="utf-8"))
        self.assertEqual(repaired_state["unrelated-surrogate"], "\ud800")
        connection = sqlite3.connect(self.state_db)
        repaired_policy = json.loads(
            connection.execute(
                "SELECT sandbox_policy FROM threads WHERE id = 'thread-1'"
            ).fetchone()[0]
        )
        connection.close()
        self.assertEqual(repaired_policy["unrelated-surrogate"], "\ud800")

    def test_apply_moves_repairs_backs_up_and_verifies(self) -> None:
        _, planned = self.plan()
        token = planned["apply_token"]
        result, payload = self.run_script(
            "apply", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(self.old.resolve(), self.new.resolve())
        self.assertEqual((self.new / "payload.txt").read_text(), "unchanged\n")

        connection = sqlite3.connect(self.state_db)
        cwd, policy_text = connection.execute(
            "SELECT cwd, sandbox_policy FROM threads WHERE id = 'thread-1'"
        ).fetchone()
        connection.close()
        self.assertEqual(cwd, str(self.new))
        policy = json.loads(policy_text)
        policy_paths = [
            entry["path"]["path"]
            for entry in policy["file_system"]["entries"]
            if entry["path"]["type"] == "path"
        ]
        self.assertIn(str(self.new), policy_paths)
        self.assertIn(str(self.new / ".git"), policy_paths)
        self.assertNotIn(str(self.old), policy_paths)

        lines = self.session.read_text(encoding="utf-8").splitlines(keepends=True)
        self.assertEqual(json.loads(lines[0])["payload"]["cwd"], str(self.new))
        self.assertEqual(lines[1], self.history_line)
        global_state = json.loads(self.global_state.read_text(encoding="utf-8"))
        self.assertEqual(global_state["electron-saved-workspace-roots"], [str(self.new)])
        self.assertEqual(
            global_state["prompt-history"]["thread-1"],
            [f"Historical prose keeps {self.old} unchanged"],
        )
        self.assertEqual(
            global_state["thread-writable-roots"]["thread-1"],
            [str(self.new), str(self.root / "extra")],
        )

        backup_root = self.codex_home / "relocation-backups"
        backups = list(backup_root.iterdir())
        self.assertEqual(len(backups), 1)
        manifest = json.loads((backups[0] / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")

        verify, verified = self.run_script(
            "verify", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(verify.returncode, 0, verify.stderr)
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["metadata"]["change_count"], 0)

    def test_apply_resumes_after_move_before_link(self) -> None:
        _, planned = self.plan()
        token = planned["apply_token"]
        os.rename(self.old, self.new)
        result, payload = self.run_script(
            "apply", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["filesystem"]["status"], "resumed")
        self.assertTrue(self.old.is_symlink())

    def test_repeating_apply_is_a_noop(self) -> None:
        _, planned = self.plan()
        token = planned["apply_token"]
        first, _ = self.run_script(
            "apply", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(first.returncode, 0)
        backup_count = len(list((self.codex_home / "relocation-backups").iterdir()))
        second, payload = self.run_script(
            "apply", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(second.returncode, 0)
        self.assertEqual(payload["status"], "already-complete")
        self.assertEqual(
            backup_count, len(list((self.codex_home / "relocation-backups").iterdir()))
        )

    def test_linkless_apply_requires_matching_policy_and_verifies(self) -> None:
        planned_result, planned = self.run_script(
            "plan", str(self.old), str(self.new), "--without-link"
        )
        self.assertEqual(planned_result.returncode, 0)
        token = planned["apply_token"]
        applied, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            token,
            "--without-link",
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(payload["status"], "completed")
        self.assertFalse(os.path.lexists(self.old))
        self.assertTrue(self.new.is_dir())
        verified, verify_payload = self.run_script(
            "verify",
            str(self.old),
            str(self.new),
            "--token",
            token,
            "--without-link",
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        self.assertEqual(verify_payload["status"], "verified")

    def test_open_state_database_refuses_before_move(self) -> None:
        _, planned = self.plan()
        connection = sqlite3.connect(self.state_db)
        try:
            result, payload = self.run_script(
                "apply",
                str(self.old),
                str(self.new),
                "--token",
                planned["apply_token"],
            )
        finally:
            connection.close()
        self.assertEqual(result.returncode, 2)
        self.assertIn("still using the state database", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_missing_related_rollout_blocks_plan(self) -> None:
        self.session.unlink()

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["apply_token"])
        self.assertFalse(payload["metadata"]["audit_complete"])
        self.assertIn("rollout_path is absent", " ".join(payload["metadata"]["audit_errors"]))

    def test_symlinked_session_subtree_is_refused(self) -> None:
        external = self.root / "external-sessions"
        external.mkdir()
        link = self.codex_home / "sessions" / "linked-external"
        link.symlink_to(external, target_is_directory=True)

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("contains a symlink", payload["error"])

    def test_global_allowlist_type_drift_blocks_plan(self) -> None:
        state = json.loads(self.global_state.read_text(encoding="utf-8"))
        state["electron-saved-workspace-roots"] = str(self.old)
        self.global_state.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["apply_token"])
        self.assertIn("is not a list", " ".join(payload["metadata"]["audit_errors"]))

    def test_wal_header_without_sidecars_is_audited_read_only(self) -> None:
        connection = sqlite3.connect(self.state_db)
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.close()
        self.assertEqual(str(mode).lower(), "wal")

        result, payload = self.plan()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["metadata"]["audit_complete"])

    def test_codex_home_nested_inside_source_is_refused(self) -> None:
        nested_home = self.old / "codex-home"
        self.codex_home.rename(nested_home)
        self.codex_home = nested_home
        self.state_db = nested_home / "state_5.sqlite"
        self.global_state = nested_home / ".codex-global-state.json"
        self.session = nested_home / self.session.relative_to(self.root / "codex-home")

        result, payload = self.plan()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("must not contain one another", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_metadata_only_repair_is_idempotent(self) -> None:
        os.rename(self.old, self.new)
        os.symlink(str(self.new), str(self.old), target_is_directory=True)
        planned_result, planned = self.run_script("repair", str(self.old), str(self.new))
        self.assertEqual(planned_result.returncode, 0)
        self.assertEqual(planned["status"], "repair-ready")
        token = planned["repair_apply_token"]
        applied, payload = self.run_script(
            "repair", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertEqual(payload["status"], "completed")
        second, second_payload = self.run_script("repair", str(self.old), str(self.new))
        self.assertEqual(second.returncode, 0)
        self.assertEqual(second_payload["status"], "clean")
        self.assertEqual(second_payload["metadata"]["change_count"], 0)

    def test_repair_token_rejects_replaced_destination_identity(self) -> None:
        os.rename(self.old, self.new)
        os.symlink(str(self.new), str(self.old), target_is_directory=True)
        _, planned = self.run_script("repair", str(self.old), str(self.new))
        token = planned["repair_apply_token"]
        before = {
            "db": digest(self.state_db),
            "session": digest(self.session),
            "global": digest(self.global_state),
        }
        displaced = self.root / "approved-destination"
        os.rename(self.new, displaced)
        self.new.mkdir()

        result, payload = self.run_script(
            "repair",
            str(self.old),
            str(self.new),
            "--token",
            token,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertIn("identity changed", payload["error"])
        self.assertEqual(before["db"], digest(self.state_db))
        self.assertEqual(before["session"], digest(self.session))
        self.assertEqual(before["global"], digest(self.global_state))

    def test_repair_rechecks_destination_after_backup_before_writes(self) -> None:
        os.rename(self.old, self.new)
        os.symlink(str(self.new), str(self.old), target_is_directory=True)
        _, planned = self.run_script("repair", str(self.old), str(self.new))
        before = {
            "db": digest(self.state_db),
            "session": digest(self.session),
            "global": digest(self.global_state),
        }
        spec = importlib.util.spec_from_file_location("relocate_repair_race_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_prepare = module.prepare_metadata_backup
        displaced = self.root / "approved-destination"

        def replace_after_backup(*args, **kwargs):
            prepared = original_prepare(*args, **kwargs)
            os.rename(self.new, displaced)
            self.new.mkdir()
            return prepared

        args = SimpleNamespace(
            old=str(self.old),
            new=str(self.new),
            codex_home=str(self.codex_home),
            state_db=None,
            token=planned["repair_apply_token"],
            without_link=False,
        )
        with mock.patch.object(
            module,
            "prepare_metadata_backup",
            side_effect=replace_after_backup,
        ):
            with self.assertRaises(module.Refusal) as raised:
                module.repair_command(args)

        self.assertIn("identity changed", str(raised.exception))
        self.assertEqual(before["db"], digest(self.state_db))
        self.assertEqual(before["session"], digest(self.session))
        self.assertEqual(before["global"], digest(self.global_state))
        self.assertFalse((self.codex_home / "relocation-backups").exists())

    def test_apply_rechecks_destination_before_metadata_writes(self) -> None:
        _, planned = self.plan()
        before = {
            "db": digest(self.state_db),
            "session": digest(self.session),
            "global": digest(self.global_state),
        }
        spec = importlib.util.spec_from_file_location("relocate_apply_race_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_apply_filesystem = module.apply_filesystem
        displaced = self.root / "approved-destination"

        def replace_after_filesystem(*args, **kwargs):
            result = original_apply_filesystem(*args, **kwargs)
            os.rename(self.new, displaced)
            self.new.mkdir()
            return result

        args = SimpleNamespace(
            old=str(self.old),
            new=str(self.new),
            codex_home=str(self.codex_home),
            state_db=None,
            token=planned["apply_token"],
            without_link=False,
        )
        with mock.patch.object(
            module,
            "apply_filesystem",
            side_effect=replace_after_filesystem,
        ):
            with self.assertRaises(module.PartialFailure) as raised:
                module.apply_command(args)

        self.assertIn(
            "destination",
            json.dumps(raised.exception.details).lower(),
        )
        self.assertEqual(before["db"], digest(self.state_db))
        self.assertEqual(before["session"], digest(self.session))
        self.assertEqual(before["global"], digest(self.global_state))
        self.assertTrue((self.codex_home / "relocation-backups").is_dir())

    def test_backup_preparation_failure_refuses_before_move(self) -> None:
        _, planned = self.plan()
        before = {
            "db": digest(self.state_db),
            "session": digest(self.session),
            "global": digest(self.global_state),
        }
        (self.codex_home / "relocation-backups").write_text(
            "not a directory\n", encoding="utf-8"
        )

        result, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertEqual(before["db"], digest(self.state_db))
        self.assertEqual(before["session"], digest(self.session))
        self.assertEqual(before["global"], digest(self.global_state))

    def test_missing_lsof_refuses_before_move(self) -> None:
        _, planned = self.plan()
        empty_path = self.root / "empty-bin"
        empty_path.mkdir()

        with mock.patch.dict(os.environ, {"PATH": str(empty_path)}):
            result, payload = self.run_script(
                "apply",
                str(self.old),
                str(self.new),
                "--token",
                planned["apply_token"],
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn("lsof", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_open_wal_sidecar_refuses_before_move(self) -> None:
        wal = Path(f"{self.state_db}-wal")
        wal.touch()
        _, planned = self.plan()
        self.assertTrue(wal.exists())

        holder = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "handle = open(sys.argv[1], 'rb'); "
                    "print('ready', flush=True); "
                    "sys.stdin.read()"
                ),
                str(wal),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert holder.stdout is not None
            self.assertEqual(holder.stdout.readline().strip(), "ready")
            result, payload = self.run_script(
                "apply",
                str(self.old),
                str(self.new),
                "--token",
                planned["apply_token"],
            )
        finally:
            assert holder.stdin is not None
            holder.stdin.close()
            holder.wait(timeout=5)
            assert holder.stdout is not None
            assert holder.stderr is not None
            holder.stdout.close()
            holder.stderr.close()

        self.assertEqual(result.returncode, 2)
        self.assertIn(str(wal), json.dumps(payload, ensure_ascii=False))
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())

    def test_plan_does_not_touch_residual_wal_or_shm_and_apply_succeeds(self) -> None:
        writer = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import os, sqlite3, sys; "
                    "connection = sqlite3.connect(sys.argv[1]); "
                    "connection.execute('PRAGMA journal_mode=WAL'); "
                    "connection.execute('PRAGMA wal_autocheckpoint=0'); "
                    "connection.execute('CREATE TABLE IF NOT EXISTS wal_marker(value TEXT)'); "
                    "connection.execute(\"INSERT INTO wal_marker VALUES ('committed')\"); "
                    "connection.commit(); "
                    "os._exit(0)"
                ),
                str(self.state_db),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(writer.returncode, 0, writer.stderr)
        wal = Path(f"{self.state_db}-wal")
        shm = Path(f"{self.state_db}-shm")
        self.assertTrue(wal.is_file())
        self.assertTrue(shm.is_file())
        before = {
            "wal": (digest(wal), wal.stat().st_mtime_ns),
            "shm": (digest(shm), shm.stat().st_mtime_ns),
        }

        planned_result, planned = self.plan()

        self.assertEqual(planned_result.returncode, 0, planned_result.stderr)
        self.assertEqual(planned["status"], "ready")
        self.assertEqual(before["wal"], (digest(wal), wal.stat().st_mtime_ns))
        self.assertEqual(before["shm"], (digest(shm), shm.stat().st_mtime_ns))
        result, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
        )
        self.assertEqual(
            result.returncode,
            0,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        self.assertEqual(payload["status"], "completed")
        backup_db = (
            Path(payload["metadata"]["backup"])
            / "files"
            / self.state_db.relative_to(self.codex_home)
        )
        backup_connection = sqlite3.connect(backup_db)
        try:
            marker_count = backup_connection.execute(
                "SELECT COUNT(*) FROM wal_marker WHERE value = 'committed'"
            ).fetchone()[0]
        finally:
            backup_connection.close()
        self.assertEqual(marker_count, 1)

    def test_verify_fails_when_destination_identity_was_replaced(self) -> None:
        _, planned = self.plan()
        token = planned["apply_token"]
        applied, _ = self.run_script(
            "apply", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(applied.returncode, 0)

        original = self.root / "original-planned-directory"
        os.rename(self.new, original)
        self.new.mkdir()
        (self.new / "impostor.txt").write_text("replacement\n", encoding="utf-8")

        result, payload = self.run_script(
            "verify", str(self.old), str(self.new), "--token", token
        )

        self.assertEqual(result.returncode, 4)
        self.assertEqual(payload["status"], "verification-failed")
        self.assertFalse(payload["identity_verified_by_token"])

    def test_linkless_apply_refuses_existing_compatibility_link(self) -> None:
        planned_result, planned = self.run_script(
            "plan", str(self.old), str(self.new), "--without-link"
        )
        self.assertEqual(planned_result.returncode, 0)
        os.rename(self.old, self.new)
        os.symlink(str(self.new), str(self.old), target_is_directory=True)
        before = {
            "db": digest(self.state_db),
            "session": digest(self.session),
            "global": digest(self.global_state),
        }

        result, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
            "--without-link",
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(payload["status"], "unsafe-refused")
        self.assertTrue(self.old.is_symlink())
        self.assertEqual(before["db"], digest(self.state_db))
        self.assertEqual(before["session"], digest(self.session))
        self.assertEqual(before["global"], digest(self.global_state))

    def test_exact_repair_token_replay_is_a_noop(self) -> None:
        os.rename(self.old, self.new)
        os.symlink(str(self.new), str(self.old), target_is_directory=True)
        _, planned = self.run_script("repair", str(self.old), str(self.new))
        token = planned["repair_apply_token"]
        first, _ = self.run_script(
            "repair", str(self.old), str(self.new), "--token", token
        )
        self.assertEqual(first.returncode, 0)
        backup_root = self.codex_home / "relocation-backups"
        backup_count = len(list(backup_root.iterdir()))
        before = {
            "db": digest(self.state_db),
            "session": digest(self.session),
            "global": digest(self.global_state),
        }

        second, payload = self.run_script(
            "repair", str(self.old), str(self.new), "--token", token
        )

        self.assertEqual(second.returncode, 0)
        self.assertEqual(payload["status"], "already-complete")
        self.assertEqual(backup_count, len(list(backup_root.iterdir())))
        self.assertEqual(before["db"], digest(self.state_db))
        self.assertEqual(before["session"], digest(self.session))
        self.assertEqual(before["global"], digest(self.global_state))

    def test_metadata_change_after_plan_refuses_before_move(self) -> None:
        _, planned = self.plan()
        state = json.loads(self.global_state.read_text(encoding="utf-8"))
        state["unrelated-after-plan"] = "changed"
        self.global_state.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        result, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("changed after planning", payload["error"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())
        self.assertFalse((self.codex_home / "relocation-backups").exists())

    def test_global_rewrite_collision_preserves_preexisting_duplicates(self) -> None:
        extra = str(self.root / "duplicate-extra")
        state = json.loads(self.global_state.read_text(encoding="utf-8"))
        state["project-order"] = [str(self.old), str(self.new), extra, extra]
        self.global_state.write_text(
            json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

        _, planned = self.plan()
        project_actions = [
            change
            for change in planned["metadata"]["changes"]
            if change.get("pointer", "").startswith("/project-order/")
        ]
        self.assertTrue(
            any(
                change.get("operation") == "remove_rewrite_collision"
                for change in project_actions
            )
        )
        result, _ = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
        )
        self.assertEqual(result.returncode, 0)
        repaired = json.loads(self.global_state.read_text(encoding="utf-8"))
        self.assertEqual(repaired["project-order"], [str(self.new), extra, extra])

    def test_apply_with_already_clean_metadata_runs_final_audit(self) -> None:
        self.make_metadata_clean_for_destination()

        _, planned = self.plan()
        self.assertEqual(planned["metadata"]["change_count"], 0)
        result, payload = self.run_script(
            "apply",
            str(self.old),
            str(self.new),
            "--token",
            planned["apply_token"],
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["metadata"]["status"], "clean")
        self.assertFalse((self.codex_home / "relocation-backups").exists())

    def test_zero_change_apply_detects_old_path_injected_after_move(self) -> None:
        self.make_metadata_clean_for_destination()
        _, planned = self.plan()
        self.assertEqual(planned["metadata"]["change_count"], 0)

        spec = importlib.util.spec_from_file_location("relocate_race_test", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        original_apply_filesystem = module.apply_filesystem

        def inject_after_move(*args, **kwargs):
            result = original_apply_filesystem(*args, **kwargs)
            state = json.loads(self.global_state.read_text(encoding="utf-8"))
            state["project-order"] = [str(self.old)]
            self.global_state.write_text(
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            return result

        args = SimpleNamespace(
            old=str(self.old),
            new=str(self.new),
            codex_home=str(self.codex_home),
            state_db=None,
            token=planned["apply_token"],
            without_link=False,
        )
        with mock.patch.object(
            module, "apply_filesystem", side_effect=inject_after_move
        ):
            with self.assertRaises(module.PartialFailure) as raised:
                module.apply_command(args)

        self.assertIn("metadata", str(raised.exception).lower())
        self.assertTrue(self.old.is_symlink())
        self.assertTrue(self.new.is_dir())


class SchemaGuardTests(RelocateFixture):
    def test_untested_schema_refuses_before_move(self) -> None:
        connection = sqlite3.connect(self.state_db)
        connection.execute("UPDATE _sqlx_migrations SET version = 41")
        connection.commit()
        connection.close()
        result, planned = self.plan()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(planned["status"], "blocked")
        self.assertIsNone(planned["apply_token"])
        self.assertFalse(planned["metadata"]["repair_supported"])
        self.assertTrue(self.old.is_dir())
        self.assertFalse(self.new.exists())


if __name__ == "__main__":
    unittest.main()

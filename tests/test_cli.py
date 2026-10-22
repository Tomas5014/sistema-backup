from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.cli import main, verify_integration, verify_sftp_integration


class RecoveryAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def snapshots(self, destination, password: str, timeout_seconds: int):
        self.calls.append(("snapshots", destination.id, password, timeout_seconds))
        return [{"id": "a" * 64, "short_id": "a" * 8}]

    def check(self, destination, password: str, *, read_data: bool, timeout_seconds: int) -> None:
        self.calls.append(("check", destination.id, password, read_data, timeout_seconds))

    def restore(
        self,
        destination,
        password: str,
        snapshot_id: str,
        target: Path,
        *,
        timeout_seconds: int,
    ) -> None:
        self.calls.append(
            ("restore", destination.id, password, snapshot_id, target, timeout_seconds)
        )


class IntegrationValidationCommandTests(unittest.TestCase):
    def test_returns_failure_when_restic_is_absent(self) -> None:
        output = io.StringIO()
        with patch("macip_backup.cli.shutil.which", return_value=None), patch("sys.stdout", output):
            exit_code = verify_integration()

        self.assertEqual(exit_code, 1)
        self.assertIn("binário restic não encontrado", output.getvalue())

    def test_sftp_validation_fails_without_isolated_environment(self) -> None:
        output = io.StringIO()
        with patch.dict("macip_backup.cli.os.environ", {}, clear=True), patch(
            "macip_backup.cli.shutil.which", return_value="/usr/bin/tool"
        ), patch("sys.stdout", output):
            exit_code = verify_sftp_integration()

        self.assertEqual(exit_code, 1)
        self.assertIn("ambiente isolado não foi preparado", output.getvalue())

    def test_invalid_configuration_does_not_create_adapter_or_repository(self) -> None:
        config = {
            "id": "task-invalid",
            "name": "Tarefa inválida",
            "client": {"id": "client-invalid", "name": "Cliente inválido"},
            "device_id": "device-invalid",
            "sources": ["/tmp/source"],
            "exclusions": "*.tmp",
            "destinations": [
                {
                    "id": "destination-invalid",
                    "name": "Destino inválido",
                    "kind": "local",
                    "repository": "/tmp/repository",
                    "password_environment": "MACIP_TEST_PASSWORD",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "invalid.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(sys, "argv", ["macip-backup-poc", "run", str(config_path)]), patch(
                "macip_backup.cli.ResticAdapter"
            ) as adapter, patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as context:
                    main()

        self.assertEqual(context.exception.code, 2)
        adapter.assert_not_called()

    def test_snapshots_check_and_restore_use_selected_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self._write_recovery_config(root)
            (root / "source").rmdir()
            target = root / "restored"
            adapter = RecoveryAdapter()
            with patch.dict(os.environ, {"MACIP_RECOVERY_PASSWORD": "test-password"}), patch(
                "macip_backup.cli.ResticAdapter", return_value=adapter
            ), patch("sys.stdout", io.StringIO()) as output:
                with patch.object(
                    sys, "argv", ["macip-backup-poc", "snapshots", str(config_path), "local"]
                ):
                    self.assertEqual(main(), 0)
                snapshots_output = json.loads(output.getvalue())
                self.assertEqual(snapshots_output["snapshots"][0]["short_id"], "a" * 8)
                output.seek(0)
                output.truncate(0)
                with patch.object(
                    sys,
                    "argv",
                    ["macip-backup-poc", "check", str(config_path), "local", "--read-data"],
                ):
                    self.assertEqual(main(), 0)
                check_output = json.loads(output.getvalue())
                self.assertTrue(check_output["read_data"])
                output.seek(0)
                output.truncate(0)
                with patch.object(
                    sys,
                    "argv",
                    [
                        "macip-backup-poc",
                        "restore",
                        str(config_path),
                        "local",
                        "a" * 64,
                        str(target),
                    ],
                ):
                    self.assertEqual(main(), 0)
                restore_output = json.loads(output.getvalue())

        self.assertEqual(
            adapter.calls,
            [
                ("snapshots", "local", "test-password", 17),
                ("check", "local", "test-password", True, 19),
                ("restore", "local", "test-password", "a" * 64, target.resolve(), 23),
            ],
        )
        self.assertEqual(restore_output["target"], str(target.resolve()))

    def test_restore_rejects_existing_target_before_creating_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self._write_recovery_config(root)
            target = root / "already-exists"
            target.mkdir()
            with patch.object(
                sys,
                "argv",
                ["macip-backup-poc", "restore", str(config_path), "local", "a" * 64, str(target)],
            ), patch("macip_backup.cli.ResticAdapter") as adapter, patch(
                "sys.stderr", io.StringIO()
            ):
                with self.assertRaises(SystemExit) as context:
                    main()

        self.assertEqual(context.exception.code, 2)
        adapter.assert_not_called()

    def test_recovery_rejects_unknown_destination_before_creating_adapter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = self._write_recovery_config(Path(directory))
            with patch.object(
                sys, "argv", ["macip-backup-poc", "snapshots", str(config_path), "missing"]
            ), patch("macip_backup.cli.ResticAdapter") as adapter, patch(
                "sys.stderr", io.StringIO()
            ):
                with self.assertRaises(SystemExit) as context:
                    main()

        self.assertEqual(context.exception.code, 2)
        adapter.assert_not_called()

    def test_recovery_rejects_missing_credential_without_exposing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = self._write_recovery_config(Path(directory))
            error_output = io.StringIO()
            with patch.dict(
                os.environ, {"UNRELATED_SECRET": "secret-not-for-output"}, clear=True
            ), patch.object(
                sys, "argv", ["macip-backup-poc", "snapshots", str(config_path), "local"]
            ), patch("macip_backup.cli.ResticAdapter") as adapter, patch(
                "sys.stderr", error_output
            ):
                with self.assertRaises(SystemExit) as context:
                    main()

        self.assertEqual(context.exception.code, 2)
        self.assertIn("credencial ausente", error_output.getvalue())
        self.assertNotIn("secret-not-for-output", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())
        adapter.assert_not_called()

    def test_restore_rejects_target_parent_of_source_or_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_parent_of_source = root / "restore-parent-source"
            source_config = self._write_recovery_config(
                root,
                source=target_parent_of_source / "removed-source",
                repository=root / "repository",
                create_source=False,
            )
            target_parent_of_repository = root / "restore-parent-repository"
            repository_config = self._write_recovery_config(
                root / "repository-case",
                repository=target_parent_of_repository / "repository",
            )

            for config_path, target, field_name in (
                (source_config, target_parent_of_source, "origem"),
                (repository_config, target_parent_of_repository, "repositório"),
            ):
                with self.subTest(field_name=field_name), patch.object(
                    sys,
                    "argv",
                    ["macip-backup-poc", "restore", str(config_path), "local", "a" * 64, str(target)],
                ), patch("macip_backup.cli.ResticAdapter") as adapter, patch(
                    "sys.stderr", io.StringIO()
                ):
                    with self.assertRaises(SystemExit) as context:
                        main()

                self.assertEqual(context.exception.code, 2)
                adapter.assert_not_called()

    def test_restore_rejects_equal_or_nested_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self._write_recovery_config(root)
            cases = (
                ("origem igual", root / "source"),
                ("origem descendente", root / "source" / "restore-inside"),
                ("repositório igual", root / "repository"),
                ("repositório descendente", root / "repository" / "restore-inside"),
            )
            for field_name, target in cases:
                with self.subTest(field_name=field_name), patch.object(
                    sys,
                    "argv",
                    ["macip-backup-poc", "restore", str(config_path), "local", "a" * 64, str(target)],
                ), patch("macip_backup.cli.ResticAdapter") as adapter, patch(
                    "sys.stderr", io.StringIO()
                ) as error_output:
                    with self.assertRaises(SystemExit) as context:
                        main()

                self.assertEqual(context.exception.code, 2)
                self.assertNotIn("Traceback", error_output.getvalue())
                adapter.assert_not_called()

    @staticmethod
    def _write_recovery_config(
        root: Path,
        *,
        source: Path | None = None,
        repository: Path | None = None,
        create_source: bool = True,
    ) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        source = source or root / "source"
        if create_source:
            source.mkdir(parents=True, exist_ok=True)
        repository = repository or root / "repository"
        config = {
            "id": "task-recovery",
            "name": "Recuperação",
            "client": {"id": "client-recovery", "name": "Cliente"},
            "device_id": "device-recovery",
            "sources": [str(source)],
            "exclusions": [],
            "destinations": [
                {
                    "id": "local",
                    "name": "Local",
                    "kind": "local",
                    "repository": str(repository),
                    "password_environment": "MACIP_RECOVERY_PASSWORD",
                }
            ],
            "timeouts": {
                "snapshots_seconds": 17,
                "check_seconds": 19,
                "restore_seconds": 23,
            },
        }
        path = root / "task.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

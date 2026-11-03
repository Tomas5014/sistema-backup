from __future__ import annotations

import io
import json
import os
import subprocess
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
    def test_schedule_pull_public_entrypoint_forwards_enable_and_serializes_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "agent.db"
            central = root / "central.json"
            for enable in (False, True):
                output = io.StringIO()
                arguments = [
                    "macip-backup-poc", "schedule-pull", "--store", str(store),
                    "--central", str(central), "--calendar", "*-*-* *:*:10",
                ]
                if enable:
                    arguments.append("--enable")
                installation = {
                    "timer_name": "macip-backup-pull-test.timer",
                    "enabled": enable,
                }
                with self.subTest(enable=enable), patch.object(
                    sys, "argv", arguments
                ), patch(
                    "macip_backup.distribution.schedule_pull",
                    return_value=installation,
                ) as schedule, patch("sys.stdout", output):
                    self.assertEqual(main(), 0)

                schedule.assert_called_once_with(
                    store, central, "*-*-* *:*:10", enable=enable
                )
                self.assertEqual(json.loads(output.getvalue()), installation)

    def test_unschedule_pull_public_entrypoint_is_idempotent_and_serializes_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "agent.db"
            central = root / "central.json"
            results = (
                {
                    "store": str(store),
                    "central": str(central),
                    "removed": ["pull.timer", "pull.service"],
                },
                {"store": str(store), "central": str(central), "removed": []},
            )
            arguments = [
                "macip-backup-poc", "unschedule-pull", "--store", str(store),
                "--central", str(central),
            ]
            with patch(
                "macip_backup.distribution.unschedule_pull", side_effect=results
            ) as unschedule:
                for expected in results:
                    output = io.StringIO()
                    with patch.object(sys, "argv", arguments), patch("sys.stdout", output):
                        self.assertEqual(main(), 0)
                    self.assertEqual(json.loads(output.getvalue()), expected)

        self.assertEqual(unschedule.call_count, 2)
        unschedule.assert_called_with(store, central)

    def test_schedule_pull_public_entrypoint_preserves_operational_error_exit(self) -> None:
        error_output = io.StringIO()
        with patch.object(
            sys,
            "argv",
            [
                "macip-backup-poc", "schedule-pull", "--store", "/tmp/agent.db",
                "--central", "/tmp/central.json", "--calendar", "daily",
            ],
        ), patch(
            "macip_backup.distribution.schedule_pull",
            side_effect=OSError("falha operacional simulada"),
        ), patch("sys.stderr", error_output):
            with self.assertRaises(SystemExit) as context:
                main()

        self.assertEqual(context.exception.code, 2)
        self.assertIn("falha operacional simulada", error_output.getvalue())
        self.assertNotIn("Traceback", error_output.getvalue())

    def test_pull_schedule_subprocess_isolated_install_enable_and_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            fake_bin = root / "bin"
            home.mkdir()
            fake_bin.mkdir()
            systemctl_log = root / "systemctl.log"
            self._write_executable(
                fake_bin / "systemd-analyze",
                "#!/bin/sh\nexit 0\n",
            )
            self._write_executable(
                fake_bin / "systemctl",
                '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$MACIP_TEST_SYSTEMCTL_LOG"\n',
            )
            central_a = self._write_central(root / "central-a.json")
            central_b = self._write_central(root / "central-b.json")
            store_a = root / "agent-a.db"
            store_b = root / "agent-b.db"
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{environment.get('PATH', '')}",
                    "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
                    "MACIP_TEST_SYSTEMCTL_LOG": str(systemctl_log),
                }
            )

            installed_a = self._run_cli_subprocess(
                environment,
                "schedule-pull", "--store", str(store_a), "--central",
                str(central_a), "--calendar", "*-*-* *:*:10",
            )
            self.assertFalse(installed_a["enabled"])
            self.assertFalse(systemctl_log.exists())
            for field in ("service", "timer"):
                self.assertTrue(Path(installed_a[field]).is_file())

            installed_b = self._run_cli_subprocess(
                environment,
                "schedule-pull", "--store", str(store_b), "--central",
                str(central_b), "--calendar", "*-*-* *:*:20", "--enable",
            )
            self.assertTrue(installed_b["enabled"])
            calls_after_enable = systemctl_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(calls_after_enable[0], "--user daemon-reload")
            self.assertEqual(
                calls_after_enable[1],
                f"--user enable --now {installed_b['timer_name']}",
            )

            removed = self._run_cli_subprocess(
                environment,
                "unschedule-pull", "--store", str(store_a), "--central", str(central_a),
            )
            self.assertEqual(
                set(removed["removed"]),
                {installed_a["service"], installed_a["timer"]},
            )
            for field in ("service", "timer"):
                self.assertFalse(Path(installed_a[field]).exists())
                self.assertTrue(Path(installed_b[field]).exists())

            calls_before_repeat = systemctl_log.read_text(encoding="utf-8")
            repeated = self._run_cli_subprocess(
                environment,
                "unschedule-pull", "--store", str(store_a), "--central", str(central_a),
            )
            self.assertEqual(repeated["removed"], [])
            self.assertEqual(
                systemctl_log.read_text(encoding="utf-8"), calls_before_repeat
            )

    def test_schedule_sync_forwards_store_central_calendar_and_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = root / "agent.db"
            central = root / "central.json"
            output = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "macip-backup-poc", "schedule-sync", "--store", str(store),
                    "--central", str(central), "--calendar", "hourly",
                    "--limit", "7", "--enable",
                ],
            ), patch(
                "macip_backup.systemd.install_sync_schedule",
                return_value={"timer_name": "macip-backup-sync-test.timer"},
            ) as install, patch("sys.stdout", output):
                self.assertEqual(main(), 0)
        install.assert_called_once_with(
            store, central, "hourly", limit=7, enable=True
        )
        self.assertIn("macip-backup-sync-test.timer", output.getvalue())

    def test_schedule_forwards_optional_central(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = self._write_recovery_config(root)
            central = root / "central.json"
            output = io.StringIO()
            with patch.object(
                sys,
                "argv",
                [
                    "macip-backup-poc", "schedule", str(config_path), "--store",
                    str(root / "agent.db"), "--calendar", "daily", "--central", str(central),
                ],
            ), patch(
                "macip_backup.systemd.install_schedule", return_value={"timer_name": "x"}
            ) as install, patch("sys.stdout", output):
                self.assertEqual(main(), 0)
        self.assertEqual(install.call_args.kwargs["central_path"], central)

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
    def _write_executable(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        path.chmod(0o755)

    @staticmethod
    def _write_central(path: Path) -> Path:
        path.write_text(
            json.dumps(
                {
                    "url": "http://127.0.0.1:8444",
                    "agent_id": "381143c2-d36e-4933-82cb-edafe49a0070",
                    "token_environment": "MACIP_TEST_API_TOKEN",
                }
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def _run_cli_subprocess(environment: dict[str, str], *arguments: str) -> dict:
        completed = subprocess.run(
            ["python3", "-m", "macip_backup.cli", *arguments],
            capture_output=True,
            text=True,
            env=environment,
            timeout=10,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"CLI falhou ({completed.returncode}): {completed.stderr}"
            )
        return json.loads(completed.stdout)

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

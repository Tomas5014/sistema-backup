from __future__ import annotations

from pathlib import Path
import re
import sys
import unittest

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.models import ConfigurationError, TaskSpec


def valid_config() -> dict[str, object]:
    return {
        "id": "task-unit",
        "name": "Tarefa unitária",
        "client": {"id": "client-unit", "name": "Cliente unitário"},
        "device_id": "device-unit",
        "sources": ["/tmp/source"],
        "exclusions": ["*.tmp"],
        "compression": "auto",
        "destinations": [
            {
                "id": "destination-unit",
                "name": "Destino unitário",
                "kind": "local",
                "repository": "/tmp/repository",
                "password_environment": "MACIP_TEST_PASSWORD",
            }
        ],
    }


class TaskSpecFromDictTests(unittest.TestCase):
    def test_accepts_empty_exclusions_list(self) -> None:
        config = valid_config()
        config["exclusions"] = []

        task = TaskSpec.from_dict(config)

        self.assertEqual(task.exclusions, ())

    def test_rejects_exclusions_string_before_tuple_conversion(self) -> None:
        config = valid_config()
        config["exclusions"] = "*.tmp"

        with self.assertRaisesRegex(ConfigurationError, "exclusions deve ser uma lista"):
            TaskSpec.from_dict(config)

    def test_rejects_required_collections_when_not_lists(self) -> None:
        for field_name, invalid_value in (
            ("sources", "/tmp/source"),
            ("exclusions", "*.tmp"),
            ("destinations", {"id": "destination"}),
        ):
            with self.subTest(field_name=field_name):
                config = valid_config()
                config[field_name] = invalid_value
                with self.assertRaisesRegex(ConfigurationError, f"{field_name} deve ser uma lista"):
                    TaskSpec.from_dict(config)

    def test_requires_at_least_one_source_and_destination(self) -> None:
        for field_name in ("sources", "destinations"):
            with self.subTest(field_name=field_name):
                config = valid_config()
                config[field_name] = []
                with self.assertRaisesRegex(ConfigurationError, "ao menos"):
                    TaskSpec.from_dict(config)

    def test_rejects_invalid_collection_items(self) -> None:
        cases = (
            ("sources", [42], "sources[0] deve ser texto"),
            ("exclusions", [False], "exclusions[0] deve ser texto"),
            ("destinations", ["destination"], "destinations[0] deve ser um objeto"),
        )
        for field_name, invalid_value, message in cases:
            with self.subTest(field_name=field_name):
                config = valid_config()
                config[field_name] = invalid_value
                with self.assertRaises(ConfigurationError) as context:
                    TaskSpec.from_dict(config)
                self.assertIn(message, str(context.exception))

    def test_rejects_non_object_client_and_retention(self) -> None:
        config = valid_config()
        config["client"] = []
        with self.assertRaisesRegex(ConfigurationError, "client deve ser um objeto"):
            TaskSpec.from_dict(config)

        config = valid_config()
        destination = config["destinations"][0]
        assert isinstance(destination, dict)
        destination["retention"] = None
        with self.assertRaisesRegex(ConfigurationError, "retention deve ser um objeto"):
            TaskSpec.from_dict(config)

    def test_accepts_absent_or_null_keep_last(self) -> None:
        for retention in ({}, {"keep_last": None}):
            with self.subTest(retention=retention):
                config = valid_config()
                destination = config["destinations"][0]
                assert isinstance(destination, dict)
                destination["retention"] = retention

                task = TaskSpec.from_dict(config)

                self.assertIsNone(task.destinations[0].retention.keep_last)

    def test_rejects_invalid_keep_last(self) -> None:
        for value in (True, 1.5, 0, -1, "1"):
            with self.subTest(value=value):
                config = valid_config()
                destination = config["destinations"][0]
                assert isinstance(destination, dict)
                destination["retention"] = {"keep_last": value}
                with self.assertRaises(ConfigurationError) as context:
                    TaskSpec.from_dict(config)
                self.assertIn(
                    "destinations[0].retention.keep_last deve ser inteiro positivo",
                    str(context.exception),
                )

    def test_parses_per_operation_timeouts(self) -> None:
        config = valid_config()
        config["timeouts"] = {
            "initialize_seconds": 3,
            "backup_seconds": 4,
            "snapshots_seconds": 5,
            "check_seconds": 6,
            "restore_seconds": 7,
        }

        task = TaskSpec.from_dict(config)

        self.assertEqual(task.timeouts.backup_seconds, 4)
        self.assertEqual(task.timeouts.restore_seconds, 7)

    def test_kind_missing_null_or_invalid_is_configuration_error(self) -> None:
        for value, expected in (
            (None, "destinations[0].kind é obrigatório"),
            ("ftp", "destinations[0].kind não é homologado: ftp"),
        ):
            with self.subTest(value=value):
                config = valid_config()
                destination = config["destinations"][0]
                assert isinstance(destination, dict)
                if value is None:
                    destination["kind"] = None
                else:
                    destination["kind"] = value
                with self.assertRaisesRegex(ConfigurationError, re.escape(expected)):
                    TaskSpec.from_dict(config)

        config = valid_config()
        destination = config["destinations"][0]
        assert isinstance(destination, dict)
        del destination["kind"]
        with self.assertRaisesRegex(
            ConfigurationError, re.escape("destinations[0].kind é obrigatório")
        ):
            TaskSpec.from_dict(config)

    def test_parses_sftp_without_resolving_remote_path(self) -> None:
        config = valid_config()
        config["destinations"] = [
            {
                "id": "destination-sftp",
                "name": "SFTP isolado",
                "kind": "sftp",
                "password_environment": "MACIP_RESTIC_PASSWORD_SFTP",
                "sftp": {
                    "host": "backup.example.test",
                    "port": 2222,
                    "user": "macip_backup",
                    "remote_path": "/srv/restic/client",
                    "identity_file": "/run/macip/id_ed25519",
                    "known_hosts_file": "/run/macip/known_hosts",
                },
            }
        ]

        task = TaskSpec.from_dict(config)

        connection = task.destinations[0].sftp
        assert connection is not None
        self.assertEqual(connection.remote_path, "/srv/restic/client")
        self.assertTrue(connection.remote_path_is_absolute)
        self.assertIsNone(task.destinations[0].repository)

        config["destinations"][0]["sftp"]["remote_path"] = "repositories/client"  # type: ignore[index]
        task = TaskSpec.from_dict(config)
        assert task.destinations[0].sftp is not None
        self.assertFalse(task.destinations[0].sftp.remote_path_is_absolute)

    def test_rejects_invalid_sftp_types_and_injection_fields(self) -> None:
        config = valid_config()
        destination = config["destinations"][0]
        assert isinstance(destination, dict)
        destination.pop("repository")
        destination["kind"] = "sftp"
        destination["sftp"] = {
            "host": "backup.example.test;rm",
            "port": "22",
            "user": "backup user",
            "remote_path": "~/repo",
            "identity_file": "/tmp/key",
            "known_hosts_file": "/tmp/known_hosts",
        }
        with self.assertRaisesRegex(ConfigurationError, "sftp.port deve ser inteiro"):
            TaskSpec.from_dict(config)

        destination["sftp"]["port"] = 22  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "sftp.host contém caracteres"):
            TaskSpec.from_dict(config)

        destination["sftp"]["host"] = "backup.example.test"  # type: ignore[index]
        destination["sftp"]["command"] = "ssh -oStrictHostKeyChecking=no"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "sftp.command não é permitido"):
            TaskSpec.from_dict(config)

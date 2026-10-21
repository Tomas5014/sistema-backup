from __future__ import annotations

import sys
import unittest
from pathlib import Path

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

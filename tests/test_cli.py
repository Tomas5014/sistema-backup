from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.cli import main, verify_integration


class IntegrationValidationCommandTests(unittest.TestCase):
    def test_returns_failure_when_restic_is_absent(self) -> None:
        output = io.StringIO()
        with patch("macip_backup.cli.shutil.which", return_value=None), patch("sys.stdout", output):
            exit_code = verify_integration()

        self.assertEqual(exit_code, 1)
        self.assertIn("binário restic não encontrado", output.getvalue())

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

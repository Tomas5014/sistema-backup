"""Integrações reais S3; exigem endpoint S3 compatível preparado."""

from __future__ import annotations

import io
import os
import secrets
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.logging import StructuredLogger
from macip_backup.models import (
    Client,
    CompressionMode,
    Destination,
    DestinationKind,
    S3Connection,
    TaskSpec,
)
from macip_backup.orchestrator import (
    BackupOrchestrator,
    DestinationStatus,
    ExecutionStatus,
)
from macip_backup.restic import ResticAdapter


def _environment_ready() -> bool:
    required = (
        "MACIP_S3_INTEGRATION",
        "MACIP_S3_ENDPOINT",
        "MACIP_S3_BUCKET",
        "MACIP_S3_ACCESS_KEY_ENV",
        "MACIP_S3_SECRET_KEY_ENV",
    )
    return (
        shutil.which("restic") is not None
        and os.environ.get("MACIP_S3_INTEGRATION") == "1"
        and all(os.environ.get(name) for name in required)
        and bool(os.environ.get(os.environ.get("MACIP_S3_ACCESS_KEY_ENV", ""), ""))
        and bool(os.environ.get(os.environ.get("MACIP_S3_SECRET_KEY_ENV", ""), ""))
    )


@unittest.skipUnless(_environment_ready(), "ambiente S3 de teste não preparado")
class S3IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password = secrets.token_urlsafe(32)
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-s3-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        (self.source / "documents").mkdir(parents=True)
        (self.source / "documents" / "report.txt").write_text("dados S3\n")
        (self.source / "ignored.tmp").write_text("ignorar\n")
        base_prefix = os.environ.get("MACIP_S3_PREFIX", "").rstrip("/")
        repository_id = f"macip-test-{self.id().rsplit('.', 1)[-1]}-{secrets.token_hex(6)}"
        self.prefix = f"{base_prefix}/{repository_id}" if base_prefix else repository_id
        self.connection = S3Connection(
            endpoint=os.environ["MACIP_S3_ENDPOINT"],
            bucket=os.environ["MACIP_S3_BUCKET"],
            access_key_environment=os.environ["MACIP_S3_ACCESS_KEY_ENV"],
            secret_key_environment=os.environ["MACIP_S3_SECRET_KEY_ENV"],
            prefix=self.prefix,
            region=os.environ.get("MACIP_S3_REGION") or None,
            bucket_lookup=os.environ.get("MACIP_S3_BUCKET_LOOKUP") or None,
        )
        self.adapter = ResticAdapter(cache_directory=self.root / "cache")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_s3_backup_check_restore(self) -> None:
        destination = self._s3_destination()
        task = self._task(destination)

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            task, lambda _: self.password
        )

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        snapshot_id = result.destinations[0].snapshot_id or ""
        self.assertTrue(snapshot_id)

        fresh = ResticAdapter(cache_directory=self.root / "cache-fresh")
        fresh.check(destination, self.password, read_data=True)
        self.assertTrue(fresh.snapshots(destination, self.password))
        restored = self.root / "restored"
        fresh.restore(destination, self.password, snapshot_id, restored, self.source)
        self.assertEqual(
            next(restored.rglob("report.txt")).read_text(), "dados S3\n"
        )
        self.assertFalse(list(restored.rglob("ignored.tmp")))

    def test_local_and_s3_destinations_are_independent(self) -> None:
        local = Destination(
            id="local",
            name="Local temporário",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "local-repository"),
            password_environment="IGNORED",
        )
        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            self._task(local, self._s3_destination()), lambda _: self.password
        )
        self.assertEqual(result.status, ExecutionStatus.SUCCESS)

    def test_wrong_credentials_fail_only_s3_destination(self) -> None:
        wrong_secret = "MACIP_S3_WRONG_SECRET_TEST"
        connection = replace(self.connection, secret_key_environment=wrong_secret)
        bad = replace(self._s3_destination(), s3=connection)
        local = Destination(
            id="local",
            name="Local temporário",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "local-repository"),
            password_environment="IGNORED",
        )
        with patch.dict(
            os.environ, {wrong_secret: "segredo-errado"}, clear=False
        ):
            result = BackupOrchestrator(
                self.adapter, StructuredLogger(io.StringIO())
            ).execute(self._task(bad, local), lambda _: self.password)

        self.assertEqual(result.status, ExecutionStatus.PARTIAL)
        self.assertEqual(result.destinations[0].status, DestinationStatus.FAILED)
        self.assertEqual(result.destinations[1].status, DestinationStatus.SUCCESS)

    def test_missing_credential_env_fails_s3_destination(self) -> None:
        connection = replace(
            self.connection, access_key_environment="MACIP_S3_ENV_INEXISTENTE"
        )
        destination = replace(self._s3_destination(), s3=connection)

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            self._task(destination), lambda _: self.password
        )

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("credencial ausente", result.destinations[0].error or "")

    def _s3_destination(self) -> Destination:
        return Destination(
            id="s3",
            name="S3 temporário",
            kind=DestinationKind.S3,
            repository=None,
            password_environment="IGNORED",
            s3=self.connection,
        )

    def _task(self, *destinations: Destination) -> TaskSpec:
        return TaskSpec(
            id="s3-integration",
            name="Integração S3 real",
            client=Client(id="client-s3", name="Cliente S3"),
            device_id="device-s3",
            sources=(self.source,),
            exclusions=("*.tmp",),
            compression=CompressionMode.AUTO,
            destinations=destinations,
        )

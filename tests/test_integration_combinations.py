"""Matriz de combinações de destinos em um único backup."""

from __future__ import annotations

import io
import os
import secrets
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.logging import StructuredLogger
from macip_backup.models import (
    Client,
    CompressionMode,
    Destination,
    DestinationKind,
    S3Connection,
    SFTPConnection,
    SMBMount,
    TaskSpec,
)
from macip_backup.orchestrator import BackupOrchestrator, ExecutionStatus
from macip_backup.restic import ResticAdapter


def _sftp_ready() -> bool:
    required = (
        "MACIP_SFTP_HOST",
        "MACIP_SFTP_USER",
        "MACIP_SFTP_REMOTE_PATH",
        "MACIP_SFTP_IDENTITY_FILE",
        "MACIP_SFTP_KNOWN_HOSTS_FILE",
    )
    return all(os.environ.get(name) for name in required)


def _smb_ready() -> bool:
    required = ("MACIP_SMB_SERVER", "MACIP_SMB_SHARE", "MACIP_SMB_MOUNT_POINT")
    return os.environ.get("MACIP_SMB_INTEGRATION") == "1" and all(
        os.environ.get(name) for name in required
    )


def _s3_ready() -> bool:
    required = (
        "MACIP_S3_ENDPOINT",
        "MACIP_S3_BUCKET",
        "MACIP_S3_ACCESS_KEY_ENV",
        "MACIP_S3_SECRET_KEY_ENV",
    )
    return (
        os.environ.get("MACIP_S3_INTEGRATION") == "1"
        and all(os.environ.get(name) for name in required)
        and bool(os.environ.get(os.environ.get("MACIP_S3_ACCESS_KEY_ENV", ""), ""))
        and bool(os.environ.get(os.environ.get("MACIP_S3_SECRET_KEY_ENV", ""), ""))
    )


def _environment_ready() -> bool:
    return (
        shutil.which("restic") is not None
        and _sftp_ready()
        and _smb_ready()
        and _s3_ready()
    )


@unittest.skipUnless(
    _environment_ready(), "nem todos os backends de destino estão preparados"
)
class DestinationCombinationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password = secrets.token_urlsafe(32)
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-combo-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        (self.source / "documents").mkdir(parents=True)
        (self.source / "documents" / "report.txt").write_text("dados combinados\n")
        self.adapter = ResticAdapter(cache_directory=self.root / "cache")
        self.smb_repositories: list[Path] = []

    def tearDown(self) -> None:
        for repository in self.smb_repositories:
            shutil.rmtree(repository, ignore_errors=True)
        self.temp_dir.cleanup()

    def test_all_backends_in_single_task_and_each_restores(self) -> None:
        destinations = (
            self._local_destination(),
            self._sftp_destination(),
            self._smb_destination(),
            self._s3_destination(),
        )
        task = self._task(destinations)

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            task, lambda _: self.password
        )

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(
            {destination.destination_id for destination in result.destinations},
            {"local", "sftp", "smb", "s3"},
        )

        fresh = ResticAdapter(cache_directory=self.root / "cache-fresh")
        for index, destination in enumerate(destinations):
            with self.subTest(destination=destination.id):
                snapshot_id = result.destinations[index].snapshot_id or ""
                self.assertTrue(snapshot_id)
                fresh.check(destination, self.password, read_data=True)
                restored = self.root / f"restored-{destination.id}"
                fresh.restore(
                    destination, self.password, snapshot_id, restored, self.source
                )
                self.assertEqual(
                    next(restored.rglob("report.txt")).read_text(),
                    "dados combinados\n",
                )

    def test_remote_backends_without_local_in_single_task(self) -> None:
        destinations = (
            self._sftp_destination(),
            self._smb_destination(),
            self._s3_destination(),
        )

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            self._task(destinations), lambda _: self.password
        )

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        self.assertEqual(len(result.destinations), 3)

    def _local_destination(self) -> Destination:
        return Destination(
            id="local",
            name="Local",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "local-repository"),
            password_environment="IGNORED",
        )

    def _sftp_destination(self) -> Destination:
        repository_id = f"{os.environ['MACIP_SFTP_REMOTE_PATH'].rstrip('/')}/macip-combo-{secrets.token_hex(6)}"
        return Destination(
            id="sftp",
            name="SFTP",
            kind=DestinationKind.SFTP,
            repository=None,
            password_environment="IGNORED",
            sftp=SFTPConnection(
                host=os.environ["MACIP_SFTP_HOST"],
                port=int(os.environ.get("MACIP_SFTP_PORT", "22")),
                user=os.environ["MACIP_SFTP_USER"],
                remote_path=repository_id,
                identity_file=os.environ["MACIP_SFTP_IDENTITY_FILE"],
                known_hosts_file=os.environ["MACIP_SFTP_KNOWN_HOSTS_FILE"],
            ),
        )

    def _smb_destination(self) -> Destination:
        mount = SMBMount(
            server=os.environ["MACIP_SMB_SERVER"],
            share=os.environ["MACIP_SMB_SHARE"],
            mount_point=os.environ["MACIP_SMB_MOUNT_POINT"],
            marker=os.environ.get("MACIP_SMB_MARKER") or None,
        )
        repository_id = f"macip-combo-{secrets.token_hex(6)}"
        destination = Destination(
            id="smb",
            name="SMB",
            kind=DestinationKind.SMB,
            repository=repository_id,
            password_environment="IGNORED",
            smb=mount,
        )
        self.smb_repositories.append(destination.repository_path)
        return destination

    def _s3_destination(self) -> Destination:
        base_prefix = os.environ.get("MACIP_S3_PREFIX", "").rstrip("/")
        repository_id = f"macip-combo-{secrets.token_hex(6)}"
        prefix = f"{base_prefix}/{repository_id}" if base_prefix else repository_id
        return Destination(
            id="s3",
            name="S3",
            kind=DestinationKind.S3,
            repository=None,
            password_environment="IGNORED",
            s3=S3Connection(
                endpoint=os.environ["MACIP_S3_ENDPOINT"],
                bucket=os.environ["MACIP_S3_BUCKET"],
                access_key_environment=os.environ["MACIP_S3_ACCESS_KEY_ENV"],
                secret_key_environment=os.environ["MACIP_S3_SECRET_KEY_ENV"],
                prefix=prefix,
                region=os.environ.get("MACIP_S3_REGION") or None,
                bucket_lookup=os.environ.get("MACIP_S3_BUCKET_LOOKUP") or None,
            ),
        )

    def _task(self, destinations: tuple[Destination, ...]) -> TaskSpec:
        return TaskSpec(
            id="combinations",
            name="Matriz de combinações",
            client=Client(id="client-combo", name="Cliente combinações"),
            device_id="device-combo",
            sources=(self.source,),
            exclusions=(),
            compression=CompressionMode.AUTO,
            destinations=destinations,
        )

"""Integrações reais SMB; exigem montagem preparada pelo sistema."""

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
    SMBMount,
    Source,
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
        "MACIP_SMB_INTEGRATION",
        "MACIP_SMB_SERVER",
        "MACIP_SMB_SHARE",
        "MACIP_SMB_MOUNT_POINT",
    )
    return (
        shutil.which("restic") is not None
        and os.environ.get("MACIP_SMB_INTEGRATION") == "1"
        and all(os.environ.get(name) for name in required)
    )


@unittest.skipUnless(_environment_ready(), "montagem SMB de teste não preparada")
class SMBIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.password = secrets.token_urlsafe(32)
        self.temp_dir = tempfile.TemporaryDirectory(prefix=".test-smb-", dir=Path.cwd())
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        (self.source / "documents").mkdir(parents=True)
        (self.source / "documents" / "report.txt").write_text("dados SMB\n")
        (self.source / "ignored.tmp").write_text("ignorar\n")
        self.mount = SMBMount(
            server=os.environ["MACIP_SMB_SERVER"],
            share=os.environ["MACIP_SMB_SHARE"],
            mount_point=os.environ["MACIP_SMB_MOUNT_POINT"],
            marker=os.environ.get("MACIP_SMB_MARKER") or None,
        )
        self.adapter = ResticAdapter(cache_directory=self.root / "cache")
        self.repositories: list[Path] = []

    def tearDown(self) -> None:
        for repository in self.repositories:
            shutil.rmtree(repository, ignore_errors=True)
        self.temp_dir.cleanup()

    def test_smb_backup_check_restore(self) -> None:
        destination = self._smb_destination()
        task = TaskSpec(
            id="smb-integration",
            name="Integração SMB real",
            client=Client(id="client-smb", name="Cliente SMB"),
            device_id="device-smb",
            sources=(self.source,),
            exclusions=("*.tmp",),
            compression=CompressionMode.AUTO,
            destinations=(destination,),
        )

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            task, lambda _: self.password
        )

        self.assertEqual(result.status, ExecutionStatus.SUCCESS)
        snapshot_id = result.destinations[0].snapshot_id or ""
        self.assertTrue(snapshot_id)

        fresh = ResticAdapter(cache_directory=self.root / "cache-fresh")
        fresh.check(destination, self.password, read_data=True)
        restored = self.root / "restored"
        fresh.restore(destination, self.password, snapshot_id, restored, self.source)
        self.assertEqual(
            next(restored.rglob("report.txt")).read_text(), "dados SMB\n"
        )
        self.assertFalse(list(restored.rglob("ignored.tmp")))

    def test_wrong_share_identity_fails_only_smb_destination(self) -> None:
        local = Destination(
            id="local",
            name="Local temporário",
            kind=DestinationKind.LOCAL,
            repository=str(self.root / "local-repository"),
            password_environment="IGNORED",
        )
        wrong_share = Destination(
            id="smb-wrong",
            name="Compartilhamento errado",
            kind=DestinationKind.SMB,
            repository="macip-test-wrong-share",
            password_environment="IGNORED",
            smb=SMBMount(
                server=self.mount.server,
                share="compartilhamento-inexistente",
                mount_point=self.mount.mount_point,
            ),
        )
        task = TaskSpec(
            id="smb-mismatch",
            name="Identidade SMB incorreta",
            client=Client(id="client-smb", name="Cliente SMB"),
            device_id="device-smb",
            sources=(self.source,),
            exclusions=(),
            compression=CompressionMode.AUTO,
            destinations=(wrong_share, local),
        )

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            task, lambda _: self.password
        )

        self.assertEqual(result.status, ExecutionStatus.PARTIAL)
        self.assertEqual(result.destinations[0].status, DestinationStatus.FAILED)
        self.assertEqual(result.destinations[1].status, DestinationStatus.SUCCESS)

    def test_missing_marker_fails_smb_destination(self) -> None:
        mount = SMBMount(
            server=self.mount.server,
            share=self.mount.share,
            mount_point=self.mount.mount_point,
            marker=f".macip-ausente-{secrets.token_hex(4)}",
        )
        destination = Destination(
            id="smb-marker",
            name="Marcador ausente",
            kind=DestinationKind.SMB,
            repository="macip-test-missing-marker",
            password_environment="IGNORED",
            smb=mount,
        )
        task = TaskSpec(
            id="smb-marker",
            name="Marcador SMB ausente",
            client=Client(id="client-smb", name="Cliente SMB"),
            device_id="device-smb",
            sources=(self.source,),
            exclusions=(),
            compression=CompressionMode.AUTO,
            destinations=(destination,),
        )

        result = BackupOrchestrator(self.adapter, StructuredLogger(io.StringIO())).execute(
            task, lambda _: self.password
        )

        self.assertEqual(result.status, ExecutionStatus.FAILED)
        self.assertIn("marcador", result.destinations[0].error or "")

    def test_smb_source_is_validated_against_mount(self) -> None:
        mount = SMBMount(
            server=self.mount.server,
            share=self.mount.share,
            mount_point=self.mount.mount_point,
        )
        smb_source = Source(
            path=Path(self.mount.mount_point_path), smb=mount
        )
        smb_source.validate_for_execution()

        outside = Source(path=Path("/fora/do/volume"), smb=mount)
        try:
            outside.validate_for_execution()
        except Exception as error:
            self.assertIn("fora do ponto de montagem", str(error))
        else:
            self.fail("origem fora do ponto de montagem não foi rejeitada")

    def _smb_destination(self) -> Destination:
        repository_id = f"macip-test-{self.id().rsplit('.', 1)[-1]}-{secrets.token_hex(6)}"
        destination = Destination(
            id="smb",
            name="SMB temporário",
            kind=DestinationKind.SMB,
            repository=repository_id,
            password_environment="IGNORED",
            smb=self.mount,
        )
        self.repositories.append(destination.repository_path)
        return destination

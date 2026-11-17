"""Executor A em árvore descartável; Restic e ssh-keygen reais quando presentes."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from macip_backup_api.provisioner import (
    LabFileVault,
    LabPrimaryAdapter,
    ProvisioningFailure,
    reconcile_operation,
)


@unittest.skipUnless(shutil.which("restic") and shutil.which("ssh-keygen"),
                     "Restic/ssh-keygen locais indisponíveis")
class ProvisionerLabTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".test-provisioner-", dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        root.chmod(0o700)
        self.vault = LabFileVault(root / "vault")
        self.primary = LabPrimaryAdapter(
            root / "primary-a",
            host="backup.lab",
            known_hosts="backup.lab ssh-ed25519 " + "A" * 44,
        )
        self.client_id = str(uuid.uuid4())
        self.installation_id = str(uuid.uuid4())
        self.operation = {
            "id": str(uuid.uuid4()),
            "claim_id": str(uuid.uuid4()),
            "attempt": 1,
            "client_id": self.client_id,
            "client_name": "Cliente Fictício",
            "installation_id": self.installation_id,
            "installation_identifier": "pc-lab-01",
            "machine_name": "PC-LAB",
            "suggested_time": "22:30",
            "account_name": "macip" + hashlib.sha256(
                f"{self.client_id}:pc-lab-01".encode()
            ).hexdigest()[:20],
        }

    def test_real_restic_reconciliation_is_idempotent_and_profile_matches_windows_013(self):
        first = reconcile_operation(self.operation, self.vault, self.primary)
        second = reconcile_operation(self.operation, self.vault, self.primary)
        self.assertEqual(first, second)
        self.assertRegex(first["repository_id"], r"^[a-f0-9]{64}$")
        self.assertEqual(
            set(first["profile"]),
            {"nome", "repositorio", "repositorio_id", "known_hosts", "horario_sugerido"},
        )
        self.assertEqual(first["profile"]["repositorio_id"], first["repository_id"])
        self.assertEqual(
            first["profile"]["repositorio"],
            f"sftp:{self.operation['account_name']}@backup.lab:repository",
        )
        authorized = (
            self.primary.account_path(self.operation["account_name"])
            / ".ssh/authorized_keys"
        )
        self.assertTrue(authorized.read_text().startswith("ssh-ed25519 "))

        # Contrato do consumidor congelado: lê os mesmos cinco campos.
        try:
            source = subprocess.check_output(
                ["git", "show", "7d62a800325146d1357f8832b1df5c4c3b2eff8d:agent/windows/gui/MacipBackup-Gui.ps1"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            self.skipTest("commit congelado Windows 0.1.3 indisponível neste clone")
        self.assertIn(
            "$script:PerfilCampos = @('nome', 'repositorio', 'repositorio_id', 'known_hosts', 'horario_sugerido')",
            source,
        )

    def test_missing_secret_after_state_exists_is_not_regenerated(self):
        reconcile_operation(self.operation, self.vault, self.primary)
        reference = f"installation/{self.installation_id}/bundle"
        self.vault._path(reference).unlink()
        with self.assertRaisesRegex(ProvisioningFailure, "secret_missing"):
            reconcile_operation(self.operation, self.vault, self.primary)
        self.assertFalse(self.vault._path(reference).exists())

    def test_repository_id_divergence_stops_reconciliation(self):
        reconcile_operation(self.operation, self.vault, self.primary)
        account = self.primary.account_path(self.operation["account_name"])
        (account / ".repository-id").write_text("b" * 64 + "\n")
        with self.assertRaisesRegex(ProvisioningFailure, "repository_mismatch"):
            reconcile_operation(self.operation, self.vault, self.primary)

    def test_foreign_account_identity_is_access_denied(self):
        reconcile_operation(self.operation, self.vault, self.primary)
        marker = self.primary.account_path(self.operation["account_name"]) / "installation.json"
        marker.write_text(json.dumps({"installation_id": str(uuid.uuid4()), "account": "foreign"}))
        with self.assertRaisesRegex(ProvisioningFailure, "access_denied"):
            reconcile_operation(self.operation, self.vault, self.primary)

    def test_account_symlink_is_denied_before_generating_secret(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(mode=0o700)
        account = self.primary.account_path(self.operation["account_name"])
        account.symlink_to(outside, target_is_directory=True)
        reference = f"installation/{self.installation_id}/bundle"
        with self.assertRaisesRegex(ProvisioningFailure, "access_denied"):
            reconcile_operation(self.operation, self.vault, self.primary)
        self.assertFalse(self.vault._path(reference).exists())


if __name__ == "__main__":
    unittest.main()

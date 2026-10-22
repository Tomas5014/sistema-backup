"""Resolução de credenciais: ambiente ou kernel keyring."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.credentials import credential_from_environment_or_os
from macip_backup.restic import ResticError


class CredentialResolutionTests(unittest.TestCase):
    def test_environment_variable_wins_without_keyctl(self) -> None:
        with patch.dict(os.environ, {"MACIP_TEST_SECRET": "da-env"}), patch(
            "macip_backup.credentials._keyctl_lookup"
        ) as keyctl:
            self.assertEqual(
                credential_from_environment_or_os("MACIP_TEST_SECRET"), "da-env"
            )
        keyctl.assert_not_called()

    def test_falls_back_to_kernel_keyring(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="segredo-do-keyring\n", stderr=""
        )
        with patch.dict(os.environ, {}, clear=True), patch(
            "macip_backup.credentials.subprocess.run", return_value=completed
        ) as run:
            self.assertEqual(
                credential_from_environment_or_os("MINHA_CRED"), "segredo-do-keyring"
            )
        command = run.call_args.args[0]
        self.assertEqual(command[:2], [command[0], "pipe"])
        self.assertIn("%user:macip.MINHA_CRED", command[2])

    def test_missing_credential_raises_controlled_error(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="key not found"
        )
        with patch.dict(os.environ, {}, clear=True), patch(
            "macip_backup.credentials.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(ResticError, "credencial ausente"):
                credential_from_environment_or_os("INEXISTENTE")

    def test_keyctl_absent_is_controlled_error_not_fallback_silence(self) -> None:
        with patch.dict(os.environ, {}, clear=True), patch(
            "macip_backup.credentials.shutil.which", return_value=None
        ):
            with self.assertRaisesRegex(ResticError, "credencial ausente"):
                credential_from_environment_or_os("INEXISTENTE")

    def test_never_returns_empty_secret(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="\n", stderr=""
        )
        with patch.dict(os.environ, {}, clear=True), patch(
            "macip_backup.credentials.subprocess.run", return_value=completed
        ):
            with self.assertRaisesRegex(ResticError, "credencial ausente"):
                credential_from_environment_or_os("VAZIA")


@unittest.skipUnless(shutil.which("keyctl"), "keyctl não está instalado")
class KernelKeyringRealTests(unittest.TestCase):
    """Round-trip real no kernel keyring da sessão de testes."""

    def test_real_keyctl_roundtrip_and_absent_key(self) -> None:
        name = f"teste-{secrets.token_hex(6)}"
        key_name = f"macip.{name}"
        subprocess.run(
            ["keyctl", "add", "user", key_name, "segredo-real", "@s"],
            check=True,
            capture_output=True,
        )
        try:
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    credential_from_environment_or_os(name), "segredo-real"
                )
        finally:
            key_id = subprocess.run(
                ["keyctl", "search", "@s", "user", key_name],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if key_id:
                subprocess.run(
                    ["keyctl", "revoke", key_id], check=False, capture_output=True
                )

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ResticError, "credencial ausente"):
                credential_from_environment_or_os(f"ausente-{secrets.token_hex(4)}")

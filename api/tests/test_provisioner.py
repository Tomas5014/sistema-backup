"""Executor A em árvore descartável; Restic e ssh-keygen reais quando presentes."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import shutil
import tempfile
from threading import Thread
import unittest
import uuid

from macip_backup_api.provisioner import (
    ApiTransport,
    LabFileVault,
    LabPrimaryAdapter,
    ProvisioningFailure,
    reconcile_operation,
)


class ApiTransportTests(unittest.TestCase):
    def test_https_and_explicit_exact_loopback_exception(self):
        ApiTransport("https://backup.lab", "synthetic", "test-a")
        ApiTransport("http://127.0.0.1:8080", "synthetic", "test-a", allow_lab_http=True)
        with self.assertRaises(ValueError):
            ApiTransport("http://127.0.0.1:8080", "synthetic", "test-a")
        for url in (
            "http://127.0.0.1.example.test",
            "http://127.0.0.1@other.example.test",
            "http://localhost",
            "http://127.0.0.2",
            "http://[::1]",
            "https://u:synthetic-secret@backup.lab",
            "https://backup.lab:65536",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as error:
                ApiTransport(url, "synthetic", "test-a", allow_lab_http=True)
            self.assertNotIn("synthetic-secret", str(error.exception))

    def test_authenticated_request_never_follows_redirect(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/ok":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"operation": null}')
                elif self.path.startswith("/redirect/"):
                    self.send_response(int(self.path.rsplit("/", 1)[-1]))
                    self.send_header("Location", "/target")
                    self.end_headers()
                else:
                    self.send_response(500)
                    self.end_headers()

            do_GET = do_POST

            def log_message(self, *_args):
                pass

        with HTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                transport = ApiTransport(
                    f"http://127.0.0.1:{server.server_port}",
                    "synthetic", "test-a", allow_lab_http=True,
                )
                self.assertEqual(transport.request("/ok", {}), {"operation": None})
                redirects = [f"/redirect/{code}" for code in (301, 302, 303, 307, 308)]
                for path in redirects:
                    with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "recusado"):
                        transport.request(path, {})
                self.assertEqual(seen, [(p, "Bearer synthetic") for p in ["/ok", *redirects]])
            finally:
                server.shutdown()
                thread.join()


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

    def test_real_restic_reconciliation_is_idempotent(self):
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
            f"sftp://{self.operation['account_name']}@backup.lab//repository",
        )
        authorized = (
            self.primary.account_path(self.operation["account_name"])
            / ".ssh/authorized_keys"
        )
        self.assertTrue(authorized.read_text().startswith("ssh-ed25519 "))

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

    def test_missing_config_of_verified_repo_is_never_reinitialized(self):
        reconcile_operation(self.operation, self.vault, self.primary)
        account = self.primary.account_path(self.operation["account_name"])
        config = account / "repository/config"
        config.unlink()
        with self.assertRaisesRegex(ProvisioningFailure, "repository_mismatch"):
            reconcile_operation(self.operation, self.vault, self.primary)
        self.assertFalse(config.exists())

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

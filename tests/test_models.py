from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from macip_backup.models import (
    ConfigurationError,
    DestinationKind,
    SMBMount,
    Source,
    TaskSpec,
    _findmnt_entry,
    _probe_smb_path,
)


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
            ("sources", [42], "sources[0] deve ser texto ou objeto"),
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
            "forget_seconds": 8,
        }

        task = TaskSpec.from_dict(config)

        self.assertEqual(task.timeouts.backup_seconds, 4)
        self.assertEqual(task.timeouts.restore_seconds, 7)
        self.assertEqual(task.timeouts.forget_seconds, 8)

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


class SMBMountValidationTests(unittest.TestCase):
    """Valida a identidade do volume SMB sem depender de I/O de rede."""

    def _mount(self, **overrides: object) -> SMBMount:
        values: dict[str, object] = {
            "server": "nas.local",
            "share": "backups",
            "mount_point": "/mnt/nas",
        }
        values.update(overrides)
        return SMBMount(**values)  # type: ignore[arg-type]

    def _findmnt(self, mount: SMBMount, source: str, fstype: str, target: Path | None = None):
        return patch(
            "macip_backup.models._findmnt_entry",
            return_value=(target or mount.mount_point_path, source, fstype),
        )

    def test_accepts_expected_cifs_share_and_probes_marker(self) -> None:
        mount = self._mount(marker=".macip-volume")
        with self._findmnt(mount, "//nas.local/backups", "cifs"), patch(
            "macip_backup.models._probe_smb_path"
        ) as probe:
            mount.validate_for_execution()

        probe.assert_called_once_with(
            mount.mount_point_path / ".macip-volume", mount.probe_timeout_seconds
        )

    def test_rejects_point_that_is_not_a_mount(self) -> None:
        mount = self._mount()
        with self._findmnt(mount, "/dev/sda1", "ext4", target=Path("/")):
            with self.assertRaisesRegex(ConfigurationError, "não é ponto de montagem"):
                mount.validate_for_execution()

    def test_rejects_wrong_share_or_non_cifs_filesystem(self) -> None:
        mount = self._mount()
        for source, fstype, message in (
            ("//nas.local/outro-share", "cifs", "expõe //nas.local/outro-share"),
            ("//nas.local/backups", "nfs4", "não é CIFS/SMB"),
        ):
            with self.subTest(source=source, fstype=fstype), self._findmnt(
                mount, source, fstype
            ):
                with self.assertRaisesRegex(ConfigurationError, re.escape(message)):
                    mount.validate_for_execution()

    def test_marker_probe_failure_or_timeout_fails_validation(self) -> None:
        mount = self._mount(marker=".macip-volume")
        with self._findmnt(mount, "//nas.local/backups", "cifs"), patch(
            "macip_backup.models._probe_smb_path",
            side_effect=ConfigurationError("volume SMB não respondeu em 10s"),
        ):
            with self.assertRaisesRegex(ConfigurationError, "não respondeu"):
                mount.validate_for_execution()

    def test_probe_timeout_is_controlled_error(self) -> None:
        with patch(
            "macip_backup.models.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["stat"], 10),
        ):
            with self.assertRaisesRegex(ConfigurationError, "não respondeu em 10s"):
                _probe_smb_path(Path("/mnt/nas/.macip-volume"), 10)

    def test_findmnt_parses_pairs_and_rejects_failures(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='TARGET="/mnt/nas" SOURCE="//nas.local/backups" FSTYPE="cifs"\n',
            stderr="",
        )
        with patch(
            "macip_backup.models.shutil.which", return_value="/usr/bin/findmnt"
        ), patch("macip_backup.models.subprocess.run", return_value=completed):
            target, source, fstype = _findmnt_entry(Path("/mnt/nas"), 10)

        self.assertEqual(target, Path("/mnt/nas"))
        self.assertEqual((source, fstype), ("//nas.local/backups", "cifs"))

        failed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="not found"
        )
        with patch(
            "macip_backup.models.shutil.which", return_value="/usr/bin/findmnt"
        ), patch("macip_backup.models.subprocess.run", return_value=failed):
            with self.assertRaisesRegex(ConfigurationError, "não localizou"):
                _findmnt_entry(Path("/mnt/nas"), 10)


class SMBSourceValidationTests(unittest.TestCase):
    def test_source_inside_mount_runs_mount_validation(self) -> None:
        mount = SMBMount(server="nas", share="docs", mount_point="/mnt/docs")
        source = Source(path=Path("/mnt/docs/pasta"), smb=mount)
        with patch.object(SMBMount, "validate_for_execution") as validate:
            source.validate_for_execution()

        validate.assert_called_once()

    def test_source_outside_mount_is_rejected(self) -> None:
        mount = SMBMount(server="nas", share="docs", mount_point="/mnt/docs")
        source = Source(path=Path("/outro/lugar"), smb=mount)
        with patch.object(SMBMount, "validate_for_execution"):
            with self.assertRaisesRegex(ConfigurationError, "fora do ponto de montagem"):
                source.validate_for_execution()

    def test_source_without_smb_needs_no_mount(self) -> None:
        Source(path=Path("/srv/dados")).validate_for_execution()


class SMBConfigurationTests(unittest.TestCase):
    def _smb_destination(self, **overrides: object) -> dict[str, object]:
        destination: dict[str, object] = {
            "id": "nas",
            "name": "NAS",
            "kind": "smb",
            "repository": "backups/cliente-a",
            "password_environment": "MACIP_SMB_PASSWORD",
            "smb": {
                "server": "10.10.20.239",
                "share": "backups",
                "mount_point": "/mnt/nas-backups",
                "marker": ".macip-volume",
            },
        }
        destination.update(overrides)
        return destination

    def test_parses_smb_destination_and_object_source(self) -> None:
        config = valid_config()
        config["sources"] = [
            "/srv/local",
            {
                "path": "/mnt/nas-docs",
                "smb": {
                    "server": "10.10.20.239",
                    "share": "docs",
                    "mount_point": "/mnt/nas-docs",
                },
            },
        ]
        config["destinations"] = [self._smb_destination()]

        task = TaskSpec.from_dict(config)

        self.assertEqual(task.source_paths, (Path("/srv/local"), Path("/mnt/nas-docs")))
        assert task.sources[1].smb is not None
        self.assertEqual(task.sources[1].smb.share, "docs")
        destination = task.destinations[0]
        self.assertEqual(destination.kind, DestinationKind.SMB)
        self.assertEqual(
            destination.repository_path, Path("/mnt/nas-backups/backups/cliente-a")
        )

    def test_rejects_smb_destination_misconfigurations(self) -> None:
        cases = (
            ({"smb": None}, "smb deve ser um objeto"),
            ({"smb": "remover"}, "smb é obrigatório"),
            ({"repository": "/absoluto"}, "deve ser caminho relativo"),
            ({"repository": "backups/../fora"}, "segmento de caminho inválido"),
            ({"sftp": {"x": 1}}, "sftp exige kind sftp"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                config = valid_config()
                destination = self._smb_destination(**overrides)
                if overrides.get("smb") == "remover":
                    destination.pop("smb")
                config["destinations"] = [destination]
                with self.assertRaisesRegex(ConfigurationError, re.escape(message)):
                    TaskSpec.from_dict(config)

    def test_rejects_smb_block_on_non_smb_kinds(self) -> None:
        config = valid_config()
        config["destinations"][0]["smb"] = {  # type: ignore[index]
            "server": "nas",
            "share": "backups",
            "mount_point": "/mnt/nas",
        }
        with self.assertRaisesRegex(ConfigurationError, "exige kind smb"):
            TaskSpec.from_dict(config)

    def test_rejects_invalid_smb_fields(self) -> None:
        config = valid_config()
        config["destinations"] = [self._smb_destination()]
        smb = config["destinations"][0]["smb"]  # type: ignore[index]
        smb["server"] = "servidor com espaço"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "server contém caracteres"):
            TaskSpec.from_dict(config)

        config = valid_config()
        config["destinations"] = [self._smb_destination()]
        smb = config["destinations"][0]["smb"]  # type: ignore[index]
        smb["marker"] = "../fora"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "marker contém segmento de caminho inválido"):
            TaskSpec.from_dict(config)

        config = valid_config()
        config["destinations"] = [self._smb_destination()]
        smb = config["destinations"][0]["smb"]  # type: ignore[index]
        smb["mount_point"] = "mnt/relativo"  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "mount_point deve ser caminho absoluto"):
            TaskSpec.from_dict(config)


class S3ConfigurationTests(unittest.TestCase):
    def _s3_destination(self, **overrides: object) -> dict[str, object]:
        destination: dict[str, object] = {
            "id": "nuvem",
            "name": "Nuvem",
            "kind": "s3",
            "password_environment": "MACIP_RESTIC_PASSWORD_S3",
            "s3": {
                "endpoint": "http://10.10.20.239:9000",
                "bucket": "macip-backups",
                "prefix": "restic/cliente-a",
                "bucket_lookup": "path",
                "access_key_environment": "MACIP_S3_ACCESS",
                "secret_key_environment": "MACIP_S3_SECRET",
            },
        }
        destination.update(overrides)
        return destination

    def test_parses_s3_destination_and_builds_reference(self) -> None:
        config = valid_config()
        config["destinations"] = [self._s3_destination()]

        task = TaskSpec.from_dict(config)

        destination = task.destinations[0]
        self.assertEqual(destination.kind, DestinationKind.S3)
        assert destination.s3 is not None
        self.assertEqual(
            destination.s3.repository_reference,
            "s3:http://10.10.20.239:9000/macip-backups/restic/cliente-a",
        )

    def test_rejects_s3_misconfigurations(self) -> None:
        cases = (
            ({"endpoint": "ftp://servidor"}, "URL http(s) válida"),
            ({"endpoint": "http://user:senha@host"}, "não pode conter credenciais"),
            ({"endpoint": "http://host/caminho"}, "caminho, query ou fragmento"),
            ({"bucket": "Bucket_Inválido!"}, "nome de bucket válido"),
            ({"prefix": "../fora"}, "não é um caminho remoto permitido"),
            ({"prefix": "/absoluto"}, "relativo ao bucket"),
            ({"bucket_lookup": "virtual"}, "auto, path ou dns"),
        )
        for field, message in cases:
            with self.subTest(field=field):
                config = valid_config()
                destination = self._s3_destination()
                destination["s3"].update(field)  # type: ignore[union-attr]
                config["destinations"] = [destination]
                with self.assertRaisesRegex(ConfigurationError, re.escape(message)):
                    TaskSpec.from_dict(config)

    def test_rejects_repository_and_cross_fields_on_s3(self) -> None:
        config = valid_config()
        config["destinations"] = [self._s3_destination(repository="/tmp/repo")]
        with self.assertRaisesRegex(ConfigurationError, "repository não é permitido"):
            TaskSpec.from_dict(config)

        config = valid_config()
        config["destinations"][0]["s3"] = self._s3_destination()["s3"]  # type: ignore[index]
        with self.assertRaisesRegex(ConfigurationError, "s3 exige kind s3"):
            TaskSpec.from_dict(config)

    def test_s3_requires_s3_block(self) -> None:
        config = valid_config()
        destination = self._s3_destination()
        destination.pop("s3")
        config["destinations"] = [destination]
        with self.assertRaisesRegex(ConfigurationError, "s3 é obrigatório"):
            TaskSpec.from_dict(config)

    def test_s3_credentials_resolved_at_adapter_injection(self) -> None:
        from macip_backup.credentials import CredentialError
        from macip_backup.restic import ResticAdapter

        config = valid_config()
        config["destinations"] = [self._s3_destination()]
        destination = TaskSpec.from_dict(config).destinations[0]
        adapter = ResticAdapter()

        with patch.dict(os.environ, {}, clear=True), patch(
            "macip_backup.credentials.shutil.which", return_value=None
        ):
            with self.assertRaisesRegex(CredentialError, "credencial ausente"):
                adapter._inject_destination_credentials(destination, {})

        with patch.dict(
            os.environ, {"MACIP_S3_ACCESS": "key", "MACIP_S3_SECRET": "secret"}
        ):
            environment: dict[str, str] = {}
            adapter._inject_destination_credentials(destination, environment)
        self.assertEqual(
            environment,
            {"AWS_ACCESS_KEY_ID": "key", "AWS_SECRET_ACCESS_KEY": "secret"},
        )


class RetryPolicyTests(unittest.TestCase):
    def test_defaults_when_retry_absent(self) -> None:
        task = TaskSpec.from_dict(valid_config())
        self.assertEqual((task.retry.attempts, task.retry.backoff_seconds), (1, 0))

    def test_parses_retry_policy(self) -> None:
        config = valid_config()
        config["retry"] = {"attempts": 3, "backoff_seconds": 30}
        task = TaskSpec.from_dict(config)
        self.assertEqual((task.retry.attempts, task.retry.backoff_seconds), (3, 30))

    def test_rejects_invalid_retry_values(self) -> None:
        for retry, message in (
            ({"attempts": 0}, "attempts deve ser inteiro positivo"),
            ({"attempts": -2}, "attempts deve ser inteiro positivo"),
            ({"backoff_seconds": -1}, "backoff_seconds deve ser inteiro não negativo"),
            ({"backoff_seconds": True}, "backoff_seconds deve ser inteiro não negativo"),
        ):
            with self.subTest(retry=retry):
                config = valid_config()
                config["retry"] = retry
                with self.assertRaisesRegex(ConfigurationError, re.escape(message)):
                    TaskSpec.from_dict(config)

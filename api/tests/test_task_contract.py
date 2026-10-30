"""Contrato de exportação: a revisão exportada carrega no AGENTE atual.

Valida os 4 tipos de destino com `TaskSpec.from_dict` — sem executar
validações ambientais (findmnt/restic), só o parse estrutural. A
configuração é construída pelo schema central e exportada pelo mesmo
mapeamento da rota /export.

O agente é lido do fonte em src/ (teste pode ajustar sys.path; código
de produção da API nunca depende disso).
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from macip_backup.models import TaskSpec  # noqa: E402

from macip_backup_api.task_contract import (  # noqa: E402
    TaskConfigIn,
    export_taskspec,
)


def _doc(**overrides) -> dict:
    config = {
        "name": "Tarefa contrato",
        "spec": {
            "sources": ["/dados/x"],
            "exclusions": ["*.tmp"],
            "compression": "auto",
            "destinations": [],
        },
        "schedule": {"kind": "daily", "timezone": "UTC", "hour": 3,
                     "minute": 30},
    }
    config.update(overrides)
    return TaskConfigIn(**config).model_dump(mode="json", exclude_none=True)


def _export(payload: dict) -> dict:
    return export_taskspec(
        task_id=uuid.uuid4(),
        name=payload["name"],
        client_id=uuid.uuid4(),
        client_name="Cliente X",
        agent_id=uuid.uuid4(),
        spec=payload["spec"],
    )


@pytest.mark.parametrize(
    "kind,dest",
    [
        ("local", {
            "id": "d-local", "name": "Local", "kind": "local",
            "repository": "/srv/backup/repo",
            "password_environment": "MACIP_PW_LOCAL",
            "retention": {"keep_last": 7},
        }),
        ("sftp", {
            "id": "d-sftp", "name": "SFTP", "kind": "sftp",
            "password_environment": "MACIP_PW_SFTP",
            "sftp": {
                "host": "backup.example", "port": 22, "user": "bak",
                "remote_path": "repos/cliente",
                "identity_file": "/etc/macip/keys/k",
                "known_hosts_file": "/etc/macip/known_hosts",
            },
        }),
        ("smb", {
            "id": "d-smb", "name": "SMB", "kind": "smb",
            "repository": "repo/cliente",
            "password_environment": "MACIP_PW_SMB",
            "smb": {
                "server": "files01", "share": "backups",
                "mount_point": "/mnt/macip-backups",
                "marker": ".macip-marker",
                "probe_timeout_seconds": 10,
            },
        }),
        ("s3", {
            "id": "d-s3", "name": "S3", "kind": "s3",
            "password_environment": "MACIP_PW_S3",
            "s3": {
                "endpoint": "https://minio.lab.local",
                "bucket": "macip-backups",
                "access_key_environment": "MACIP_S3_ACCESS",
                "secret_key_environment": "MACIP_S3_SECRET",
                "prefix": "cliente-x",
                "region": "us-east-1",
            },
        }),
    ],
)
def test_export_carrega_no_taskspec_do_agente(kind, dest):
    payload = _doc(spec={
        "sources": ["/dados/x"],
        "destinations": [dest],
        "compression": "max",
        "retry": {"attempts": 3, "backoff_seconds": 60},
    })
    doc = _export(payload)
    spec = TaskSpec.from_dict(doc)  # parse real do agente
    assert spec.id == doc["id"]
    assert spec.device_id == doc["device_id"]
    assert spec.client.id == doc["client"]["id"]
    assert spec.compression.value == "max"
    assert spec.retry.attempts == 3
    assert spec.destinations[0].kind.value == kind


def test_sources_smb_e_multiplos_destinos_carregam():
    payload = _doc(spec={
        "sources": [
            "/dados/local",
            {
                "path": "/mnt/macip-backups/origem",
                "smb": {"server": "files01", "share": "dados",
                        "mount_point": "/mnt/macip-backups"},
            },
        ],
        "exclusions": ["*.bak"],
        "destinations": [
            {"id": "l", "name": "L", "kind": "local",
             "repository": "/srv/r", "password_environment": "PW"},
            {"id": "s", "name": "S", "kind": "s3",
             "password_environment": "PW3",
             "s3": {"endpoint": "https://m.local", "bucket": "bkt-abc",
                    "access_key_environment": "AK",
                    "secret_key_environment": "SK"}},
        ],
    })
    spec = TaskSpec.from_dict(_export(payload))
    assert len(spec.sources) == 2
    assert spec.sources[1].smb.server == "files01"
    assert spec.exclusions == ("*.bak",)


def test_origem_smb_fora_do_mount_rejeitada_na_central():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        _doc(spec={
            "sources": [{"path": "/outro/lugar",
                         "smb": {"server": "f", "share": "d",
                                 "mount_point": "/mnt/mb"}}],
            "destinations": [{"id": "d", "name": "d", "kind": "local",
                              "repository": "/srv/r",
                              "password_environment": "PW"}],
        })


# ---- entradas de borda que o agente rejeitaria -------------------------------
# A central não pode aceitar o que o TaskSpec.from_dict recusa depois —
# conferência LEXICAL apenas: nada de filesystem, Restic, SSH ou SMB.

from pydantic import ValidationError  # noqa: E402


def _spec(destinations=None, sources=None, **kw):
    spec = {
        "sources": sources or ["/dados/x"],
        "destinations": destinations or [
            {"id": "d", "name": "d", "kind": "local",
             "repository": "/srv/b", "password_environment": "PW"},
        ],
    }
    spec.update(kw)
    return spec


@pytest.mark.parametrize("repository", [
    "/backup/../dados/x/repository",   # resolve para DENTRO da origem
    "/backup/sub/../../dados/x/repo",
    "/srv//backup",                    # separador duplicado
    "/",                               # raiz inteira
    "srv/backup",                      # relativo
])
def test_repository_local_com_segmento_inseguro_rejeitado(repository):
    with pytest.raises(ValidationError):
        _doc(spec=_spec(destinations=[
            {"id": "d", "name": "d", "kind": "local",
             "repository": repository, "password_environment": "PW"},
        ]))


def test_repository_local_com_espaco_legitimo_passa():
    """Espaços são caminhos legítimos no Linux — preservados."""
    payload = _doc(spec=_spec(destinations=[
        {"id": "d", "name": "d", "kind": "local",
         "repository": "/srv/backup dir/rep ository",
         "password_environment": "PW"},
    ]))
    doc = _export(payload)
    spec = TaskSpec.from_dict(doc)
    assert str(spec.destinations[0].repository) == "/srv/backup dir/rep ository"


@pytest.mark.parametrize("field", [
    "identity_file", "known_hosts_file", "remote_path",
])
def test_sftp_campo_de_caminho_rejeita_controle(field):
    """newline/caractere de controle quebraria o TaskSpec no agente."""
    sftp = {
        "host": "h", "user": "u", "remote_path": "r",
        "identity_file": "/k", "known_hosts_file": "/kh",
    }
    sftp[field] = "/linha\nquebrada"
    with pytest.raises(ValidationError):
        _doc(spec=_spec(destinations=[
            {"id": "d", "name": "d", "kind": "sftp",
             "password_environment": "PW", "sftp": sftp},
        ]))


@pytest.mark.parametrize("value", [
    "/dados\nx", "/dados\tx", "/dados\x00x", "nome\tdestino",
])
def test_caminhos_e_textos_rejeitam_caractere_de_controle(value):
    with pytest.raises(ValidationError):
        _doc(spec=_spec(sources=[value]))


def test_retry_attempts_limitado_ao_teto_do_recebimento():
    """64 é o MAX_ATTEMPTS do schemas.py — acima disso a central
    receberia tentativas que o relatório não consegue representar."""
    with pytest.raises(ValidationError):
        _doc(spec=_spec(retry={"attempts": 65}))
    # no limite passa
    _doc(spec=_spec(retry={"attempts": 64}))


def test_origem_com_espaco_legitimo_passa():
    payload = _doc(spec=_spec(sources=["/dados do financeiro"]))
    spec = TaskSpec.from_dict(_export(payload))
    assert str(spec.sources[0].path) == "/dados do financeiro"

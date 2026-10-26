"""Agendamento via systemd --user: um par service+timer por tarefa.

O service é `Type=oneshot` chamando `python -m macip_backup.cli agent`
— o mesmo caminho da CLI, compartilhando lock de execução e resolução
de credenciais. `EnvironmentFile=-%h/.config/macip-backup/agent.env`
(opcional, modo 600) é a forma de prover segredos sem sessão de
terminal — complementada pelo kernel keyring `@u` (persiste logout,
até reboot). O timer usa `OnCalendar` informado pelo operador com
`Persistent=true` para recuperar execuções perdidas com a máquina off.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .models import ConfigurationError, TaskSpec

_UNIT_PREFIX = "macip-backup-agent-"
_SYNC_UNIT_PREFIX = "macip-backup-sync-"


def unit_directory() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _escape_unit(value: object) -> str:
    """Escape de conteúdo para sintaxe de unit systemd.

    NÃO é shlex.quote: dentro de aspas duplas, o systemd escapa apenas
    `\\` e `"` (e expansões `%`/`$` não se aplicam a caminhos gerados
    aqui). Serve para argumentos de ExecStart e valores de Environment.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _unit_value(value: object) -> str:
    return f'"{_escape_unit(value)}"'


def _installed_launcher() -> Path | None:
    """Launcher do deploy/install.sh — presente só na instalação operacional."""
    bin_dir = Path(
        os.environ.get("MACIP_BIN", Path.home() / ".local" / "bin")
    )
    launcher = bin_dir / "macip-backup"
    return launcher if launcher.is_file() else None


def _installation_root(launcher: Path | None) -> str:
    """Identidade estável gravada em cada unit desta instalação."""
    configured = os.environ.get("MACIP_HOME")
    if configured:
        return configured
    if launcher is not None:
        # Launcher normal: <MACIP_HOME>/bin/macip-backup.
        return str(launcher.parent.parent)
    # Desenvolvimento: src/macip_backup/systemd.py → raiz do checkout.
    return str(Path(__file__).resolve().parents[2])


def _ownership_marker(install_root: str) -> str:
    return f"# macip-backup: install={install_root}"


def _owns_unit(path: Path, install_root: str) -> bool:
    if not path.exists():
        return False
    try:
        return _ownership_marker(install_root) in path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False


def _assert_writable_units(paths: tuple[Path, ...], install_root: str) -> None:
    """Nunca substitui unit alheia ou sem marcador exato."""
    for path in paths:
        if path.exists() and not _owns_unit(path, install_root):
            raise ConfigurationError(
                f"{path} existe e sua propriedade não é desta instalação — conteúdo preservado"
            )


def _sync_identity(store_path: Path, central_path: Path) -> str:
    """Sem segredo: impede outro banco/config de substituir timer atual."""
    material = f"{store_path}\0{central_path}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _sync_unit_name(identity: str) -> str:
    """Nome estável por vínculo banco+central, seguro para testes e produção."""
    return f"{_SYNC_UNIT_PREFIX}{identity[:16]}"


def _validate_calendar(calendar: str) -> None:
    """Rejeita expressão OnCalendar inválida ANTES de tocar nas units."""
    tool = shutil.which("systemd-analyze")
    if tool is None:
        raise ConfigurationError(
            "systemd-analyze ausente — não é possível validar OnCalendar"
        )
    completed = subprocess.run(
        [tool, "calendar", calendar],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ConfigurationError(
            f"expressão OnCalendar inválida '{calendar}': {detail}"
        )


def _systemctl(*args: str, check: bool = True) -> None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        raise ConfigurationError("systemctl não está instalado")
    completed = subprocess.run(
        [systemctl, "--user", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if check and completed.returncode != 0:
        raise ConfigurationError(
            f"systemctl --user {' '.join(args)} falhou: {completed.stderr.strip()}"
        )


def _execution_lines(
    command: str,
    arguments: str,
    install_root: str,
) -> tuple[str, str]:
    """Monta ExecStart para launcher instalado ou checkout de desenvolvimento."""
    launcher = _installed_launcher()
    if launcher is not None:
        return f"ExecStart={_unit_value(launcher)} {command} {arguments}", ""
    src_root = Path(__file__).resolve().parents[1]
    return (
        f"ExecStart={_unit_value(sys.executable)} -m macip_backup.cli {command} {arguments}",
        f'Environment="PYTHONPATH={_escape_unit(src_root)}"\n',
    )


def _environment_file_line() -> str:
    config_dir = os.environ.get("MACIP_CONFIG")
    # EnvironmentFile trata a linha inteira como filename; aspas virariam
    # parte do nome. O conteúdo continua fora de argumentos/processos.
    if config_dir:
        return f"EnvironmentFile=-{Path(config_dir) / 'agent.env'}"
    return "EnvironmentFile=-%h/.config/macip-backup/agent.env"


def _write_unit_pair(
    name: str,
    description: str,
    service_body: str,
    calendar: str,
    install_root: str,
    extra_markers: tuple[str, ...] = (),
) -> tuple[Path, Path]:
    unit_dir = unit_directory()
    unit_dir.mkdir(parents=True, exist_ok=True)
    service_path = unit_dir / f"{name}.service"
    timer_path = unit_dir / f"{name}.timer"
    _assert_writable_units((service_path, timer_path), install_root)
    markers = "\n".join((_ownership_marker(install_root), *extra_markers))
    service_path.write_text(
        f"""{markers}
[Unit]
Description={description}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{service_body}""",
        encoding="utf-8",
    )
    timer_path.write_text(
        f"""{markers}
[Unit]
Description=Agendamento: {description}

[Timer]
OnCalendar={calendar}
Persistent=true
AccuracySec=1s

[Install]
WantedBy=timers.target
""",
        encoding="utf-8",
    )
    return service_path, timer_path


def install_schedule(
    task: TaskSpec,
    config_path: Path,
    store_path: Path,
    calendar: str,
    enable: bool = False,
    central_path: Path | None = None,
) -> dict[str, str]:
    """Escreve service+timer systemd --user para a tarefa."""
    _validate_calendar(calendar)  # antes de sobrescrever units existentes
    config_path = Path(config_path).resolve()
    store_path = Path(store_path).resolve()
    name = f"{_UNIT_PREFIX}{task.id}"
    launcher = _installed_launcher()
    install_root = _installation_root(launcher)
    if central_path is not None:
        from .central import load_central

        central_path = Path(central_path).resolve()
        load_central(central_path)
        central_argument = f" --central {_unit_value(central_path)}"
    else:
        central_argument = ""
    exec_line, pythonpath_line = _execution_lines(
        "agent",
        f"{_unit_value(config_path)} --store {_unit_value(store_path)}{central_argument}",
        install_root,
    )
    service_path, timer_path = _write_unit_pair(
        name,
        f"MAC IP Backup agent — tarefa {task.id}",
        f"{pythonpath_line}{_environment_file_line()}\n{exec_line}\n",
        calendar,
        install_root,
    )

    if enable:
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", f"{name}.timer")

    return {
        "task_id": task.id,
        "service": str(service_path),
        "timer": str(timer_path),
        "timer_name": f"{name}.timer",
        "enabled": enable,
        "hint": (
            f"systemctl --user daemon-reload && "
            f"systemctl --user enable --now {name}.timer"
        ),
    }


def install_sync_schedule(
    store_path: Path,
    central_path: Path,
    calendar: str,
    limit: int = 50,
    enable: bool = False,
) -> dict[str, str]:
    """Instala timer independente para rodadas limitadas de ``sync``.

    Cada vínculo banco+central recebe timer próprio e estável. Trocar qualquer
    um cria outro par, nunca substitui silenciosamente um agendamento anterior.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ConfigurationError("--limit deve ser inteiro positivo")
    _validate_calendar(calendar)
    store_path = Path(store_path).resolve()
    central_path = Path(central_path).resolve()
    from .central import load_central

    load_central(central_path)
    launcher = _installed_launcher()
    install_root = _installation_root(launcher)
    identity = _sync_identity(store_path, central_path)
    name = _sync_unit_name(identity)
    unit_dir = unit_directory()
    sync_paths = tuple(
        unit_dir / f"{name}.{suffix}" for suffix in ("service", "timer")
    )
    marker = f"# macip-backup: sync-id={identity}"
    for path in sync_paths:
        if path.exists() and _owns_unit(path, install_root):
            if marker not in path.read_text(encoding="utf-8").splitlines():
                raise ConfigurationError(f"{path} tem identidade de sync inválida")
    exec_line, pythonpath_line = _execution_lines(
        "sync",
        f"--store {_unit_value(store_path)} --central {_unit_value(central_path)} "
        f"--limit {limit}",
        install_root,
    )
    service_path, timer_path = _write_unit_pair(
        name,
        "MAC IP Backup — sincronização da outbox",
        f"{pythonpath_line}{_environment_file_line()}\n{exec_line}\n",
        calendar,
        install_root,
        ("# macip-backup: schedule=sync", f"# macip-backup: sync-id={identity}"),
    )
    if enable:
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", f"{name}.timer")
    return {
        "store": str(store_path),
        "central": str(central_path),
        "service": str(service_path),
        "timer": str(timer_path),
        "timer_name": f"{name}.timer",
        "limit": str(limit),
        "enabled": enable,
        "hint": (
            f"systemctl --user daemon-reload && systemctl --user enable --now "
            f"{name}.timer"
        ),
    }


def uninstall_schedule(task_id: str) -> dict[str, str]:
    """Desativa e remove service+timer da tarefa."""
    name = f"{_UNIT_PREFIX}{task_id}"
    return _uninstall_unit_pair(name, {"task_id": task_id})


def uninstall_sync_schedule(store_path: Path, central_path: Path) -> dict[str, str]:
    """Remove somente o timer de sync cujo banco/central foram selecionados."""
    store_path = Path(store_path).resolve()
    central_path = Path(central_path).resolve()
    install_root = _installation_root(_installed_launcher())
    identity = _sync_identity(store_path, central_path)
    name = _sync_unit_name(identity)
    sync_paths = tuple(
        unit_directory() / f"{name}.{suffix}"
        for suffix in ("service", "timer")
    )
    expected = f"# macip-backup: sync-id={identity}"
    for path in sync_paths:
        if not path.exists():
            continue
        if not _owns_unit(path, install_root):
            raise ConfigurationError(
                f"{path} existe e sua propriedade não é desta instalação — conteúdo preservado"
            )
        if expected not in path.read_text(encoding="utf-8").splitlines():
            raise ConfigurationError(
                "agendamento de sync pertence a outro banco ou configuração central — conteúdo preservado"
            )
    return _uninstall_unit_pair(name, {"store": str(store_path), "central": str(central_path)})


def _uninstall_unit_pair(name: str, result: dict[str, str]) -> dict[str, str]:
    """Desativa antes de remover, somente quando propriedade é comprovada."""
    unit_dir = unit_directory()
    install_root = _installation_root(_installed_launcher())
    paths = tuple(unit_dir / f"{name}.{suffix}" for suffix in ("timer", "service"))
    existing = tuple(path for path in paths if path.exists())
    _assert_writable_units(existing, install_root)
    if not existing:
        return {**result, "removed": []}
    # Se disable falhar, NADA é apagado. Isso preserva recuperação manual.
    _systemctl("disable", "--now", f"{name}.timer")
    removed = []
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(str(path))
    _systemctl("daemon-reload")
    return {**result, "removed": removed}


def schedule_json(installation: dict[str, object]) -> str:
    return json.dumps(installation, ensure_ascii=False)

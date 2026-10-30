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
_MANAGED_PREFIX = "macip-backup-managed-"
_PULL_PREFIX = "macip-backup-pull-"

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


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


def schedule_to_calendar(schedule: dict) -> str | None:
    """Agenda estruturada (contrato central v1) → expressão OnCalendar.

    - manual → None (configuração disponível, sem timer automático);
    - hourly/daily/weekly → expressão com fuso IANA sufixado
      (systemd aceita `<expr> <TIMEZONE>`; `systemd-analyze calendar`
      valida antes de qualquer escrita).

    `Persistent=true` é aplicado pelo par service+timer padrão: um
    disparo vencido com a máquina desligada corre NA PRÓXIMA ativação —
    nunca como efeito imediato de baixar a configuração.
    """
    kind = schedule.get("kind")
    if kind == "manual" or kind is None:
        return None
    if kind not in {"hourly", "daily", "weekly"}:
        raise ConfigurationError(f"schedule.kind desconhecido: {kind}")
    timezone = schedule["timezone"]
    if kind == "hourly":
        return f"*-*-* *:{schedule['minute']:02d}:00 {timezone}"
    if kind == "daily":
        return f"*-*-* {schedule['hour']:02d}:{schedule['minute']:02d}:00 {timezone}"
    days = ",".join(
        sorted(_WEEKDAY_NAMES[day - 1] for day in schedule["weekdays"])
    )
    return (
        f"{days} {schedule['hour']:02d}:{schedule['minute']:02d}:00 "
        f"{timezone}"
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


def _managed_markers(task_id: str, agent_uuid: str) -> tuple[str, ...]:
    return (
        f"# macip-backup: managed-task={task_id}",
        f"# macip-backup: central-agent={agent_uuid}",
    )


def _assert_managed_units(
    task_id: str, agent_uuid: str, install_root: str
) -> None:
    """Unit gerenciada é propriedade VINCULADA: pertence a esta
    instalação, àquela tarefa e àquele vínculo central. Units sem
    marcador ou de outro vínculo NUNCA são sobrescritas/removidas."""
    unit_dir = unit_directory()
    expected = set(_managed_markers(task_id, agent_uuid))
    for suffix in ("service", "timer"):
        path = unit_dir / f"{_MANAGED_PREFIX}{task_id}.{suffix}"
        if not path.exists():
            continue
        if not _owns_unit(path, install_root):
            raise ConfigurationError(
                f"{path} existe e não pertence a esta instalação — "
                "conteúdo preservado"
            )
        markers = set(path.read_text(encoding="utf-8").splitlines())
        if not expected.issubset(markers):
            raise ConfigurationError(
                f"{path} pertence a outra tarefa ou vínculo central — "
                "conteúdo preservado"
            )


def install_managed_task(
    task_id: str,
    config_path: Path,
    store_path: Path,
    central_path: Path,
    agent_uuid: str,
    schedule: dict,
    *,
    activate: bool = True,
) -> dict[str, str]:
    """Escreve service (+timer quando a agenda é automática) para a
    tarefa GERENCIADA. O ExecStart aponta para `current.json` — caminho
    estável cujo conteúdo só avança depois dos efeitos locais.

    `activate=False` escreve e valida as units sem daemon-reload/
    enable (prova/inspeção sem sessão systemd)."""
    config_path = Path(config_path).resolve()
    store_path = Path(store_path).resolve()
    central_path = Path(central_path).resolve()
    calendar = schedule_to_calendar(schedule)
    if calendar is not None:
        _validate_calendar(calendar)  # antes de tocar nas units
    launcher = _installed_launcher()
    install_root = _installation_root(launcher)
    _assert_managed_units(task_id, agent_uuid, install_root)
    name = f"{_MANAGED_PREFIX}{task_id}"
    exec_line, pythonpath_line = _execution_lines(
        "agent",
        f"{_unit_value(config_path)} --store {_unit_value(store_path)}"
        f" --central {_unit_value(central_path)}",
        install_root,
    )
    unit_dir = unit_directory()
    unit_dir.mkdir(parents=True, exist_ok=True)
    service_path = unit_dir / f"{name}.service"
    timer_path = unit_dir / f"{name}.timer"
    markers = "\n".join(
        (_ownership_marker(install_root),
         *_managed_markers(task_id, agent_uuid))
    )
    service_path.write_text(
        f"""{markers}
[Unit]
Description=MAC IP Backup agent — tarefa gerenciada {task_id}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{pythonpath_line}{_environment_file_line()}
{exec_line}
""",
        encoding="utf-8",
    )
    timer_written = False
    if calendar is not None:
        timer_path.write_text(
            f"""{markers}
[Unit]
Description=Agendamento gerenciado: tarefa {task_id}

[Timer]
OnCalendar={calendar}
Persistent=true
AccuracySec=1s

[Install]
WantedBy=timers.target
""",
            encoding="utf-8",
        )
        timer_written = True
    elif timer_path.exists():
        # agenda virou `manual` (ou ficou manual): o timer desta tarefa
        # — e só ele — sai do caminho, sem tocar no backup em execução
        _assert_managed_units(task_id, agent_uuid, install_root)
        if activate:
            _systemctl("disable", "--now", f"{name}.timer")
        timer_path.unlink()
    if activate:
        _systemctl("daemon-reload")
        if timer_written:
            _systemctl("enable", "--now", f"{name}.timer")
    return {
        "task_id": task_id,
        "service": str(service_path),
        "timer": str(timer_path) if timer_written else None,
        "timer_name": f"{name}.timer" if timer_written else None,
        "activated": activate and timer_written,
    }


def suspend_managed_task(
    task_id: str, agent_uuid: str, *, activate: bool = True
) -> dict[str, str]:
    """Suspensão: DESABILITA o timer da tarefa gerenciada — e nada
    mais. Configuração, unit e backups em execução seguem intocados."""
    install_root = _installation_root(_installed_launcher())
    _assert_managed_units(task_id, agent_uuid, install_root)
    name = f"{_MANAGED_PREFIX}{task_id}"
    timer_path = unit_directory() / f"{name}.timer"
    disabled = False
    if timer_path.exists() and activate:
        _systemctl("disable", "--now", f"{name}.timer")
        disabled = True
    return {
        "task_id": task_id,
        "timer_name": f"{name}.timer",
        "disabled": disabled,
        "preserved": str(timer_path),
    }


def _pull_identity(store_path: Path, central_path: Path) -> str:
    """Identidade do vínculo de consulta — sem segredo."""
    material = f"pull\0{store_path}\0{central_path}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _pull_unit_name(identity: str) -> str:
    return f"{_PULL_PREFIX}{identity[:16]}"


def install_pull_schedule(
    store_path: Path,
    central_path: Path,
    calendar: str,
    enable: bool = False,
) -> dict[str, str]:
    """Timer INDEPENDENTE para a consulta/reconciliação — não substitui
    os timers de backup nem o de sincronização da outbox."""
    _validate_calendar(calendar)
    store_path = Path(store_path).resolve()
    central_path = Path(central_path).resolve()
    from .central import load_central

    load_central(central_path)
    launcher = _installed_launcher()
    install_root = _installation_root(launcher)
    identity = _pull_identity(store_path, central_path)
    name = _pull_unit_name(identity)
    unit_dir = unit_directory()
    pull_paths = tuple(
        unit_dir / f"{name}.{suffix}" for suffix in ("service", "timer")
    )
    marker = f"# macip-backup: pull-id={identity}"
    for path in pull_paths:
        if path.exists() and _owns_unit(path, install_root):
            if marker not in path.read_text(encoding="utf-8").splitlines():
                raise ConfigurationError(
                    f"{path} tem identidade de consulta inválida"
                )
    _assert_writable_units(pull_paths, install_root)
    exec_line, pythonpath_line = _execution_lines(
        "pull-tasks",
        f"--store {_unit_value(store_path)} --central {_unit_value(central_path)}",
        install_root,
    )
    service_path, timer_path = _write_unit_pair(
        name,
        "MAC IP Backup — consulta de tarefas gerenciadas",
        f"{pythonpath_line}{_environment_file_line()}\n{exec_line}\n",
        calendar,
        install_root,
        ("# macip-backup: schedule=pull", f"# macip-backup: pull-id={identity}"),
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
        "enabled": enable,
    }


def uninstall_pull_schedule(
    store_path: Path, central_path: Path
) -> dict[str, str]:
    """Remove SOMENTE o timer de consulta do vínculo informado."""
    store_path = Path(store_path).resolve()
    central_path = Path(central_path).resolve()
    install_root = _installation_root(_installed_launcher())
    identity = _pull_identity(store_path, central_path)
    name = _pull_unit_name(identity)
    pull_paths = tuple(
        unit_directory() / f"{name}.{suffix}" for suffix in ("service", "timer")
    )
    expected = f"# macip-backup: pull-id={identity}"
    for path in pull_paths:
        if not path.exists():
            continue
        if not _owns_unit(path, install_root):
            raise ConfigurationError(
                f"{path} existe e sua propriedade não é desta instalação — "
                "conteúdo preservado"
            )
        if expected not in path.read_text(encoding="utf-8").splitlines():
            raise ConfigurationError(
                "timer de consulta pertence a outro vínculo — conteúdo preservado"
            )
    return _uninstall_unit_pair(
        name, {"store": str(store_path), "central": str(central_path)}
    )


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

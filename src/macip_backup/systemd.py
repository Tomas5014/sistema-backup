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

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .models import ConfigurationError, TaskSpec

_UNIT_PREFIX = "macip-backup-agent-"


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


def install_schedule(
    task: TaskSpec,
    config_path: Path,
    store_path: Path,
    calendar: str,
    enable: bool = False,
) -> dict[str, str]:
    """Escreve service+timer systemd --user para a tarefa."""
    _validate_calendar(calendar)  # antes de sobrescrever units existentes
    config_path = Path(config_path).resolve()
    store_path = Path(store_path).resolve()
    unit_dir = unit_directory()
    unit_dir.mkdir(parents=True, exist_ok=True)
    name = f"{_UNIT_PREFIX}{task.id}"

    launcher = _installed_launcher()
    if launcher is not None:
        # Instalado: ExecStart no launcher — resolve `current` a cada
        # invocação, então a unit gerada continua servindo após update.
        exec_line = (
            f"ExecStart={_unit_value(launcher)} agent "
            f"{_unit_value(config_path)} --store {_unit_value(store_path)}"
        )
        pythonpath_line = ""
        install_root = os.environ.get("MACIP_HOME")
        if install_root is None:
            # sys.executable = <MACIP_HOME>/current/bin/python — sem
            # resolve(): o venv é symlink para o python do sistema.
            install_root = str(Path(sys.executable).parents[2])
    else:
        src_root = Path(__file__).resolve().parents[1]
        exec_line = (
            f"ExecStart={_unit_value(sys.executable)} -m macip_backup.cli agent "
            f"{_unit_value(config_path)} --store {_unit_value(store_path)}"
        )
        pythonpath_line = f'Environment="PYTHONPATH={_escape_unit(src_root)}"\n'
        install_root = str(src_root)

    config_dir = os.environ.get("MACIP_CONFIG")
    # EnvironmentFile: '-' literal + caminho SEM aspas — a diretiva trata
    # a linha inteira como filename; aspas virariam parte do nome.
    env_file = (
        f"EnvironmentFile=-{Path(config_dir) / 'agent.env'}"
        if config_dir
        else "EnvironmentFile=-%h/.config/macip-backup/agent.env"
    )

    service_path = unit_dir / f"{name}.service"
    service_path.write_text(
        f"""# macip-backup: install={install_root}
[Unit]
Description=MAC IP Backup agent — tarefa {_unit_value(task.id)}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
{pythonpath_line}{env_file}
{exec_line}
""",
        encoding="utf-8",
    )
    timer_path = unit_dir / f"{name}.timer"
    timer_path.write_text(
        f"""# macip-backup: install={install_root}
[Unit]
Description=Agendamento do backup {task.id}

[Timer]
OnCalendar={calendar}
Persistent=true
AccuracySec=1s

[Install]
WantedBy=timers.target
""",
        encoding="utf-8",
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


def uninstall_schedule(task_id: str) -> dict[str, str]:
    """Desativa e remove service+timer da tarefa."""
    name = f"{_UNIT_PREFIX}{task_id}"
    unit_dir = unit_directory()
    _systemctl("disable", "--now", f"{name}.timer", check=False)
    removed = []
    for suffix in ("timer", "service"):
        path = unit_dir / f"{name}.{suffix}"
        if path.exists():
            path.unlink()
            removed.append(str(path))
    _systemctl("daemon-reload", check=False)
    return {"task_id": task_id, "removed": removed}


def schedule_json(installation: dict[str, object]) -> str:
    return json.dumps(installation, ensure_ascii=False)

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
import shutil
import subprocess
import sys
from pathlib import Path

from .models import ConfigurationError, TaskSpec

_UNIT_PREFIX = "macip-backup-agent-"


def unit_directory() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


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
    config_path = Path(config_path).resolve()
    store_path = Path(store_path).resolve()
    src_root = Path(__file__).resolve().parents[1]
    unit_dir = unit_directory()
    unit_dir.mkdir(parents=True, exist_ok=True)
    name = f"{_UNIT_PREFIX}{task.id}"

    service_path = unit_dir / f"{name}.service"
    service_path.write_text(
        f"""[Unit]
Description=MAC IP Backup agent — tarefa {task.id}
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PYTHONPATH={src_root}
EnvironmentFile=-%h/.config/macip-backup/agent.env
ExecStart={sys.executable} -m macip_backup.cli agent {config_path} --store {store_path}
""",
        encoding="utf-8",
    )
    timer_path = unit_dir / f"{name}.timer"
    timer_path.write_text(
        f"""[Unit]
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

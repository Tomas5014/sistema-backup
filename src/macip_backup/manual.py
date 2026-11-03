"""Entrada manual explícita com subconjunto literal de EnvironmentFile."""

import argparse
import os
import re
import stat
import sys
from pathlib import Path

from .models import ConfigurationError


def load_environment(path: Path) -> dict[str, str]:
    """Lê UTF-8 KEY=value literal; nunca interpreta shell ou dotenv."""
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) not in {0o600, 0o400}):
            raise ConfigurationError("arquivo de ambiente exige dono atual e modo 0600/0400")
        try:
            text = stream.read().decode("utf-8")
        except UnicodeError:
            raise ConfigurationError("arquivo de ambiente exige UTF-8") from None
    result = {}
    for number, line in enumerate(text.split("\n"), 1):
        if "\r" in line or "\x00" in line:
            raise ConfigurationError(f"arquivo de ambiente: formato incompatível na linha {number}")
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if (not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key in result or not value or value != value.strip()
                or any(character in line for character in "\r\x00\\\"'")):
            raise ConfigurationError(f"arquivo de ambiente: formato incompatível na linha {number}")
        if key in {"MACIP_HOME", "MACIP_BIN", "MACIP_CONFIG", "MACIP_STATE", "MACIP_CACHE", "MACIP_LOCK_DIR"}:
            raise ConfigurationError(f"arquivo de ambiente: layout não permitido na linha {number}")
        result[key] = value
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("comando CLI obrigatório após --")
    try:
        environment = load_environment(args.env_file)
    except (OSError, ConfigurationError) as error:
        # Não reproduzir linhas/valores do arquivo no diagnóstico.
        parser.error(str(error))
    os.environ.update(environment)
    sys.argv = ["macip-backup", *command]
    from .cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())

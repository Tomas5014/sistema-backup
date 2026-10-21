from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class SFTPIntegrationScriptTests(unittest.TestCase):
    def test_compose_up_failure_cleans_only_its_project_and_keeps_exit_code(self) -> None:
        repository = Path(__file__).parents[1]
        script = repository / "tests" / "sftp" / "run-integration.sh"
        with tempfile.TemporaryDirectory(prefix="macip-script-test-") as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "docker.log"
            docker = fake_bin / "docker"
            docker.write_text(
                "#!/usr/bin/env sh\n"
                "printf '%s\\n' \"$*\" >> \"$FAKE_DOCKER_LOG\"\n"
                "if [ \"$1\" = info ]; then exit 0; fi\n"
                "case \" $* \" in\n"
                "  *' up '*) exit 23 ;;\n"
                "  *' down '*) exit 0 ;;\n"
                "esac\n"
                "exit 0\n",
                encoding="utf-8",
            )
            docker.chmod(0o700)
            environment = os.environ | {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "TMPDIR": str(root),
                "FAKE_DOCKER_LOG": str(log),
            }

            result = subprocess.run(
                ["bash", str(script)],
                cwd=repository,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 23, result.stderr)
            commands = log.read_text(encoding="utf-8").splitlines()
            up = next(command for command in commands if " up " in f" {command} ")
            down = next(command for command in commands if " down " in f" {command} ")
            self.assertIn("--volumes", down)
            self.assertEqual(_project_name(up), _project_name(down))
            self.assertTrue(_project_name(up).startswith("macip-sftp-"))
            self.assertFalse(any(path.name.startswith("macip-sftp-integration.") for path in root.iterdir()))


def _project_name(command: str) -> str:
    arguments = command.split()
    return arguments[arguments.index("--project-name") + 1]


if __name__ == "__main__":
    unittest.main()

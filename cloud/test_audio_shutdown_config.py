"""Offline checks of the embedded systemd shutdown configuration."""

import configparser
import unittest
from pathlib import Path
import json
import os
import subprocess
import tempfile



SOURCE = Path(__file__).with_name("audio_user_data.yaml")


def read_embedded_unit(filename):
    """Extract a literal unit block from this project's cloud-config."""
    lines = SOURCE.read_text(encoding="utf-8").splitlines()
    target = f"  - path: /etc/systemd/system/{filename}"

    start = lines.index(target) + 1
    for index in range(start, len(lines)):
        line = lines[index]
        if line.startswith("  - path:"):
            break
        if line == "    content: |":
            content = []
            for body in lines[index + 1:]:
                if body and not body.startswith("      "):
                    break
                content.append(body[6:] if body else "")
            parser = configparser.ConfigParser(interpolation=None)
            parser.read_string("\n".join(content))
            return parser

    raise ValueError(f"Missing literal content block: {filename}")


class AudioShutdownConfigTests(unittest.TestCase):
    def setUp(self):
        self.job = read_embedded_unit("mip-audio-job.service")
        self.timer = read_embedded_unit("mip-max-runtime.timer")
        self.ceiling = read_embedded_unit("mip-max-runtime.service")

    def test_python_worker_runs_without_root_or_command_elevation(self):
        service = self.job["Service"]
        self.assertEqual(service["User"], "mip-audio")
        self.assertEqual(service["Group"], "mip-audio")
        self.assertEqual(service["NoNewPrivileges"], "true")
        self.assertTrue(
            service["ExecStart"].startswith("/opt/mip/venv/bin/python ")
        )
        self.assertNotIn("--profile", service["ExecStart"])

    def test_shutdown_runs_after_exit_instead_of_during_startup(self):
        service = self.job["Service"]
        self.assertEqual(
            service["ExecStopPost"],
            "+/usr/bin/systemctl --no-block poweroff",
        )
        for directive in ("ExecStartPre", "ExecStart", "ExecStartPost"):
            self.assertNotIn("poweroff", service.get(directive, ""))
        self.assertNotEqual(service.get("RemainAfterExit", "no"), "yes")

    def test_worker_cannot_automatically_restart(self):
        self.assertEqual(self.job["Service"]["Restart"], "no")
        self.assertNotIn("Install", self.job.sections())

    def test_job_requires_timer_check_and_loads_environment(self):
        service = self.job["Service"]
        self.assertEqual(
            service["ExecStartPre"],
            "+/usr/bin/systemctl is-active --quiet mip-max-runtime.timer",
        )
        self.assertEqual(
            service["EnvironmentFile"],
            "/etc/mip/audio.env",
        )

    def test_backup_shutdown_is_independent_of_python(self):
        timer = self.timer["Timer"]
        self.assertEqual(timer["OnBootSec"], "90min")
        self.assertEqual(timer["Unit"], "mip-max-runtime.service")
        self.assertEqual(
            self.ceiling["Service"]["ExecStart"],
            "/usr/bin/systemctl poweroff",
        )
        self.assertNotIn("Requires", self.timer["Unit"])
        self.assertNotIn("PartOf", self.timer["Unit"])


"""Exercise the installation exit handler without AWS or real shutdown."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


class AudioInstallShutdownTests(unittest.TestCase):
    def setUp(self):
        source = Path(__file__).with_name("audio_install_parameters.json")
        config = json.loads(source.read_text(encoding="utf-8"))
        commands = config["commands"]

        self.assertEqual(commands[0], "set -eu")
        handler = commands[1]
        self.assertTrue(handler.startswith("trap "))
        self.assertEqual(handler.count("/usr/bin/systemctl"), 1)

        # Never execute the real systemctl command.
        self.handler = handler.replace(
            "/usr/bin/systemctl", "fake_systemctl"
        )

        root = Path("local_runs/audio/offline_tests")
        root.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=root)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.marker = self.root / "shutdown_calls.txt"

    def run_handler(self, *, install_status, shutdown_status=0):
        script = "\n".join([
            "set -eu",
            "fake_systemctl() {",
            '  printf "%s\\n" "$*" >> "$SHUTDOWN_MARKER"',
            '  return "$SHUTDOWN_STATUS"',
            "}",
            self.handler,
            f"exit {install_status}",
        ])
        environment = {
            **os.environ,
            "SHUTDOWN_MARKER": str(self.marker),
            "SHUTDOWN_STATUS": str(shutdown_status),
        }
        return subprocess.run(
            ["/bin/sh", "-c", script],
            env=environment,
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )

    def test_success_does_not_request_shutdown(self):
        result = self.run_handler(install_status=0)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.marker.exists())

    def test_failure_requests_shutdown_and_preserves_exit_code(self):
        result = self.run_handler(install_status=23)

        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(
            self.marker.read_text(encoding="utf-8").splitlines(),
            ["--no-block poweroff"],
        )

    def test_shutdown_failure_preserves_original_installation_error(self):
        result = self.run_handler(
            install_status=23,
            shutdown_status=7,
        )

        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(
            self.marker.read_text(encoding="utf-8").splitlines(),
            ["--no-block poweroff"],
        )


if __name__ == "__main__":
    unittest.main()
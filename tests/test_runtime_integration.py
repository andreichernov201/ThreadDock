import shutil
import subprocess
import unittest

from services.processes import worker_environment


@unittest.skipUnless(shutil.which("deno"), "Deno is not installed")
class DenoPermissionsTests(unittest.TestCase):
    def test_clean_worker_environment_and_default_network_denial(self):
        program = "try { await fetch('https://127.0.0.1'); Deno.exit(2); } catch (e) { if (!(e instanceof Deno.errors.NotCapable)) Deno.exit(3); }"
        result = subprocess.run(
            [
                shutil.which("deno"),
                "run",
                "--ext=js",
                "--no-prompt",
                "--no-remote",
                "--no-lock",
                "--no-config",
                "--no-npm",
                "--cached-only",
                "-",
            ],
            input=program.encode(),
            capture_output=True,
            timeout=15,
            env=worker_environment(),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))

import contextlib
import io
import os
import plistlib
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from thisdl_login import cli


class FakeLaunchd:
    def __init__(self):
        self.running = False
        self.legacy_running = False

    def __call__(self, command, *args):
        if command == "print":
            running = self.legacy_running if cli.LEGACY_LABEL in args[0] else self.running
            return types.SimpleNamespace(returncode=0 if running else 1,
                                         stdout="state = running\npid = 123\n" if running else "", stderr="")
        if command == "bootstrap":
            self.running = True
            cli.READY.write_text("123", encoding="ascii")
        elif command == "bootout":
            if cli.LEGACY_LABEL in args[0]:
                self.legacy_running = False
            else:
                self.running = False
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        home = Path(self.temp.name)
        app = home / "Library/Application Support/ThisDLLogin"
        values = {
            "HOME": home, "APP": app, "LAUNCHER": app / "thisdl-login",
            "AGENT": home / "Library/LaunchAgents" / (cli.LABEL + ".plist"),
            "BIN": home / ".local/bin/thisdl-login", "LOG": app / "activity.log",
            "MARKER": app / ".thisdl-login", "READY": app / ".ready",
        }
        self.patches = patch.multiple(cli, **values)
        self.patches.start()
        self.addCleanup(self.patches.stop)
        self.launchd = FakeLaunchd()
        self.launch_patch = patch.object(cli, "launchctl", self.launchd)
        self.launch_patch.start()
        self.addCleanup(self.launch_patch.stop)
        self.output = io.StringIO()
        self.capture = contextlib.redirect_stdout(self.output)
        self.capture.__enter__()
        self.addCleanup(self.capture.__exit__, None, None, None)

    def test_install_disable_enable_uninstall(self):
        cli.install()
        self.assertTrue(cli.MARKER.is_file())
        self.assertTrue(cli.BIN.is_symlink())
        self.assertTrue(self.launchd.running)
        result = subprocess.run([sys.executable, str(cli.BIN), "--help"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uninstall", result.stdout)
        with cli.AGENT.open("rb") as stream:
            config = plistlib.load(stream)
        self.assertNotIn("StartInterval", config)
        self.assertTrue(config["KeepAlive"])
        self.assertEqual(cli.status(), 0)
        cli.install()
        self.assertTrue(self.launchd.running)
        cli.disable()
        self.assertFalse(cli.AGENT.exists())
        self.assertFalse(self.launchd.running)
        cli.disable()
        cli.enable()
        self.assertTrue(self.launchd.running)
        cli.enable()
        cli.uninstall()
        self.assertFalse(cli.MARKER.exists())
        self.assertFalse(cli.BIN.exists())
        self.assertFalse(cli.AGENT.exists())
        self.assertFalse(self.launchd.running)
        cli.uninstall()

    def test_refuses_to_replace_existing_command(self):
        cli.BIN.parent.mkdir(parents=True)
        cli.BIN.write_text("existing", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            cli.install()
        self.assertEqual(cli.BIN.read_text(encoding="utf-8"), "existing")

    def test_failed_first_install_can_be_retried(self):
        original = cli.atomic_write

        def fail_during_copy(path, data, mode):
            if path.name == "auth.py":
                raise OSError("simulated write failure")
            original(path, data, mode)

        with patch.object(cli, "atomic_write", side_effect=fail_during_copy):
            with self.assertRaisesRegex(OSError, "simulated write failure"):
                cli.install()
        self.assertFalse(cli.APP.exists())
        self.assertFalse(cli.BIN.exists())
        self.assertFalse(self.launchd.running)
        cli.install()
        self.assertTrue(cli.MARKER.is_file())
        self.assertTrue(self.launchd.running)

    def test_killed_first_install_can_be_retried(self):
        # Exit without exception handling or TemporaryDirectory cleanup.
        script = '''
import os
from pathlib import Path
from thisdl_login import cli
cli.APP = Path(os.environ["THISDL_TEST_APP"])
cli.MARKER = cli.APP / ".thisdl-login"
cli.BIN = cli.APP.parent / "test-bin"
cli.AGENT = cli.APP.parent / "test-agent.plist"
original = cli.atomic_write
def stop_during_copy(path, data, mode):
    original(path, data, mode)
    if path.name == "auth.py":
        os._exit(17)
cli.atomic_write = stop_during_copy
cli.install()
'''
        result = subprocess.run([sys.executable, "-c", script], cwd=cli.SOURCE,
                                env={**os.environ, "THISDL_TEST_APP": str(cli.APP)},
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertFalse(cli.APP.exists())
        cli.install()
        self.assertTrue(cli.MARKER.is_file())
        self.assertTrue(self.launchd.running)

    def test_refuses_unmarked_existing_directory(self):
        cli.APP.mkdir(parents=True)
        foreign = cli.APP / "other.txt"
        foreign.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "不属于本工具"):
            cli.install()
        self.assertEqual(foreign.read_text(encoding="utf-8"), "keep")

    def test_refuses_foreign_launch_agent(self):
        cli.AGENT.parent.mkdir(parents=True)
        cli.AGENT.write_bytes(plistlib.dumps({"Label": cli.LABEL, "ProgramArguments": ["/other"]}))
        with self.assertRaises(RuntimeError):
            cli.install()
        self.assertTrue(cli.AGENT.exists())

    def test_migrates_only_known_legacy_agent(self):
        old = cli.HOME / "Library/LaunchAgents" / (cli.LEGACY_LABEL + ".plist")
        old.parent.mkdir(parents=True)
        old_program = cli.HOME / "Library/Application Support/CampusAutoLogin/campus-auto-connect.py"
        old.write_bytes(plistlib.dumps({"Label": cli.LEGACY_LABEL, "ProgramArguments": ["python3", str(old_program)]}))
        self.launchd.legacy_running = True
        cli.install()
        self.assertFalse(old.exists())
        self.assertFalse(self.launchd.legacy_running)
        self.assertTrue(self.launchd.running)

    def test_failed_start_keeps_legacy_agent_running(self):
        old = cli.HOME / "Library/LaunchAgents" / (cli.LEGACY_LABEL + ".plist")
        old.parent.mkdir(parents=True)
        old_program = cli.HOME / "Library/Application Support/CampusAutoLogin/campus-auto-connect.py"
        old.write_bytes(plistlib.dumps({"Label": cli.LEGACY_LABEL, "ProgramArguments": ["python3", str(old_program)]}))
        self.launchd.legacy_running = True
        original = self.launchd

        def without_ready(command, *args):
            result = original(command, *args)
            if command == "bootstrap":
                cli.READY.unlink(missing_ok=True)
            return result

        with patch.object(cli, "launchctl", side_effect=without_ready), \
             patch.object(cli.time, "monotonic", side_effect=[0, 9]):
            with self.assertRaisesRegex(RuntimeError, "后台监听器未能启动"):
                cli.install()
        self.assertTrue(old.exists())
        self.assertTrue(self.launchd.legacy_running)
        self.assertFalse(self.launchd.running)


if __name__ == "__main__":
    unittest.main()

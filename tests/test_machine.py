"""machine/wakeci-sleep and the lines machine/install.sh writes, run with fake system
commands: nothing here suspends, reboots or needs root."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from wakeci import actions

ROOT = Path(__file__).resolve().parent.parent
SLEEP = (ROOT / "machine" / "wakeci-sleep").read_text(encoding="utf-8")
INSTALL = (ROOT / "machine" / "install.sh").read_text(encoding="utf-8")
FLAG = "/var/run/reboot-required"


class SleepScriptTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "calls"
        self.flag = self.dir / "reboot-required"
        # The same script, with the reboot flag moved into the test directory.
        self.assertIn(FLAG, SLEEP)
        self.script = self.dir / "wakeci-sleep"
        self.script.write_text(SLEEP.replace(FLAG, str(self.flag)), encoding="utf-8")
        self.fake("systemd-run", f'echo "systemd-run $*" >> {self.calls}')
        self.busy(False)

    def fake(self, name, body):
        path = self.bin / name
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o755)

    def busy(self, yes):
        self.fake("pgrep", "exit 0" if yes else "exit 1")

    def run_script(self, *args):
        env = {"PATH": f"{self.bin}:/usr/bin:/bin"}
        done = subprocess.run(["bash", str(self.script), *args], capture_output=True, text=True, env=env)
        calls = self.calls.read_text() if self.calls.exists() else ""
        return done.returncode, done.stdout, calls

    def test_no_argument_suspends_as_before(self):
        code, _, calls = self.run_script()
        self.assertEqual(code, 0)
        self.assertIn("/usr/bin/systemctl suspend", calls)

    def test_a_running_job_refuses(self):
        self.busy(True)
        code, _, calls = self.run_script()
        self.assertEqual((code, calls), (3, ""))

    def test_a_pending_reboot_reboots(self):
        self.flag.write_text("")
        code, _, calls = self.run_script()
        self.assertEqual(code, 4)
        self.assertIn("/usr/bin/systemctl reboot", calls)

    def test_check_never_suspends_and_says_what_it_would_do(self):
        for busy, flag, state in (
            (False, False, "would-suspend"),
            (True, False, "busy"),
            (False, True, "would-reboot"),
            (True, True, "busy"),
        ):
            self.busy(busy)
            if flag:
                self.flag.write_text("")
            elif self.flag.exists():
                self.flag.unlink()
            code, out, calls = self.run_script("check")
            self.assertEqual((code, calls), (actions.SLEEP_CHECKED, ""), state)
            self.assertEqual(out, f"wakeci-sleep protocol=1 state={state}\n", state)

    def test_any_other_argument_is_refused_and_does_nothing(self):
        for arg in ("suspend", "", "check extra", "--help"):
            code, _, calls = self.run_script(arg)
            self.assertEqual((code, calls), (2, ""), repr(arg))
        code, _, calls = self.run_script("check", "x")
        self.assertEqual((code, calls), (2, ""))


class ForcedCommandTest(unittest.TestCase):
    """The command authorized_keys forces. sshd runs it with the user's shell; a client
    command may only turn a suspend into a check, never reach the script itself."""

    def forced(self):
        m = re.search(r'command=\\"([^"]*)\\"', INSTALL)
        self.assertIsNotNone(m, "the forced command in machine/install.sh")
        # In install.sh it sits in a double-quoted bash string, so "$" is written "\$".
        return m.group(1).replace("\\$", "$")

    def test_a_client_command_becomes_check_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "sudo"
            fake.write_text('#!/bin/sh\nfor a in "$@"; do printf "[%s]" "$a"; done\n', encoding="utf-8")
            fake.chmod(0o755)
            for client, args in (
                (None, "[-n][/usr/local/sbin/wakeci-sleep]"),
                ("", "[-n][/usr/local/sbin/wakeci-sleep]"),
                ("check", "[-n][/usr/local/sbin/wakeci-sleep][check]"),
                ("$(touch pwned)", "[-n][/usr/local/sbin/wakeci-sleep][check]"),
                ("; touch pwned", "[-n][/usr/local/sbin/wakeci-sleep][check]"),
                ("a b c", "[-n][/usr/local/sbin/wakeci-sleep][check]"),
            ):
                env = {"PATH": f"{d}:/usr/bin:/bin"}
                if client is not None:
                    env["SSH_ORIGINAL_COMMAND"] = client
                done = subprocess.run(["bash", "-c", self.forced()], capture_output=True, text=True, env=env, cwd=d)
                self.assertEqual(done.stdout, args, repr(client))
                self.assertFalse(os.path.exists(os.path.join(d, "pwned")), repr(client))

    def test_the_key_stays_restricted(self):
        self.assertIn(',restrict $KEY wakeci"', INSTALL)


class SudoersTest(unittest.TestCase):
    def lines(self):
        found = re.findall(r'^\s*echo "(\$USER_NAME ALL=\(root\) NOPASSWD: .*)"$', INSTALL, re.M)
        return [line.replace("$USER_NAME", "wakeci").replace('\\"', '"') for line in found]

    def test_only_the_two_forms_are_allowed_without_a_password(self):
        self.assertEqual(
            self.lines(),
            [
                'wakeci ALL=(root) NOPASSWD: /usr/local/sbin/wakeci-sleep ""',
                "wakeci ALL=(root) NOPASSWD: /usr/local/sbin/wakeci-sleep check",
            ],
        )

    @unittest.skipUnless(shutil.which("visudo"), "visudo is not installed")
    def test_visudo_accepts_them(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers") as f:
            f.write("\n".join(self.lines()) + "\n")
            f.flush()
            done = subprocess.run(["visudo", "-cf", f.name], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()

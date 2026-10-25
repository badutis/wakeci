import contextlib
import io
import os
import subprocess
import tempfile
import unittest

from scripts import public_safe as ps

# This file is scanned by the guard like any other, so every bad sample is assembled at
# run time and never appears in the source as one piece.
DOT, COLON = ".", ":"
PRIV = DOT.join(["192", "168", "1", "10"])
MAC = COLON.join(["fc", "3f", "db", "12", "34", "56"])
GMAIL = "a.person" + "@" + "gmail" + ".com"
TOOL = "Clau" + "de"


def found(text):
    return list(ps.findings_in_text(text))


class AddressTest(unittest.TestCase):
    def test_private_addresses_are_refused(self):
        for parts in (
            ["10", "1", "2", "3"],
            ["172", "20", "0", "5"],
            ["192", "168", "1", "10"],
            ["100", "64", "3", "4"],
            ["169", "254", "0", "1"],
        ):
            ip = DOT.join(parts)
            self.assertEqual(found(f"host: {ip}"), [f"private address {ip}"], ip)

    def test_address_at_the_end_of_a_sentence_or_inside_a_name_is_refused(self):
        self.assertEqual(found(f"the host is {PRIV}."), [f"private address {PRIV}"])
        self.assertEqual(found(f"{PRIV}.nip.io"), [f"private address {PRIV}"])
        self.assertEqual(found(f"http://{PRIV}:9105/metrics"), [f"private address {PRIV}"])

    def test_leading_zeros_do_not_hide_an_address(self):
        self.assertEqual(found(DOT.join(["192", "168", "001", "010"])), [f"private address {PRIV}"])

    def test_private_ipv6_is_refused_documentation_ipv6_passes(self):
        ula = COLON.join(["fd12", "3456", "", "1"])
        self.assertEqual(found(f"addr {ula}"), [f"private address {ula}"])
        self.assertTrue(found("fe" + "80::1%eth0"))
        self.assertEqual(found("2001:db8::1"), [])
        self.assertEqual(found("2001:db8:10:20:30:40:50:60"), [])
        self.assertEqual(found(f"addr:{ula}"), [f"private address {ula}"])

    def test_documentation_public_and_special_addresses_pass(self):
        for ip in (
            "192.0.2.11",
            "198.51.100.7",
            "203.0.113.9",
            "0.0.0.0:9105",
            "127.0.0.1",
            "255.255.255.255",
            "version 1.2.3",
            "a 999.1.1.1 b",
        ):
            self.assertEqual(found(ip), [], ip)


class MacTest(unittest.TestCase):
    def test_real_looking_mac_is_refused_in_every_notation(self):
        dashed = MAC.replace(":", "-").upper()
        dotted = "fc3f" + DOT + "db12" + DOT + "3456"
        for text in (
            f"mac {MAC}",
            f"mac {dashed}",
            f"mac{COLON}{MAC}",
            f"a-{MAC}",
            f"hwaddr {MAC}:",
            dotted,
            f"switch shows {dotted}.",
        ):
            self.assertEqual(len(found(text)), 1, text)

    def test_documentation_and_placeholder_macs_pass(self):
        for mac in (
            "00:00:5e:00:53:01",
            "00:00:5E:00:53:ff",
            "mac:00:00:5e:00:53:02",
            "ff:ff:ff:ff:ff:ff",
            "00:00:00:00:00:03",
            "0000.5e00.5301",
        ):
            self.assertEqual(found(mac), [], mac)


class EmailAndToolTest(unittest.TestCase):
    def test_personal_email_is_refused_placeholders_pass(self):
        self.assertEqual(found(GMAIL), [f"email address {GMAIL}"])
        for ok in ("1+someone@users.noreply.github.com", "noreply@github.com", "ops@example.org"):
            self.assertEqual(found(ok), [], ok)

    def test_dependabots_sign_off_passes_but_not_other_github_addresses(self):
        # Dependabot signs its commits off with this address; CI refused every one of them.
        self.assertEqual(found("Signed-off-by: dependabot[bot] <support@github.com>"), [])
        gh = "@" + "github" + ".com"  # assembled, so the guard does not find these in this file
        for bad in ("someone" + gh, "support" + gh + ".example" + ".net", "xsupport" + gh):
            self.assertEqual(found(bad), [f"email address {bad}"], bad)

    def test_ai_attribution_is_refused_in_any_spelling(self):
        for text in (
            f"Co-Authored-By: {TOOL} <x@example.com>",
            f"{TOOL.lower()}_code",
            f"{TOOL}Code",
            "Generated with Chat" + "GPT",
            "written by Co" + "pilot",
        ):
            self.assertTrue(found(text), text)

    def test_ordinary_words_pass(self):
        self.assertEqual(found("the controller suspends idle machines"), [])


class RepositoryTest(unittest.TestCase):
    """The whole script against a throw-away repository."""

    def run_in_repo(self, files, message="add file", history=True, author="t", tag_message=None, raw=False, after=None):
        old = os.getcwd()
        with tempfile.TemporaryDirectory() as d:
            env = dict(
                os.environ,
                GIT_AUTHOR_NAME=author,
                GIT_COMMITTER_NAME="t",
                GIT_AUTHOR_EMAIL="t@example.com",
                GIT_COMMITTER_EMAIL="t@example.com",
                GIT_CONFIG_GLOBAL=os.devnull,
                GIT_CONFIG_NOSYSTEM="1",
                HOME=d,
            )

            def git(*a):
                subprocess.run(
                    ["git", "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *a],
                    cwd=d,
                    check=True,
                    capture_output=True,
                    env=env,
                )

            git("init", "-q")
            for content in files:
                with open(
                    os.path.join(d, "a.txt"), "wb" if raw else "w", **({} if raw else {"encoding": "utf-8"})
                ) as f:
                    f.write(content)
                git("add", "a.txt")
                git("commit", "-q", "-m", message)
            if tag_message is not None:
                git("tag", "-a", "v1", "-m", tag_message)
            if after:
                after(d, git)
            os.chdir(d)
            try:
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    code = ps.main(["--history"] if history else [])
            finally:
                os.chdir(old)
        return code, out.getvalue()

    def test_clean_repository_passes(self):
        self.assertEqual(self.run_in_repo(["broadcast 192.0.2.255\n"]), (0, ""))

    def test_value_removed_from_the_tree_is_still_found_in_history(self):
        files = [f"host {PRIV}\n", "host 192.0.2.20\n"]
        self.assertEqual(self.run_in_repo(files, history=False), (0, ""))
        code, out = self.run_in_repo(files)
        self.assertEqual(code, 1)
        self.assertIn(f"private address {PRIV}", out)

    def test_commit_message_and_author_name_are_scanned(self):
        code, out = self.run_in_repo(["x\n"], message=f"fix\n\nCo-Authored-By: {TOOL} <a@example.com>")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"commit [0-9a-f]{7}:\d+: AI attribution")
        code, out = self.run_in_repo(["x\n"], author=TOOL)
        self.assertEqual(code, 1)
        self.assertRegex(out, r"commit [0-9a-f]{7}:\d+: AI attribution")

    def test_a_message_with_control_characters_does_not_crash(self):
        self.assertEqual(self.run_in_repo(["x\n"], message="a\x1eb"), (0, ""))

    def test_a_tag_pointing_straight_at_a_blob_is_scanned(self):
        def after(d, git):
            blob = (
                subprocess.run(
                    ["git", "hash-object", "-w", "--stdin"],
                    cwd=d,
                    capture_output=True,
                    input=f"host {PRIV}\n".encode(),
                    check=True,
                )
                .stdout.decode()
                .strip()
            )
            git("tag", "blobtag", blob)

        code, out = self.run_in_repo(["x\n"], after=after)
        self.assertEqual(code, 1)
        self.assertIn("(blob)", out)

    def test_annotated_tag_message_is_scanned(self):
        code, out = self.run_in_repo(["x\n"], tag_message=f"release, host {PRIV}")
        self.assertEqual(code, 1)
        self.assertRegex(out, r"tag object [0-9a-f]{7} \(v1\):\d+: private address")

    def test_file_that_is_not_utf8_is_still_read(self):
        code, out = self.run_in_repo([f"ip {PRIV} ".encode() + b"\xe0\n"], raw=True, history=False)
        self.assertEqual(code, 1)
        self.assertIn(f"private address {PRIV}", out)

    def test_dangling_symlink_and_deleted_file_do_not_crash(self):
        def after(d, git):
            os.symlink("does-not-exist", os.path.join(d, "link"))
            git("add", "link")
            git("commit", "-q", "-m", "link")
            os.remove(os.path.join(d, "a.txt"))

        self.assertEqual(self.run_in_repo(["x\n"], after=after), (0, ""))

    def leak_also_at_the_guard_path(self, d, git, tag_only):
        """The same leaking content at b.txt in an older commit and later at the guard's
        own path; both are scanned."""
        leak = f"host {PRIV}\n"
        with open(os.path.join(d, "b.txt"), "w", encoding="utf-8") as f:
            f.write(leak)
        git("add", "b.txt")
        git("commit", "-q", "-m", "b")
        if tag_only:
            git("tag", "side")
            git("reset", "-q", "--hard", "HEAD~1")
        else:
            git("rm", "-q", "b.txt")
        os.makedirs(os.path.join(d, "scripts"), exist_ok=True)
        with open(os.path.join(d, "scripts", "public_safe.py"), "w", encoding="utf-8") as f:
            f.write(leak)
        git("add", "scripts/public_safe.py")
        git("commit", "-q", "-m", "guard")

    def test_a_leak_inside_the_guard_file_itself_is_found(self):
        def after(d, git):
            os.makedirs(os.path.join(d, "scripts"), exist_ok=True)
            with open(os.path.join(d, "scripts", "public_safe.py"), "w", encoding="utf-8") as f:
                f.write(f"# host {PRIV}\n")
            git("add", "scripts/public_safe.py")
            git("commit", "-q", "-m", "guard")

        code, out = self.run_in_repo(["x\n"], after=after)
        self.assertEqual(code, 1)
        self.assertIn("scripts/public_safe.py", out)

    def test_content_at_the_guard_path_and_elsewhere_is_found(self):
        for tag_only in (False, True):
            code, out = self.run_in_repo(
                ["x\n"], after=lambda d, git, tag_only=tag_only: self.leak_also_at_the_guard_path(d, git, tag_only)
            )
            self.assertEqual(code, 1, tag_only)
            self.assertIn(f"private address {PRIV}", out, tag_only)


class GuardSourceTest(unittest.TestCase):
    def test_the_guard_finds_nothing_in_its_own_source(self):
        # It is scanned like any other file; a pattern written out in it would fail CI.
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "public_safe.py")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(ps.scan_lines("scripts/public_safe.py", f.read()), [])

    def test_earlier_versions_are_listed_by_full_object_id(self):
        for sha in ps.EARLIER_VERSIONS_OF_THIS_FILE:
            self.assertRegex(sha, r"^[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()

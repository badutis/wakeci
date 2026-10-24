import argparse
import copy
import errno
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

from tests.test_io import FakeResponse
from wakeci import actions, doctor, github

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = json.loads((ROOT / "controller" / "config.example.json").read_text(encoding="utf-8"))
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
# Assembled at run time, so the repository never holds anything that looks like a secret.
TOKEN = "ghp_" + "Q" * 36
HOOK = "https://discord.com/api/webhooks/1/" + "S" * 20


class FakeSocket:
    def __init__(self, log, bind_error=None, connect_error=None):
        self.log, self.bind_error, self.connect_error = log, bind_error, connect_error

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def setsockopt(self, *a):
        pass

    def bind(self, addr):
        if self.bind_error:
            raise self.bind_error

    def connect(self, addr):
        self.log.append(("connect", addr))
        if self.connect_error:
            raise self.connect_error

    def sendto(self, data, addr):
        self.log.append(("sendto", data, addr))


class FakeGitHubAPI:
    """Answers the few GETs doctor makes. ``codes`` overrides a path's status."""

    def __init__(self, test):
        self.t = test
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        assert req.get_method() == "GET"
        if self.t.network_error:
            raise self.t.network_error
        path = req.full_url.replace(github.API + "/", "")
        base = path.split("?")[0]
        code = next((c for p, c in self.t.codes.items() if base.startswith(p)), 200)
        headers = {"Date": format_datetime(NOW + self.t.clock_skew, usegmt=True)}
        if base == "rate_limit":
            headers.update(self.t.token_headers)
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "no", headers, None)
        if base.endswith("/actions/runners"):
            body = {"runners": self.t.runners.get(base.replace("/actions/runners", ""), [])}
        else:
            body = {}
        resp = FakeResponse(body)
        resp.headers = headers
        return resp


class DoctorTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name
        self.cfg = copy.deepcopy(EXAMPLE)
        self.write_config()
        etc = self.p("/etc/wakeci")
        os.makedirs(etc)
        os.makedirs(self.p("/var/lib/wakeci"))
        for name in ("id_ed25519", "token-example-org", "token-example-org2", "notify-url"):
            self.secret(name, TOKEN if name.startswith("token") else (HOOK if name == "notify-url" else "KEY"))
        Path(etc, "known_hosts").write_text("hosts\n")
        # What the fakes answer.
        self.known = {m["host"] for m in self.cfg["machines"]}
        self.key_ok = True
        self.ifaces = [{"ifname": "lo", "addr_info": [{"local": "127.0.0.1", "prefixlen": 8}]},
                       {"ifname": "eth0", "addr_info": [{"local": "192.0.2.5", "prefixlen": 24}]}]  # fmt: skip
        self.ssh = {}  # host -> CompletedProcess
        self.codes = {}
        self.network_error = None
        self.clock_skew = timedelta(0)
        self.token_headers = {"X-RateLimit-Remaining": "4999"}
        self.runners = {
            "repos/example-org/app": [
                {"name": "host1-a-1", "status": "offline"},
                {"name": "host2-a-1", "status": "online"},
            ],
            "orgs/example-org2": [
                {"name": "host1-b-1", "status": "offline"},
                {"name": "host3-a-1", "status": "offline"},
            ],
        }
        self.commands = []
        self.socks = []
        self.bind_error = None
        self.connect_error = None
        self.posts = []
        self.post_error = None
        self.slept = 0
        self.api = FakeGitHubAPI(self)

    # --- helpers -----------------------------------------------------------------------

    def p(self, path):
        return os.path.join(self.root, path.lstrip("/"))

    def write_config(self):
        self.config = os.path.join(self.root, "config.json")
        Path(self.config).write_text(json.dumps(self.cfg))

    def secret(self, name, content):
        path = self.p(f"/etc/wakeci/{name}")
        Path(path).write_text(content + "\n")
        os.chmod(path, 0o600)

    def fake_run(self, cmd, **kw):
        self.commands.append(cmd)
        if cmd[0] == "ssh-keygen" and "-y" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if self.key_ok else 1, "ssh-ed25519 AAAA", "")
        if cmd[0] == "ssh-keygen" and "-F" in cmd:
            return subprocess.CompletedProcess(cmd, 0 if cmd[cmd.index("-F") + 1] in self.known else 1, "", "")
        if cmd[0] == "ip":
            if self.ifaces is None:
                raise FileNotFoundError("ip")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(self.ifaces), "")
        if cmd[0] == "ssh":
            host = cmd[-2].split("@")[1]
            return self.ssh.get(
                host, subprocess.CompletedProcess(cmd, 255, "", "ssh: connect to host: Connection timed out")
            )
        raise AssertionError(f"unexpected command {cmd}")

    def fake_sock(self, *a):
        return FakeSocket(self.socks, self.bind_error, self.connect_error)

    def fake_post(self, req, timeout):
        self.posts.append(req)
        if self.post_error:
            raise self.post_error
        return FakeResponse({})

    def env(self):
        def sleep(s):
            self.slept += s

        return doctor.Env(
            opener=self.api,
            post=self.fake_post,
            run=self.fake_run,
            sock=self.fake_sock,
            now=lambda: NOW + timedelta(seconds=self.slept),
            sleep=sleep,
            path=self.p,
        )

    def doctor(self, *flags, wake=None):
        ns = argparse.Namespace(
            config=self.config,
            offline="--offline" in flags,
            probe="--probe" in flags,
            wake=wake,
            notify_test="--notify-test" in flags,
            json="--json" in flags,
        )
        out = io.StringIO()
        old, sys.stdout = sys.stdout, out
        try:
            code = doctor.run(ns, self.env())
        finally:
            sys.stdout = old
        return code, out.getvalue()

    def results(self, *flags, wake=None):
        code, out = self.doctor("--json", *flags, wake=wake)
        return code, [(r["id"], r["status"], r["message"]) for r in json.loads(out)]

    def status(self, results, id):
        return [s for i, s, _ in results if i == id]

    def message(self, results, id):
        return " | ".join(m for i, _, m in results if i == id)

    # --- the whole run -----------------------------------------------------------------

    def test_a_good_setup_passes_and_nothing_is_sent(self):
        code, res = self.results()
        self.assertEqual(code, 0, res)
        self.assertEqual([r for r in res if r[1] in (doctor.FAIL, doctor.WARN)], [])
        for id in ("config", "files", "state", "metrics", "ssh-key", "host-key", "wol", "notify", "clock", "token",
                   "token-expiry", "token-perms", "runners"):  # fmt: skip
            self.assertEqual(self.status(res, id)[0], doctor.OK, id)
        # By default nothing reaches a machine or the webhook.
        self.assertFalse(any(c[0] == "ssh" for c in self.commands))
        self.assertFalse(any(s[0] == "sendto" for s in self.socks))
        self.assertEqual(self.posts, [])

    def test_offline_skips_the_network(self):
        code, res = self.results("--offline")
        self.assertEqual(code, 0)
        self.assertEqual(self.api.requests, [])
        for id in ("clock", "token", "token-perms", "runners"):
            self.assertEqual(self.status(res, id), [doctor.SKIP], id)

    def test_offline_refuses_the_flags_that_need_the_network(self):
        for flags, wake in (
            (("--offline", "--probe"), None),
            (("--offline", "--notify-test"), None),
            (("--offline",), "host1"),
        ):
            self.assertEqual(self.doctor(*flags, wake=wake)[0], 2)

    def test_an_invalid_configuration_stops_there(self):
        self.cfg["idle_minute"] = 3
        self.write_config()
        code, res = self.results()
        self.assertEqual(code, 1)
        self.assertEqual([(i, s) for i, s, _ in res], [("config", doctor.FAIL)])

    def test_config_warnings_are_shown(self):
        self.cfg["machines"][1]["runner_prefixes"] = ["host1-a-x"]
        self.write_config()
        _, res = self.results("--offline")
        self.assertIn(doctor.WARN, self.status(res, "config"))

    # --- files and local checks ------------------------------------------------------

    def test_file_problems(self):
        os.unlink(self.p("/etc/wakeci/token-example-org2"))
        os.chmod(self.p("/etc/wakeci/token-example-org"), 0o644)
        code, res = self.results("--offline")
        self.assertEqual(code, 1)
        msg = self.message(res, "files")
        self.assertIn("/etc/wakeci/token-example-org2 is missing", msg)
        self.assertIn("/etc/wakeci/token-example-org is a secret but readable by others (mode 644)", msg)

    def test_a_shared_token_file_is_reported_once(self):
        self.cfg["sources"][1]["token_file"] = self.cfg["sources"][0]["token_file"]
        self.write_config()
        os.unlink(self.p("/etc/wakeci/token-example-org"))
        _, res = self.results("--offline")
        self.assertEqual(self.message(res, "files").count("token-example-org is missing"), 1)

    def test_a_symbolic_link_is_refused(self):
        target = self.p("/etc/wakeci/real")
        Path(target).write_text(TOKEN)
        os.unlink(self.p("/etc/wakeci/notify-url"))
        os.symlink(target, self.p("/etc/wakeci/notify-url"))
        _, res = self.results("--offline")
        self.assertIn("/etc/wakeci/notify-url is a symbolic link", self.message(res, "files"))

    @unittest.skipIf(os.getuid() == 0, "root can write anywhere")
    def test_a_state_directory_that_cannot_be_written(self):
        os.chmod(self.p("/var/lib/wakeci"), 0o500)
        self.addCleanup(os.chmod, self.p("/var/lib/wakeci"), 0o700)
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "state"), [doctor.FAIL])

    def test_a_missing_state_directory_is_only_a_warning(self):
        os.rmdir(self.p("/var/lib/wakeci"))
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "state"), [doctor.WARN])

    def test_the_metrics_port(self):
        self.bind_error = OSError(errno.EADDRINUSE, "Address already in use")
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "metrics"), [doctor.WARN])
        self.bind_error = OSError(errno.EACCES, "Permission denied")
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "metrics"), [doctor.FAIL])

    def test_the_ssh_key(self):
        self.key_ok = False
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "ssh-key"), [doctor.FAIL])
        os.unlink(self.p("/etc/wakeci/id_ed25519"))
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "ssh-key"), [doctor.SKIP])

    def test_a_missing_host_key_names_the_machine(self):
        self.known.discard("192.0.2.12")
        code, res = self.results("--offline")
        self.assertEqual(code, 1)
        self.assertIn("host2 (192.0.2.12)", self.message(res, "host-key"))

    def test_ssh_tools_missing(self):
        def missing(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        env = self.env()
        env.run = missing
        d = doctor.Doctor(self.config, env, offline=True)
        res = d.run()
        self.assertEqual([r.status for r in res if r.id in ("ssh-key", "host-key")], [doctor.FAIL, doctor.FAIL])
        self.assertIn("not installed", [r.message for r in res if r.id == "ssh-key"][0])

    def test_wake_on_lan_address(self):
        _, res = self.results("--offline")
        self.assertIn("broadcast address of eth0", self.message(res, "wol"))
        self.assertEqual(self.socks, [("connect", ("192.0.2.255", 9))])
        self.cfg["broadcast"] = "198.51.100.255"
        self.write_config()
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "wol"), [doctor.WARN])
        self.cfg["broadcast"] = "255.255.255.255"
        self.write_config()
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "wol"), [doctor.OK])
        self.ifaces.append({"ifname": "wlan0", "addr_info": [{"local": "198.51.100.7", "prefixlen": 24}]})
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "wol"), [doctor.WARN])
        self.connect_error = OSError(errno.ENETUNREACH, "Network is unreachable")
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "wol"), [doctor.FAIL])
        self.connect_error, self.ifaces = None, None
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "wol"), [doctor.OK])

    def test_the_webhook_url(self):
        self.secret("notify-url", "https://example.com/hook/" + "S" * 20)
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "notify"), [doctor.WARN])
        self.assertIn("example.com", self.message(res, "notify"))
        self.secret("notify-url", "http://example.com/hook/" + "S" * 20)
        _, out = self.doctor("--offline")
        self.assertIn("FAIL  notify", out)
        self.assertNotIn("S" * 20, out)
        for host in ("ptb.discord.com", "canary.discord.com"):
            self.secret("notify-url", f"https://{host}/api/webhooks/1/x")
            _, res = self.results("--offline")
            self.assertEqual(self.status(res, "notify"), [doctor.OK], host)
        del self.cfg["notify"]
        self.write_config()
        _, res = self.results("--offline")
        self.assertEqual(self.status(res, "notify"), [doctor.SKIP])

    # --- GitHub ------------------------------------------------------------------------

    def test_an_invalid_token(self):
        self.codes = {"rate_limit": 401}
        code, res = self.results()
        self.assertEqual(code, 1)
        self.assertEqual(self.status(res, "token"), [doctor.FAIL, doctor.FAIL])
        self.assertEqual(self.status(res, "runners"), [doctor.SKIP])

    def test_the_clock(self):
        self.clock_skew = timedelta(minutes=5)
        code, res = self.results()
        self.assertEqual((code, self.status(res, "clock")), (1, [doctor.FAIL]))
        self.clock_skew = timedelta(seconds=30)
        _, res = self.results()
        self.assertEqual(self.status(res, "clock"), [doctor.OK])

    def test_token_expiry(self):
        for value, status in (
            ("2026-10-27 12:00:00 UTC", doctor.WARN),
            ("2026-10-20 12:00:00 UTC", doctor.FAIL),
            ("2027-01-01 00:00:00 +0200", doctor.OK),
            ("soon", doctor.WARN),
        ):
            self.token_headers = {"github-authentication-token-expiration": value}
            _, res = self.results()
            self.assertEqual(set(self.status(res, "token-expiry")), {status}, value)

    def test_a_classic_token_that_can_write(self):
        self.token_headers = {"X-OAuth-Scopes": "repo, read:org"}
        _, res = self.results()
        self.assertEqual(set(self.status(res, "token-scope")), {doctor.WARN})
        self.assertIn("repo", self.message(res, "token-scope"))
        for scopes in ("user", "write:packages", "read:org, gist", "manage_runners:org"):
            self.token_headers = {"X-OAuth-Scopes": scopes}
            _, res = self.results()
            self.assertEqual(set(self.status(res, "token-scope")), {doctor.WARN}, scopes)
        self.token_headers = {"X-OAuth-Scopes": "read:org, user:email"}
        _, res = self.results()
        self.assertEqual(set(self.status(res, "token-scope")), {doctor.OK})
        self.token_headers = {}
        _, res = self.results()
        self.assertEqual(self.status(res, "token-scope"), [])  # fine-grained: nothing to say

    def test_missing_permissions(self):
        self.codes = {"orgs/example-org2/actions/runners": 403, "repos/example-org2/repo-b/actions/runs": 404}
        code, res = self.results()
        self.assertEqual(code, 1)
        msg = self.message(res, "token-perms")
        self.assertIn(
            "orgs/example-org2: the token cannot read its runners (403), the jobs of example-org2/repo-b (404)", msg
        )
        self.assertEqual(self.status(res, "runners"), [doctor.SKIP])

    def test_runner_layout(self):
        self.runners["orgs/example-org2"] = [{"name": "stray-1", "status": "online"}]
        _, res = self.results()
        msg = self.message(res, "runners")
        self.assertIn("no runner at GitHub for host3", msg)
        self.assertIn("runners on no machine (they always count as free): stray-1", msg)

    def test_github_unreachable(self):
        self.network_error = urllib.error.URLError("Temporary failure in name resolution")
        code, res = self.results()
        self.assertEqual(code, 1)
        self.assertIn("network: Temporary failure in name resolution", self.message(res, "token"))
        self.assertEqual(self.status(res, "clock"), [doctor.SKIP])

    def test_an_unreadable_token_file_skips_its_source(self):
        os.unlink(self.p("/etc/wakeci/token-example-org"))
        _, res = self.results()
        self.assertIn("repos/example-org/app: its token file cannot be read", self.message(res, "token"))
        self.assertEqual(self.status(res, "token"), [doctor.SKIP, doctor.OK])

    def test_secrets_never_appear(self):
        cases = [{}, {"rate_limit": 401}, {"rate_limit": 500}, {"orgs/example-org2/actions/runners": 403}]
        for codes in cases:
            self.codes = codes
            for flags in ((), ("--json",), ("--notify-test",)):
                _, out = self.doctor(*flags)
                self.assertNotIn(TOKEN, out, (codes, flags))
                self.assertNotIn(HOOK.rsplit("/", 1)[1], out, (codes, flags))
        self.answer("192.0.2.11", 255, "", f"error {TOKEN}")
        _, out = self.doctor("--probe")
        self.assertNotIn(TOKEN, out)
        _, out = self.doctor(wake="host3")
        self.assertNotIn(TOKEN, out)
        self.network_error = RuntimeError(f"boom {TOKEN} {HOOK}")
        self.post_error = RuntimeError(f"boom {HOOK}")
        _, out = self.doctor("--notify-test")
        self.assertNotIn(TOKEN, out)
        self.assertNotIn(HOOK.rsplit("/", 1)[1], out)

    # --- --probe -----------------------------------------------------------------------

    def answer(self, host, code, out="", err=""):
        self.ssh[host] = subprocess.CompletedProcess(["ssh"], code, out, err)

    def test_probe(self):
        self.answer("192.0.2.11", 10, "wakeci-sleep protocol=1 state=would-suspend\n")
        self.answer("192.0.2.12", 0)  # an older machine: it took the check for a suspend
        code, res = self.results("--probe")
        self.assertEqual(code, 1)
        probes = [(s, m) for i, s, m in res if i == "sleep-probe"]
        self.assertEqual(probes[0][0], doctor.OK)
        self.assertIn("host1: wakeci-sleep answers (state=would-suspend)", probes[0][1])
        self.assertEqual(probes[1][0], doctor.FAIL)
        self.assertIn("host2: an older wakeci-sleep took the check for a suspend and is suspending", probes[1][1])
        self.assertEqual(probes[2][0], doctor.SKIP)  # host3 did not answer: asleep
        ssh = [c for c in self.commands if c[0] == "ssh"]
        self.assertEqual(len(ssh), 3)
        for c in ssh:
            self.assertEqual(c[-1], "check")
            self.assertIn("StrictHostKeyChecking=yes", c)
            self.assertIn("BatchMode=yes", c)
        first = doctor.main.machines_from(self.cfg)[0]
        key, known = self.p("/etc/wakeci/id_ed25519"), self.p("/etc/wakeci/known_hosts")
        self.assertEqual(ssh[0], actions.ssh_check_command(first, key, known))

    def test_probe_answers(self):
        m = doctor.main.machines_from(self.cfg)[0]
        for code, out, err, status, text in (
            (10, "wakeci-sleep protocol=1 state=busy", "", doctor.OK, "state=busy"),
            (4, "", "", doctor.FAIL, "rebooting"),
            (3, "", "busy", doctor.FAIL, "older wakeci-sleep"),
            (1, "", "sudo: a password is required", doctor.FAIL, "sudoers rule is missing"),
            (
                255,
                "",
                "@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\nHost key verification failed.",
                doctor.FAIL,
                "does not match known_hosts",
            ),
            (
                255,
                "",
                "No ED25519 host key is known for 192.0.2.11 and you have requested strict checking.\n"
                "Host key verification failed.",
                doctor.FAIL,
                "not in known_hosts",
            ),
            (
                255,
                "",
                'Load key "/etc/wakeci/id_ed25519": Permission denied\n'
                "wakeci@192.0.2.11: Permission denied (publickey).",
                doctor.FAIL,
                "cannot be read by this user",
            ),
            (doctor.TIMED_OUT, "", "", doctor.FAIL, "did not finish in time"),
            (255, "", "wakeci@192.0.2.11: Permission denied (publickey).", doctor.FAIL, "authorized_keys"),
            (255, "", "ssh: connect to host 192.0.2.11 port 22: No route to host", doctor.SKIP, "asleep"),
            (2, "", "usage", doctor.FAIL, "exit code 2"),
            (10, "garbage", "", doctor.FAIL, "exit code 10"),
        ):
            got = doctor.probe_result(m, subprocess.CompletedProcess([], code, out, err))
            self.assertEqual(got[0], status, (code, err))
            self.assertIn(text, got[1], (code, err))

    # --- --notify-test -----------------------------------------------------------------

    def test_notify_test(self):
        code, res = self.results("--notify-test")
        self.assertEqual(self.status(res, "notify-test"), [doctor.OK])
        self.assertEqual(len(self.posts), 1)
        req = self.posts[0]
        self.assertEqual((req.get_method(), req.full_url), ("POST", HOOK))
        body = json.loads(req.data)
        self.assertIn("wakeci doctor: a test message", body["content"])
        self.assertEqual(body["allowed_mentions"], {"parse": []})
        self.post_error = urllib.error.HTTPError(HOOK, 404, "Not Found", {}, None)
        code, res = self.results("--notify-test")
        self.assertEqual((code, self.status(res, "notify-test")), (1, [doctor.FAIL]))
        self.assertIn("HTTP 404", self.message(res, "notify-test"))

    # --- --wake ------------------------------------------------------------------------

    def test_wake_an_awake_machine_proves_nothing(self):
        _, res = self.results(wake="host2")
        self.assertEqual(self.status(res, "wake"), [doctor.WARN])
        self.assertIn("does not show that Wake-on-LAN works", self.message(res, "wake"))
        self.assertEqual(len([s for s in self.socks if s[0] == "sendto"]), 1)

    def test_wake_when_the_packet_cannot_be_sent(self):
        class NoSend(FakeSocket):
            def sendto(self, data, addr):
                raise OSError(errno.ENETUNREACH, "Network is unreachable")

        env = self.env()
        env.sock = lambda *a: NoSend(self.socks)
        res = doctor.Doctor(self.config, env, wake="host1").run()
        wake = [r for r in res if r.id == "wake"]
        self.assertEqual([r.status for r in wake], [doctor.FAIL])
        self.assertIn("could not be sent to 192.0.2.255: Network is unreachable", wake[0].message)
        self.assertEqual(res[0].id, "config")  # the other results are still there

    def test_a_broken_check_does_not_hide_the_others(self):
        env = self.env()
        env.run = lambda cmd, **kw: (_ for _ in ()).throw(RuntimeError(f"boom {TOKEN}"))
        res = doctor.Doctor(self.config, env, offline=True).run()
        broken = [r for r in res if r.status == doctor.FAIL]
        self.assertEqual({r.id for r in broken}, {"ssh-key", "host-key", "wol"})  # each runs a command
        self.assertIn("the check itself failed (RuntimeError)", broken[0].message)
        self.assertNotIn(TOKEN, " ".join(r.message for r in res))
        self.assertIn("notify", [r.id for r in res])

    def test_a_command_that_cannot_run(self):
        def denied(cmd, **kw):
            raise PermissionError(13, "Permission denied")

        env = self.env()
        env.run = denied
        res = doctor.Doctor(self.config, env, offline=True).run()
        self.assertEqual([r.status for r in res if r.id == "ssh-key"], [doctor.FAIL])

    def test_wake_sends_one_packet_and_waits_for_a_runner(self):
        polls = []
        original = doctor.Doctor._runners

        def runners(d):
            polls.append(1)
            # 1: the runners check, 2: before the packet, 3 and 4: after it
            if len(polls) >= 4:
                self.runners["repos/example-org/app"][0]["status"] = "online"
            return original(d)

        doctor.Doctor._runners = runners
        self.addCleanup(setattr, doctor.Doctor, "_runners", original)
        code, res = self.results(wake="host1")
        self.assertEqual(code, 0, res)
        self.assertIn("host1 woke: a runner came online after about 20 s", self.message(res, "wake"))
        sent = [s for s in self.socks if s[0] == "sendto"]
        self.assertEqual(sent, [("sendto", actions.magic_packet("00:00:5e:00:53:01"), ("192.0.2.255", 9))])

    def test_wake_that_never_comes(self):
        code, res = self.results(wake="host3")
        self.assertEqual(code, 1)
        self.assertIn("host3 had no runner online 180 s after the packet", self.message(res, "wake"))
        self.assertEqual(self.slept, doctor.WAKE_WAIT)

    def test_wake_an_unknown_machine(self):
        self.assertEqual(self.doctor(wake="nope")[0], 2)
        self.assertEqual(self.api.requests, [])

    # --- the report --------------------------------------------------------------------

    def test_the_text_report_links_each_problem(self):
        self.key_ok = False
        code, out = self.doctor("--offline")
        self.assertEqual(code, 1)
        lines = out.splitlines()
        i = next(n for n, line in enumerate(lines) if line.startswith("FAIL  ssh-key"))
        self.assertEqual(lines[i + 1].strip(), f"{doctor.DOCS}#ssh-key")
        self.assertRegex(lines[-1], r"^\d+ ok, \d+ warnings, 1 failures, 4 skipped$")

    def test_every_check_id_has_a_section_in_the_troubleshooting_guide(self):
        text = (ROOT / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
        headings = re.findall(r"^## (\S+)$", text, re.M)
        self.assertEqual(headings, list(doctor.IDS))
        source = (ROOT / "wakeci" / "doctor.py").read_text(encoding="utf-8")
        used = set(re.findall(r'add\(\s*"([a-z-]+)"', source)) | set(
            re.findall(r'\(\s*"([a-z-]+)",\s*(?:OK|WARN|FAIL|SKIP)', source)
        )
        self.assertLessEqual(used, set(doctor.IDS))

    def test_the_controller_wrapper_runs(self):
        done = subprocess.run(
            [
                sys.executable,
                str(ROOT / "controller" / "doctor.py"),
                "--offline",
                str(ROOT / "controller" / "config.example.json"),
            ],
            capture_output=True,
            text=True,
            cwd="/",
        )
        self.assertIn("OK    config", done.stdout)
        self.assertIn(done.returncode, (0, 1))


if __name__ == "__main__":
    unittest.main()

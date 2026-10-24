"""``wakeci doctor``: everything that can be checked before the controller runs.

Run it on the controller host as the service user, so files are read with its rights:

    sudo -u wakeci python3 /opt/wakeci/controller/doctor.py /etc/wakeci/config.json

Every check prints one line: ``OK``, ``WARN``, ``FAIL`` or ``SKIP``, its id and what it
found; a warning or failure also names its section in docs/troubleshooting.md. The exit
code is 1 when anything failed.

By default nothing is changed and nothing is sent to a machine: GitHub is only read (GET),
and the SSH, Wake-on-LAN and webhook checks only look at files and addresses. Three flags
do more, and only when given:

- ``--probe`` connects to every awake machine and asks ``wakeci-sleep check``. A machine
  set up with a WakeCI older than 0.5.0 ignores the question and **suspends**; run
  ``machine/install.sh`` again on every machine first.
- ``--wake NAME`` sends a Wake-on-LAN packet and waits until a runner of NAME is online.
- ``--notify-test`` posts one test message to the webhook.

``--offline`` skips everything that needs the network. Tokens and the webhook URL are
secrets: they are never printed, and an error names only its kind.
"""

import email.utils
import errno
import ipaddress
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import __version__, actions, config_check, github, main, notify

DOCS = "https://github.com/badutis/wakeci/blob/main/docs/troubleshooting.md"
OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
CLOCK_LIMIT = timedelta(minutes=2)
TIMED_OUT = -9  # the exit code _cmd gives a command that did not finish in time
EXPIRY_WARN = timedelta(days=14)
WAKE_WAIT = 180  # seconds a woken machine has to bring a runner online
WAKE_POLL = 10
# Classic token scopes that only read; every other scope can change something.
READ_SCOPES = ("user:email",)  # and every "read:..." scope
SECRET_KINDS = ("ssh_key", "token")  # token also covers the webhook URL file
# Every check id; docs/troubleshooting.md has a section for each (a test keeps them in step).
IDS = (
    "config", "files", "state", "metrics", "ssh-key", "host-key", "wol", "notify", "clock",
    "token", "token-expiry", "token-scope", "token-perms", "runners", "sleep-probe", "notify-test", "wake",
)  # fmt: skip


@dataclass(frozen=True)
class Result:
    id: str
    status: str
    message: str


@dataclass
class Env:
    """What the checks touch outside Python, so tests can replace it."""

    opener: Callable = urllib.request.urlopen  # GitHub, GET only
    post: Callable = notify._OPENER.open  # the webhook (redirects refused)
    run: Callable = subprocess.run
    sock: Callable = socket.socket
    now: Callable = field(default=lambda: datetime.now(timezone.utc))
    sleep: Callable = time.sleep
    path: Callable = field(default=lambda p: p)  # tests move /etc/wakeci elsewhere


class Doctor:
    def __init__(self, config: str, env: Env, offline=False, probe=False, wake=None, notify_test=False):
        self.config = config
        self.env = env
        self.offline = offline
        self.probe = probe
        self.wake_name = wake
        self.notify_test = notify_test
        self.results: list[Result] = []
        self.cfg: dict = {}
        self.tokens: dict[str, str] = {}  # runners_path -> token, for the sources that could be read

    def add(self, id: str, status: str, message: str) -> None:
        assert id in IDS, id
        self.results.append(Result(id, status, message))

    # --- the run ---------------------------------------------------------------------

    def run(self) -> list[Result]:
        if not self.check_config():
            return self.results
        checks = [
            ("files", self.check_files),
            ("state", self.check_state),
            ("metrics", self.check_metrics),
            ("ssh-key", self.check_ssh_key),
            ("host-key", self.check_host_keys),
            ("wol", self.check_wol),
            ("notify", self.check_notify),
        ]
        if self.offline:
            for id in ("clock", "token", "token-perms", "runners"):
                self.add(id, SKIP, "--offline")
        else:
            checks.append(("token", self.check_github))
        if self.probe:
            checks.append(("sleep-probe", self.check_probe))
        if self.notify_test:
            checks.append(("notify-test", self.check_notify_test))
        if self.wake_name:
            checks.append(("wake", self.check_wake))
        for id, check in checks:
            try:
                check()
            except Exception as e:  # noqa: BLE001 — one broken check must not hide the others
                # Only the kind: the text of an unexpected error could hold a secret.
                self.add(id, FAIL, f"the check itself failed ({type(e).__name__}); please report it")
        return self.results

    # --- local -----------------------------------------------------------------------

    def check_config(self) -> bool:
        warnings: list[str] = []
        try:
            self.lines = config_check.check(self.config, warnings)
            self.cfg = main.load_config(self.config)
            self.machines = main.machines_from(self.cfg)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self.add("config", FAIL, f"{self.config}: {e}")
            return False
        self.add("config", OK, f"{self.config} is valid ({len(self.machines)} machines)")
        for w in warnings:
            self.add("config", WARN, w)
        return True

    def check_files(self) -> None:
        bad = False
        for kind, value in dict.fromkeys(self.lines):  # sources may share a token file
            if kind == "host":
                continue
            p = self.env.path(value)
            if os.path.islink(p):
                self.add("files", FAIL, f"{value} is a symbolic link (the installer refuses those)")
            elif not os.path.exists(p):
                self.add("files", FAIL, f"{value} is missing")
            elif not os.access(p, os.R_OK):
                self.add("files", FAIL, f"{value} is not readable by this user ({_user()})")
            elif kind in SECRET_KINDS and os.stat(p).st_mode & 0o077:
                mode = oct(os.stat(p).st_mode & 0o777)[2:]
                self.add("files", WARN, f"{value} is a secret but readable by others (mode {mode}): chmod 0600")
            else:
                continue
            bad = True
        if not bad:
            self.add("files", OK, "the key, known_hosts and every token file are in place and readable")

    def check_state(self) -> None:
        state = self.cfg["state_file"]
        d = self.env.path(os.path.dirname(state))
        if not os.path.isdir(d):
            self.add(
                "state",
                WARN,
                f"{os.path.dirname(state)} does not exist yet (systemd creates it when the service starts)",
            )
        elif not os.access(d, os.W_OK | os.X_OK):
            self.add("state", FAIL, f"{os.path.dirname(state)} is not writable by this user ({_user()})")
        elif os.path.exists(self.env.path(state)) and not os.access(self.env.path(state), os.W_OK):
            self.add("state", FAIL, f"{state} is not writable by this user ({_user()})")
        else:
            self.add("state", OK, f"{state} can be written")

    def check_metrics(self) -> None:
        host, _, port = self.cfg["metrics_listen"].rpartition(":")
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        try:
            with self.env.sock(family, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((host.strip("[]"), int(port)))
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                self.add("metrics", WARN, f"{self.cfg['metrics_listen']} is in use (expected while the service runs)")
            else:
                self.add("metrics", FAIL, f"cannot listen on {self.cfg['metrics_listen']}: {e.strerror or e}")
            return
        self.add("metrics", OK, f"{self.cfg['metrics_listen']} is free")

    def _cmd(self, cmd: list, timeout: float = 15) -> "Optional[subprocess.CompletedProcess]":
        try:
            return self.env.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout)
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(cmd, TIMED_OUT, "", "")
        except OSError as e:
            return subprocess.CompletedProcess(cmd, 126, "", f"cannot run {cmd[0]}: {e.strerror or e}")

    def check_ssh_key(self) -> None:
        key = self.env.path(self.cfg["ssh_key"])
        if not os.access(key, os.R_OK):
            self.add("ssh-key", SKIP, f"{self.cfg['ssh_key']} cannot be read (see files)")
            return
        done = self._cmd(["ssh-keygen", "-y", "-P", "", "-f", key])
        if done is None:
            self.add("ssh-key", FAIL, "ssh-keygen is not installed (the OpenSSH client)")
        elif done.returncode != 0:
            self.add("ssh-key", FAIL, f"{self.cfg['ssh_key']} cannot be used without a passphrase")
        else:
            self.add("ssh-key", OK, f"{self.cfg['ssh_key']} is a usable private key")

    def check_host_keys(self) -> None:
        known = self.env.path(self.cfg["known_hosts"])
        if not os.access(known, os.R_OK):
            self.add("host-key", SKIP, f"{self.cfg['known_hosts']} cannot be read (see files)")
            return
        missing = []
        for m in self.machines:
            done = self._cmd(["ssh-keygen", "-F", m.host, "-f", known])
            if done is None:
                self.add("host-key", FAIL, "ssh-keygen is not installed (the OpenSSH client)")
                return
            if done.returncode != 0:
                missing.append(f"{m.name} ({m.host})")
        if missing:
            self.add(
                "host-key",
                FAIL,
                f"no host key in known_hosts for {', '.join(missing)}: these cannot be suspended; "
                "run controller/install.sh again while they are awake",
            )
        else:
            self.add("host-key", OK, f"known_hosts has every machine's host key ({len(self.machines)})")

    def check_wol(self) -> None:
        bcast = self.cfg["broadcast"]
        try:
            with self.env.sock(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.connect((bcast, 9))  # chooses a route; sends nothing
        except OSError as e:
            self.add("wol", FAIL, f"cannot send to {bcast}: {e.strerror or e}")
            return
        nets = self._networks()
        if nets is None:
            self.add("wol", OK, f"{bcast} is reachable (the interfaces could not be listed: no ip command)")
        elif bcast == "255.255.255.255":
            if len(nets) > 1:
                names = ", ".join(n for n, _ in nets)
                self.add(
                    "wol", WARN, f"255.255.255.255 leaves through one interface only, and there are several ({names})"
                )
            else:
                self.add("wol", OK, "255.255.255.255 (the only interface's segment)")
        else:
            on = [n for n, b in nets if b == bcast]
            if on:
                self.add("wol", OK, f"{bcast} is the broadcast address of {on[0]}")
            else:
                found = ", ".join(f"{n} {b}" for n, b in nets) or "none"
                self.add("wol", WARN, f"{bcast} is not the broadcast address of any interface here ({found})")

    def _networks(self) -> "Optional[list[tuple[str, str]]]":
        """(interface, broadcast address) of every IPv4 interface but loopback."""
        done = self._cmd(["ip", "-j", "-4", "addr", "show"])
        if done is None or done.returncode != 0:
            return None
        out = []
        try:
            for iface in json.loads(done.stdout):
                for a in iface.get("addr_info", []):
                    net = ipaddress.IPv4Interface(f"{a['local']}/{a['prefixlen']}").network
                    if not net.is_loopback:
                        out.append((iface["ifname"], str(net.broadcast_address)))
        except (ValueError, KeyError, TypeError):
            return None
        return out

    def check_notify(self) -> None:
        n = self.cfg.get("notify")
        if not n:
            self.add("notify", SKIP, "no notify block: messages go to the journal only")
            return
        try:
            url = notify.read_url(self.env.path(n["url_file"]))
        except (OSError, ValueError):
            # The message of read_url names the mapped path; ours names the configured one.
            self.add("notify", FAIL, f"{n['url_file']} cannot be read or does not hold an https URL")
            return
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        expect = {
            "discord": ("discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"),
            "slack": ("hooks.slack.com",),
        }.get(n["format"])
        if expect and host not in expect:
            # The host of a webhook URL is not secret; its path is.
            self.add("notify", WARN, f"format {n['format']} but the URL's host is {host}, not {expect[0]}")
        else:
            self.add("notify", OK, f"{n['format']} webhook URL in {n['url_file']}")
        self.url = url

    # --- GitHub ----------------------------------------------------------------------

    def _get(self, path: str, token: str) -> "tuple[int, dict, object]":
        req = urllib.request.Request(f"{github.API}/{path}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", f"wakeci-doctor/{__version__}")
        try:
            with self.env.opener(req, timeout=15) as resp:
                return (
                    resp.status if hasattr(resp, "status") else 200,
                    dict(resp.headers),
                    json.loads(resp.read() or b"{}"),
                )
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers or {}), None

    def check_github(self) -> None:
        clock_done = False
        for s in self.cfg["sources"]:
            path = s["runners_path"]
            try:
                token = github.read_token(self.env.path(s["token_file"]))
            except (OSError, ValueError):
                for id in ("token", "token-perms", "runners"):
                    self.add(id, SKIP, f"{path}: its token file cannot be read (see files)")
                continue
            try:
                if self.check_source(path, s["repos"], token, clock_done):
                    self.tokens[path] = token
            except Exception as e:  # noqa: BLE001 — reported by kind only, never with the token
                reason = github.first_error(e) or type(e).__name__
                self.add("token", FAIL, f"{path}: GitHub cannot be reached: {reason}")
            clock_done = clock_done or any(r.id == "clock" for r in self.results)
        if not clock_done:
            self.add("clock", SKIP, "no answer from GitHub to compare with")
        self.check_runners()

    def check_source(self, path: str, repos: list, token: str, clock_done: bool) -> bool:
        """True when the token can do all that the controller needs."""
        code, raw, _ = self._get("rate_limit", token)
        headers = {k.lower(): v for k, v in raw.items()}
        if not clock_done and "date" in headers:
            self.check_clock(headers["date"])
        if code == 401:
            self.add("token", FAIL, f"{path}: the token is not valid or has expired")
            return False
        if code != 200:
            self.add("token", FAIL, f"{path}: GitHub answered {code} to a token check")
            return False
        left = headers.get("x-ratelimit-remaining", "?")
        self.add("token", OK, f"{path}: the token is valid; {left} requests left this hour")
        self.check_expiry(path, headers.get("github-authentication-token-expiration"))
        self.check_scopes(path, headers.get("x-oauth-scopes"))
        return self.check_perms(path, repos, token)

    def check_clock(self, date: str) -> None:
        try:
            theirs = email.utils.parsedate_to_datetime(date)
        except (TypeError, ValueError):
            self.add("clock", SKIP, "GitHub's Date header could not be read")
            return
        off = self.env.now() - theirs
        if abs(off) > CLOCK_LIMIT:
            self.add("clock", FAIL, f"this clock is {int(off.total_seconds())} s off GitHub's (is time sync on?)")
        else:
            self.add("clock", OK, f"within {int(abs(off.total_seconds()))} s of GitHub's")

    def check_expiry(self, path: str, value: Optional[str]) -> None:
        if not value:
            self.add("token-expiry", OK, f"{path}: the token does not expire")
            return
        try:
            when = datetime.strptime(value.replace("UTC", "+0000").strip(), "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            self.add("token-expiry", WARN, f"{path}: the token's expiry date could not be read")
            return
        left = when - self.env.now()
        if left <= timedelta(0):
            self.add("token-expiry", FAIL, f"{path}: the token expired on {when.date()}")
        elif left < EXPIRY_WARN:
            self.add("token-expiry", WARN, f"{path}: the token expires on {when.date()}, in {left.days} days")
        else:
            self.add("token-expiry", OK, f"{path}: the token expires on {when.date()}")

    def check_scopes(self, path: str, value: Optional[str]) -> None:
        if value is None:
            return  # a fine-grained token: its permissions are not in the headers
        scopes = [s.strip() for s in value.split(",") if s.strip()]
        broad = [s for s in scopes if not (s.startswith("read:") or s in READ_SCOPES)]
        if broad:
            self.add(
                "token-scope",
                WARN,
                f"{path}: a classic token with {', '.join(broad)}, which can also write: "
                "use a fine-grained token with read access only",
            )
        else:
            self.add("token-scope", OK, f"{path}: a classic token with read scopes only")

    def check_perms(self, path: str, repos: list, token: str) -> bool:
        bad = []
        code, _, _ = self._get(f"{path}/actions/runners?per_page=1", token)
        if code != 200:
            bad.append(f"its runners ({code})")
        for repo in repos:
            code, _, _ = self._get(f"repos/{repo}/actions/runs?status=queued&per_page=1", token)
            if code != 200:
                bad.append(f"the jobs of {repo} ({code})")
        if bad:
            self.add("token-perms", FAIL, f"{path}: the token cannot read {', '.join(bad)}")
            return False
        self.add("token-perms", OK, f"{path}: the token reads its runners and the jobs of {len(repos)} repositories")
        return True

    def _runners(self) -> "Optional[list[dict]]":
        gh = github.GitHub([], opener=self.env.opener)
        out: list[dict] = []
        for s in self.cfg["sources"]:
            token = self.tokens.get(s["runners_path"])
            if token is None:
                return None
            out += gh.runners(github.Source(s["runners_path"], tuple(s["repos"]), token))
        return out

    def check_runners(self) -> None:
        try:
            runners = self._runners()
        except Exception as e:  # noqa: BLE001 — reported by kind only
            self.add("runners", FAIL, f"the runner list cannot be read: {github.first_error(e) or type(e).__name__}")
            return
        if runners is None:
            self.add("runners", SKIP, "not every source could be read")
            return
        names = [r["name"] for r in runners]
        empty = [m.name for m in self.machines if not any(m.owns(n) for n in names)]
        alone = [n for n in names if not any(m.owns(n) for m in self.machines)]
        both = [n for n in names if sum(m.owns(n) for m in self.machines) > 1]
        if empty:
            self.add("runners", WARN, f"no runner at GitHub for {', '.join(empty)}")
        if alone:
            self.add("runners", WARN, f"runners on no machine (they always count as free): {', '.join(alone)}")
        if both:
            self.add("runners", WARN, f"runners on more than one machine: {', '.join(both)}")
        if not (empty or alone or both):
            self.add("runners", OK, f"{len(names)} runners, each on one of the {len(self.machines)} machines")

    # --- the flags -------------------------------------------------------------------

    def check_probe(self) -> None:
        key, known = self.env.path(self.cfg["ssh_key"]), self.env.path(self.cfg["known_hosts"])
        for m in self.machines:
            done = self._cmd(actions.ssh_check_command(m, key, known), timeout=20)
            if done is None:
                self.add("sleep-probe", FAIL, "ssh is not installed (the OpenSSH client)")
                return
            self.add("sleep-probe", *probe_result(m, done))

    def check_notify_test(self) -> None:
        url = getattr(self, "url", None)
        if not url:
            self.add("notify-test", SKIP, "no usable webhook (see notify)")
            return
        n = self.cfg["notify"]
        text = f"wakeci doctor: a test message from {socket.gethostname()}"
        body = json.dumps(notify.payload(n["format"], notify.Message("doctor", text))).encode("utf-8")
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", f"wakeci-doctor/{__version__}")
        try:
            with self.env.post(req, timeout=10) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            e.close()
            self.add("notify-test", FAIL, f"the webhook answered HTTP {e.code}")
            return
        except Exception as e:  # noqa: BLE001 — the text may hold the URL; only the kind is shown
            self.add("notify-test", FAIL, f"the post failed ({type(e).__name__})")
            return
        self.add("notify-test", OK, "a test message was posted; check the channel")

    def check_wake(self) -> None:
        m = next(x for x in self.machines if x.name == self.wake_name)

        def online() -> Optional[bool]:
            runners = self._runners()
            if runners is None:
                return None
            return any(m.owns(r["name"]) and r.get("status") == "online" for r in runners)

        try:
            now_on = online()
        except Exception as e:  # noqa: BLE001
            self.add("wake", FAIL, f"the runner list cannot be read: {github.first_error(e) or type(e).__name__}")
            return
        if now_on is None:
            self.add("wake", FAIL, "not every source could be read (see token), so a wake cannot be seen")
            return
        try:
            actions.wake(m, self.cfg["broadcast"], sock=self.env.sock)
        except OSError as e:
            self.add("wake", FAIL, f"the packet could not be sent to {self.cfg['broadcast']}: {e.strerror or e}")
            return
        if now_on:
            # GitHub shows a runner online for a few minutes after its machine suspends.
            self.add(
                "wake",
                WARN,
                f"a packet was sent, but {m.name} already had a runner online, so this does not show that "
                "Wake-on-LAN works: try again when it has been asleep for 5 minutes",
            )
            return
        waited = 0
        while waited < WAKE_WAIT:
            self.env.sleep(WAKE_POLL)
            waited += WAKE_POLL
            try:
                if online():
                    self.add("wake", OK, f"{m.name} woke: a runner came online after about {waited} s")
                    return
            except Exception:  # noqa: BLE001 — a failed poll is tried again
                continue
        self.add(
            "wake",
            FAIL,
            f"{m.name} had no runner online {WAKE_WAIT} s after the packet: check Wake-on-LAN in its BIOS, "
            "the cable (not Wi-Fi), its MAC and the broadcast address",
        )


def probe_result(m, done: subprocess.CompletedProcess) -> "tuple[str, str]":
    out, err = done.stdout.strip(), done.stderr.strip()
    code = done.returncode
    if code == actions.SLEEP_CHECKED and out.startswith("wakeci-sleep protocol="):
        state = dict(p.split("=", 1) for p in out.split()[1:] if "=" in p).get("state", "?")
        return OK, f"{m.name}: wakeci-sleep answers (state={state}); key, host key and sudo work"
    if code in (0, 4):
        return FAIL, (
            f"{m.name}: an older wakeci-sleep took the check for a suspend and is "
            f"{'suspending' if code == 0 else 'rebooting'}: run machine/install.sh again on it"
        )
    if code == 3:
        return (
            FAIL,
            f"{m.name}: an older wakeci-sleep (it would have suspended, but a job runs): run machine/install.sh again",
        )
    if code == 1 and "password" in err.lower():
        return FAIL, f"{m.name}: sudo asks for a password: the sudoers rule is missing; run machine/install.sh again"
    if code == TIMED_OUT:
        return FAIL, f"{m.name}: SSH did not finish in time (connected, but no answer)"
    if code == 255:
        low = err.lower()
        if "remote host identification has changed" in low:
            return FAIL, f"{m.name}: its host key does not match known_hosts"
        if "host key is known" in low or "host key verification failed" in low:
            return FAIL, f"{m.name}: its host key is not in known_hosts (see host-key)"
        if "load key" in low:
            return FAIL, f"{m.name}: the SSH key cannot be read by this user"
        if "permission denied" in low:
            return FAIL, f"{m.name}: the key is refused: it is not in {m.ssh_user}'s authorized_keys"
        return SKIP, f"{m.name}: no SSH connection (asleep or unreachable)"
    return FAIL, f"{m.name}: unexpected answer (exit code {code})"


def _user() -> str:
    try:
        import pwd

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError):
        return str(os.getuid())


LABEL = {OK: "OK", WARN: "WARN", FAIL: "FAIL", SKIP: "SKIP"}


def report(results: list, out) -> None:
    for r in results:
        print(f"{LABEL[r.status]:<4}  {r.id:<12}  {r.message}", file=out)
        if r.status in (WARN, FAIL):
            print(f"{'':<20}{DOCS}#{r.id}", file=out)
    count = {s: sum(r.status == s for r in results) for s in LABEL}
    print(f"\n{count[OK]} ok, {count[WARN]} warnings, {count[FAIL]} failures, {count[SKIP]} skipped", file=out)


def run(ns, env: Optional[Env] = None) -> int:
    if ns.offline and (ns.probe or ns.wake or ns.notify_test):
        print("wakeci doctor: --offline does not go with --probe, --wake or --notify-test", file=sys.stderr)
        return 2
    d = Doctor(ns.config, env or Env(), ns.offline, ns.probe, ns.wake, ns.notify_test)
    if ns.wake:
        # Checked before anything runs, so a typo does not wait for nothing.
        try:
            names = [m["name"] for m in main.load_config(ns.config)["machines"]]
        except (OSError, ValueError, KeyError, TypeError):
            names = []
        if names and ns.wake not in names:
            print(f"wakeci doctor: --wake {ns.wake}: no such machine ({', '.join(names)})", file=sys.stderr)
            return 2
    results = d.run()
    if ns.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        report(results, sys.stdout)
    return 1 if any(r.status == FAIL for r in results) else 0

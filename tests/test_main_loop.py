"""The control loop `main.run` end to end, with everything outside it replaced.

The clock, `time.sleep`, the GitHub API, Wake-on-LAN, SSH and the webhook sender are fakes,
so a poll takes no time and the loop stops after a set number of polls. The decisions
themselves are tested in test_decide.py; these tests check what the loop does with them:
what is sent, what is only logged, what is written to disk and what is notified.
"""

import contextlib
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from wakeci import actions, github, main

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
APP = "repos/example-org/app"
OTHER = "repos/example-org/other"
LARGE = [{"name": "self-hosted"}, {"name": "large"}]


class _Stop(Exception):
    """Raised by the fake `time.sleep` to end the loop after the last poll."""


def runner(name="host1-1", online=True, busy=False):
    return {"name": name, "status": "online" if online else "offline", "busy": busy, "labels": LARGE}


def queued_job(job_id=11):
    return {"id": job_id, "status": "queued", "labels": ["self-hosted", "large"], "created_at": "2026-10-01T11:59:00Z"}


def source_api(runners=(), jobs=()):
    """Answers for one source whose repository has one queued run holding `jobs`."""

    def answer(path, poll=0):
        if "/actions/runners?" in path:
            return {"runners": list(runners)}
        if "/actions/runs?status=queued" in path:
            return {"workflow_runs": [{"id": 1}] if jobs else []}
        if "/actions/runs?status=in_progress" in path:
            return {"workflow_runs": []}
        if "/actions/runs/1/jobs" in path:
            return {"jobs": list(jobs)}
        raise AssertionError(f"unexpected request {path}")

    return answer


class FakeResponse:
    def __init__(self, body, headers):
        self._body = json.dumps(body).encode("utf-8")
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSender:
    """Records messages instead of posting them; a key in `fail_once` fails its first post."""

    def __init__(self, loop, url, fmt, user_agent):
        self.loop = loop
        self.url = url
        self.fmt = fmt
        self.user_agent = user_agent
        self.sent = []
        self._seq = 0
        self._undelivered = []

    def send(self, msg):
        self._seq += 1
        self.sent.append((self.loop.poll, msg))
        if msg.key in self.loop.fail_once:
            self.loop.fail_once.discard(msg.key)
            self._undelivered.append((self._seq, msg))
        return self._seq

    def take_undelivered(self):
        out, self._undelivered = self._undelivered, []
        return out


class Loop:
    """One run of `main.run(cfg)` that ends after `polls` polls.

    `api(path, poll)` answers a GitHub request (the path without the API prefix) with a
    body, a `(body, headers)` pair, or by raising. `gaps[n]` adds extra seconds to the clock
    before poll `n`, for a controller that fell behind.
    """

    def __init__(self, test, api, polls, gaps=None, **cfg):
        self.test = test
        self.api = api
        self.polls = polls
        self.gaps = gaps or {}
        self.poll = 0
        self.now = T0
        self.requests = []  # (poll, path)
        self.woken = []  # (machine, broadcast, poll)
        self.suspended = []  # (machine, key, known_hosts, poll)
        self.sleep_results = []  # what put_to_sleep returns, in order; then "suspends in 5 s"
        self.saves = []  # poll of each state write
        self.save_errors = []  # raised by the next state writes, in order
        self.fail_once = set()
        self.senders = []
        self.servers = []
        self.metrics = []
        tmpdir = tempfile.TemporaryDirectory()
        test.addCleanup(tmpdir.cleanup)
        tmp = Path(tmpdir.name)
        (tmp / "token").write_text("test-token\n", encoding="utf-8")
        (tmp / "hook").write_text("https://hooks.example.com/test\n", encoding="utf-8")
        self.state_file = tmp / "state" / "last-online.json"
        config = {
            "config_version": 1,
            "dry_run": False,
            "poll_seconds": 20,
            "metrics_listen": "127.0.0.1:0",
            "state_file": str(self.state_file),
            "ssh_key": str(tmp / "key"),
            "known_hosts": str(tmp / "known_hosts"),
            "broadcast": "192.0.2.255",
            "machines": [
                {"name": "host1", "mac": "00:00:5e:00:53:01", "host": "192.0.2.11", "runner_prefixes": ["host1-"]}
            ],
            "sources": [{"runners_path": APP, "repos": ["example-org/app"], "token_file": str(tmp / "token")}],
        }
        config.update(cfg)
        config["sources"] = [{"token_file": str(tmp / "token"), **s} for s in config["sources"]]
        if config.get("notify") is not None:
            config["notify"] = {"url_file": str(tmp / "hook"), "format": "json", **config["notify"]}
        path = tmp / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        self.cfg = main.load_config(str(path))

    # --- fakes ---------------------------------------------------------------------------

    def _open(self, req, timeout):
        path = req.full_url.replace(github.API + "/", "")
        self.requests.append((self.poll, path))
        got = self.api(path, self.poll)
        body, headers = got if isinstance(got, tuple) else (got, {})
        return FakeResponse(body, headers)

    def _sleep(self, seconds):
        self.test.assertEqual(seconds, self.cfg["poll_seconds"])
        self.poll += 1
        if self.poll >= self.polls:
            raise _Stop
        self.now += timedelta(seconds=seconds + self.gaps.get(self.poll, 0))

    def _wake(self, m, broadcast):
        self.woken.append((m.name, broadcast, self.poll))

    def _put_to_sleep(self, m, key, known_hosts):
        self.suspended.append((m.name, key, known_hosts, self.poll))
        result = self.sleep_results.pop(0) if self.sleep_results else "suspends in 5 s"
        if isinstance(result, Exception):
            raise result
        return result

    def _save(self, path, data):
        self.saves.append(self.poll)
        if self.save_errors:
            raise self.save_errors.pop(0)
        real_save(path, data)

    # --- running -------------------------------------------------------------------------

    def run(self):
        loop = self

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                assert tz is timezone.utc
                return loop.now

        class Server(ThreadingHTTPServer):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                loop.servers.append(self)
                # Cleanups run last first: shutdown() stops serve_forever, then the socket closes.
                loop.test.addCleanup(self.server_close)
                loop.test.addCleanup(self.shutdown)

            def serve_forever(self, poll_interval=0.5):
                super().serve_forever(poll_interval=0.01)  # so shutdown() at cleanup is quick

        # Defined here, not at module level, so it inherits whatever main.Metrics.update a
        # test has patched in (see test_an_error_in_one_poll_does_not_end_the_loop).
        class Metrics(main.Metrics):
            def __init__(self):
                super().__init__()
                loop.metrics.append(self)

        def gh(sources):
            return github.GitHub(sources, opener=self._open, clock=lambda: loop.now)

        def sender(url, fmt, user_agent):
            s = FakeSender(loop, url, fmt, user_agent)
            loop.senders.append(s)
            return s

        with contextlib.ExitStack() as stack:
            for target, name, fake in (
                (main, "datetime", Clock),
                (main, "time", SimpleNamespace(sleep=self._sleep)),
                (main, "GitHub", gh),
                (main, "Sender", sender),
                (main, "Metrics", Metrics),
                (main, "ThreadingHTTPServer", Server),
                (main, "save_state", self._save),
                (actions, "wake", self._wake),
                (actions, "put_to_sleep", self._put_to_sleep),
            ):
                stack.enter_context(mock.patch.object(target, name, fake))
            logs = stack.enter_context(self.test.assertLogs("wakeci", level="INFO"))
            with self.test.assertRaises(_Stop):
                main.run(self.cfg)
        self.logs = logs.output
        return self

    # --- reading the outcome -------------------------------------------------------------

    @property
    def messages(self):
        """(poll, key, text) of every notification sent."""
        return [(poll, m.key, m.text) for s in self.senders for poll, m in s.sent]

    def metric(self, line_start):
        lines = [x for x in self.metrics[0].text.splitlines() if x.startswith(line_start + " ")]
        self.test.assertEqual(len(lines), 1, line_start)
        return float(lines[0].rsplit(" ", 1)[1])

    def logged(self, part):
        return [x for x in self.logs if part in x]


real_save = main.save_state


def answers(**by_source):
    """An `api` that sends each request to the answer of its source."""

    def api(path, poll):
        for prefix, answer in by_source.items():
            if path.startswith(prefix):
                return answer(path, poll)
        raise AssertionError(f"no source for {path}")

    return api


class WakeTest(unittest.TestCase):
    def test_a_queued_job_wakes_a_sleeping_machine(self):
        api = answers(**{APP: source_api([runner(online=False)], [queued_job()])})
        loop = Loop(self, api, polls=1).run()
        self.assertEqual(loop.woken, [("host1", "192.0.2.255", 0)])
        self.assertEqual(len(loop.logged("wake host1: 1 queued job(s) with `large` — packet sent")), 1)
        self.assertEqual(loop.metric('wakeci_actions_total{kind="wake",machine="host1"}'), 1)
        self.assertEqual(loop.metric('wakeci_machine_waking{machine="host1"}'), 1)
        self.assertEqual(loop.metric("wakeci_dry_run"), 0)

    def test_in_dry_run_the_wake_is_only_logged(self):
        api = answers(**{APP: source_api([runner(online=False)], [queued_job()])})
        loop = Loop(self, api, polls=1, dry_run=True).run()
        self.assertEqual(loop.woken, [])
        self.assertEqual(len(loop.logged("[dry-run] wake host1: 1 queued job(s) with `large`")), 1)
        self.assertEqual(loop.metric('wakeci_actions_total{kind="wake",machine="host1"}'), 1)
        self.assertEqual(loop.metric("wakeci_dry_run"), 1)

    def test_a_machine_that_does_not_wake_raises_one_alert(self):
        # Two packets three minutes apart, then an alert and no more packets for a while.
        api = answers(**{APP: source_api([runner(online=False)], [queued_job()])})
        loop = Loop(self, api, polls=5, poll_seconds=200, notify={}).run()
        self.assertEqual([w[2] for w in loop.woken], [0, 1])
        alerts = [m for m in loop.messages if m[1] == "wake:host1"]
        self.assertEqual(alerts, [(2, "wake:host1", "wakeci: host1 did not wake up after 2 attempts.")])
        self.assertEqual(len(loop.logged("alert host1: did not wake up after 2 attempts — logged")), 1)

    def test_state_on_disk_is_loaded_at_start(self):
        # Online a day ago according to the state file: no weekly wake. Without it, the
        # machine has never been seen online and is woken at once.
        api = answers(**{APP: source_api([runner(online=False)])})
        loop = Loop(self, api, polls=1)
        loop.state_file.parent.mkdir()
        loop.state_file.write_text(
            json.dumps({"host1": {"last_online": (T0 - timedelta(days=1)).isoformat()}}), encoding="utf-8"
        )
        self.assertEqual(loop.run().woken, [])
        fresh = Loop(self, api, polls=1).run()
        self.assertEqual(fresh.woken, [("host1", "192.0.2.255", 0)])
        self.assertEqual(len(fresh.logged("weekly: the runners must connect")), 1)


class SuspendTest(unittest.TestCase):
    def test_an_idle_machine_is_suspended_once_idle_minutes_have_passed(self):
        api = answers(**{APP: source_api([runner()])})
        loop = Loop(self, api, polls=4, poll_seconds=600, idle_minutes=30).run()
        cfg = loop.cfg
        self.assertEqual(loop.suspended, [("host1", cfg["ssh_key"], cfg["known_hosts"], 3)])
        self.assertEqual(len(loop.logged("sleep host1: idle for 30 min — suspends in 5 s")), 1)
        self.assertEqual(loop.metric('wakeci_actions_total{kind="sleep",machine="host1"}'), 1)

    def test_in_dry_run_a_would_be_suspend_is_logged_once_an_hour(self):
        api = answers(**{APP: source_api([runner()])})
        loop = Loop(self, api, polls=11, poll_seconds=600, idle_minutes=30, dry_run=True).run()
        self.assertEqual(loop.suspended, [])
        self.assertEqual(len(loop.logged("[dry-run] sleep host1: idle for 30 min")), 1)
        self.assertEqual(len(loop.logged("[dry-run] sleep host1: idle for 90 min")), 1)
        self.assertEqual(len(loop.logged("[dry-run] sleep host1")), 2)

    def test_three_failed_suspends_in_a_row_raise_an_alert(self):
        # A refusal and an SSH error both count; each one restarts the idle time.
        api = answers(**{APP: source_api([runner()])})
        loop = Loop(self, api, polls=10, poll_seconds=600, idle_minutes=30, notify={})
        loop.sleep_results = [
            "refused: a job is running on the machine",
            OSError("no route"),
            "refused: a job is running on the machine",
        ]
        loop.run()
        self.assertEqual([s[3] for s in loop.suspended], [3, 6, 9])
        self.assertEqual(len(loop.logged("error: OSError('no route')")), 1)
        self.assertEqual(loop.metric('wakeci_actions_total{kind="sleep_failed",machine="host1"}'), 3)
        alerts = [m for m in loop.messages if m[1] == "sleep:host1"]
        self.assertEqual(
            alerts,
            [
                (
                    9,
                    "sleep:host1",
                    "wakeci: host1 failed to suspend 3 times in a row: refused: a job is running on the machine.",
                )
            ],
        )
        self.assertEqual(len(loop.logged("failed to suspend 3 times in a row")), 1)

    def test_an_error_on_one_machine_does_not_stop_the_others(self):
        machines = [
            {"name": n, "mac": f"00:00:5e:00:53:0{i}", "host": f"192.0.2.1{i}", "runner_prefixes": [f"{n}-"]}
            for i, n in ((1, "host1"), (2, "host2"))
        ]
        api = answers(**{APP: source_api([runner("host1-1"), runner("host2-1")])})
        loop = Loop(self, api, polls=1, idle_minutes=0, machines=machines)
        loop.sleep_results = [OSError("no route")]
        loop.run()
        self.assertEqual([s[0] for s in loop.suspended], ["host1", "host2"])
        self.assertEqual(len(loop.logged("sleep host1: idle for 0 min — error: OSError('no route')")), 1)
        self.assertEqual(len(loop.logged("sleep host2: idle for 0 min — suspends in 5 s")), 1)
        self.assertEqual(loop.metric('wakeci_actions_total{kind="sleep_failed",machine="host1"}'), 1)


class FailingSourceTest(unittest.TestCase):
    def test_an_unreadable_source_suspends_nothing_and_is_notified_after_the_wait(self):
        # host1 is idle on the readable source from the start; the other source answers 401
        # for the first 40 minutes, longer than idle_minutes. Nothing is suspended until it
        # is readable again, and then host1 is suspended at once.
        def other(path, poll):
            if poll < 8:
                raise urllib.error.HTTPError(github.API + "/" + path, 401, "Unauthorized", {}, None)
            return source_api()(path)

        sources = [
            {"runners_path": APP, "repos": ["example-org/app"]},
            {"runners_path": OTHER, "repos": ["example-org/other"]},
        ]
        api = answers(**{APP: source_api([runner()]), OTHER: other})
        loop = Loop(
            self, api, polls=10, poll_seconds=300, idle_minutes=30, sources=sources, notify={"after_minutes": 10}
        ).run()
        self.assertEqual([s[3] for s in loop.suspended], [8])
        source_msgs = [m for m in loop.messages if m[1] == f"source:{OTHER}"]
        self.assertEqual(
            source_msgs,
            [
                (
                    2,
                    f"source:{OTHER}",
                    f"wakeci: GitHub source {OTHER} cannot be read: GitHub 401 Unauthorized. "
                    "No machine is suspended while this lasts (check its token).",
                ),
                (8, f"source:{OTHER}", f"wakeci: GitHub source {OTHER} is readable again."),
            ],
        )
        self.assertEqual(len(loop.logged(f"source {OTHER} not read: GitHub 401 Unauthorized")), 8)
        self.assertEqual(loop.metric(f'wakeci_github_ok{{source="{OTHER}"}}'), 1)
        self.assertEqual(loop.metric(f'wakeci_github_ok{{source="{APP}"}}'), 1)

    def test_github_ok_is_zero_while_the_source_fails(self):
        def broken(path, poll):
            raise urllib.error.URLError("timed out")

        loop = Loop(self, answers(**{APP: broken}), polls=1).run()
        self.assertEqual(loop.metric(f'wakeci_github_ok{{source="{APP}"}}'), 0)
        self.assertEqual(len(loop.logged(f"source {APP} not read: network: timed out")), 1)


class RateLimitTest(unittest.TestCase):
    def test_a_nearly_used_up_limit_pauses_polling_until_the_reset(self):
        reset = T0 + timedelta(minutes=15)

        def app(path, poll):
            left = "50" if poll == 0 else "4999"
            return source_api([runner()])(path), {
                "X-RateLimit-Remaining": left,
                "X-RateLimit-Reset": str(int(reset.timestamp())),
            }

        loop = Loop(self, answers(**{APP: app}), polls=5, poll_seconds=300, notify={}).run()
        self.assertEqual(sorted({poll for poll, _ in loop.requests}), [0, 3, 4])
        limit = [m for m in loop.messages if m[1] == "limit"]
        self.assertEqual(
            limit,
            [
                (
                    1,
                    "limit",
                    "wakeci: GitHub request limit nearly used up; not polling until 2026-10-01 12:15 UTC. "
                    "Machines are neither woken nor suspended meanwhile.",
                ),
                (3, "limit", "wakeci: GitHub request limit reset; polling again."),
            ],
        )
        self.assertEqual(len(loop.logged("waiting until 2026-10-01T12:15:00+00:00")), 2)
        self.assertEqual(loop.metric(f'wakeci_github_remaining{{source="{APP}"}}'), 4999)


class StateFileTest(unittest.TestCase):
    def test_state_is_written_only_when_the_day_changes(self):
        # Online at every poll; last_online changes each time, but only its date is
        # worth a write: once at the first poll and once after midnight.
        api = answers(**{APP: source_api([runner(busy=True)])})
        loop = Loop(self, api, polls=4, poll_seconds=600)
        loop.now = T0.replace(hour=23, minute=40)
        loop.run()
        self.assertEqual(loop.saves, [0, 2])
        saved = json.loads(loop.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"host1": {"last_online": "2026-10-02T00:00:00+00:00"}})

    def test_a_weekly_hold_is_written_as_soon_as_it_starts(self):
        # Last online eight days ago: woken for the weekly check, held awake an hour once its
        # runners are back. Both the new day and the hold are written at once.
        state = {"host1": {"last_online": (T0 - timedelta(days=8)).isoformat()}}
        api = answers(**{APP: lambda path, poll: source_api([runner(online=poll >= 1)])(path, poll)})
        loop = Loop(self, api, polls=3)
        loop.state_file.parent.mkdir()
        loop.state_file.write_text(json.dumps(state), encoding="utf-8")
        loop.run()
        self.assertEqual(loop.woken, [("host1", "192.0.2.255", 0)])
        self.assertEqual(loop.saves, [1])
        saved = json.loads(loop.state_file.read_text(encoding="utf-8"))
        held_from = T0 + timedelta(seconds=20)
        self.assertEqual(
            saved,
            {
                "host1": {
                    "last_online": held_from.isoformat(),
                    "hold_until": (held_from + timedelta(hours=1)).isoformat(),
                }
            },
        )

    def test_a_failed_write_is_tried_again_at_the_next_poll(self):
        api = answers(**{APP: source_api([runner(busy=True)])})
        loop = Loop(self, api, polls=3)
        loop.save_errors = [OSError("read-only file system")]
        loop.run()
        self.assertEqual(loop.saves, [0, 1])
        self.assertEqual(len(loop.logged("loop error")), 1)
        saved = json.loads(loop.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"host1": {"last_online": (T0 + timedelta(seconds=20)).isoformat()}})

    def test_a_hold_from_disk_keeps_the_machine_awake_after_a_restart(self):
        # Restarted in the middle of a weekly hold: the idle machine is not suspended
        # before the hold ends, although idle_minutes is 0.
        hold = T0 + timedelta(minutes=30)
        state = {"host1": {"last_online": (T0 - timedelta(minutes=30)).isoformat(), "hold_until": hold.isoformat()}}
        api = answers(**{APP: source_api([runner()])})
        loop = Loop(self, api, polls=4, poll_seconds=600, idle_minutes=0)
        loop.state_file.parent.mkdir()
        loop.state_file.write_text(json.dumps(state), encoding="utf-8")
        loop.run()
        self.assertEqual([s[3] for s in loop.suspended], [3])

    def test_a_machine_in_the_state_file_but_not_in_the_config_is_ignored(self):
        state = {
            "gone": {"last_online": (T0 - timedelta(days=1)).isoformat()},
            "host1": {"last_online": (T0 - timedelta(days=1)).isoformat()},
        }
        api = answers(**{APP: source_api([runner(busy=True)])})
        loop = Loop(self, api, polls=1)
        loop.state_file.parent.mkdir()
        loop.state_file.write_text(json.dumps(state), encoding="utf-8")
        loop.run()
        self.assertEqual(loop.logged("loop error"), [])
        saved = json.loads(loop.state_file.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"host1": {"last_online": T0.isoformat()}})


class NotifyTest(unittest.TestCase):
    def test_the_start_is_notified_with_the_version_and_mode(self):
        api = answers(**{APP: source_api([runner()])})
        loop = Loop(self, api, polls=1, dry_run=True, notify={}).run()
        (sender,) = loop.senders
        self.assertEqual(sender.url, "https://hooks.example.com/test")
        self.assertEqual(sender.fmt, "json")
        version = main.read_version()
        self.assertEqual(sender.user_agent, f"wakeci/{version}")
        self.assertEqual(loop.messages, [(0, "start", f"wakeci {version} started: 1 machines, dry_run=true.")])
        self.assertEqual(len(loop.logged(f"started {version}: 1 machines, dry_run=True")), 1)

    def test_without_a_notify_block_nothing_is_sent(self):
        api = answers(**{APP: source_api([runner(online=False)], [queued_job()])})
        loop = Loop(self, api, polls=5, poll_seconds=200).run()
        self.assertEqual(loop.senders, [])
        self.assertEqual(len(loop.logged("started ")), 1)
        self.assertEqual(len(loop.logged("alert host1: did not wake up after 2 attempts")), 1)

    def test_an_undelivered_message_is_counted_and_sent_again_later(self):
        api = answers(**{APP: source_api([runner()])})
        loop = Loop(self, api, polls=17, notify={})
        loop.fail_once = {"start"}
        loop.run()
        starts = [(poll, text) for poll, key, text in loop.messages if key == "start"]
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0][0], 0)
        self.assertEqual(starts[1][0], 15)  # 5 minutes after the failure at the first poll
        self.assertTrue(starts[1][1].endswith("(delayed: first failed 2026-10-01 12:00 UTC)"), starts[1][1])
        self.assertEqual(loop.metric("wakeci_notify_failed_total"), 1)


class LoopTest(unittest.TestCase):
    def test_busy_time_is_added_per_poll_and_capped_after_a_long_gap(self):
        api = answers(**{APP: source_api([runner(busy=True), runner("host1-2")])})
        loop = Loop(self, api, polls=4, gaps={2: 600}).run()
        # 20 s, then a 620 s gap counted as 40 s, then 20 s; the free runner counts nothing.
        self.assertEqual(loop.metric('wakeci_runner_busy_seconds_total{runner="host1-1"}'), 80)
        self.assertNotIn("host1-2", loop.metrics[0].text)

    def test_an_error_in_one_poll_does_not_end_the_loop(self):
        api = answers(**{APP: source_api([runner(online=False)], [queued_job()])})
        real_step = main.Controller.step
        calls = []

        def step(ctl, obs, now):
            calls.append(now)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real_step(ctl, obs, now)

        real_update = main.Metrics.update

        def update(metrics, *a):
            if len(calls) == 2:
                raise RuntimeError("metrics boom")
            return real_update(metrics, *a)

        with mock.patch.object(main.Controller, "step", step), mock.patch.object(main.Metrics, "update", update):
            loop = Loop(self, api, polls=3).run()
        self.assertEqual(len(calls), 3)
        self.assertEqual(loop.woken, [("host1", "192.0.2.255", 1)])
        self.assertEqual(len(loop.logged("loop error")), 1)
        self.assertEqual(len(loop.logged("metrics error")), 1)

    def test_metrics_are_served_over_http(self):
        api = answers(**{APP: source_api([runner()], [queued_job()])})
        loop = Loop(self, api, polls=1).run()
        (server,) = loop.servers
        host, port = server.server_address[:2]
        # Straight to the loopback server, whatever proxy the environment sets.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://{host}:{port}/metrics", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers["Content-Type"], "text/plain; version=0.0.4")
            body = resp.read().decode("utf-8")
        self.assertEqual(body, loop.metrics[0].text)
        self.assertIn('wakeci_machine_awake{machine="host1"} 1', body)
        self.assertIn('wakeci_queued_jobs{labels="large"} 1', body)
        self.assertIn('wakeci_queued_oldest_seconds{labels="large"} 60', body)
        with self.assertRaises(urllib.error.HTTPError) as err:
            opener.open(f"http://{host}:{port}/other", timeout=5)
        self.assertEqual(err.exception.code, 404)
        err.exception.close()


if __name__ == "__main__":
    unittest.main()

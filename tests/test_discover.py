import contextlib
import copy
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.test_io import FakeResponse
from wakeci import cli, config_check, discover, github

EXAMPLE = json.loads((Path(__file__).resolve().parent.parent / "controller" / "config.example.json").read_text())
# Assembled at run time, so the repository never holds anything that looks like a token.
TOKEN = "ghp_" + "Z" * 36


def runner(name, online=True, labels=("self-hosted",)):
    return {"name": name, "status": "online" if online else "offline", "labels": [{"name": n} for n in labels]}


def cfg_with(*machines):
    cfg = copy.deepcopy(EXAMPLE)
    cfg["machines"] = [copy.deepcopy(m) for m in machines]
    return cfg


HOST1 = EXAMPLE["machines"][0]  # host1-a-, host1-b-


class PrefixTest(unittest.TestCase):
    def test_the_number_after_a_separator_is_dropped(self):
        for name, prefix in (
            ("ci5-3", "ci5-"),
            ("runner5-cpp-12", "runner5-cpp-"),
            ("box_2", "box_"),
            ("rack.7", "rack."),
        ):
            self.assertEqual(discover.prefix_of(name), prefix, name)

    def test_a_name_without_a_separated_number_is_its_own_prefix(self):
        # "host1" and "host10" could be two machines: stripping the number would join them.
        for name in ("host1", "host10", "builder", "a-b", "7"):
            self.assertEqual(discover.prefix_of(name), name, name)


class PlanTest(unittest.TestCase):
    def plan(self, cfg, runners, machines=None):
        return discover.plan(cfg, {"repos/example-org/app": runners}, machines or {})

    def test_runners_of_an_existing_machine_add_nothing(self):
        cfg = cfg_with(HOST1)
        new, report = self.plan(cfg, [runner("host1-a-1"), runner("host1-b-1", online=False)])
        self.assertEqual(new, cfg)
        self.assertEqual(report.added, [])
        self.assertEqual(report.stale, [])

    def test_a_new_prefix_becomes_a_machine_with_placeholders(self):
        new, report = self.plan(cfg_with(HOST1), [runner("host1-a-1"), runner("ci5-1"), runner("ci5-2")])
        added = new["machines"][-1]
        self.assertEqual(
            added,
            {
                "name": "ci5",
                "mac": "FILL-IN",
                "host": "FILL-IN",
                "ssh_user": "wakeci",
                "runner_prefixes": ["ci5-"],
                "rank": 1,
            },
        )
        self.assertEqual(report.added, ["ci5"])
        self.assertEqual(report.to_fill, ["machines[1].mac", "machines[1].host"])

    def test_existing_fields_are_never_changed(self):
        cfg = cfg_with(*EXAMPLE["machines"])
        cfg["machines"][0]["rank"] = 7
        before = json.dumps(cfg)
        new, _ = self.plan(cfg, [runner("host1-a-1"), runner("zz-1"), runner("host9-1")])
        self.assertEqual(json.dumps(cfg), before, "the input is not modified")
        self.assertEqual(new["machines"][: len(cfg["machines"])], cfg["machines"])
        for key in cfg:
            if key != "machines":
                self.assertEqual(new[key], cfg[key], key)

    def test_prefixes_without_runners_are_reported_not_removed(self):
        cfg = cfg_with(*EXAMPLE["machines"])
        new, report = self.plan(cfg, [runner("host1-a-1")])
        self.assertEqual(new, cfg)
        self.assertIn(("host1", "host1-b-"), report.stale)
        self.assertIn(("host3", "host3-a-"), report.stale)

    def test_prefixes_with_the_same_number_are_suggested_not_joined(self):
        new, report = self.plan(cfg_with(HOST1), [runner("ci5-1"), runner("runner5-cpp-1"), runner("ci6-1")])
        self.assertEqual([m["name"] for m in new["machines"][1:]], ["ci5", "ci6", "runner5-cpp"])
        self.assertEqual(report.suggestions, ["looks like one machine: --machine ci5=ci5-,runner5-cpp-"])

    def test_a_new_prefix_with_the_number_of_an_existing_machine_is_pointed_out(self):
        machine = dict(HOST1, name="runner5", runner_prefixes=["ci5-"])
        _, report = self.plan(cfg_with(machine), [runner("ci5-1"), runner("runner5-cpp-1")])
        self.assertEqual(
            report.suggestions,
            ["runner5-cpp- may belong to the existing machine runner5: add it to its runner_prefixes by hand"],
        )

    def test_machine_arguments_join_prefixes(self):
        new, report = self.plan(
            cfg_with(HOST1),
            [runner("ci5-1"), runner("runner5-cpp-1"), runner("runner5-cpp-2")],
            {"runner5": ["ci5-", "runner5-cpp-"]},
        )
        self.assertEqual(new["machines"][1]["name"], "runner5")
        self.assertEqual(new["machines"][1]["runner_prefixes"], ["ci5-", "runner5-cpp-"])
        self.assertEqual(len(new["machines"]), 2)
        self.assertEqual(report.suggestions, [])

    def test_a_machine_argument_for_an_existing_machine_is_refused(self):
        with self.assertRaisesRegex(ValueError, "host1 already exists"):
            self.plan(cfg_with(HOST1), [runner("ci5-1")], {"host1": ["ci5-"]})

    def test_a_machine_argument_that_matches_no_runner_is_refused(self):
        with self.assertRaisesRegex(ValueError, "no runner starts with 'ci9-'"):
            self.plan(cfg_with(HOST1), [runner("ci5-1")], {"runner9": ["ci9-"]})

    def test_a_machine_argument_with_a_bad_name_is_refused(self):
        with self.assertRaisesRegex(ValueError, "machine name"):
            self.plan(cfg_with(HOST1), [runner("ci5-1")], {"a b": ["ci5-"]})

    def test_machine_arguments_must_not_repeat_or_be_empty(self):
        for machines, text in (
            ({"runner5": []}, "at least one prefix"),
            ({"runner5": ["ci5-"], "runner6": ["ci5-"]}, "more than one machine"),
            ({"runner5": ["host1-a-"]}, "already a prefix of an existing machine"),
        ):
            with self.assertRaisesRegex(ValueError, text):
                self.plan(cfg_with(HOST1), [runner("ci5-1"), runner("host1-a-1")], machines)

    def test_a_file_without_machines_is_only_added_to(self):
        cfg = cfg_with(HOST1)
        del cfg["machines"]
        new, report = self.plan(cfg, [runner("ci5-1")])
        self.assertEqual(report.added, ["ci5"])
        self.assertEqual({k: v for k, v in new.items() if k != "machines"}, cfg)

    def test_a_new_name_that_is_taken_gets_a_suffix(self):
        machine = dict(HOST1, name="ci5", runner_prefixes=["other-"])
        new, _ = self.plan(cfg_with(machine), [runner("other-1"), runner("ci5-1")])
        self.assertEqual(new["machines"][1]["name"], "ci5-2")

    def test_runners_whose_names_overlap_are_reported(self):
        # box1 has no separated number, so it is its own prefix, and it also matches box10.
        _, report = self.plan(cfg_with(HOST1), [runner("box1"), runner("box10")])
        self.assertIn("a runner whose name starts with 'box10' belongs to both box1 and box10", report.warnings)

    def test_groups_count_runners_online_and_labels(self):
        _, report = self.plan(
            cfg_with(HOST1),
            [runner("ci5-1", labels=("self-hosted", "gpu")), runner("ci5-2", online=False, labels=("self-hosted",))],
        )
        group = next(g for g in report.groups if g.prefix == "ci5-")
        self.assertEqual((group.machine, group.runners, group.online), ("ci5", 2, 1))
        self.assertEqual(group.labels, ("gpu", "self-hosted"))
        self.assertEqual(group.source, "repos/example-org/app")

    def test_the_result_with_placeholders_is_refused_by_check_until_filled_in(self):
        new, report = self.plan(cfg_with(HOST1), [runner("ci5-1")])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            Path(path).write_text(json.dumps(new))
            with self.assertRaisesRegex(ValueError, r"machines\[1\].mac is still FILL-IN"):
                config_check.check(path)
            new["machines"][1].update(mac="00:00:5e:00:53:55", host="192.0.2.55")
            Path(path).write_text(json.dumps(new))
            config_check.check(path)

    def test_a_new_configuration_starts_safe(self):
        cfg = discover.new_config(
            [("orgs/example-org", ["example-org/app"]), ("repos/example-org2/x", ["example-org2/x"])]
        )
        self.assertIs(cfg["dry_run"], True)
        self.assertEqual(cfg["config_version"], config_check.CONFIG_VERSION)
        self.assertEqual(
            [s["token_file"] for s in cfg["sources"]],
            ["/etc/wakeci/token-example-org", "/etc/wakeci/token-example-org2"],
        )
        self.assertEqual(cfg["machines"], [])

    def test_two_new_sources_of_one_owner_get_two_token_files(self):
        cfg = discover.new_config([("repos/o/a", ["o/a"]), ("repos/o/b", ["o/b"])])
        self.assertEqual([s["token_file"] for s in cfg["sources"]], ["/etc/wakeci/token-o-a", "/etc/wakeci/token-o-b"])


# --- the command ---------------------------------------------------------------------


class FakeRunners:
    """GitHub that answers the runner list of each source, page by page."""

    def __init__(self, by_path, fail=None):
        self.by_path = by_path
        self.fail = fail
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        if self.fail:
            raise urllib.error.HTTPError(req.full_url, self.fail, "Bad credentials", {}, None)
        url = req.full_url.replace(github.API + "/", "")
        path, _, query = url.partition("?")
        page = int(dict(p.split("=") for p in query.split("&"))["page"])
        runners = self.by_path[path.replace("/actions/runners", "")]
        return FakeResponse({"total_count": len(runners), "runners": runners[(page - 1) * 100 : page * 100]})


def run(args, opener):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:  # not one parenthesised `with`: that needs Python 3.10
        stack.enter_context(mock.patch.object(discover, "OPENER", opener))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            code = cli.main(["discover", *args])
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.token = os.path.join(self.dir.name, "token")
        Path(self.token).write_text(TOKEN + "\n")
        self.config = os.path.join(self.dir.name, "config.json")
        cfg = cfg_with(HOST1)
        cfg["sources"] = cfg["sources"][:1]
        Path(self.config).write_text(json.dumps(cfg, indent=2) + "\n")
        self.github = FakeRunners({"repos/example-org/app": [runner("host1-a-1"), runner("ci5-1")]})

    def update(self, *extra):
        return run(["--update", self.config, "--token", f"repos/example-org/app={self.token}", *extra], self.github)

    def test_new_file_goes_to_stdout(self):
        code, out, err = run(
            ["--source", "repos/example-org/app", "--repos", "example-org/app", "--token-file", self.token], self.github
        )
        self.assertEqual(code, 0, err)
        cfg = json.loads(out)
        self.assertEqual([m["name"] for m in cfg["machines"]], ["ci5", "host1-a"])
        self.assertIn("not deployable yet: 4 fields to fill", err)

    def test_update_prints_the_result_and_a_diff_and_leaves_the_file(self):
        before = Path(self.config).read_text()
        code, out, err = self.update()
        self.assertEqual(code, 0, err)
        self.assertEqual(Path(self.config).read_text(), before)
        self.assertEqual(json.loads(out)["machines"][1]["name"], "ci5")
        self.assertIn('+      "name": "ci5",', err)
        self.assertFalse(os.path.exists(self.config + ".bak"))

    def test_update_write_replaces_the_file_and_keeps_a_backup(self):
        before = Path(self.config).read_text()
        code, out, err = self.update("--write")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "")
        self.assertEqual(Path(self.config + ".bak").read_text(), before)
        self.assertEqual(json.loads(Path(self.config).read_text())["machines"][1]["name"], "ci5")

    def test_nothing_new_writes_nothing(self):
        self.github.by_path["repos/example-org/app"] = [runner("host1-a-1")]
        code, _, err = self.update("--write")
        self.assertEqual(code, 0, err)
        self.assertIn("nothing to add", err)
        self.assertFalse(os.path.exists(self.config + ".bak"))

    def test_write_follows_a_symbolic_link_and_keeps_mode_and_owner(self):
        real = os.path.join(self.dir.name, "real.json")
        os.replace(self.config, real)
        os.chmod(real, 0o400)
        os.symlink(real, self.config)
        code, _, err = self.update("--write")
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.islink(self.config))
        self.assertEqual(json.loads(Path(real).read_text())["machines"][1]["name"], "ci5")
        self.assertTrue(os.path.exists(real + ".bak"))
        st = os.stat(real)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid), (0o400, os.getuid(), os.getgid()))

    def test_a_file_without_a_final_newline_gives_a_clean_diff(self):
        Path(self.config).write_text(Path(self.config).read_text().rstrip("\n"))
        code, _, err = self.update()
        self.assertEqual(code, 0, err)
        self.assertNotIn("}+", err)
        self.assertTrue(err.endswith("   ]\n }\n"), err[-40:])

    def test_malformed_machines_are_reported_not_a_traceback(self):
        cfg = json.loads(Path(self.config).read_text())
        cfg["machines"].append({"name": "broken"})
        Path(self.config).write_text(json.dumps(cfg))
        code, _, err = self.update()
        self.assertEqual(code, 1)
        self.assertIn("malformed: run wakeci check", err)

    def test_a_repeated_machine_argument_is_refused(self):
        code, _, err = self.update("--machine", "a=ci5-", "--machine", "a=ci6-")
        self.assertEqual(code, 2)
        self.assertIn("--machine a is given twice", err)

    def test_more_than_one_page_of_runners(self):
        self.github.by_path["repos/example-org/app"] = [runner(f"ci{i}-1") for i in range(150)]
        code, out, err = self.update()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)["machines"]), 151)

    def test_json_report(self):
        code, out, err = self.update("--json")
        self.assertEqual(code, 0, err)
        body = json.loads(out)
        self.assertEqual(body["report"]["added"], ["ci5"])
        self.assertEqual(body["config"]["machines"][1]["name"], "ci5")

    def test_every_source_of_the_file_needs_a_token(self):
        code, _, err = run(["--update", self.config], self.github)
        self.assertEqual(code, 2)
        self.assertIn("no token for repos/example-org/app", err)

    def test_the_token_is_never_shown(self):
        for fail in (None, 401, 403, 404, 500):
            self.github.fail = fail
            for args in ((), ("--json",), ("--write",)):
                code, out, err = self.update(*args)
                self.assertNotIn(TOKEN, out + err, (fail, args))
                if fail:
                    self.assertEqual(code, 1)
                    self.assertIn(f"GitHub {fail}", err)
            if os.path.exists(self.config):
                self.assertNotIn(TOKEN, Path(self.config).read_text())

    def test_only_get_requests_with_the_token_in_a_header(self):
        self.update()
        for req in self.github.requests:
            self.assertEqual(req.get_method(), "GET")
            self.assertNotIn(TOKEN, req.full_url)
            self.assertEqual(req.headers["Authorization"], f"Bearer {TOKEN}")

    def test_the_token_can_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"WAKECI_TOKEN": TOKEN}):
            code, out, err = run(["--source", "repos/example-org/app", "--repos", "example-org/app"], self.github)
        self.assertEqual(code, 0, err)

    def test_usage_errors(self):
        for args, text in (
            ([], "--source or --update"),
            (["--source", "repos/o/r"], "one --repos for each --source"),
            (["--source", "repos/o/r", "--repos", "o/r", "--token-file", "a", "--token-file", "b"], "--token-file"),
            (["--source", "nope", "--repos", "o/r", "--token-file", "a"], "repos/<owner>/<repo> or orgs/<org>"),
            (["--update", "c.json", "--source", "repos/o/r"], "not with --update"),
            (["--source", "repos/o/r", "--repos", "o/r", "--token-file", "a", "--write"], "--write needs --update"),
            (["--update", "c.json", "--machine", "x"], "NAME=PREFIX"),
            (["--update", "c.json", "--token", "x"], "SOURCE=FILE"),
        ):
            code, _, err = run(args, self.github)
            self.assertEqual(code, 2, args)
            self.assertIn(text, err, args)


if __name__ == "__main__":
    unittest.main()

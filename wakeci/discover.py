"""``wakeci discover``: build or extend a configuration from the runners GitHub knows.

GitHub knows each runner's name, labels and status, but not which machine it runs on, nor
that machine's MAC or address. So runners are grouped into machines by name, and what only
the user knows is written as ``FILL-IN``, which ``wakeci check`` refuses until it is filled.

Grouping: a runner's prefix is its name without a number that follows a separator
(``ci5-3`` -> ``ci5-``). A name without one (``host1``) is its own prefix: stripping the
number would put ``host1`` and ``host10`` on one machine. A runner that an existing
machine's prefix matches stays with that machine. Every other prefix becomes a machine of
its own unless ``--machine NAME=PREFIX,...`` joins several. Prefixes that look like one
machine (the same number: ``ci5-`` and ``runner5-cpp-``) are only suggested for joining:
joining them wrongly would suspend a machine on which another machine's runner works.

With ``--update`` nothing in the file is changed or removed; machines are only added.
Only GET requests are made, and the token is never printed or written.
"""

import contextlib
import copy
import dataclasses
import difflib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Optional

from . import config_check, github, main

PLACEHOLDER = config_check.PLACEHOLDER
SSH_USER = "wakeci"  # the account machine/install.sh creates
SCHEMA_URL = "https://raw.githubusercontent.com/badutis/wakeci/main/docs/config.schema.json"
SEPARATED_NUMBER = re.compile(r"^(.*[-_.])[0-9]+$")
NUMBER = re.compile(r"[0-9]+")
OPENER = None  # urllib's urlopen; tests replace it


class UsageError(Exception):
    pass


@dataclass
class Group:
    """Runners that share a prefix, and the machine they go to."""

    prefix: str
    source: str
    machine: str = ""
    runners: int = 0
    online: int = 0
    labels: tuple = ()


@dataclass
class Report:
    groups: list = field(default_factory=list)
    added: list = field(default_factory=list)  # names of added machines
    to_fill: list = field(default_factory=list)  # where FILL-IN is left, e.g. "machines[4].mac"
    suggestions: list = field(default_factory=list)
    stale: list = field(default_factory=list)  # (machine, prefix) that no runner matches
    warnings: list = field(default_factory=list)


def prefix_of(name: str) -> str:
    m = SEPARATED_NUMBER.match(name)
    return m.group(1) if m else name


def _number(prefix: str) -> Optional[str]:
    m = NUMBER.search(prefix)
    return m.group() if m else None


def _owners(machines: list, runner_name: str) -> list:
    return [m for m in machines if any(runner_name.startswith(p) for p in m.get("runner_prefixes", []))]


def _new_name(prefix: str, taken: set) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "-", prefix).strip("-_.") or "machine"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def placeholders(value, where: str = "") -> list:
    """Every place that still holds FILL-IN, in file order."""
    if isinstance(value, str):
        return [where] if PLACEHOLDER in value else []
    if isinstance(value, dict):
        items = [(f"{where}.{k}" if where else str(k), v) for k, v in value.items()]
    elif isinstance(value, list):
        items = [(f"{where}[{i}]", v) for i, v in enumerate(value)]
    else:
        return []
    return [p for sub_where, sub in items for p in placeholders(sub, sub_where)]


def new_config(sources: list) -> dict:
    """A configuration with these (runners_path, repos) sources and no machines yet."""
    owners = [path.split("/")[1] for path, _ in sources]
    out = []
    for (path, repos), owner in zip(sources, owners):
        name = owner
        if owners.count(owner) > 1 and path.startswith("repos/"):
            name = f"{owner}-{path.split('/')[2]}"
        name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
        out.append({"runners_path": path, "repos": list(repos), "token_file": f"{config_check.ETC}/token-{name}"})
    return {
        "$schema": SCHEMA_URL,
        "config_version": config_check.CONFIG_VERSION,
        "dry_run": True,
        "ssh_key": f"{config_check.ETC}/id_ed25519",
        "known_hosts": f"{config_check.ETC}/known_hosts",
        "sources": out,
        "machines": [],
    }


def plan(cfg: dict, runners_by_source: dict, machine_args: dict) -> "tuple[dict, Report]":
    """The configuration with machines added for the runners no machine has yet. ``cfg``
    itself is not changed, and nothing already in it is changed in the result."""
    new = copy.deepcopy(cfg)
    machines = new.setdefault("machines", [])
    existing = list(machines)
    taken = {m.get("name") for m in existing}
    every = [r for runners in runners_by_source.values() for r in runners]
    for name, prefixes in machine_args.items():
        if name in taken:
            raise ValueError(f"machine {name} already exists: add the prefixes to its runner_prefixes by hand")
        if not config_check.MACHINE_NAME.fullmatch(name):
            raise ValueError(f"machine name {name!r}: letters, digits, '.', '_' and '-' only")
        if not prefixes:
            raise ValueError(f"--machine {name}: give at least one prefix")
        for p in prefixes:
            if not any(r["name"].startswith(p) for r in every):
                raise ValueError(f"--machine {name}: no runner starts with {p!r}")
            if any(p in m.get("runner_prefixes", []) for m in existing):
                raise ValueError(f"--machine {name}: {p!r} is already a prefix of an existing machine")
            if sum(p in ps for ps in machine_args.values()) > 1:
                raise ValueError(f"--machine: {p!r} is given for more than one machine")

    groups: dict = {}
    for source, runners in runners_by_source.items():
        for r in runners:
            name = r["name"]
            owners = _owners(existing, name)
            if owners:
                machine = owners[0]["name"]
                prefix = next(p for p in owners[0]["runner_prefixes"] if name.startswith(p))
            else:
                chosen = [(p, m) for m, ps in machine_args.items() for p in ps if name.startswith(p)]
                prefix, machine = max(chosen, key=lambda x: len(x[0])) if chosen else (prefix_of(name), "")
            g = groups.setdefault(prefix, Group(prefix=prefix, source=source, machine=machine))
            if source not in g.source.split(", "):
                g.source += f", {source}"
            g.runners += 1
            g.online += r.get("status") == "online"
            g.labels = tuple(sorted(set(g.labels) | {lab["name"] for lab in r.get("labels", [])}))

    report = Report()
    for m in existing:
        for p in m.get("runner_prefixes", []):
            if not any(r["name"].startswith(p) for r in every):
                report.stale.append((m["name"], p))

    def add(name: str, prefixes: list) -> None:
        machines.append(
            {
                "name": name,
                "mac": PLACEHOLDER,
                "host": PLACEHOLDER,
                "ssh_user": SSH_USER,
                "runner_prefixes": prefixes,
                "rank": 1,
            }
        )
        taken.add(name)
        report.added.append(name)

    for name, prefixes in machine_args.items():
        add(name, list(prefixes))
    alone = sorted((g for g in groups.values() if not g.machine), key=lambda g: g.prefix)
    for g in alone:
        g.machine = _new_name(g.prefix, taken)
        add(g.machine, [g.prefix])

    # Suggestions only: joining is the user's call (see the module docstring).
    by_number: dict = {}
    for g in alone:
        n = _number(g.prefix)
        if n is not None:
            by_number.setdefault(n, []).append(g)
    for n, same in by_number.items():
        known = [m["name"] for m in existing if n in {_number(p) for p in m.get("runner_prefixes", [])}]
        if known:
            for g in same:
                report.suggestions.append(
                    f"{g.prefix} may belong to the existing machine {known[0]}: add it to its runner_prefixes by hand"
                )
        elif len(same) > 1:
            report.suggestions.append(
                f"looks like one machine: --machine {same[0].machine}={','.join(g.prefix for g in same)}"
            )

    report.groups = sorted(groups.values(), key=lambda g: g.prefix)
    report.to_fill = placeholders(new)
    config_check.check_overlaps(main.machines_from(new), report.warnings)
    return new, report


# --- the command -----------------------------------------------------------------------


def _pairs(values: Optional[list], option: str, form: str) -> dict:
    out: dict = {}
    for v in values or []:
        key, sep, rest = v.partition("=")
        if not sep or not key or not rest:
            raise UsageError(f"{option} takes {form}, not {v!r}")
        if key in out:
            raise UsageError(f"{option} {key} is given twice")
        out[key] = rest
    return out


def _sources(ns) -> "tuple[dict, Optional[str], list, list]":
    """The configuration to start from, its text (with --update), its sources and the token
    files the command line gives for them (None where none is given)."""
    if ns.update:
        if ns.source or ns.repos or ns.token_file:
            raise UsageError(
                "--source, --repos and --token-file are not with --update: the sources come from the file; "
                "give their tokens with --token SOURCE=FILE"
            )
        tokens = _pairs(ns.token, "--token", "SOURCE=FILE")
        with open(ns.update, encoding="utf-8") as f:
            text = f.read()
        cfg = json.loads(text)
        if not isinstance(cfg, dict) or not isinstance(cfg.get("sources"), list):
            raise ValueError("it has no sources list")
        sources = [(s["runners_path"], s["repos"]) for s in cfg["sources"]]
        unknown = set(tokens) - {p for p, _ in sources}
        if unknown:
            raise UsageError(f"--token for a source the file does not have: {', '.join(sorted(unknown))}")
        return cfg, text, sources, [tokens.get(p) for p, _ in sources]
    if not ns.source:
        raise UsageError("give --source or --update")
    if ns.write:
        raise UsageError("--write needs --update: a new configuration goes to standard output")
    if ns.token:
        raise UsageError("--token is for --update; give --token-file after each --source")
    if len(ns.repos or []) != len(ns.source):
        raise UsageError("give one --repos for each --source")
    if ns.token_file and len(ns.token_file) != len(ns.source):
        raise UsageError("give one --token-file for each --source")
    sources = []
    for path, repos in zip(ns.source, ns.repos):
        if not config_check.RUNNERS_PATH.fullmatch(path):
            raise UsageError(f"--source {path!r}: repos/<owner>/<repo> or orgs/<org>")
        names = [r for r in repos.split(",") if r]
        if not names or not all(config_check.REPO.fullmatch(r) for r in names):
            raise UsageError(f"--repos {repos!r}: <owner>/<repo>[,<owner>/<repo>...]")
        sources.append((path, names))
    return new_config(sources), None, sources, list(ns.token_file or [None] * len(sources))


def _tokens(sources: list, files: list) -> list:
    env = os.environ.get("WAKECI_TOKEN", "").strip()
    out = []
    for (path, _), file in zip(sources, files):
        if file:
            try:
                out.append(github.read_token(os.path.expanduser(file)))
            except (OSError, ValueError) as e:
                # The message names the file, never its content.
                raise UsageError(f"token file for {path}: {getattr(e, 'strerror', None) or e}") from None
        elif env and len(sources) == 1:
            out.append(env)
        else:
            raise UsageError(f"no token for {path}: give --token {path}=FILE (or WAKECI_TOKEN for a single source)")
    return out


def _print_report(report: Report, changed: bool, out) -> None:
    for g in report.groups:
        labels = ", ".join(g.labels)
        print(
            f"  {g.prefix:<20} -> {g.machine:<16} {g.runners} runners, {g.online} online  [{labels}]  {g.source}",
            file=out,
        )
    if report.added:
        print(f"added machines: {', '.join(report.added)}", file=out)
    for machine, prefix in report.stale:
        print(f"no runners at GitHub for {prefix!r} of machine {machine} (kept)", file=out)
    for s in report.suggestions:
        print(f"suggestion: {s}", file=out)
    for w in report.warnings:
        print(f"warning: {w}", file=out)
    if report.to_fill:
        print(f"fill in: {', '.join(report.to_fill)}", file=out)
        print(f"not deployable yet: {len(report.to_fill)} fields to fill", file=out)
    elif not changed:
        print("nothing to add", file=out)
    else:
        print("complete: check it with wakeci check", file=out)


def _write(path: str, text: str) -> None:
    """Replace the file in one step and keep the old one as PATH.bak. A symbolic link is
    followed: the file it points to is replaced, and the link stays."""
    path = os.path.realpath(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".wakeci-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        st = os.stat(path)
        os.chmod(tmp, stat.S_IMODE(st.st_mode))
        # Keeps the group the service reads it through, e.g. root:wakeci 0640.
        with contextlib.suppress(PermissionError):
            os.chown(tmp, st.st_uid, st.st_gid)
        shutil.copy2(path, path + ".bak")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run(ns) -> int:
    err = sys.stderr
    try:
        machine_args = {
            k: [p for p in v.split(",") if p]
            for k, v in _pairs(ns.machine, "--machine", "NAME=PREFIX[,PREFIX...]").items()
        }
        cfg, text, sources, files = _sources(ns)
        tokens = _tokens(sources, files)
    except UsageError as e:
        print(f"wakeci discover: {e}", file=err)
        return 2
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"wakeci discover: {ns.update} cannot be read: {e}", file=err)
        return 1

    gh = github.GitHub([], opener=OPENER)
    found = {}
    for (path, repos), token in zip(sources, tokens):
        try:
            found[path] = gh.runners(github.Source(path, tuple(repos), token))
        except Exception as e:  # noqa: BLE001 — any failure is reported without its details
            reason = github.first_error(e) or type(e).__name__
            hint = ""
            if " 401 " in f" {reason} ":
                hint = ": the token is not valid or has expired"
            elif " 403 " in f" {reason} " or " 404 " in f" {reason} ":
                hint = ": the token cannot read this source's self-hosted runners, or its rate limit is used up"
            print(f"wakeci discover: {path}: {reason}{hint}", file=err)
            return 1

    try:
        new, report = plan(cfg, found, machine_args)
    except ValueError as e:
        print(f"wakeci discover: {e}", file=err)
        return 2
    except (KeyError, TypeError, AttributeError):
        print(f"wakeci discover: the machines in {ns.update} are malformed: run wakeci check on it", file=err)
        return 1
    changed = bool(report.added)
    out_text = json.dumps(new, indent=2, ensure_ascii=False) + "\n"
    _print_report(report, changed, err)

    if ns.write and changed:
        _write(ns.update, out_text)
        print(f"wrote {ns.update} (the old one is {ns.update}.bak)", file=err)
    if ns.json:
        print(json.dumps({"config": new, "report": dataclasses.asdict(report)}, indent=2, ensure_ascii=False))
    elif not ns.write:
        sys.stdout.write(out_text)
        if text is not None and changed:
            old = [line.rstrip("\r\n") + "\n" for line in text.splitlines()]
            err.writelines(difflib.unified_diff(old, out_text.splitlines(True), ns.update, "new"))
    return 0

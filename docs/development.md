# Development

How to contribute (sign-off, what needs tests, how a change is reviewed) is in
[CONTRIBUTING.md](https://github.com/badutis/wakeci/blob/main/CONTRIBUTING.md); security
reports go through [SECURITY.md](https://github.com/badutis/wakeci/blob/main/SECURITY.md).

## Layout

| Path | Runs on | What it does |
|---|---|---|
| `wakeci/decide.py` | controller | when to wake and when to suspend; pure logic, no I/O |
| `wakeci/github.py` | controller | reads runners and queued jobs (GET only, ETag caching) |
| `wakeci/actions.py` | controller | Wake-on-LAN packet, suspend over SSH |
| `wakeci/main.py` | controller | the loop, `/metrics`, the state file |
| `wakeci/notify.py` | controller | webhook notifications (Discord, Slack, JSON) |
| `wakeci/cli.py` | both | the `wakeci` command |
| `wakeci/config_check.py` | both | the configuration rules `wakeci check` and the installer apply |
| `wakeci/discover.py` | your computer | `wakeci discover`: a configuration from the runners GitHub knows |
| `wakeci/doctor.py` | controller | `wakeci doctor`: every check that can run before the controller does |
| `controller/install.sh` | controller | installs or updates the controller on its host |
| `controller/deploy.sh` | your computer | packs one version with your configuration and installs it on the host |
| `controller/check_config.py` | both | checks a configuration before it is installed |
| `controller/doctor.py` | controller | runs `wakeci doctor` where the package is not on the path |
| `controller/` (other files) | controller | example configuration and the `systemd` unit |
| `machine/wakeci-sleep` | runner machine | suspends the machine unless a job is running; `check` only reports |
| `machine/install.sh` | runner machine | installs `wakeci-sleep`, the `sudoers` rule, the restricted key and Wake-on-LAN |
| `docs/`, `zensical.toml` | — | this site and `config.schema.json`; `zensical.toml` builds the site |
| `scripts/public_safe.py` | CI, pre-push | refuses private addresses, MAC addresses, personal emails and AI attribution |
| `scripts/pre-push`, `scripts/install-hooks.sh` | your computer | runs that check before every push |
| `scripts/config_reference.py` | you, CI | writes the key tables of [Configuration](configuration.md) from the schema |

## Tests and checks

```bash
python3 -m unittest discover -s tests -t .
shellcheck machine/install.sh machine/wakeci-sleep controller/install.sh controller/deploy.sh tests/install_smoke.sh tests/machine_smoke.sh scripts/pre-push scripts/install-hooks.sh
python3 scripts/public_safe.py --history
```

Lint, formatting, types and coverage use pinned tools (installed by hash, never a newer
release by surprise). They need Python 3.11 or newer (CI uses 3.13), in a virtual
environment:

```bash
python3 -m venv .venv && . .venv/bin/activate
python3 -m pip install --require-hashes --no-deps -r requirements-dev.txt
ruff check . && ruff format --check . && mypy
coverage run -m unittest discover -s tests -t . && coverage report
```

CI runs these on GitHub-hosted runners, and also:

- `tests/install_smoke.sh` on x86_64 and on arm64: installs the controller from scratch on
  a clean Ubuntu with systemd and starts it with fake tokens;
- `tests/machine_smoke.sh`: installs the machine side and talks to it through a real sshd
  with the restricted key. It checks the modes, the `sudoers` rule, that client commands
  never run, that a second install changes nothing, and how `wakeci doctor --probe` reads
  real ssh answers. It only ever asks for a check, never a suspend.

Both change the system, so never run them on a real host. CI never runs on the
self-hosted runners, because those are the machines this controller suspends.

**Nothing installation-specific goes into the repository.** That means no real IP or MAC
addresses, no machine, user or repository names, no tokens and no personal email
addresses. Examples use the documentation ranges (`192.0.2.0/24`, `00:00:5e:00:53:xx`)
and `example-org`. Your real configuration lives in your own infrastructure repository.

`scripts/public_safe.py` catches the leaks that have a recognisable shape: private
addresses, MAC addresses, personal emails and AI attribution. It checks the whole history
of the branch and of every tag, and every tag object, not only the latest commit. It
cannot recognise public addresses or the names of real machines, users and repositories.
Keeping those out is up to the author.

The guard scans its own source too, so nothing in it may look like a finding: networks are
written as numbers and tool names are built from pieces. A test checks this. Earlier
versions of the file, which spelled the patterns out, are skipped by their exact object id.

CI runs that check only after a push, when the commit is already public. Install the
pre-push hook once per clone, so that a push holding a finding is refused on your own
computer instead:

```bash
scripts/install-hooks.sh
```

For every ref you push (any branch or tag), it checks the whole history of that commit and
every tag object on the way to it. Ref deletions pass. A local tag you do not push is not
checked. On a branch whose guard is too old for the hook, the push is refused with a message
saying so. The installer refuses when `core.hooksPath` is set: that directory is shared, so
put the hook there yourself if you want it. Run the installer again after `scripts/pre-push`
changes.

## This site

```bash
python3 -m pip install --require-hashes --no-deps -r requirements-docs.txt
zensical serve       # http://localhost:8000, rebuilt on every change
zensical build --strict
```

CI builds it with `--strict` on every pull request (a broken link fails the build). It is
published to GitHub Pages from each release tag, so it describes the version people
install. After changing `docs/config.schema.json`, run `python3 scripts/config_reference.py`;
a test fails while the tables are out of date.

## Releases

A release is an annotated tag `vX.Y.Z` on `main`, matching `__version__` in
`wakeci/__init__.py` and a `## [X.Y.Z]` section in `CHANGELOG.md`. Pushing the tag runs
`.github/workflows/release.yml`, which:

1. checks the version, the tag and the changelog (`scripts/release.py check`);
2. runs the guard over the whole history, and the tests;
3. builds the wheel and the sdist with build tools pinned by hash
   (`requirements-release.txt`, no build isolation);
4. installs the wheel and runs it;
5. writes `SHA256SUMS`, attests the build provenance of each artifact, and publishes the
   GitHub release with the changelog section as notes.

Verify a downloaded artifact:

```bash
sha256sum -c --ignore-missing SHA256SUMS
gh attestation verify wakeci-X.Y.Z-py3-none-any.whl --repo badutis/wakeci \
  --signer-workflow badutis/wakeci/.github/workflows/release.yml --source-ref refs/tags/vX.Y.Z
```

Running the workflow by hand on a branch rehearses steps 1-4 without publishing anything.

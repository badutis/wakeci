# Contributing to WakeCI

Thank you for helping. WakeCI controls real machines, so changes are held to a careful
standard: every change comes with tests, and nothing about any one installation goes into
the repository.

## Ground rules

- **No installation data, ever:** no real IP or MAC addresses, machine, user or repository
  names, tokens or personal email addresses. Examples use the documentation ranges
  (`192.0.2.0/24`, `00:00:5e:00:53:xx`) and `example-org`. `scripts/public_safe.py` enforces
  the parts of this it can recognise, over the whole history, in CI and in the pre-push hook.
- **Sign off every commit** (`git commit -s`). It certifies the
  [Developer Certificate of Origin](https://developercertificate.org/): you wrote the change
  or have the right to submit it under the project's MIT license. CI refuses a pull request
  with an unsigned commit (`scripts/dco.py`).
- **Behaviour changes need tests.** The decision logic (`wakeci/decide.py`) is pure and has
  no I/O; test it with `tests/test_decide.py`. Network code is tested against fakes and
  local servers, never the real GitHub.
- **`/metrics` names are a contract.** Renaming one breaks the monitoring of every user;
  `tests/test_io.py` pins them. A new metric is fine, a renamed one is a breaking change.
- **A new `wakeci doctor` check** gets an id in `doctor.IDS` and a section of that name in
  `docs/troubleshooting.md` (a test keeps them in step). By default a check changes nothing
  and sends nothing to a machine; anything that does goes behind its own flag.
- **The configuration format is versioned** (`config_version`). An incompatible change raises
  it, updates `docs/config.schema.json` and `wakeci/config_check.py` together (a test keeps
  them in step), and says how to migrate in the changelog.

## Setting up

```bash
git clone https://github.com/badutis/wakeci && cd wakeci
scripts/install-hooks.sh            # the pre-push guard
python3 -m venv .venv && . .venv/bin/activate   # Python 3.11+ for the dev tools
python3 -m pip install --require-hashes --no-deps -r requirements-dev.txt
```

WakeCI itself needs only the Python 3.9+ standard library.

## Before you open a pull request

```bash
python3 -m unittest discover -s tests -t .
ruff check . && ruff format --check . && mypy
coverage run -m unittest discover -s tests -t . && coverage report
shellcheck machine/install.sh machine/wakeci-sleep controller/install.sh controller/deploy.sh \
  tests/install_smoke.sh tests/machine_smoke.sh scripts/pre-push scripts/install-hooks.sh
python3 scripts/public_safe.py --history
```

CI runs all of these, plus the install smoke test on x86_64 and arm64 (it changes the
system, so it only runs there). Never run `tests/install_smoke.sh` or `tests/machine_smoke.sh` on a real host.

Add a line under `## [Unreleased]` in `CHANGELOG.md` for anything a user would notice.

## Pull requests

- One topic per pull request; keep refactoring apart from behaviour changes.
- Commit messages: `type(scope): what changed` (`feat`, `fix`, `docs`, `ci`, `build`,
  `style`, `test`, `chore`), with the reason in the body.
- `main` is protected: changes land only through pull requests with every check green.

## Releases

Maintainers release by tagging. The steps, and how the release workflow checks and builds,
are in the README ("Releases"):

1. In one pull request, set `__version__` in `wakeci/__init__.py` to `X.Y.Z` and move the
   `## [Unreleased]` entries under `## [X.Y.Z] - date`.
2. After it is merged, rehearse: run the Release workflow by hand on `main`.
3. Push an annotated tag: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.

Versions follow [Semantic Versioning](https://semver.org/); before 1.0 an incompatible
change raises the minor number.

## Reporting security issues

Not in a public issue: see [SECURITY.md](SECURITY.md).

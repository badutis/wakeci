# Changelog

All notable changes to WakeCI. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/): before 1.0, an incompatible
change raises the minor number.

## [Unreleased]

### Added
- `wakeci discover`: builds a configuration from the runners GitHub knows, or adds machines
  for new runners to an existing one (`--update`, `--write`) without changing anything
  already there. Machines it cannot know the MAC or address of get `FILL-IN`; prefixes
  that look like one machine are suggested for joining, never joined.

### Changed
- `wakeci check` refuses a value that still holds `FILL-IN`, two machines with the same MAC
  or host, and the same runner prefix on two machines. A prefix that starts another
  machine's prefix (`host1` and `host10-`) is a warning: such a runner belongs to both.

## [0.4.0] - 2026-10-22

The first release under the WakeCI name.

### Changed
- **Renamed from rackctl to WakeCI.** The package, command, paths (`/etc/wakeci`,
  `/opt/wakeci`, `/var/lib/wakeci`), unit, system user, machine-side script
  (`wakeci-sleep`) and metrics (`wakeci_*`) all changed. See "Upgrading from rackctl" in
  the README.
- The configuration check refuses unknown keys, control characters in names and paths, and
  machine names outside `[A-Za-z0-9._-]`.

### Added
- The `wakeci` command (`run`, `check`, `version`) and a Python package
  (`pyproject.toml`, no dependencies).
- `config_version` in the configuration, and `docs/config.schema.json` for editors.
- Release workflow: checked tag, build tools pinned by hash, `SHA256SUMS`, build provenance
  attestation.
- CI: lint (ruff), types (mypy), coverage (at least 80%).
- Project files: SECURITY.md (private vulnerability reporting), CONTRIBUTING.md, the
  Contributor Covenant code of conduct, issue and pull request templates.
- Every pull request commit must be signed off (DCO), checked in CI.
- Dependabot keeps the pinned CI actions and dev tools current (version updates wait until
  a release is 14 days old; security updates come at once); CodeQL and secret scanning are
  on.

## [0.3.0] - 2026-10-22

### Added
- Webhook notifications (Discord, Slack, JSON): an unreadable GitHub source, the request
  limit, a machine that did not wake up or failed to suspend, the controller starting.
  Problems are reported after a wait, repeated while they last and followed by a "resolved"
  message; failed posts are retried.

## [0.2.0] - 2026-10-22

### Changed
- Runs on any Linux host with systemd (`orangepi/` became `controller/`).
- The journal is moved to RAM only with `--ram-journal`.
- Log messages and code comments in English.

### Added
- CI installs and starts the controller on arm64 as well as x86_64.
- A pre-push hook that runs the installation-data guard before anything is pushed.

## [0.1.0] - 2026-10-22

### Added
- The controller: wakes machines with Wake-on-LAN for queued GitHub Actions jobs and
  suspends idle ones over a restricted SSH command; `/metrics`; `dry_run` by default.
- Installation on the controller host (`install.sh`) and from your own computer
  (`deploy.sh`), and the machine side (`machine/install.sh`).

[Unreleased]: https://github.com/badutis/wakeci/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/badutis/wakeci/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/badutis/wakeci/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/badutis/wakeci/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/badutis/wakeci/releases/tag/v0.1.0

# WakeCI

**Self-hosted GitHub Actions runners that sleep when there is no work.**

Your own CI machines run all day while most of the day nothing is queued. WakeCI watches
the job queue, wakes the machines a queued job needs, and suspends a machine once its runners
have been idle for a while. The first job after a quiet period waits while its machine
resumes (typically tens of seconds); the machines no longer run idle around the clock.

```mermaid
flowchart LR
    GH[GitHub Actions<br/>job queue] -- "polled, read-only token" --> W[WakeCI<br/>small always-on host]
    W -- "Wake-on-LAN" --> M[Runner machines]
    W -- "SSH: one command,<br/>suspend if idle" --> M
    M -- "runners pick up jobs" --> GH
```

- **No Kubernetes, no cloud.** For runners on your own hardware: a lab, an office, a
  homelab. It runs on any small Linux host with systemd, such as an Orange Pi or Raspberry
  Pi (512 MB RAM is plenty), or any x86 machine, on the same network segment as the runners.
- **Safe by design.** Read-only GitHub tokens. The SSH key can run one command, which
  suspends a machine only if no job is running on it. It needs no inbound connection, no
  incoming GitHub webhook, and no token that can change anything.
- **Nothing to install but Python.** Standard library only (3.9+), no Docker, no database.
  The only state on disk is a small file, written a few times a day.
- **Observable.** Prometheus `/metrics`, and webhook notifications to Discord, Slack or any
  JSON endpoint when something needs a person.
- **Careful by default.** It starts in `dry_run` and only logs what it would do until you
  turn that off; `wakeci doctor` checks the whole setup before that.

| | Supported |
|---|---|
| Controller host | Linux with systemd; x86_64 and arm64 tested in CI, 32-bit ARM on real boards |
| Python | 3.9 to 3.13 on the controller (3.11+ for the dev tools) |
| Runner machines | Ubuntu or Debian with systemd, Wake-on-LAN and suspend-to-RAM |
| GitHub | github.com, repository and organisation runners |

## Where to go next

- [Getting started](getting-started.md): from runners on GitHub to a controller in
  `dry_run`, step by step.
- [How it works](design.md): how decisions are made, the security model and the known
  limits.
- [Troubleshooting](troubleshooting.md): what each `wakeci doctor` finding means.

WakeCI is open source under the MIT license, with no paid edition and no limits. The code,
issues and releases are on [GitHub](https://github.com/badutis/wakeci).

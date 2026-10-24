# Commands

```text
wakeci run CONFIG       run the controller (what the systemd unit starts)
wakeci check CONFIG     check a configuration; exit 0 when it is valid
wakeci discover ...     build or extend a configuration from the runners GitHub knows
wakeci doctor CONFIG    check everything that can be checked before the controller runs
wakeci version          print the version
```

`wakeci COMMAND --help` lists each command's options.

| Command | Where | Page |
|---|---|---|
| `run` | the controller host, as its systemd unit | [Install](install.md) |
| `check` | anywhere; `deploy.sh` and `install.sh` run it too | [Configuration](configuration.md) |
| `discover` | your computer, with read-only tokens | [Generating a configuration](discover.md) |
| `doctor` | the controller host, as the service user | [Troubleshooting](troubleshooting.md) |

Installed with `pip install` or `pipx install` (the release's wheel, or a checkout) the
command is on your path, for checking and generating configurations on your own computer;
from a checkout, `python3 -m wakeci` works without installing.

The package holds only the Python code: the controller host is set up with
`controller/deploy.sh` and `controller/install.sh` ([Install](install.md)). There the
package is not on the path, so its unit runs `python3 -m wakeci run
/etc/wakeci/config.json` from `/opt/wakeci`, and `doctor` runs as
`python3 /opt/wakeci/controller/doctor.py`.

Exit codes: `0` success, `1` a problem was found (an invalid configuration, a failed
check, a GitHub error), `2` a usage error.

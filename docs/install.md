# Install

## Requirements

**The controller host:**

- Linux with **systemd**, **Python 3.9+** with its standard library, and the OpenSSH
  client. On Debian, Ubuntu, Raspberry Pi OS and Armbian the installer adds missing
  packages with `apt`. Elsewhere (Fedora, Arch, …) install them first. Non-systemd systems
  such as Alpine are not supported.
- Any CPU architecture: the controller is pure Python and shell, with nothing to compile.
  CI installs and starts it on x86_64 and arm64 (Ubuntu 24.04). 32-bit ARM (for example an
  Orange Pi Zero) has no GitHub-hosted runner, so it is tested on real boards only.
- Little memory and CPU: one Python process polling every 20 s.

**The runner machines:**

- Ubuntu or Debian with systemd (`machine/install.sh` uses `apt` for `ethtool`).
- Wake-on-LAN and working suspend-to-RAM, enabled in the BIOS. Check this by hand on one
  machine first: suspend it, wake it with a packet from another machine, and confirm its
  runners come back online in GitHub.
- The controller on the **same layer-2 segment** (VLAN) as the machines. Wake-on-LAN
  broadcasts do not cross routers.
- Runners registered with a name prefix per machine (for example `host1-a-1`,
  `host1-a-2`), so that the controller can tell which machine a runner belongs to.

## The runner machines

On each machine, as root, from a checkout of the release you install:

```bash
sudo bash machine/install.sh --user wakeci --ci-user runner --pubkey /tmp/wakeci.pub
```

- `--user` is the account the controller logs in as. It gets the controller's public key,
  restricted to the single command
  `sudo -n /usr/local/sbin/wakeci-sleep ${SSH_ORIGINAL_COMMAND:+check}`. A command the
  client sends is not run: it only adds the word `check`, and `wakeci-sleep check`
  changes nothing and reports what would happen (`wakeci doctor --probe` uses it). The
  account's login shell must be `sh`, `dash` or `bash`.
- `--ci-user` is the account that runs CI jobs. The script refuses to give it the key.
- `--iface` (default `eno1`) is the interface Wake-on-LAN is enabled on.

Running the script again changes nothing. **After upgrading WakeCI, run it again on every
machine**: a machine set up with an older version ignores the client's command and would
suspend when asked for a check.

## The controller host

Start from a minimal system: on an ARM board, its minimal Debian-based image (Armbian
*minimal*, Raspberry Pi OS *Lite*), with no desktop and no Docker; on an x86 machine, a
server install of Debian or Ubuntu. SSH into it as a user with `sudo`. Then, from a
checkout of WakeCI on your own computer:

```bash
controller/deploy.sh <ssh-target> vX.Y.Z /path/to/your/config.json
```

`vX.Y.Z` is the release tag to install (see the
[releases](https://github.com/badutis/wakeci/releases)). On a board that runs from an SD
card, add `--ram-journal` (before `<ssh-target>`). It keeps the systemd journal of the
whole host in RAM, so all logs are lost on reboot. Leave it out on a machine with a real
disk.

`deploy.sh` packs that tag's code (`git archive`, not your working tree) and your
`config.json`, checks the configuration with that version's checker (the SSH key,
`known_hosts` and the token files must be directly in `/etc/wakeci/`), copies the bundle
with `scp` and runs `controller/install.sh` on the host with `sudo`. The host needs no
access to GitHub for this.

`install.sh` runs as root on the host. It:

1. checks that the host runs systemd; prints its model, architecture and memory; installs
   `python3` and `openssh-client` with `apt` if they are missing (elsewhere it stops and
   says what to install);
2. creates the system user `wakeci` (no home, no login shell);
3. puts the code at `/opt/wakeci/wakeci/` (owned by root, precompiled) and a copy of
   the installer at `/opt/wakeci/controller/`;
4. installs the configuration as `/etc/wakeci/config.json` (`0640`, group `wakeci`);
5. creates the SSH key for the machines **on the host**, if there is none yet
   (`/etc/wakeci/id_ed25519`, `0600`, owner `wakeci`), and prints its public key for
   `machine/install.sh --pubkey`;
6. scans the host key of every machine that is not in `known_hosts` yet, and prints its
   fingerprint. Compare it with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the
   machine. A sleeping machine cannot be scanned: run the installer again once it is
   awake. Until then the controller cannot suspend it;
7. with `--ram-journal` only, keeps the systemd journal in RAM (`Storage=volatile`);
8. installs and enables `wakeci.service`, and starts it only if every token file the
   configuration names exists and is readable by `wakeci`;
9. shows what `wakeci doctor --offline` finds.

A run without `--set-token` never replaces an existing SSH key, host key or token. Run it
again at any time:

```bash
sudo bash /opt/wakeci/controller/install.sh
```

## Tokens on the host

Tokens are typed on the host, never put in the bundle:

```bash
ssh -t <ssh-target> sudo bash /opt/wakeci/controller/install.sh --set-token token-example-org
```

This reads the token without echoing it and writes `/etc/wakeci/<name>` (`0600`, owner
`wakeci`). The name must match the `token_file` in your configuration. The webhook URL
for [notifications](monitoring.md#notifications) is set the same way:
`--set-token notify-url`. When every file is in place, run the installer again to start
the service.

## Check it

```bash
ssh -t <ssh-target> sudo -u wakeci python3 /opt/wakeci/controller/doctor.py /etc/wakeci/config.json
```

`wakeci doctor` checks the configuration, the files and their modes, the clock against
GitHub's, every token (valid, when it expires, whether it can write, whether it can read
the runners and jobs), the SSH key and host keys, the Wake-on-LAN address and the webhook.
Each warning or failure links to its section in [Troubleshooting](troubleshooting.md). By
default it changes nothing and sends nothing to a machine; `--wake NAME` sends a real
Wake-on-LAN packet and waits for the machine's runners, `--notify-test` posts a test
message, and `--probe` asks every awake machine `wakeci-sleep check`.

## Watch it

`journalctl -u wakeci -f` and `curl http://<host>:9105/metrics` ([metrics](monitoring.md#metrics)).
While `"dry_run": true`, it only logs what it would do. When the log looks right, set
`"dry_run": false` in your configuration and deploy again.

## Upgrading

**From v0.4.0.** Deploy the new version, then run `machine/install.sh` again on every
machine: the forced SSH command and the `sudoers` rule changed. Then run `wakeci doctor`;
use `--probe` only once every machine has been updated.

**From `rackctl` (v0.1.0-v0.3.0).** The project was renamed in v0.4.0, and every name
changed with it. The new install does not touch the old one, so on the controller host:

1. Stop the old unit: `systemctl disable --now rackctl`.
2. Install WakeCI as above. It creates a new SSH key and asks for the tokens again
   (`--set-token`), and the configuration paths become `/etc/wakeci/…`.
3. On every runner machine, run the new `machine/install.sh` with the new public key. Then
   remove `/usr/local/sbin/rack-sleep`, `/etc/sudoers.d/rack-sleep` and the old key line in
   `authorized_keys`.
4. Remove `/etc/rackctl`, `/opt/rackctl`, `/etc/systemd/system/rackctl.service` and the
   `rackctl` user.
5. Monitoring: the metrics are now `wakeci_*`.

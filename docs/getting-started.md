# Getting started

From runners registered at GitHub to a controller that runs in `dry_run`. About 15 minutes
of typing, plus the time to try Wake-on-LAN on one machine. Each step links to the page
with the details.

You need:

- runner machines with Ubuntu or Debian, whose runners are registered with a name prefix
  per machine (`host1-a-1`, `host1-a-2`, …);
- a small always-on Linux host with systemd on the **same network segment** (VLAN), for the
  controller;
- a checkout of [WakeCI](https://github.com/badutis/wakeci) on your own computer, at the
  [latest release](https://github.com/badutis/wakeci/releases/latest) tag (`vX.Y.Z` below);
  the `wakeci` command is `python3 -m wakeci` from that checkout, or `pipx install` the
  release's wheel.

## 1. Try Wake-on-LAN on one machine

Enable Wake-on-LAN and suspend-to-RAM in the machine's BIOS. Suspend it
(`sudo systemctl suspend`), wake it from another machine on the same segment (for example
`wakeonlan <mac>`), and check that its runners come back online at GitHub. If this does not
work, nothing else will; see [wake](troubleshooting.md#wake).

## 2. Create read-only tokens

One fine-grained token per source (a repository or an organisation), with read access only:
see [Tokens](configuration.md#tokens). Keep a copy on your computer for step 3.

## 3. Generate the configuration

```bash
wakeci discover \
  --source orgs/example-org --repos example-org/app --token-file ~/.config/wakeci/token-a \
  > config.json
```

It groups your runners into machines and writes `FILL-IN` for each machine's MAC and
address, which GitHub does not know: fill them in. Prefixes that look like one machine are
suggested for joining. `wakeci check config.json` says when the file is complete. Details:
[Generating a configuration](discover.md) and [Configuration](configuration.md).

## 4. Install the controller host

```bash
controller/deploy.sh <ssh-target> vX.Y.Z config.json
```

The installer creates the controller's SSH key on the host and prints its public key: copy
it to `/tmp/wakeci.pub` on each machine. The service does not start yet: the tokens are
missing. Details: [Install](install.md#the-controller-host).

## 5. Install the machine side

On each runner machine, from a checkout of the same release:

```bash
sudo bash machine/install.sh --user wakeci --ci-user runner --pubkey /tmp/wakeci.pub
```

Details: [Install](install.md#the-runner-machines).

## 6. Give the controller its tokens

```bash
ssh -t <ssh-target> sudo bash /opt/wakeci/controller/install.sh --set-token token-example-org
ssh -t <ssh-target> sudo bash /opt/wakeci/controller/install.sh
```

One `--set-token` per `token_file` in the configuration (and `notify-url` for
[notifications](monitoring.md#notifications)). The last run scans the host keys of the
machines that are awake and starts the service.

## 7. Check everything

```bash
ssh -t <ssh-target> sudo -u wakeci python3 /opt/wakeci/controller/doctor.py /etc/wakeci/config.json --probe
ssh -t <ssh-target> sudo -u wakeci python3 /opt/wakeci/controller/doctor.py /etc/wakeci/config.json --wake host1
```

The first checks files, clock, tokens, host keys, the network and every awake machine; the
second wakes one machine and waits for its runners. Each finding links to
[Troubleshooting](troubleshooting.md).

## 8. A week in `dry_run`

The configuration starts with `"dry_run": true`: the controller logs what it would do and
sends nothing. Watch it with `journalctl -u wakeci -f` and its [metrics](monitoring.md).

## 9. Turn it on

Set `"dry_run": false`, deploy again, and watch the first suspends and wakes.

# Troubleshooting

`wakeci doctor` checks everything that can be checked before the controller runs. Every
warning or failure it prints links to its section below. Run it on the controller host as
the service user, so files are read with the service's rights:

```bash
sudo -u wakeci python3 /opt/wakeci/controller/doctor.py /etc/wakeci/config.json
```

By default it changes nothing and sends nothing to a machine. Three flags do more:

- `--probe` asks every awake machine `wakeci-sleep check` over SSH. **A machine set up
  with a WakeCI older than 0.5.0 suspends instead**: run `machine/install.sh` again on
  every machine first.
- `--wake NAME` sends a Wake-on-LAN packet to NAME and waits up to 3 minutes for one of its
  runners to come online.
- `--notify-test` posts one test message to the webhook.

`--offline` skips everything that needs the network; `--json` prints the results as JSON.
The exit code is 1 when a check failed.

## config

The configuration does not pass `wakeci check`. The message says which rule; the
[configuration reference](../README.md#configuration) lists every key. A value that still
holds `FILL-IN` comes from `wakeci discover`: fill in the machine's MAC and address.
Warnings here (for example overlapping runner prefixes) do not stop the controller but are
usually a mistake.

## files

The SSH key, `known_hosts`, a token file or the webhook URL file is missing, unreadable by
the service user, a symbolic link, or a secret that others can read.

- A missing token or webhook URL: `sudo bash /opt/wakeci/controller/install.sh --set-token NAME`.
- An unreadable token or webhook URL file: write it again with `--set-token NAME`, which
  gives it the right owner and mode (`0600`, owned by `wakeci`).
- An unreadable SSH key or `known_hosts`: run `sudo bash /opt/wakeci/controller/install.sh`
  again; it sets their owners and modes.
- A secret readable by others: `sudo chmod 0600 FILE`.

## state

The directory of `state_file` (by default `/var/lib/wakeci`) cannot be written by the
service user. systemd creates it when the service starts (`StateDirectory=wakeci`), so a
missing directory before the first start is only a warning. If it exists but is not
writable, fix its owner: `sudo chown wakeci:wakeci /var/lib/wakeci`.

## metrics

The `metrics_listen` port cannot be opened. "In use" is expected while the `wakeci`
service runs. Otherwise another program holds the port, or the address is not one of this
host's: change `metrics_listen`.

## ssh-key

The private key in `ssh_key` cannot be used without a passphrase. The controller runs
without a person, so the key must have none. `controller/install.sh` creates a suitable key
when there is none; to replace a bad one, remove it and run the installer again (then give
the new public key to every machine with `machine/install.sh --pubkey`).

## host-key

`known_hosts` has no host key for these machines, so the controller refuses to connect
(`StrictHostKeyChecking=yes`) and cannot suspend them. The installer scans the machines
that are awake when it runs. Wake them (`wakeci doctor --wake NAME`) and run
`sudo bash /opt/wakeci/controller/install.sh` again.

## wol

Wake-on-LAN packets go to `broadcast`.

- **Not the broadcast address of any interface:** set it to the broadcast address of the
  segment the machines are on (for `192.0.2.0/24` that is `192.0.2.255`).
- **`255.255.255.255` with several interfaces:** the packet leaves through one interface
  only; set the segment's own broadcast address instead.
- **Cannot send:** no route to that address from this host.

Packets do not cross routers; the controller must be on the machines' segment (or VLAN).

## notify

The webhook URL file cannot be read, does not hold an `https` URL (plain `http` only to
`localhost`), or its host does not match `notify.format` (`discord.com` for `discord`,
`hooks.slack.com` for `slack`). Write the URL again with
`sudo bash /opt/wakeci/controller/install.sh --set-token notify-url`.

## clock

This host's clock is more than 2 minutes off GitHub's. Idle times and stuck-job times are
measured with it. Boards without a battery-backed clock (Orange Pi, Raspberry Pi) start
with a wrong time until they sync: enable time sync (`timedatectl set-ntp true`) and check
`timedatectl status`.

## token

GitHub refuses the source's token (`401`: not valid or expired), answers with another
error, or cannot be reached (DNS, no route, proxy). Create a new token
([Tokens](../README.md#tokens)) and store it with
`sudo bash /opt/wakeci/controller/install.sh --set-token NAME`.

## token-expiry

The token expires within 14 days, or has expired. Create a new one before then; once it
has expired, the controller cannot read that source and suspends nothing.

## token-scope

A classic personal access token with scopes that can write (`repo`, `admin:org`,
`workflow`, ...). WakeCI only reads; a stolen classic token with these scopes can change
your repositories. Use a fine-grained token with read access only
([Tokens](../README.md#tokens)).

## token-perms

The token is valid but cannot read the source's runners (needs *Administration: read* for a
repository source, *Self-hosted runners: read* for an organisation) or the queued jobs of a
repository (*Actions: read*), or the repository name is wrong (`404`).

## runners

The runners GitHub lists do not match the machines in the configuration:

- **No runner for a machine:** its `runner_prefixes` match no runner name, or its runners
  were removed (GitHub removes a runner that has not connected for 14 days).
- **Runners on no machine:** they always count as free and are never woken or suspended.
  Fine for an always-on runner; otherwise add them (`wakeci discover --update`).
- **Runners on more than one machine:** one prefix starts another; make them distinct.

## sleep-probe

The answer of `wakeci-sleep check` over SSH (`--probe`):

- **an older `wakeci-sleep` took the check for a suspend:** the machine was set up with a
  WakeCI older than 0.5.0 and is now suspending (or rebooting). Run `machine/install.sh`
  again on it.
- **sudo asks for a password:** the `sudoers` rule is missing; run `machine/install.sh`.
- **the key is refused:** the controller's public key is not in the machine user's
  `authorized_keys`; run `machine/install.sh --pubkey` with the key the installer printed.
- **the host key is not in known_hosts:** see [host-key](#host-key).
- **the SSH key cannot be read:** see [files](#files).
- **SSH did not finish in time:** the machine accepted the connection but did not answer;
  log in to it and check `sudo /usr/local/sbin/wakeci-sleep check`.
- **the host key does not match:** the machine was reinstalled, or it is not the machine you
  think. Check, then remove the old line (`ssh-keygen -R HOST -f /etc/wakeci/known_hosts`)
  and run the controller installer again.
- **no SSH connection** (skipped): the machine is asleep or unreachable.

## notify-test

The test message (`--notify-test`) was not accepted. `404`: the webhook was deleted;
`401`/`403`: the URL is wrong; `429`: rate limited, try later. A redirect is refused on
purpose (it would turn the post into a get).

## wake

No runner of the machine came online within 3 minutes of the packet (`--wake`). In order:

1. Wake-on-LAN is enabled in the machine's BIOS/UEFI (often "Wake on LAN" or "Power on by
   PCIe"), and the machine is suspended or off, not unplugged.
2. It is on a cable: Wi-Fi does not wake.
3. Its `mac` is the cable interface's MAC.
4. `broadcast` is its segment's broadcast address (see [wol](#wol)).
5. Its runner services start on boot and resume.

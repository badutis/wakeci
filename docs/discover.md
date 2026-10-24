# Generating a configuration

`wakeci discover` reads the runners of your sources from GitHub and groups them into
machines. Run it on your own computer with a read-only token (see
[Tokens](configuration.md#tokens)); it makes GET requests only and never prints or writes
the token.

```bash
# A new configuration, to standard output:
wakeci discover \
  --source orgs/example-org --repos example-org/app,example-org/lib --token-file ~/.config/wakeci/token-a \
  --source repos/example-org2/tool --repos example-org2/tool --token-file ~/.config/wakeci/token-b \
  > config.json

# Machines for new runners, added to an existing configuration:
wakeci discover --update config.json \
  --token orgs/example-org=~/.config/wakeci/token-a --token repos/example-org2/tool=~/.config/wakeci/token-b
```

## How runners become machines

- A runner's **prefix** is its name without a number after a separator: `ci5-3` becomes
  `ci5-`. A name without one (`host1`) is its own prefix, because stripping the number would
  put `host1` and `host10` on one machine.
- A runner an existing machine's prefix matches stays with that machine. Every other
  prefix becomes **a machine of its own**, with `mac` and `host` set to `FILL-IN`, which
  `wakeci check` refuses until you fill them in. GitHub knows neither.
- A new prefix that would also match another machine's prefix (`ci5-` when a machine has
  `ci5-cpp-`, or `host1` next to `host10`) is **not added** and is reported, because
  a prefix match would give that machine's runners to both. Rename the runners, or add the
  machine by hand with a longer prefix. A `--machine` prefix that overlaps is refused.
- Several prefixes on one machine (`ci5-` and `runner5-cpp-`) are only **suggested** for
  joining, never joined: a wrong join would suspend a machine on which another machine's
  runner works. Join them with `--machine runner5=ci5-,runner5-cpp-`.

## Updating a file

- `--update` **never changes or removes** anything in the file. Prefixes that no runner
  matches any more are only reported.
- Without `--write` the result goes to standard output and the diff to standard error.
- With `--write` the file is replaced and the old one is kept as `config.json.bak`. A
  symbolic link is followed and the mode is kept; the owner and group are kept when the
  user running it may set them, so run it as the file's owner or with `sudo`. The file is
  written back with two-space indentation, whatever its formatting was.
- `--token SOURCE=FILE` gives each source's token: the file's `token_file` paths are the
  controller's, not your computer's.

## Other options

- The token can also come from `WAKECI_TOKEN` when there is one source.
- `--json` prints the configuration and the report together.

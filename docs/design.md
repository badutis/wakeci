# Design

The goal: runner machines are on only while there is work for them. Queued jobs wake
the machines they need. A machine whose runners have done nothing for `idle_minutes` is
suspended. The runners stay registered and enabled the whole time, and a job simply
waits in the queue until its machine is awake.

The cost: the first job after a quiet period waits until the machine has resumed and its
runners have reconnected. Measure this on your hardware; with suspend-to-RAM it is
usually tens of seconds.

## The loop

Every `poll_seconds` the controller reads, for each source:

- the runners: name, online, busy, labels;
- the queued jobs of the source's repositories, with their labels.

It passes this to `Controller.step` (`wakeci/decide.py`), which returns a list of
actions (`wake`, `sleep`, `alert`) and keeps the rest of its memory in RAM. Only two
things go to disk: when each machine was last online, and until when a weekly wake holds
it awake. After a restart everything else is learned again from GitHub.

There are no webhooks, so the controller needs no inbound connection. It works behind
NAT or a mobile uplink.

## Waking

1. Queued jobs are grouped by source and label set.
2. For each group the controller counts the **free online runners that can take the
   job**: same source, and the runner has every label the job asks for. A runner on a
   configured machine counts only while that machine is awake and not being suspended.
   A runner that belongs to no configured machine always counts.
3. **Runners on their way are counted too:** runners of a machine that was woken less than
   3 minutes ago, and runners of a machine that has just resumed but has not reconnected
   all its runners yet. Without this, every poll during resume would wake another
   machine.
4. If jobs are still uncovered, sleeping machines that have runners for those labels are
   woken, ordered by `rank` and then name, until the missing slots are covered.
5. If a woken machine has no runner online after 3 minutes, the packet is sent once more.
   After the second failure the controller raises an alert and leaves that machine alone
   for 30 minutes. Alerts for the same machine are repeated at most every 6 hours.

**Stuck jobs.** A job that has waited 30 minutes while a free, awake runner that could
take it was online is treated as stuck: GitHub is not giving it to anyone. Such a job no
longer wakes machines or keeps them awake. The 30 minutes count from the moment such a
runner appeared, not from when the job was created. Otherwise an old job seen after a
controller outage would never be served.

## Suspending

A machine is suspended when **all** of these are true:

- every runner of the machine has been not busy for `idle_minutes`;
- no queued job could be taken by one of its online runners;
- it is not inside a weekly hold (below);
- every source was read successfully in this poll. If one source failed (for example an
  expired token), the controller cannot know whether that machine's runners are busy,
  so **nothing is suspended**. Waking still works.

The controller then runs `ssh <ssh_user>@<host>`. The machine's `authorized_keys` forces
the command, so the controller cannot choose what runs. `wakeci-sleep` checks again
whether a `Runner.Worker` process is running: **the machine decides, not the API**,
because the API reports a busy runner late. Exit codes:

| Code | Meaning |
|---|---|
| `0` | suspends in 5 s (so the SSH session ends cleanly) |
| `3` | refused: a job is running |
| `4` | reboots in 5 s instead, because an update asked for a reboot |
| `2` | refused: an argument it does not know |
| `10` | `wakeci-sleep check`: nothing changed; it printed `wakeci-sleep protocol=1 state=<busy\|would-reboot\|would-suspend>` |

The controller sends no command. A command the client does send never runs: the forced
command adds the word `check` when there is one (`${SSH_ORIGINAL_COMMAND:+check}`), and
`sudoers` allows the script only with no argument or with `check`. So the most a client
with the key can do besides suspending is ask what a suspend would do.

If suspending fails, the machine counts as in use again and its idle timer restarts.
The third failure in a row is logged as an error. For 5 minutes after a suspend command
the machine's runners may still look online. During that time it is not counted as a
free machine. A new suspend attempt waits at least 10 minutes.

## Weekly wake

GitHub removes a self-hosted runner that has not connected for 14 days. A machine that
gets no work could therefore lose its registration. So every machine that has not been
online for 7 days is woken and held awake for 1 hour. A machine the state file knows
nothing about (for example on the very first start) counts as not seen, so the first run
wakes every sleeping machine once. Automatic OS updates can run during
that hour. If one of them needs a reboot, `wakeci-sleep` reboots instead of suspending.

## What the controller never does

- It never changes anything in GitHub. Its tokens are read-only, and it does not enable
  or disable runners or workflows.
- It never acts as a runner itself, and CI jobs cannot reach it.
- It never runs a command of its choosing on a machine. The machine allows exactly one
  command for its key, and `sudoers` allows exactly that one script, with no argument or
  with `check`, without a password.

## Security

| Asset | If stolen |
|---|---|
| GitHub token | read the job queue and the runner list of that source |
| SSH key | suspend a machine on which no job is running, or reboot it if an update is waiting for a reboot; learn whether a job is running (`wakeci-sleep check`, which `wakeci doctor --probe` uses) |
| Wake-on-LAN | nothing to steal; anyone on the segment can wake a machine |

`install.sh` removes any other line in `authorized_keys` that carries the same key
without the restriction, so the key never gives a full shell. It also refuses to install
the key for the account that runs CI jobs (`--ci-user`).

## Disk writes

The controller writes the state file only when a machine's "last online" day or its hold
changes, a few times a day, which is gentle on an SD card. On a board that runs from one,
`controller/install.sh --ram-journal` also keeps the systemd journal of the whole host in RAM
(`Storage=volatile`, at most 16 MB). It is opt-in, because all logs are then lost on
reboot. On Armbian the other logs already go to RAM (`armbian-ramlog`).

## Dry run

With `"dry_run": true` (the default) every decision is logged with `[dry-run]` and
nothing is sent. Failed wakes are not retried and do not alert, because no packet was
sent. The same "would suspend" line is repeated at most hourly. Run a week like this
before switching it off.

## Known limits

- **A broken controller leaves the machines asleep.** Jobs then wait in the queue (up to
  GitHub's 24-hour limit). Nothing checks the controller automatically. Watch its
  metrics, or move the jobs off the self-hosted runners by hand.
- **The window between the check and the suspend.** GitHub can hand a job to a runner in
  the second between `wakeci-sleep`'s check and the suspend. That job is interrupted.
  Stopping the runner services before suspending would close the window, at the cost
  of starting them again on resume.
- **One unreadable source stops all suspending.** It is visible as
  `wakeci_github_ok = 0`, as a warning in the log on every poll and, with `notify` set, as
  a webhook message after `after_minutes`.
- **One source's rate limit pauses both.** When a token is close to its limit, the loop
  waits for that token's reset before polling any source. Watch
  `wakeci_github_remaining`.
- **How long until GitHub marks a suspended machine's runners offline** is assumed to be
  under 5 minutes (`sleep_grace`). Measure it on your setup.
- **Request cost.** Jobs of in-progress runs are re-read every 60 s (every 5 min when the
  limit is low) and when the run's `updated_at` changes. A run that updates very often
  costs more requests.
- **Jobs with `needs:`.** Not verified whether the API already lists a job as `queued`
  while it waits for an earlier job. If it does, a machine is woken early, and if no work
  arrives it is suspended again after `idle_minutes`.
- **Notifications** need a webhook (`notify`); without one they go only to the log. A
  controller that is down sends nothing, so watch it from outside too (its `/metrics`).
